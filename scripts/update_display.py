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

import trmnl_gray

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_DIR = os.path.dirname(SCRIPT_DIR)
WAVESHARE_LIB = os.path.expanduser(
    os.getenv("WAVESHARE_LIB", "~/e-Paper/RaspberryPi_JetsonNano/python/lib")
)
sys.path.insert(0, WAVESHARE_LIB)

# Supported Waveshare drivers, chosen with TRMNL_PANEL. image_mode is what the
# image is converted to before the driver's getbuffer(); a refresh faster than
# min_refresh_seconds means the panel did not actually redraw. gray4 names the
# driver's init, getbuffer and display methods for its 4-gray mode, and
# gray_waveforms the bb_epaper init sequences that TRMNL_GRAY_MODE can pick
# instead (see trmnl_gray.py).
PANELS = {
    # 7.3" Spectra 6: a real refresh holds BUSY for ~30s.
    "epd7in3e": {
        "image_mode": "RGB",
        "min_refresh_seconds": 10,
        "model": "Waveshare 7.3 Spectra 6",
    },
    # 7.5" V2 black and white: a full refresh holds BUSY for ~3s.
    "epd7in5_V2": {
        "image_mode": "1",
        "min_refresh_seconds": 1,
        "model": "Waveshare 7.5 V2",
        "gray4": ("init_4Gray", "getbuffer_4Gray", "display_4Gray"),
        # TRMNL firmware's EP75_800x480_4GRAY (its default) and _V2 (darker).
        "gray_waveforms": {
            "trmnl": "epd75_old_gray_init",
            "trmnl-dark": "epd75_old_gray_init2",
        },
    },
}
PANEL_NAME = os.getenv("TRMNL_PANEL", "")
PANEL = PANELS.get(PANEL_NAME, {})
# "auto" draws images that contain grays in the panel's 4-gray mode; "off"
# always dithers to black and white.
GRAYSCALE = os.getenv("TRMNL_GRAYSCALE", "auto").lower()
# Which 4-gray mode to use: a key of the panel's gray_waveforms (TRMNL's
# waveforms, read from bb_epaper), or "waveshare" for the driver's own mode.
GRAY_MODE = os.getenv("TRMNL_GRAY_MODE", "trmnl").lower()
# The four levels of a 2-bit image, black to white, and the values Waveshare's
# getbuffer_4Gray() expects for them (its GRAY4, GRAY3, GRAY2 and GRAY1).
GRAY_LEVELS = (0, 85, 170, 255)
WAVESHARE_GRAYS = (0x00, 0x80, 0xC0, 0xFF)

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
CLIENT_VERSION = "1.4.0"
# TRMNL reads FW-Version as the device's firmware and offers colour palettes,
# such as 4 grays, only from firmware 1.6; TRMNL_FW_VERSION reports a TRMNL
# firmware version instead of this client's.
FW_VERSION = os.getenv("TRMNL_FW_VERSION") or CLIENT_VERSION
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
    """Convert and size a downloaded image for the panel.

    Returns (image, gray): gray is True when the image should be drawn in the
    panel's 4-gray mode.
    """
    mode = PANEL["image_mode"]
    # Convert first: Pillow resizes palette images with NEAREST regardless
    # of the requested filter.
    img = img.convert("RGB" if mode == "RGB" else "L")
    if img.size != TARGET_RES:
        img = img.resize(TARGET_RES, Image.Resampling.LANCZOS)
    if mode != "1":
        return img, False
    has_grays = any(value not in (0, 255) for _, value in img.getcolors(256))
    if has_grays and PANEL.get("gray4") and GRAYSCALE != "off":
        # Dither to the four 2-bit levels; a 2-bit image passes through unchanged.
        # Quantize from RGB: Pillow quantizes an "L" image to a palette wrongly.
        flat = [v for level in GRAY_LEVELS for v in (level, level, level)]
        palette = Image.new("P", (1, 1))
        palette.putpalette(flat + flat[-3:] * (256 - len(GRAY_LEVELS)))
        img = img.convert("RGB").quantize(
            palette=palette, dither=Image.Dither.FLOYDSTEINBERG
        ).convert("L")
        return img, True
    # A 1-bit image passes through unchanged; grayscale is dithered.
    return img.convert("1"), False


def gray_waveform():
    """bb_epaper steps for TRMNL_GRAY_MODE, None for Waveshare's own 4-gray mode.

    Raises ValueError for an unknown mode, OSError or ValueError when bb_epaper
    or the sequence is missing.
    """
    waveforms = PANEL.get("gray_waveforms", {})
    if GRAY_MODE == "waveshare":
        return None
    if GRAY_MODE not in waveforms:
        raise ValueError(
            f"TRMNL_GRAY_MODE must be one of: {', '.join([*waveforms, 'waveshare'])} "
            f"(got {GRAY_MODE!r})"
        )
    return trmnl_gray.load_sequence(waveforms[GRAY_MODE])


def test_pattern():
    """An image with the four 2-bit grays as bands above a black-to-white gradient."""
    width, height = TARGET_RES
    band_height = height * 2 // 3
    img = Image.new("L", TARGET_RES, 255)
    for i, level in enumerate(GRAY_LEVELS):
        img.paste(level, (i * width // 4, 0, (i + 1) * width // 4, band_height))
    gradient = Image.linear_gradient("L").rotate(90, expand=True)
    img.paste(gradient.resize((width, height - band_height)), (0, band_height))
    return img


def draw(driver, img, gray):
    """Draw a prepared image on the panel, then put the panel to sleep."""
    steps = None
    if gray:
        try:
            steps = gray_waveform()
        except (OSError, ValueError) as e:
            log(f"ERROR: no {GRAY_MODE} gray waveform ({e}); drawing in black and white instead")
            img, gray = img.convert("1"), False
    epd = None
    try:
        with deadline(
            DISPLAY_TIMEOUT_SECONDS,
            f"display update exceeded {DISPLAY_TIMEOUT_SECONDS}s",
        ):
            log("Initializing display..." + (f" (4-gray, {GRAY_MODE})" if gray else ""))
            epd = driver.EPD()
            if steps is not None:
                log("Pushing image to display...")
                refresh_seconds = trmnl_gray.draw(epd, driver.epdconfig, img, steps)
            else:
                init_name, getbuffer_name, display_name = (
                    PANEL["gray4"] if gray else ("init", "getbuffer", "display")
                )
                if gray:
                    # Map each 2-bit level to the value Waveshare's driver expects.
                    img = img.point([
                        WAVESHARE_GRAYS[min(range(4), key=lambda i: abs(GRAY_LEVELS[i] - v))]
                        for v in range(256)
                    ])
                if getattr(epd, init_name)() != 0:
                    raise RuntimeError("display init failed")
                buffer_start = time.monotonic()
                buffer = getattr(epd, getbuffer_name)(img)
                log(f"Buffer took {time.monotonic() - buffer_start:.1f}s")
                log("Pushing image to display...")
                refresh_start = time.monotonic()
                getattr(epd, display_name)(buffer)
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


def draw_test_pattern():
    driver = load_driver()
    if driver is None:
        return False
    try:
        draw(driver, *prepare_image(test_pattern()))
    except Exception as e:
        log(f"Error: {e}")
        return False
    # Forget the last TRMNL image, so the next run redraws it.
    if os.path.exists(CACHE_FILE):
        os.remove(CACHE_FILE)
    log("Test pattern drawn.")
    return True


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
            "FW-Version": FW_VERSION,
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

        draw(driver, *prepare_image(img))

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
    if sys.argv[1:] == ["--test-pattern"]:
        sys.exit(0 if draw_test_pattern() else 1)
    sys.exit(0 if update_display() else 1)
