# trmnl-pi

TRMNL client for a Raspberry Pi OS host driving a Waveshare e-paper panel
through Waveshare's Python library.

It started as `trmnl-ws73`, the client for a Pi Zero W with a 7.3 inch Spectra 6
panel, and now drives either panel below. It is separate from the Pico W /
Pimoroni Inky Frame MicroPython client in `trmnlframe`.

## Panels

Set `TRMNL_PANEL` in `trmnl-pi.env` to the Waveshare driver for the attached panel:

| `TRMNL_PANEL` | Panel | Image | Real refresh |
| --- | --- | --- | --- |
| `epd7in5_V2` | 7.5 inch V2, 800×480, black and white | 1-bit or 4 grays | about 3 s (1-bit) |
| `epd7in3e` | 7.3 inch Spectra 6, 800×480 | RGB | about 30 s |

Each driver needs its BUSY timeout patch from `patches/` (see below). The
panel also sets the `Model` header and the shortest refresh that counts as a
real redraw; `TRMNL_MODEL` and `TRMNL_MIN_REFRESH_SECONDS` override them.

On TRMNL, set the device's model to one that matches the panel, so screens are
rendered for its size and inks. For the 7.5 inch panel that is "Waveshare 7.5"
B/W" (1-bit) or "TRMNL OG (2-bit)" (4 grays), which is built for the same panel.

## Grayscale

The 7.5 inch panel can also show four shades: black, dark gray, light gray and
white. An image that contains any gray is dithered to the four levels of a 2-bit
image (a 2-bit image from TRMNL passes through unchanged) and drawn in a 4-gray
mode. A pure black-and-white image keeps the 1-bit path. Set
`TRMNL_GRAYSCALE=off` to dither every image to black and white instead.

`TRMNL_GRAY_MODE` picks the 4-gray waveform:

| `TRMNL_GRAY_MODE` | Waveform |
| --- | --- |
| `trmnl` (default) | TRMNL firmware's default for this panel, bb_epaper's `EP75_800x480_4GRAY` |
| `trmnl-dark` | TRMNL's darker variant, `EP75_800x480_4GRAY_V2` |
| `waveshare` | Waveshare's `init_4Gray`/`display_4Gray`, which uses the panel's built-in waveform; black stays a dark gray |

The TRMNL waveforms come from [bb_epaper](https://github.com/bitbank2/bb_epaper),
the library TRMNL's firmware uses. bb_epaper is GPL-3.0, so this client does not
copy its tables: `scripts/trmnl_gray.py` reads the init sequence from a bb_epaper
checkout at run time (`BB_EPAPER_SRC`, default `~/bb_epaper/src`). Pin it to the
commit TRMNL's firmware uses (firmware 1.8.17 pins `1a38d706`):

```bash
git init -q ~/bb_epaper && cd ~/bb_epaper
git remote add origin https://github.com/bitbank2/bb_epaper.git
git fetch -q --depth 1 origin 1a38d706b0259d3637df65a91de09abc3f7ccd5f
git checkout -q FETCH_HEAD
```

If the checkout or the sequence is missing, gray images are drawn in black and
white and the log says why.

TRMNL only renders grays for firmware 1.6 or later, which it reads from the
`FW-Version` header; the client sends its own version (1.3.0) unless
`TRMNL_FW_VERSION` is set. For 4 grays, set `TRMNL_FW_VERSION=1.8.17`, choose the
"TRMNL OG (2-bit)" model and the 4-gray colour palette on trmnl.com, and wait for
each plugin's next refresh.

On a Pi Zero 2 W a refresh takes about 3 s in 1-bit and with the TRMNL
waveforms; Waveshare's 4-gray mode takes about 9 s, because it sends the image
one byte at a time.

To check the panel's shades without TRMNL, draw the test card: four bands in
the 2-bit grays above a black-to-white gradient. It takes the same lock as cron,
skips the Kuma heartbeat, and forgets the cached TRMNL filename so the next run
redraws the TRMNL screen:

```bash
~/trmnl-scripts/scripts/update_display.sh --test-pattern
```

## What It Does

- Fetches `/api/display` from TRMNL.
- Downloads the returned image only when the filename changes.
- Renders the image through the Waveshare driver named by `TRMNL_PANEL`.
- Runs from cron every 15 minutes and once after boot.
- Redraws an unchanged image after 12 hours, and treats a panel refresh that
  finishes faster than the panel's minimum as a failed draw, so a lost draw is
  retried.
- A root systemd timer checks reachability every 5 minutes and re-activates
  Wi-Fi, toggles the radio, then reboots if the Pi stays offline for an hour.
- Optionally pings an Uptime Kuma push monitor after each successful run, so a
  stale screen raises an alert.
- Sends the Wi-Fi signal level (`RSSI`) to TRMNL, shown on the device page.
- Refuses to run if the Waveshare BUSY timeout patch is missing.
- Uses a lock, outer timeout, HTTP retries, display timeout, and BUSY-pin timeout
  so one bad refresh cannot pile up cron jobs indefinitely.

## Files

- `scripts/update_display.py` - TRMNL API fetch, image processing, display update.
- `scripts/trmnl_gray.py` - TRMNL's 4-gray waveforms, read from a bb_epaper checkout.
- `scripts/update_display.sh` - cron-safe launcher with env loading, lock, timeout.
- `scripts/status.sh` - local diagnostic helper for stale screens and Wi-Fi issues.
- `scripts/network_watchdog.sh` - Wi-Fi recovery run by the watchdog timer.
- `config/systemd/trmnl-network-watchdog.{service,timer}` - root watchdog units.
- `config/systemd/trmnl-publish.service` - optional web server for a mirroring display.
- `trmnl-pi.env.example` - local configuration template.
- `config/cron/user-crontab` - user crontab entries.
- `config/logrotate.d/trmnl-display` - log rotation for `cron.log`.
- `config/journald.conf.d/90-trmnl-persistent-storage.conf` - optional persistent journald config.
- `patches/waveshare-<driver>-busy-timeout.patch` - patches for Waveshare's unbounded BUSY waits.

## Install

Waveshare's e-Paper Driver HAT plugs straight onto the Pi's 40-pin header. Set
its Interface Config switch to 4-line SPI; Display Config B (0.47R) suits the
7.5 inch panel.

Enable SPI and install the system packages:

```bash
sudo raspi-config nonint do_spi 0
sudo apt install git python3-requests python3-pil python3-spidev python3-gpiozero logrotate
```

Clone Waveshare's e-Paper library so the driver exists, for example:

```bash
git clone https://github.com/waveshareteam/e-Paper.git ~/e-Paper
ls ~/e-Paper/RaspberryPi_JetsonNano/python/lib/waveshare_epd/epd7in5_V2.py
```

Clone this repo on the Pi:

```bash
git clone https://github.com/runty/trmnl-pi.git ~/trmnl-scripts
cd ~/trmnl-scripts
cp trmnl-pi.env.example trmnl-pi.env
chmod 600 trmnl-pi.env
```

Edit `trmnl-pi.env` and set:

```bash
TRMNL_API_KEY=...
TRMNL_MAC=...
TRMNL_PANEL=epd7in5_V2
```

TRMNL identifies the device by `TRMNL_MAC`, not by the Pi's own address, so a
replacement Pi can keep an existing device's API key and MAC; change only the
device's model on TRMNL if the panel changed.

Install cron and log rotation:

```bash
crontab config/cron/user-crontab
sed \
  -e "s|/home/YOUR_USER|$HOME|g" \
  -e "s|su YOUR_USER YOUR_USER|su $(id -un) $(id -gn)|g" \
  config/logrotate.d/trmnl-display \
  | sudo tee /etc/logrotate.d/trmnl-display >/dev/null
sudo logrotate -d /etc/logrotate.d/trmnl-display
```

Optional but useful on Raspberry Pi OS:

```bash
sudo install -d -m 755 /etc/systemd/journald.conf.d
sudo install -m 644 config/journald.conf.d/90-trmnl-persistent-storage.conf /etc/systemd/journald.conf.d/
sudo systemctl restart systemd-journald
sudo journalctl --flush
```

Disable Wi-Fi power save for the active NetworkManager connection:

```bash
nmcli -g NAME connection show --active
sudo nmcli connection modify "<connection-name>" 802-11-wireless.powersave 2
```

That setting normally applies on the next reconnect or reboot.

Make NetworkManager retry the Wi-Fi connection forever instead of giving up
after four failed attempts:

```bash
sudo nmcli connection modify "<connection-name>" connection.autoconnect-retries 0
```

## Network Watchdog

A Pi Zero W's Wi-Fi can drop and never come back; in September 2026 the first
frame running this client sat offline for six weeks showing a stale screen.
Install the watchdog timer:

```bash
sed "s|/home/YOUR_USER|$HOME|g" config/systemd/trmnl-network-watchdog.service \
  | sudo tee /etc/systemd/system/trmnl-network-watchdog.service >/dev/null
sudo install -m 644 config/systemd/trmnl-network-watchdog.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now trmnl-network-watchdog.timer
```

Every 5 minutes it checks that `usetrmnl.com` resolves. If it does not and the
default gateway also does not answer, it re-activates `wlan0` after 2 failed
checks, toggles the Wi-Fi radio from 4, and reboots at 12 (one hour), at most
once every 6 hours. When the gateway answers but DNS fails, it only logs. Its
messages go to the journal under `trmnl-network-watchdog`.

Test the escalation without touching Wi-Fi by pointing it at a missing host and
an unreachable gateway in dry-run mode; each run adds one failure:

```bash
for i in $(seq 1 12); do
  sudo TRMNL_WATCHDOG_DRY_RUN=1 TRMNL_WATCHDOG_HOST=nonexistent.invalid \
    TRMNL_WATCHDOG_GATEWAY=192.0.2.1 TRMNL_WATCHDOG_STATE=/tmp/wd-test \
    TRMNL_WATCHDOG_REBOOT_STAMP=/tmp/wd-test-reboot \
    ~/trmnl-scripts/scripts/network_watchdog.sh
done
sudo rm -f /tmp/wd-test /tmp/wd-test-reboot
```

## Stale-Screen Alert

Create an Uptime Kuma **Push** monitor (30 minute heartbeat interval, 1 retry,
so it alerts after about an hour without a successful draw) and put its push
URL in `trmnl-pi.env`:

```bash
TRMNL_HEARTBEAT_URL=https://uptime.example.org/api/push/<token>
```

`update_display.sh` pings it after every successful run; a failed ping only
logs a warning. If the Kuma hostname resolves to an address the Pi cannot reach
(for example a Tailscale address), pin it to a LAN address with
`TRMNL_HEARTBEAT_RESOLVE=host:443:address`, which is passed to `curl --resolve`.

## Mirror to Another Display

Another display on the LAN can show exactly what this panel shows, without a
TRMNL licence of its own. Set a publish folder in `trmnl-pi.env`:

```bash
TRMNL_PUBLISH_DIR=$HOME/trmnl-scripts/www
```

After each successful draw, `update_display.py` saves the image as the panel
shows it to `current.png` there: 1-bit, or 8-bit gray holding only the four levels for
4-gray screens (smaller than a 2-bit palette file of the same pixels). A
failed save only logs a warning. Serve the folder on port 8075:

```bash
sed "s|YOUR_USER|$(id -un)|g" config/systemd/trmnl-publish.service \
  | sudo tee /etc/systemd/system/trmnl-publish.service >/dev/null
sudo systemctl daemon-reload
sudo systemctl enable --now trmnl-publish.service
```

The mirror then fetches `http://<this-host>:8075/current.png`; Python's server
answers `If-Modified-Since` with `304`, so an unchanged image is not redrawn.
For an Inkplate, HomePlate's image URL activity does this.

To fill a mirror with a different shape, set its size and gray depth:

```bash
TRMNL_PUBLISH_SIZE=960x540
TRMNL_PUBLISH_GRAYS=8
```

The image is then scaled to cover that size, keeping the top and cropping the
rest (for 800×480 on 960×540: ×1.2, 36 rows cropped off the bottom), and each
pixel is snapped to one of `TRMNL_PUBLISH_GRAYS` even steps from black to white.
Scaling adds gray edges; snapping them to an Inkplate 5's 8 shades lets it draw
them with dithering off, since it keeps the top three bits of each gray.

## Waveshare Busy Timeout

Waveshare's drivers wait on the display BUSY pin without a limit. Apply the
patch for your panel so hardware/display stalls fail the update instead of
leaving Python blocked forever:

```bash
cd ~/e-Paper/RaspberryPi_JetsonNano/python/lib
patch -p1 < ~/trmnl-scripts/patches/waveshare-epd7in5_V2-busy-timeout.patch
```

Use `waveshare-epd7in3e-busy-timeout.patch` for the 7.3 inch panel. The
timeout defaults to 180 seconds and can be changed with:

```bash
WAVESHARE_BUSY_TIMEOUT=180
```

## Run Manually

```bash
~/trmnl-scripts/scripts/update_display.sh
tail -f ~/trmnl-scripts/cron.log
```

## Check a Stale Screen

Run the status helper on the Pi:

```bash
~/trmnl-scripts/scripts/status.sh
```

It checks:

- whether an updater or timeout process is still running
- the latest `cron.log` refresh attempt
- the cached TRMNL image filename
- DNS and HTTPS reachability for `usetrmnl.com`
- NetworkManager and Wi-Fi events from the current boot
- temperature and throttling, when `vcgencmd` is available

If the log shows `NameResolutionError`, `link timed out`, or failed Wi-Fi
activation before the updater reaches `Initializing display...`, the screen is
stale because the Pi missed a network/API refresh, not because the display path
hung. Once network is back, run:

```bash
~/trmnl-scripts/scripts/update_display.sh
```

If the updater stops after `Initializing display...` or `Pushing image to display...`,
check for the Waveshare BUSY timeout patch and inspect recent `brcmf`, SPI, and
GPIO messages in `journalctl`.

## Notes

Do not commit `trmnl-pi.env`; it contains the TRMNL API key and device MAC.
