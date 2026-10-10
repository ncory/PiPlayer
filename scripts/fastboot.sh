#!/usr/bin/env bash
# Shorten the time from power-on to picture.  Run on the Pi:  sudo ./scripts/fastboot.sh
#
# For an unattended player the number that matters after a power cut is how long
# until something is on screen.  Measured on a Pi 3 (little-leota, Oct 2026):
#
#   firmware/bootloader   9.6s   reads ~28MB off the card before Linux exists
#   kernel                4.7s
#   userspace -> picture 16.8s
#   -------------------------- total 31.0s
#
# This script takes the low-risk time out of that:
#
#   * cloud-init: ~4.5s of first-boot provisioning that runs on every boot of an
#     already-provisioned player, and sits directly on the path to basic.target.
#   * camera/display autodetect: firmware probes for a camera and a DSI panel
#     that a player does not have.
#   * the boot splash.
#
# Deliberately NOT done here: shrinking or removing the 15.7MB initramfs, which
# is the single biggest item in the firmware stage.  It is worth several seconds
# but a mistake there leaves a Pi that will not boot and needs its card pulled,
# so it belongs on a test unit first.
#
# Everything here is reversible with --disable, and none of it can stop the Pi
# booting.  A reboot is needed for any of it to take effect.
set -euo pipefail

CONFIG_TXT=/boot/firmware/config.txt
CLOUD_INIT_OFF=/etc/cloud/cloud-init.disabled

DISABLE=0
[[ "${1:-}" == "--disable" ]] && DISABLE=1
[[ $# -gt 1 || ( $# -eq 1 && $DISABLE -eq 0 ) ]] && { echo "usage: $0 [--disable]" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -f $CONFIG_TXT ]] || { echo "no $CONFIG_TXT -- is this a Raspberry Pi?" >&2; exit 1; }

# Set key=value in config.txt, replacing an existing uncommented line or
# appending in its own [all] section. Never duplicates.
set_config() {
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*${key}=" "$CONFIG_TXT"; then
    sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${val}|" "$CONFIG_TXT"
  else
    printf '\n[all]\n%s=%s\n' "$key" "$val" >>"$CONFIG_TXT"
  fi
}

drop_config() {
  local key="$1"
  sed -i -E "/^[[:space:]]*${key}=/d" "$CONFIG_TXT"
  # Removing the last key under an appended [all] would leave the empty header
  # behind, and another apply/revert cycle would add a second one. Strip any
  # section header at the end of the file with nothing under it.
  awk 'BEGIN{n=0} {lines[n++]=$0}
       END{ last=n-1
            while (last>=0 && lines[last] ~ /^[[:space:]]*$/) last--
            if (last>=0 && lines[last] ~ /^\[[a-z0-9]+\]$/) {
              last--
              while (last>=0 && lines[last] ~ /^[[:space:]]*$/) last--
            }
            for (i=0;i<=last;i++) print lines[i] }' "$CONFIG_TXT" >"$CONFIG_TXT.tmp" \
    && mv "$CONFIG_TXT.tmp" "$CONFIG_TXT"
}

if [[ $DISABLE -eq 1 ]]; then
  echo "==> Restoring the stock boot behaviour"
  set_config camera_auto_detect 1
  set_config display_auto_detect 1
  drop_config disable_splash
  rm -f "$CLOUD_INIT_OFF"
  echo "Done. Reboot to apply."
  exit 0
fi

cp -n "$CONFIG_TXT" "$CONFIG_TXT.piplayer-prefastboot" || true

echo "==> Skipping camera and DSI-display probing in firmware"
set_config camera_auto_detect 0
set_config display_auto_detect 0

echo "==> Hiding the boot splash"
set_config disable_splash 1

echo "==> Disabling cloud-init (first-boot provisioning, already done)"
if [[ -d /etc/cloud ]]; then
  touch "$CLOUD_INIT_OFF"
else
  echo "    cloud-init not installed; nothing to do"
fi

cat <<EOF

Done. Reboot to apply, then check what changed:

  systemd-analyze                                   # kernel vs userspace
  systemd-analyze critical-chain piplayer.service   # what the player waits for

Revert with:  sudo $0 --disable
If you re-flash this card with Raspberry Pi Imager and want its hostname / WiFi
/ SSH settings applied on first boot, revert first -- that is cloud-init's job.
EOF
