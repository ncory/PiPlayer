#!/usr/bin/env bash
# PiPlayer installer for Raspberry Pi OS Lite (Trixie / Debian 13).
#
#   git clone git@github.com:ncory/PiPlayer.git && cd PiPlayer && sudo ./scripts/install.sh
#
# Re-run it to update: it re-syncs the code and restarts the service, keeping
# playlists, settings and media in /var/lib/piplayer.
#
# Options:
#   --no-quiet-boot   leave the boot console / login prompt on the HDMI output
#   --port N          web UI port (default 80)
set -euo pipefail

APP_DIR=/opt/piplayer
DATA_DIR=/var/lib/piplayer
SVC_USER=piplayer
PORT=80
QUIET_BOOT=1
SRC_DIR="$(cd "$(dirname "$0")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-quiet-boot) QUIET_BOOT=0 ;;
    --port) PORT="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }

echo "==> Installing packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
  python3 python3-gi python3-gst-1.0 python3-aiohttp python3-pil python3-libgpiod \
  gir1.2-gstreamer-1.0 gir1.2-gst-plugins-base-1.0 \
  gstreamer1.0-tools gstreamer1.0-plugins-base gstreamer1.0-plugins-good \
  gstreamer1.0-plugins-bad gstreamer1.0-libav gstreamer1.0-gl gstreamer1.0-alsa \
  libgl1-mesa-dri libegl-mesa0 libgbm1 libgles2 \
  alsa-utils rsync avahi-daemon

echo "==> Creating user and directories"
if ! id "$SVC_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SVC_USER"
fi
usermod -aG video,render,audio,gpio "$SVC_USER"
install -d -o "$SVC_USER" -g "$SVC_USER" "$DATA_DIR" "$DATA_DIR/media"
# let the login user drop files into the media folder (scp/rsync)
if [[ -n "${SUDO_USER:-}" && "$SUDO_USER" != root ]]; then
  usermod -aG "$SVC_USER" "$SUDO_USER"
  chmod 2775 "$DATA_DIR/media"
fi

echo "==> Installing application to $APP_DIR"
install -d "$APP_DIR"
rsync -a --delete --exclude .git --exclude .venv --exclude dev-data --exclude '__pycache__' \
  --exclude .pytest_cache --exclude .claude "$SRC_DIR/" "$APP_DIR/"

echo "==> Installing systemd service"
sed "s/@PORT@/$PORT/" "$SRC_DIR/scripts/piplayer.service" > /etc/systemd/system/piplayer.service
systemctl daemon-reload
systemctl enable piplayer.service

if [[ $QUIET_BOOT -eq 1 ]]; then
  echo "==> Quiet boot: hiding console text and the tty1 login prompt"
  CMDLINE=/boot/firmware/cmdline.txt
  if [[ -f $CMDLINE ]]; then
    cp -n "$CMDLINE" "$CMDLINE.piplayer-backup" || true
    for opt in quiet loglevel=3 logo.nologo vt.global_cursor_default=0 consoleblank=0; do
      grep -qw -- "$opt" "$CMDLINE" || sed -i "1 s/\$/ $opt/" "$CMDLINE"
    done
  fi
  systemctl disable getty@tty1.service >/dev/null 2>&1 || true
fi

systemctl restart piplayer.service
sleep 2
systemctl --no-pager --lines=5 status piplayer.service || true

HOST="$(hostname).local"
[[ "$PORT" == 80 ]] && URL="http://$HOST/" || URL="http://$HOST:$PORT/"
echo
echo "PiPlayer is installed. Open $URL"
echo "Media folder: $DATA_DIR/media   Logs: journalctl -u piplayer -f"
[[ $QUIET_BOOT -eq 1 ]] && echo "Reboot once to apply the quiet-boot settings."
