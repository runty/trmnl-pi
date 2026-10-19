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
| `epd7in5_V2` | 7.5 inch V2, 800×480, black and white | 1-bit | about 5 s |
| `epd7in3e` | 7.3 inch Spectra 6, 800×480 | RGB | about 30 s |

Each driver needs its BUSY timeout patch from `patches/` (see below). The
panel also sets the `Model` header and the shortest refresh that counts as a
real redraw; `TRMNL_MODEL` and `TRMNL_MIN_REFRESH_SECONDS` override them.

On TRMNL, set the device's model to one that matches the panel, so screens are
rendered for its size and inks.

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
- `scripts/update_display.sh` - cron-safe launcher with env loading, lock, timeout.
- `scripts/status.sh` - local diagnostic helper for stale screens and Wi-Fi issues.
- `scripts/network_watchdog.sh` - Wi-Fi recovery run by the watchdog timer.
- `config/systemd/trmnl-network-watchdog.{service,timer}` - root watchdog units.
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
