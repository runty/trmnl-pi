"""TRMNL's own 4-gray waveform for UC8179 panels such as the Waveshare 7.5" V2.

Waveshare's init_4Gray() relies on the panel's built-in waveform at a forced
temperature, which leaves black a dark gray on the 7.5" V2. TRMNL's firmware
instead loads its own waveform tables through bb_epaper
(https://github.com/bitbank2/bb_epaper). bb_epaper is GPL-3.0, so this module
does not copy the tables: it reads the named init sequence from a bb_epaper
checkout at run time (BB_EPAPER_SRC, its src/ directory).
"""
import os
import re
import time

BB_EPAPER_SRC = os.path.expanduser(os.getenv("BB_EPAPER_SRC", "~/bb_epaper/src"))
BUSY_WAIT = 0xFF  # bb_epaper's marker for "wait for BUSY" in an init sequence


def load_sequence(name):
    """Parse bb_epaper's init sequence `name` into a list of (command, data) steps.

    A step of None means wait for BUSY. Raises OSError or ValueError when the
    checkout or the sequence is missing.
    """
    with open(os.path.join(BB_EPAPER_SRC, "bb_epaper.h"), encoding="utf-8") as f:
        constants = dict(re.findall(r"\b(UC8151_\w+)\s*=\s*(0x[0-9a-fA-F]+)", f.read()))
    constants["BUSY_WAIT"] = str(BUSY_WAIT)
    with open(os.path.join(BB_EPAPER_SRC, "bb_ep.inl"), encoding="utf-8") as f:
        source = f.read()
    match = re.search(
        r"const uint8_t " + re.escape(name) + r"\[\]\s*PROGMEM\s*=\s*\{(.*?)\n\};", source, re.S
    )
    if not match:
        raise ValueError(f"{name} not found in {BB_EPAPER_SRC}/bb_ep.inl")
    body = re.sub(r"//[^\n]*", "", match.group(1))
    values = []
    for token in (t.strip() for t in body.split(",")):
        if token:
            values.append(int(constants.get(token, token), 0))
    steps, i = [], 0
    while values[i] != 0:  # a length of 0 ends the sequence
        length = values[i]
        i += 1
        if length == BUSY_WAIT:
            steps.append(None)
            continue
        steps.append((values[i], bytes(values[i + 1:i + length])))
        i += length
    return steps


def planes(img):
    """Split an "L" image of the four 2-bit levels into bb_epaper's two RAM planes.

    bb_epaper numbers the grays 3 (black) to 0 (white) and sends the high bit
    with DTM1 (0x10) and the low bit with DTM2 (0x13), one bit per pixel.
    """
    gray = [3 - min(3, (v + 42) // 85) for v in range(256)]
    high = img.point([255 if g & 2 else 0 for g in gray], "1").tobytes()
    low = img.point([255 if g & 1 else 0 for g in gray], "1").tobytes()
    return high, low


def draw(epd, epdconfig, img, steps):
    """Write img to the panel, load the waveform and refresh.

    Returns the refresh time in seconds. epd is a Waveshare EPD object for a
    UC8179 panel (it supplies reset, send_command, send_data, send_data2 and
    ReadBusy); the caller puts the panel to sleep afterwards.
    """
    if epdconfig.module_init() != 0:
        raise RuntimeError("display init failed")
    epd.reset()
    high, low = planes(img)
    epd.send_command(0x10)
    epd.send_data2(high)
    epd.send_command(0x13)
    epd.send_data2(low)
    # bb_epaper sends the waveform after the image data, just before refreshing.
    for step in steps:
        if step is None:
            epd.ReadBusy()
            continue
        command, data = step
        epd.send_command(command)
        if data:
            epd.send_data2(data)
    refresh_start = time.monotonic()
    epd.send_command(0x12)  # display refresh
    epd.send_data(0x00)
    epdconfig.delay_ms(100)
    epd.ReadBusy()
    return time.monotonic() - refresh_start
