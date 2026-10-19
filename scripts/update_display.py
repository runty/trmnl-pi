#!/usr/bin/env python3
import importlib
import os
import signal
import sys
import time
from contextlib import contextmanager
from io import BytesIO

import requests
from PIL import Image


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
WAVESHARE_LIB = os.path.expanduser(
    os.getenv("WAVESHARE_LIB", "~/e-Paper/RaspberryPi_JetsonNano/python/lib")
)
sys.path.insert(0, WAVESHARE_LIB)

# Supported Waveshare drivers, chosen with TRMNL_PANEL. image_mode is what the
# image is converted to before the driver's getbuffer(); a refresh faster than
# min_refresh_seconds means the panel did not actually redraw.
PANELS = {
    # 7.3" Spectra 6: a real refresh holds BUSY for ~30s.
    "epd7in3e": {
        "image_mode": "RGB",
        "min_refresh_seconds": 10,
        "model": "Waveshare 7.3 Spectra 6",
    },
    # 7.5" V2 black and white: a full refresh takes about 5s.
    "epd7in5_V2": {
        "image_mode": "1",
        "min_refresh_seconds": 2,
        "model": "Waveshare 7.5 V2",
    },
}
PANEL_NAME = os.getenv("TRMNL_PANEL", "")
PANEL = PANELS.get(PANEL_NAME, {})

BASE_URL = os.getenv("TRMNL_BASE_URL", "https://usetrmnl.com")
API_KEY = os.getenv("TRMNL_API_KEY", "")
MAC_ADDRESS = os.getenv("TRMNL_MAC", "")
TARGET_RES = (
    int(os.getenv("TRMNL_WIDTH", "800")),
    int(os.getenv("TRMNL_HEIGHT", "480")),
)
CACHE_FILE = os.path.expanduser(
    os.getenv("TRMNL_CACHE_FILE", os.path.join(PROJECT_DIR, "last_image.txt"))
)
MIN_REFRESH_SECONDS = float(
    os.getenv("TRMNL_MIN_REFRESH_SECONDS") or PANEL.get("min_refresh_seconds", 10)
)
# Redraw an unchanged image after this long, in case an earlier draw was lost.
FORCE_REDRAW_HOURS = float(os.getenv("TRMNL_FORCE_REDRAW_HOURS", "12"))
DISPLAY_MODEL = os.getenv("TRMNL_MODEL") or PANEL.get("model", "")
CLIENT_VERSION = "1.2.0"
WIFI_DEVICE = os.getenv("TRMNL_WIFI_DEVICE", "wlan0")
DISPLAY_TIMEOUT_SECONDS = int(os.getenv("TRMNL_DISPLAY_TIMEOUT", "600"))
DISPLAY_CLEANUP_TIMEOUT_SECONDS = int(os.getenv("TRMNL_DISPLAY_CLEANUP_TIMEOUT", "30"))
HTTP_ATTEMPTS = int(os.getenv("TRMNL_HTTP_ATTEMPTS", "3"))
HTTP_RETRY_DELAY_SECONDS = int(os.getenv("TRMNL_HTTP_RETRY_DELAY", "5"))
HTTP_TOTAL_TIMEOUT_SECONDS = int(os.getenv("TRMNL_HTTP_TOTAL_TIMEOUT", "120"))


def log(message):
    print(f"{time.strftime('%Y-%m-%dT%H:%M:%S%z')} {message}", flush=True)


@contextmanager
def deadline(seconds, message):
    def _raise_timeout(signum, frame):
        raise TimeoutError(message)

    old_handler = signal.getsignal(signal.SIGALRM)
    signal.signal(signal.SIGALRM, _raise_timeout)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old_handler)


def get_with_retries(url, total_timeout=HTTP_TOTAL_TIMEOUT_SECONDS, **kwargs):
    with deadline(total_timeout, f"HTTP request sequence exceeded {total_timeout}s"):
        for attempt in range(1, HTTP_ATTEMPTS + 1):
            try:
                return requests.get(url, **kwargs)
            except requests.RequestException as e:
                if attempt == HTTP_ATTEMPTS:
                    raise
                log(f"HTTP request failed on attempt {attempt}/{HTTP_ATTEMPTS}: {e}; retrying")
                time.sleep(HTTP_RETRY_DELAY_SECONDS * attempt)
    raise TimeoutError(f"HTTP request sequence exceeded {total_timeout}s")


def _raise_termination(signum, frame):
    raise SystemExit(128 + signum)


def sleep_display(epd):
    with deadline(
        DISPLAY_CLEANUP_TIMEOUT_SECONDS,
        f"display sleep/cleanup exceeded {DISPLAY_CLEANUP_TIMEOUT_SECONDS}s",
    ):
        log("Sleeping display...")
        epd.sleep()


def wifi_rssi():
    """Signal level in dBm from /proc/net/wireless, or None."""
    try:
        with open("/proc/net/wireless", "r", encoding="utf-8") as f:
            for line in f:
                fields = line.split()
                if fields and fields[0] == f"{WIFI_DEVICE}:":
                    return int(float(fields[3]))
    except (OSError, IndexError, ValueError):
        pass
    return None


def write_cache(filename):
    tmp = f"{CACHE_FILE}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(filename)
    os.replace(tmp, CACHE_FILE)


def load_driver():
    """Import the Waveshare driver named by TRMNL_PANEL, or log why not and return None."""
    if PANEL_NAME not in PANELS:
        log(f"Set TRMNL_PANEL to one of: {', '.join(PANELS)} (got {PANEL_NAME!r})")
        return None
    try:
        driver = importlib.import_module(f"waveshare_epd.{PANEL_NAME}")
    except ImportError as e:
        log(f"Cannot import Waveshare driver {PANEL_NAME} from {WAVESHARE_LIB}: {e}")
        return None
    # The stock drivers wait on BUSY forever; patches/ adds a bounded wait.
    if not hasattr(driver, "BUSY_TIMEOUT_SECONDS"):
        log(
            f"Waveshare BUSY timeout patch is missing from {driver.__file__}; "
            f"apply patches/waveshare-{PANEL_NAME}-busy-timeout.patch"
        )
        return None
    return driver


def prepare_image(img):
    """Convert and size a downloaded image for the panel's getbuffer()."""
    mode = PANEL["image_mode"]
    # Convert first: Pillow resizes palette images with NEAREST regardless
    # of the requested filter.
    img = img.convert("RGB" if mode == "RGB" else "L")
    if img.size != TARGET_RES:
        img = img.resize(TARGET_RES, Image.Resampling.LANCZOS)
    if mode == "1":
        # A 1-bit image passes through unchanged; grayscale is dithered.
        img = img.convert("1")
    return img


def validate_config():
    if not API_KEY:
        log("Missing TRMNL_API_KEY")
        return False
    if not MAC_ADDRESS:
        log("Missing TRMNL_MAC")
        return False
    return True


def update_display():
    driver = load_driver()
    if driver is None or not validate_config():
        return False

    try:
        api_url = f"{BASE_URL}/api/display"
        headers = {
            "ID": MAC_ADDRESS,
            "Access-Token": API_KEY,
            "Content-Type": "application/json",
            "Width": str(TARGET_RES[0]),
            "Height": str(TARGET_RES[1]),
            "Model": DISPLAY_MODEL,
            "FW-Version": CLIENT_VERSION,
        }
        rssi = wifi_rssi()
        if rssi is not None:
            headers["RSSI"] = str(rssi)
        api_response = get_with_retries(api_url, headers=headers, timeout=30)
        try:
            if api_response.status_code != 200:
                log(f"API error: {api_response.status_code} {api_response.text[:200]}")
                return False
            data = api_response.json()
        finally:
            api_response.close()

        api_status = data.get("status", 0)
        if api_status not in (0, 200):
            reason = data.get("error") or data.get("message") or "no reason given"
            log(f"API returned status {api_status}: {reason}")
            return False

        img_url = data.get("image_url")
        current_filename = data.get("filename") or data.get("image_name") or img_url

        if not img_url:
            log("No image URL in response")
            return False

        if os.path.exists(CACHE_FILE):
            with open(CACHE_FILE, "r", encoding="utf-8") as f:
                last_filename = f.read().strip()
            cache_age_hours = (time.time() - os.path.getmtime(CACHE_FILE)) / 3600
            if current_filename == last_filename:
                if FORCE_REDRAW_HOURS <= 0 or cache_age_hours < FORCE_REDRAW_HOURS:
                    log(f"No change detected ({current_filename}). Skipping update.")
                    return True
                log(
                    f"Image unchanged for {cache_age_hours:.1f}h ({current_filename}); "
                    "redrawing anyway."
                )

        log(f"New image found: {current_filename}. Downloading...")

        if img_url.startswith("/"):
            img_url = f"{BASE_URL}{img_url}"

        img_response = get_with_retries(img_url, timeout=60)
        try:
            if img_response.status_code != 200:
                log(f"Image download failed: {img_response.status_code}")
                return False
            img = Image.open(BytesIO(img_response.content))
        finally:
            img_response.close()

        img = prepare_image(img)

        epd = None
        try:
            with deadline(
                DISPLAY_TIMEOUT_SECONDS,
                f"display update exceeded {DISPLAY_TIMEOUT_SECONDS}s",
            ):
                log("Initializing display...")
                epd = driver.EPD()
                if epd.init() != 0:
                    raise RuntimeError("display init failed")
                buffer = epd.getbuffer(img)
                log("Pushing image to display...")
                refresh_start = time.monotonic()
                epd.display(buffer)
                refresh_seconds = time.monotonic() - refresh_start
                log(f"Panel refresh took {refresh_seconds:.1f}s")
                if refresh_seconds < MIN_REFRESH_SECONDS:
                    raise RuntimeError(
                        f"panel refresh took only {refresh_seconds:.1f}s; "
                        "display did not redraw"
                    )
        except BaseException as draw_error:
            # Log the draw failure before cleanup, so a cleanup failure on a
            # wedged panel cannot hide the original cause.
            log(f"Display update failed: {draw_error!r}")
            if epd is not None:
                try:
                    sleep_display(epd)
                except Exception as cleanup_error:
                    log(f"Display cleanup also failed: {cleanup_error}")
            raise
        sleep_display(epd)

        write_cache(current_filename)

        log("Update complete!")
        return True

    except TimeoutError as e:
        log(f"Timeout: {e}")
        return False
    except Exception as e:
        log(f"Error: {e}")
        return False


if __name__ == "__main__":
    signal.signal(signal.SIGTERM, _raise_termination)
    signal.signal(signal.SIGINT, _raise_termination)
    sys.exit(0 if update_display() else 1)
