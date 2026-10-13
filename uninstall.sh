#!/bin/bash
# Removes U1_notify. Run as root:
#   bash /home/lava/printer_data/U1_notify/uninstall.sh
# u1_notify.txt (your URL and printer name) and variables.cfg are kept, so
# reinstalling picks them up again; delete them yourself if you don't need
# them. /oem/.debug is left alone, since other changes may rely on it.

set -e

DATA_DIR="/home/lava/printer_data"
PLUGIN_DIR="${DATA_DIR}/U1_notify"
CONFIG_DIR="${DATA_DIR}/config"
KLIPPER_DIR="${CONFIG_DIR}/extended/klipper"
MOONRAKER_DIR="${CONFIG_DIR}/extended/moonraker"
COMPONENTS_DIR="/home/lava/moonraker/moonraker/components"
SECRETS="${DATA_DIR}/moonraker.secrets"
MOONRAKER_URL="http://127.0.0.1:7125"

msg() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "run as root" >&2; exit 1; }

msg "Removing the Moonraker component and settings"
rm -f "${COMPONENTS_DIR}/u1_notify.py" "${MOONRAKER_DIR}/u1_notify.cfg" \
    "${MOONRAKER_DIR}/u1_notify_custom.cfg" "${DATA_DIR}/.u1_notify_updated"

msg "Removing the Klipper macros"
rm -f "${KLIPPER_DIR}/u1_notify.cfg" "${KLIPPER_DIR}/u1_notify_variables.cfg"

# Drop the [notify] section from the secrets; delete the file if that was
# all it held.
if [ -f "$SECRETS" ] && grep -q '^\[notify\]' "$SECRETS"; then
    sed -i '/^\[notify\]/,/^\[/{/^\[notify\]/d;/^\[/!d}' "$SECRETS"
    grep -q '[^[:space:]]' "$SECRETS" || rm -f "$SECRETS"
fi

msg "Restarting Klipper and Moonraker"
curl -s -m 10 -X POST "${MOONRAKER_URL}/printer/restart" > /dev/null || true
sleep 2
/etc/init.d/S61moonraker restart > /dev/null

msg "Done."
# Last, as this script lives in the folder.
cd /
exec rm -rf "$PLUGIN_DIR"
