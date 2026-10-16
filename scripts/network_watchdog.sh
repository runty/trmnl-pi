#!/bin/bash
# Recover the Pi's Wi-Fi when it silently drops off the network.
#
# Runs as root from trmnl-network-watchdog.timer. Each run checks whether the
# TRMNL host resolves; consecutive failures escalate:
#   TRMNL_WATCHDOG_RECONNECT_AFTER  failures -> re-activate the NM connection
#   TRMNL_WATCHDOG_RADIO_AFTER      failures -> toggle the Wi-Fi radio
#   TRMNL_WATCHDOG_REBOOT_AFTER     failures -> reboot (rate limited)
set -u

TRMNL_HOST="${TRMNL_WATCHDOG_HOST:-usetrmnl.com}"
WIFI_DEVICE="${TRMNL_WATCHDOG_DEVICE:-wlan0}"
RECONNECT_AFTER="${TRMNL_WATCHDOG_RECONNECT_AFTER:-2}"
RADIO_AFTER="${TRMNL_WATCHDOG_RADIO_AFTER:-4}"
REBOOT_AFTER="${TRMNL_WATCHDOG_REBOOT_AFTER:-12}"
REBOOT_MIN_INTERVAL="${TRMNL_WATCHDOG_REBOOT_MIN_INTERVAL:-21600}"
STATE_FILE="${TRMNL_WATCHDOG_STATE:-/run/trmnl-network-watchdog.failures}"
REBOOT_STAMP="${TRMNL_WATCHDOG_REBOOT_STAMP:-/var/lib/trmnl-network-watchdog/last-reboot}"

log() {
    printf '%s\n' "$*"
    logger -t trmnl-network-watchdog -- "$*" 2>/dev/null || true
}

healthy() {
    timeout 10 getent ahosts "${TRMNL_HOST}" >/dev/null 2>&1
}

gateway() {
    ip -4 route show default dev "${WIFI_DEVICE}" 2>/dev/null | awk '/default/ {print $3; exit}'
}

failures=0
if [ -r "${STATE_FILE}" ]; then
    failures="$(cat "${STATE_FILE}" 2>/dev/null || echo 0)"
    case "${failures}" in ''|*[!0-9]*) failures=0 ;; esac
fi

if healthy; then
    if [ "${failures}" -gt 0 ]; then
        log "Network recovered after ${failures} failed check(s)."
    fi
    rm -f "${STATE_FILE}"
    exit 0
fi

gw="$(gateway)"
if [ -n "${gw}" ] && ping -c 1 -W 3 "${gw}" >/dev/null 2>&1; then
    # The LAN link is up; DNS or upstream is the problem, and bouncing Wi-Fi
    # will not fix it. Log it without escalating.
    log "Cannot resolve ${TRMNL_HOST}, but gateway ${gw} answers; not touching Wi-Fi."
    rm -f "${STATE_FILE}"
    exit 0
fi

failures="$((failures + 1))"
printf '%s\n' "${failures}" > "${STATE_FILE}"
log "Network check failed (${failures} in a row): no DNS for ${TRMNL_HOST}, gateway '${gw:-none}' unreachable."

if [ "${failures}" -ge "${REBOOT_AFTER}" ]; then
    now="$(date +%s)"
    last=0
    [ -r "${REBOOT_STAMP}" ] && last="$(cat "${REBOOT_STAMP}" 2>/dev/null || echo 0)"
    case "${last}" in ''|*[!0-9]*) last=0 ;; esac
    if [ "$((now - last))" -ge "${REBOOT_MIN_INTERVAL}" ]; then
        log "Rebooting to recover Wi-Fi."
        mkdir -p "$(dirname "${REBOOT_STAMP}")"
        printf '%s\n' "${now}" > "${REBOOT_STAMP}"
        sync
        systemctl reboot
        exit 0
    fi
    log "Reboot skipped; last watchdog reboot was $((now - last))s ago."
fi

if [ "${failures}" -ge "${RADIO_AFTER}" ]; then
    log "Toggling the Wi-Fi radio."
    nmcli radio wifi off
    sleep 5
    nmcli radio wifi on
elif [ "${failures}" -ge "${RECONNECT_AFTER}" ]; then
    log "Re-activating Wi-Fi on ${WIFI_DEVICE}."
    nmcli device set "${WIFI_DEVICE}" autoconnect yes >/dev/null 2>&1 || true
    timeout 60 nmcli device connect "${WIFI_DEVICE}" || true
fi
exit 0
