# ============================================================
# COB LED CONTROLLER - ESP32-S3 / MicroPython
# ============================================================
#
# Features:
#   - VEML7700 automatic brightness control
#   - Automatic operation from 1 hour before sunset until 10:30 PM
#   - Momentary push-button override outside automatic hours
#   - Second button press turns lights off and returns to AUTO
#   - Wi-Fi credentials read from lowercase variables in config.py
#   - NTP time synchronization with Central Time / DST handling
#   - Daily sunset download for Rochester, Minnesota
#   - Daily OTA update check from GitHub at or after 3:00 AM
#   - Safe shutdown on missing time, sensor failure, or program error
#
# Required files on the ESP32:
#   main.py
#   ota.py
#   config.py
#   veml7700.py
#
# config.py must contain:
#   wifi_ssid = "Your_WiFi_Name"
#   wifi_password = "Your_WiFi_Password"
#
# GitHub repository root must contain:
#   main.py
#   version.txt
#
# IMPORTANT:
#   LOCAL_VERSION below must match version.txt for the same release.
# ============================================================

from machine import Pin, I2C, PWM
import network
import ntptime
import time
import gc
import config
import ota

try:
    import urequests as requests
except ImportError:
    import requests

from veml7700 import VEML7700


# ============================================================
# PROGRAM VERSION
# ============================================================

# Increase this value every time you publish a new GitHub release.
# The same value must be stored in GitHub's version.txt file.
LOCAL_VERSION = "1.0.0"


# ============================================================
# LOCATION AND SCHEDULE SETTINGS
# ============================================================

# Rochester, Minnesota.
LATITUDE = 44.0121
LONGITUDE = -92.4802
TIMEZONE = "America/Chicago"

# Automatic lights begin 60 minutes before sunset.
MINUTES_BEFORE_SUNSET = 60

# Automatic lights turn off at 10:30 PM local time.
# AUTO_OFF_HOUR = 22
# AUTO_OFF_MINUTE = 30

AUTO_OFF_HOUR = 21
AUTO_OFF_MINUTE = 00

# Check GitHub once per local day at or after 3:00 AM.
OTA_CHECK_HOUR = 3
OTA_CHECK_MINUTE = 0


# ============================================================
# LIGHT AND PWM SETTINGS
# ============================================================

# VEML7700 white reading treated as 100 percent ambient light.
# Increase if the COB lights dim too easily.
# Decrease if the COB lights stay too bright in a bright room.
WHITE_FULL_SCALE = 600

# 16-bit PWM limits: 0 = off, 65535 = full output.

MAX_PWM = 65535
MIN_PWM = 30000

# True: manual button mode still uses the VEML7700.
# False: manual button mode uses MANUAL_PWM_PERCENT.
MANUAL_USES_LIGHT_SENSOR = True
MANUAL_PWM_PERCENT = 80

# Average recent readings to reduce brightness hunting.
AVERAGE_SAMPLES = 10

# Main loop and fade settings.
LOOP_DELAY_MS = 100
PWM_STEP = 1500


# ============================================================
# BUTTON, NETWORK, AND RETRY SETTINGS
# ============================================================

BUTTON_DEBOUNCE_MS = 250
WIFI_CHECK_INTERVAL_SECONDS = 60
SUNSET_RETRY_SECONDS = 300
NTP_SYNC_INTERVAL_SECONDS = 21600
STATUS_PRINT_INTERVAL_MS = 5000

# Print the complete sunset response when True.
DEBUG = True


# ============================================================
# GPIO SETTINGS
# ============================================================

# VEML7700 wiring:
#   SDA -> GPIO 7
#   SCL -> GPIO 8
VEML_SDA_PIN = 7
VEML_SCL_PIN = 8

# MOSFET PWM signal input.
PWM_OUTPUT_PIN = 6

# Momentary button:
#   One terminal -> GPIO 5
#   Other terminal -> GND
BUTTON_PIN = 5


# ============================================================
# HARDWARE INITIALIZATION
# ============================================================

# All grounds must be common:
# ESP32 GND, MOSFET GND, LED power-supply GND.
i2c = I2C(
    0,
    scl=Pin(VEML_SCL_PIN),
    sda=Pin(VEML_SDA_PIN),
    freq=100000
)

pwm_pin = PWM(Pin(PWM_OUTPUT_PIN))
pwm_pin.freq(1000)
pwm_pin.duty_u16(0)

# Internal pull-up means released = 1 and pressed = 0.
button = Pin(BUTTON_PIN, Pin.IN, Pin.PULL_UP)


# ============================================================
# WI-FI CREDENTIALS
# ============================================================

def get_wifi_credentials():
    """Read lowercase Wi-Fi variables from config.py."""
    ssid = getattr(config, "wifi_ssid", None)
    password = getattr(config, "wifi_password", None)

    if not ssid:
        raise ValueError("wifi_ssid was not found in config.py")

    if password is None:
        raise ValueError("wifi_password was not found in config.py")

    return ssid, password


wifi_ssid, wifi_password = get_wifi_credentials()
wlan = network.WLAN(network.STA_IF)


def connect_wifi(timeout_seconds=20):
    """Connect to Wi-Fi and return True on success."""
    wlan.active(True)

    if wlan.isconnected():
        return True

    print("Connecting to Wi-Fi:", wifi_ssid)

    try:
        wlan.connect(wifi_ssid, wifi_password)
    except Exception as error:
        print("Unable to start Wi-Fi connection:", error)
        return False

    started_ms = time.ticks_ms()

    while not wlan.isconnected():
        elapsed_ms = time.ticks_diff(time.ticks_ms(), started_ms)

        if elapsed_ms >= timeout_seconds * 1000:
            print("Wi-Fi connection timed out.")
            print("Wi-Fi status:", wlan.status())
            return False

        time.sleep_ms(250)

    print("Wi-Fi connected.")
    print("Network information:", wlan.ifconfig())
    return True


def ensure_wifi():
    """Reconnect Wi-Fi if needed."""
    if wlan.isconnected():
        return True

    print("Wi-Fi connection lost. Reconnecting.")

    try:
        wlan.disconnect()
    except Exception:
        pass

    time.sleep_ms(500)
    return connect_wifi()


# ============================================================
# NTP TIME
# ============================================================

time_is_valid = False


def sync_ntp(retries=3):
    """Synchronize the ESP32 UTC clock from an NTP server."""
    global time_is_valid

    time_was_valid = time_is_valid

    if not ensure_wifi():
        print("Cannot synchronize time: Wi-Fi unavailable.")
        time_is_valid = time_was_valid
        return False

    for attempt in range(1, retries + 1):
        try:
            print("Synchronizing Internet time. Attempt", attempt, "of", retries)
            ntptime.host = "pool.ntp.org"
            ntptime.settime()

            utc_now = time.localtime()
            if utc_now[0] < 2025:
                raise ValueError("NTP returned an invalid year")

            time_is_valid = True
            print("Internet time synchronized.")
            print("Current UTC time:", utc_now)
            return True

        except Exception as error:
            print("NTP synchronization failed:", error)
            if attempt < retries:
                time.sleep(3)

    # Keep using a previously valid RTC if a later NTP refresh fails.
    time_is_valid = time_was_valid
    return False


# ============================================================
# CENTRAL TIME AND DAYLIGHT SAVING TIME
# ============================================================

def weekday_of_date(year, month, day):
    """Return weekday where Monday = 0 and Sunday = 6."""
    timestamp = time.mktime((year, month, day, 0, 0, 0, 0, 0))
    return time.localtime(timestamp)[6]


def nth_sunday(year, month, occurrence):
    """Return the day number of a selected Sunday in a month."""
    first_weekday = weekday_of_date(year, month, 1)
    days_until_sunday = (6 - first_weekday) % 7
    return 1 + days_until_sunday + ((occurrence - 1) * 7)


def central_dst_boundaries_utc(year):
    """Return US Central daylight-saving start and end in UTC."""
    second_sunday_march = nth_sunday(year, 3, 2)
    first_sunday_november = nth_sunday(year, 11, 1)

    # DST begins at 2:00 AM CST, which is 08:00 UTC.
    dst_start_utc = time.mktime(
        (year, 3, second_sunday_march, 8, 0, 0, 0, 0)
    )

    # DST ends at 2:00 AM CDT, which is 07:00 UTC.
    dst_end_utc = time.mktime(
        (year, 11, first_sunday_november, 7, 0, 0, 0, 0)
    )

    return dst_start_utc, dst_end_utc


def central_daylight_time_active(utc_timestamp):
    """Return True while Central Daylight Time is active."""
    year = time.localtime(utc_timestamp)[0]
    dst_start, dst_end = central_dst_boundaries_utc(year)
    return dst_start <= utc_timestamp < dst_end


def get_local_time():
    """Convert the ESP32 UTC clock to Rochester local time."""
    utc_timestamp = time.time()

    if central_daylight_time_active(utc_timestamp):
        offset_hours = -5
        timezone_name = "CDT"
    else:
        offset_hours = -6
        timezone_name = "CST"

    local_timestamp = utc_timestamp + (offset_hours * 3600)
    return time.localtime(local_timestamp), timezone_name


def format_date(time_value):
    """Format a time tuple as YYYY-MM-DD."""
    return "{:04d}-{:02d}-{:02d}".format(
        time_value[0], time_value[1], time_value[2]
    )


def format_clock(time_value):
    """Format a time tuple as HH:MM:SS."""
    return "{:02d}:{:02d}:{:02d}".format(
        time_value[3], time_value[4], time_value[5]
    )


# ============================================================
# SUNSET DOWNLOAD AND SCHEDULE
# ============================================================

sunset_hour = None
sunset_minute = None
sunset_date = None


def parse_iso_hour_minute(iso_value):
    """Extract hour and minute from an ISO 8601 date/time string."""
    if not iso_value or "T" not in iso_value:
        raise ValueError("Invalid ISO sunset value")

    clock_part = iso_value.split("T")[1]
    return int(clock_part[0:2]), int(clock_part[3:5])


def get_automatic_start_minutes():
    """Return one hour before sunset as minutes after midnight."""
    if sunset_hour is None or sunset_minute is None:
        return None

    sunset_minutes = sunset_hour * 60 + sunset_minute
    return (sunset_minutes - MINUTES_BEFORE_SUNSET) % 1440


def get_automatic_off_minutes():
    """Return off time as minutes after midnight."""
    return AUTO_OFF_HOUR * 60 + AUTO_OFF_MINUTE


def minutes_to_clock(total_minutes):
    """Convert minutes after midnight to HH:MM."""
    if total_minutes is None:
        return "unknown"

    total_minutes %= 1440
    return "{:02d}:{:02d}".format(
        total_minutes // 60,
        total_minutes % 60
    )


def download_sunset(local_date):
    """Download today's Rochester sunset in local time."""
    global sunset_hour, sunset_minute, sunset_date

    if not ensure_wifi():
        print("Cannot download sunset: Wi-Fi unavailable.")
        return False

    # Split the address so rich-text software does not alter it.
    api_base = "http://" + "api.sunrise-sunset.org" + "/json"
    url = (
        api_base
        + "?lat={}"
        + "&lng={}"
        + "&date={}"
        + "&formatted=0"
        + "&tzid={}"
    ).format(LATITUDE, LONGITUDE, local_date, TIMEZONE)

    response = None

    try:
        print("Downloading sunset for:", local_date)
        if DEBUG:
            print("Sunset request:", url)

        response = requests.get(url)
        status_code = getattr(response, "status_code", 200)

        if status_code != 200:
            raise OSError("Sunset HTTP status {}".format(status_code))

        data = response.json()
        if DEBUG:
            print("Sunset response:", data)

        if data.get("status") != "OK":
            raise ValueError("Sunset API status: {}".format(data.get("status")))

        results = data.get("results")
        if not isinstance(results, dict):
            raise ValueError("Sunset results were missing")

        sunset_value = results.get("sunset")
        hour, minute = parse_iso_hour_minute(sunset_value)

        if not 0 <= hour <= 23 or not 0 <= minute <= 59:
            raise ValueError("Invalid sunset time")

        sunset_hour = hour
        sunset_minute = minute
        sunset_date = local_date

        print("Today's sunset: {:02d}:{:02d}".format(hour, minute))
        print("Automatic lighting starts:", minutes_to_clock(
            get_automatic_start_minutes()
        ))
        print("Automatic lighting ends: {:02d}:{:02d}".format(
            AUTO_OFF_HOUR, AUTO_OFF_MINUTE
        ))
        return True

    except Exception as error:
        print("Unable to download sunset:", error)
        sunset_hour = None
        sunset_minute = None
        sunset_date = None
        return False

    finally:
        if response is not None:
            try:
                response.close()
            except Exception:
                pass
        gc.collect()


def automatic_period_active(local_time):
    """Return True from one hour before sunset until 10:30 PM."""
    automatic_start = get_automatic_start_minutes()
    if automatic_start is None:
        return False

    current_minutes = local_time[3] * 60 + local_time[4]
    automatic_off = get_automatic_off_minutes()
    return automatic_start <= current_minutes < automatic_off


# ============================================================
# DAILY OTA UPDATE
# ============================================================

def check_daily_ota(local_now, local_date):
    """
    Check GitHub once per local calendar day at or after 3:00 AM.

    ota.py stores the last successful/no-update check date in
    ota_last_check.txt. On an available update it downloads and
    validates main.py, keeps main.backup.py, installs the update,
    and resets the ESP32.
    """
    current_minutes = local_now[3] * 60 + local_now[4]
    ota_start_minutes = OTA_CHECK_HOUR * 60 + OTA_CHECK_MINUTE

    if current_minutes < ota_start_minutes:
        return

    # Avoid unnecessary function work and network contact if already checked.
    if ota.already_checked_today(local_date):
        return

    if not ensure_wifi():
        print("OTA: Wi-Fi unavailable; daily check postponed.")
        return

    result = ota.check_for_update(LOCAL_VERSION, local_date)

    if result == "current":
        print("Daily OTA check complete. No update required.")
    elif result == "failed":
        print("Daily OTA check failed. Current program was kept.")


# ============================================================
# VEML7700 INITIALIZATION
# ============================================================

i2c_devices = i2c.scan()
print("I2C devices found:", [hex(address) for address in i2c_devices])

if 0x10 not in i2c_devices:
    pwm_pin.duty_u16(0)
    raise RuntimeError(
        "VEML7700 address 0x10 was not found. Check wiring."
    )

try:
    sensor = VEML7700(i2c)
    print("VEML7700 initialized.")
except Exception as error:
    pwm_pin.duty_u16(0)
    print("Unable to initialize VEML7700:", error)
    raise


# ============================================================
# BRIGHTNESS CONTROL
# ============================================================

def constrain(value, minimum, maximum):
    """Keep a value inside a selected range."""
    if value < minimum:
        return minimum
    if value > maximum:
        return maximum
    return value


def calculate_sensor_pwm(white_reading):
    """Convert white-light reading to set LED PWM. When room is dark, lights are dim."""
    ambient_percent = (
        float(white_reading) / float(WHITE_FULL_SCALE)
    ) * 100.0
    ambient_percent = constrain(ambient_percent, 0.0, 100.0)

    led_target_percent =  ambient_percent
    target_pwm = int((led_target_percent / 100.0) * MAX_PWM)
    target_pwm = constrain(target_pwm, MIN_PWM, MAX_PWM)

    return target_pwm, ambient_percent, led_target_percent


def calculate_manual_pwm(sensor_target_pwm):
    """Choose sensor-controlled or fixed manual brightness."""
    if MANUAL_USES_LIGHT_SENSOR:
        return sensor_target_pwm

    manual_percent = constrain(MANUAL_PWM_PERCENT, 0, 100)
    return int((manual_percent / 100.0) * MAX_PWM)


def move_toward(current_value, target_value, maximum_step):
    """Move PWM smoothly toward a target."""
    if current_value < target_value:
        return min(current_value + maximum_step, target_value)
    if current_value > target_value:
        return max(current_value - maximum_step, target_value)
    return current_value


# ============================================================
# BUTTON HANDLING
# ============================================================

manual_override_on = False
previous_button_value = button.value()
last_button_press_ms = 0


def check_button(current_ms, automatic_active):
    """
    Outside automatic hours:
      First press  -> manual lights on.
      Second press -> lights off and return to AUTO.

    During automatic hours the button is ignored.
    """
    global previous_button_value
    global last_button_press_ms
    global manual_override_on

    current_button_value = button.value()
    new_press = (
        previous_button_value == 1
        and current_button_value == 0
    )

    if new_press:
        since_last_press = time.ticks_diff(
            current_ms,
            last_button_press_ms
        )

        if since_last_press >= BUTTON_DEBOUNCE_MS:
            last_button_press_ms = current_ms

            if automatic_active:
                print("Button ignored during automatic hours.")
            elif manual_override_on:
                manual_override_on = False
                print("Button pressed: manual lights OFF.")
                print("Controller returned to AUTO.")
            else:
                manual_override_on = True
                print("Button pressed: manual lights ON.")

    previous_button_value = current_button_value


# ============================================================
# STARTUP
# ============================================================

print()
print("Starting COB LED controller, version", LOCAL_VERSION)
print("PWM output: GPIO", PWM_OUTPUT_PIN)
print("Push button: GPIO", BUTTON_PIN)
print("VEML7700 SDA: GPIO", VEML_SDA_PIN)
print("VEML7700 SCL: GPIO", VEML_SCL_PIN)
print("Automatic shutoff: {:02d}:{:02d}".format(
    AUTO_OFF_HOUR,
    AUTO_OFF_MINUTE
))
print("Daily OTA check: {:02d}:{:02d} local time".format(
    OTA_CHECK_HOUR,
    OTA_CHECK_MINUTE
))
print()

current_pwm = 0
pwm_pin.duty_u16(0)

connect_wifi()
sync_ntp()

last_wifi_check_ms = time.ticks_ms()
last_ntp_sync_ms = time.ticks_ms()
last_sunset_attempt_ms = 0
last_status_print_ms = 0
light_readings = []
previous_automatic_active = False


# ============================================================
# MAIN LOOP
# ============================================================

while True:
    try:
        current_ms = time.ticks_ms()

        # Check Wi-Fi periodically.
        if time.ticks_diff(current_ms, last_wifi_check_ms) >= (
            WIFI_CHECK_INTERVAL_SECONDS * 1000
        ):
            ensure_wifi()
            last_wifi_check_ms = current_ms

        # Refresh NTP periodically. Keep using a previously valid clock if the
        # Internet is temporarily unavailable.
        if (
            not time_is_valid
            or time.ticks_diff(current_ms, last_ntp_sync_ms)
            >= NTP_SYNC_INTERVAL_SECONDS * 1000
        ):
            sync_ntp()
            last_ntp_sync_ms = current_ms

        # Fail safely until the device has a valid clock.
        if not time_is_valid:
            manual_override_on = False
            current_pwm = 0
            pwm_pin.duty_u16(0)

            if time.ticks_diff(
                current_ms,
                last_status_print_ms
            ) >= STATUS_PRINT_INTERVAL_MS:
                print("Waiting for valid Internet time. Lights remain OFF.")
                last_status_print_ms = current_ms

            time.sleep_ms(LOOP_DELAY_MS)
            continue

        local_now, timezone_name = get_local_time()
        local_date = format_date(local_now)

        # Check GitHub once per day after the configured local time.
        check_daily_ota(local_now, local_date)

        # Download sunset after startup and whenever the local date changes.
        sunset_needed = (
            sunset_date != local_date
            or sunset_hour is None
            or sunset_minute is None
        )

        sunset_retry_due = time.ticks_diff(
            current_ms,
            last_sunset_attempt_ms
        ) >= SUNSET_RETRY_SECONDS * 1000

        if sunset_needed and (
            last_sunset_attempt_ms == 0
            or sunset_retry_due
        ):
            last_sunset_attempt_ms = current_ms
            download_sunset(local_date)

        automatic_active = automatic_period_active(local_now)

        # Entering automatic hours always clears any manual override.
        if automatic_active and not previous_automatic_active:
            manual_override_on = False
            print("Automatic lighting period started.")

        # At 10:30 PM, immediately turn off and return to AUTO waiting mode.
        if not automatic_active and previous_automatic_active:
            manual_override_on = False
            current_pwm = 0
            pwm_pin.duty_u16(0)
            print("Automatic lighting period ended.")
            print("Lights are OFF. Controller returned to AUTO.")

        previous_automatic_active = automatic_active

        # The button only toggles manual lighting outside automatic hours.
        check_button(current_ms, automatic_active)

        # Read and average the VEML7700.
        light = int(sensor.white())
        light_readings.append(light)

        if len(light_readings) > AVERAGE_SAMPLES:
            light_readings.pop(0)

        average_light = sum(light_readings) / len(light_readings)
        sensor_target_pwm, ambient_percent, target_led_percent = (
            calculate_sensor_pwm(average_light)
        )

        # Select the requested operating mode.
        if automatic_active:
            target_pwm = sensor_target_pwm
            state = "AUTO - scheduled"
        elif manual_override_on:
            target_pwm = calculate_manual_pwm(sensor_target_pwm)
            state = "MANUAL ON"
        else:
            target_pwm = 0
            state = "AUTO - waiting"

        # OFF is immediate. ON and brightness changes fade smoothly.
        if target_pwm == 0:
            current_pwm = 0
        else:
            current_pwm = move_toward(
                current_pwm,
                target_pwm,
                PWM_STEP
            )

        pwm_pin.duty_u16(int(current_pwm))

        # Print status periodically.
        if time.ticks_diff(
            current_ms,
            last_status_print_ms
        ) >= STATUS_PRINT_INTERVAL_MS:
            if sunset_hour is None:
                sunset_text = "unknown"
                start_text = "unknown"
            else:
                sunset_text = "{:02d}:{:02d}".format(
                    sunset_hour,
                    sunset_minute
                )
                start_text = minutes_to_clock(
                    get_automatic_start_minutes()
                )

            actual_led_percent = (
                float(current_pwm) / 65535.0
            ) * 100.0

            print(
                "{} {} {} | Version: {} | Start: {} | Sunset: {} | "
                "White: {} | Ambient: {:.1f}% | LED: {:.1f}% | "
                "PWM: {} | {}".format(
                    local_date,
                    format_clock(local_now),
                    timezone_name,
                    LOCAL_VERSION,
                    start_text,
                    sunset_text,
                    int(average_light),
                    ambient_percent,
                    actual_led_percent,
                    int(current_pwm),
                    state
                )
            )

            last_status_print_ms = current_ms

        time.sleep_ms(LOOP_DELAY_MS)

    except KeyboardInterrupt:
        pwm_pin.duty_u16(0)
        print()
        print("Program stopped. COB lights are OFF.")
        break

    except Exception as error:
        # An unexpected error forces the lights off and clears manual mode.
        pwm_pin.duty_u16(0)
        current_pwm = 0
        manual_override_on = False

        print()
        print("Main-loop error:", error)
        print("COB lights forced OFF.")
        print("Manual override cleared.")
        print("Retrying in five seconds.")
        time.sleep(5)




