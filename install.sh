#!/bin/bash
# U1_notify installer for the Snapmaker U1 with paxx12's Extended Firmware.
# Run as root over SSH:
#   curl -fsSL https://raw.githubusercontent.com/DIEHARDave/U1_notify/main/install.sh | bash
# or, after a firmware upgrade, to set up the installed copy again:
#   bash /home/lava/printer_data/U1_notify/install.sh
# Safe to re-run.

set -e

REPO="DIEHARDave/U1_notify"
REF="${U1_NOTIFY_REF:-main}"
DATA_DIR="/home/lava/printer_data"
PLUGIN_DIR="${DATA_DIR}/U1_notify"
CONFIG_DIR="${DATA_DIR}/config"
EXT_DIR="${CONFIG_DIR}/extended"
KLIPPER_DIR="${EXT_DIR}/klipper"
MOONRAKER_DIR="${EXT_DIR}/moonraker"
COMPONENTS_DIR="/home/lava/moonraker/moonraker/components"
SECRETS="${DATA_DIR}/moonraker.secrets"
VARIABLES="${CONFIG_DIR}/variables.cfg"
BACKUP_DIR="${DATA_DIR}/backup/u1_notify"
MOONRAKER_URL="http://127.0.0.1:7125"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33mWARNING:\033[0m %s\n' "$*"; }
die() { printf '\033[1;31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || die "run as root (ssh root@<printer-ip>)"
[ -d "$KLIPPER_DIR" ] && [ -d "$MOONRAKER_DIR" ] \
    || die "$EXT_DIR not found; this needs paxx12's Extended Firmware"
[ -d "$COMPONENTS_DIR" ] || die "Moonraker components not found at $COMPONENTS_DIR"
[ -f "${COMPONENTS_DIR}/notifier.py" ] || die "this Moonraker has no notifier component"
/usr/bin/python3 -c "import apprise" 2> /dev/null \
    || die "Apprise is not installed for Moonraker's Python"

# Restarting Klipper mid-print would kill the print.
state=$(curl -s -m 5 "${MOONRAKER_URL}/printer/objects/query?print_stats=state" \
    | sed -n 's/.*"state": *"\([a-z]*\)".*/\1/p')
case "$state" in
    printing|paused) die "a print is $state; run this again when the printer is idle" ;;
esac

# ---- Plugin files -----------------------------------------------------------

# Running from the installed copy (or a copy placed there by hand) uses it
# as is; otherwise the plugin is downloaded from GitHub.
SELF_DIR=""
if [ -n "${BASH_SOURCE[0]}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
    SELF_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi
if [ "$SELF_DIR" != "$PLUGIN_DIR" ]; then
    TMP_DIR="$(mktemp -d)"
    trap 'rm -rf "$TMP_DIR"' EXIT
    if [ -n "$SELF_DIR" ] && [ -f "${SELF_DIR}/component/u1_notify.py" ]; then
        msg "Copying U1_notify from $SELF_DIR"
        cp -r "$SELF_DIR" "${TMP_DIR}/src"
    else
        msg "Downloading U1_notify ($REF)"
        curl -fsSL "https://codeload.github.com/${REPO}/tar.gz/${REF}" \
            | tar -xzf - -C "$TMP_DIR" || die "download failed"
        mv "$TMP_DIR"/U1_notify-* "${TMP_DIR}/src"
    fi
    [ -f "${TMP_DIR}/src/component/u1_notify.py" ] || die "the download is incomplete"
    rm -rf "$PLUGIN_DIR"
    mv "${TMP_DIR}/src" "$PLUGIN_DIR"
fi
chown -R lava:lava "$PLUGIN_DIR"

# ---- Persistence --------------------------------------------------------------

# Everything outside printer_data, Moonraker's components included, is reset
# at boot unless /oem/.debug exists. Firmware upgrades delete it again, so
# run this installer again after each upgrade.
if [ ! -f /oem/.debug ]; then
    msg "Turning on system persistence (/oem/.debug)"
    touch /oem/.debug
fi

# ---- Earlier hand-made notify setup ---------------------------------------------

# The notify plugin set up by hand from Zmod's files defines the same
# notifiers and macros, which would clash with these.
backup() {
    mkdir -p "$BACKUP_DIR"
    chown lava:lava "$BACKUP_DIR"
    cp -p "$1" "${BACKUP_DIR}/$(basename "$1").$(date +%Y%m%d-%H%M%S)"
}

old_percent=""
for f in "${EXT_DIR}"/*.cfg; do
    [ -f "$f" ] || continue
    case "$(basename "$f")" in extended2.cfg) continue ;; esac
    grep -q 'notify_' "$f" || continue
    val=$(sed -n 's/^[[:space:]]*variable_notify_percent[[:space:]]*:[[:space:]]*\([0-9]*\).*/\1/p' "$f" | head -n 1)
    [ -n "$val" ] && old_percent="$val"
    # Remove it only if it holds nothing but notify_* variables.
    if ! grep -v -e '^[[:space:]]*$' -e '^[[:space:]]*#' -e '^\[' -e 'notify_' "$f" | grep -q .; then
        msg "Removing the old $(basename "$f") (backup in $BACKUP_DIR)"
        backup "$f"
        rm -f "$f"
    fi
done
for f in "${KLIPPER_DIR}"/*.cfg "${MOONRAKER_DIR}"/*.cfg; do
    [ -f "$f" ] && [ ! -L "$f" ] || continue
    case "$(basename "$f")" in u1_notify*) continue ;; esac
    if grep -q -e '^\[gcode_macro _NOTIFY' -e '^\[notifier ' "$f"; then
        if ! grep -v -e '^[[:space:]]' -e '^[[:space:]]*$' -e '^#' "$f" \
                | grep -v -e '^\[gcode_macro _NOTIFY' -e '^\[gcode_macro UPDATE_NOTIFY\]' \
                    -e '^\[notifier ' -e '^[a-z_]*:' | grep -q .; then
            msg "Removing the old $(basename "$f") (backup in $BACKUP_DIR)"
            backup "$f"
            rm -f "$f"
        else
            warn "$f defines notify macros or notifiers along with other settings;"
            warn "remove the notify parts yourself, they clash with U1_notify"
        fi
    fi
done

# ---- Moonraker component and config ---------------------------------------------

msg "Linking the Moonraker component"
ln -sfn "${PLUGIN_DIR}/component/u1_notify.py" "${COMPONENTS_DIR}/u1_notify.py"
chown -h lava:lava "${COMPONENTS_DIR}/u1_notify.py"

msg "Adding the Moonraker settings"
ln -sfn "${PLUGIN_DIR}/moonraker/u1_notify.cfg" "${MOONRAKER_DIR}/u1_notify.cfg"
chown -h lava:lava "${MOONRAKER_DIR}/u1_notify.cfg"
if [ ! -f "${MOONRAKER_DIR}/u1_notify_custom.cfg" ]; then
    install -o lava -g lava -m 644 "${PLUGIN_DIR}/config/u1_notify_custom.cfg" \
        "${MOONRAKER_DIR}/u1_notify_custom.cfg"
fi

# ---- Klipper macros ---------------------------------------------------------------

msg "Linking the Klipper macros"
ln -sfn "${PLUGIN_DIR}/klipper/u1_notify.cfg" "${KLIPPER_DIR}/u1_notify.cfg"
chown -h lava:lava "${KLIPPER_DIR}/u1_notify.cfg"
if grep -l '^\[save_variables\]' "${CONFIG_DIR}"/*.cfg "${KLIPPER_DIR}"/*.cfg 2> /dev/null \
        | grep -v -q 'u1_notify_variables\.cfg$'; then
    rm -f "${KLIPPER_DIR}/u1_notify_variables.cfg"
else
    ln -sfn "${PLUGIN_DIR}/klipper/save_variables.cfg" "${KLIPPER_DIR}/u1_notify_variables.cfg"
    chown -h lava:lava "${KLIPPER_DIR}/u1_notify_variables.cfg"
fi

# Carry over notify_percent from the old setup (into our variables file; a
# [save_variables] of the user's own may point anywhere).
if [ -n "$old_percent" ] && [ -L "${KLIPPER_DIR}/u1_notify_variables.cfg" ] \
        && ! grep -q '^notify_percent' "$VARIABLES" 2> /dev/null; then
    msg "Keeping your progress step: notify_percent = $old_percent"
    if grep -q '^\[Variables\]' "$VARIABLES" 2> /dev/null; then
        sed -i "/^\[Variables\]/a notify_percent = $old_percent" "$VARIABLES"
    else
        printf '[Variables]\nnotify_percent = %s\n' "$old_percent" >> "$VARIABLES"
    fi
    chown lava:lava "$VARIABLES"
fi

# ---- Notification settings ----------------------------------------------------------

msg "Adding the notification settings"
if [ ! -f "${CONFIG_DIR}/u1_notify.txt" ]; then
    # Keep a URL that's already in the secrets, e.g. from the old setup.
    if [ -f "$SECRETS" ] && grep -q '^\[notify\]' "$SECRETS"; then
        {
            sed -n '1,/^\[notify\]/p' "${PLUGIN_DIR}/config/u1_notify.txt" | sed '$d'
            sed -n '/^\[notify\]/,/^\[/p' "$SECRETS" | sed '1!{/^\[/d}'
        } > "${CONFIG_DIR}/u1_notify.txt"
    else
        cp "${PLUGIN_DIR}/config/u1_notify.txt" "${CONFIG_DIR}/u1_notify.txt"
    fi
    chown lava:lava "${CONFIG_DIR}/u1_notify.txt"
fi
# The notifiers read {secrets.notify.*} when Moonraker starts and fail to
# load without them, so seed the secrets with the placeholders until the
# user runs UPDATE_NOTIFY.
if [ ! -f "$SECRETS" ]; then
    sed -n '/^\[notify\]/,$p' "${PLUGIN_DIR}/config/u1_notify.txt" > "$SECRETS"
    chown lava:lava "$SECRETS"
    chmod 600 "$SECRETS"
elif ! grep -q '^\[notify\]' "$SECRETS" && ! grep -q '"notify"' "$SECRETS"; then
    if grep -q '^[[:space:]]*{' "$SECRETS"; then
        warn "$SECRETS is JSON; run UPDATE_NOTIFY after filling in u1_notify.txt"
    else
        printf '\n' >> "$SECRETS"
        sed -n '/^\[notify\]/,$p' "${PLUGIN_DIR}/config/u1_notify.txt" >> "$SECRETS"
    fi
fi

# ---- Restart ------------------------------------------------------------------------

msg "Restarting Klipper and Moonraker"
curl -s -m 10 -X POST "${MOONRAKER_URL}/printer/restart" > /dev/null || true
sleep 2
/etc/init.d/S61moonraker restart > /dev/null

sync
msg "Done. Set your notification URL in u1_notify.txt (Mainsail or Fluidd:"
msg "Configuration), then run UPDATE_NOTIFY in the console."
msg "After a firmware upgrade, run: bash ${PLUGIN_DIR}/install.sh"
