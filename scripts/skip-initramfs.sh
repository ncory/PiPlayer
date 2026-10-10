#!/usr/bin/env bash
# Stop the firmware loading the initramfs.  Run on the Pi:  sudo ./scripts/skip-initramfs.sh
#
# The initramfs is the largest single thing the firmware reads before Linux
# exists -- 15.7MB against a 9.7MB kernel on a Pi 3, at roughly 3MB/s through
# the firmware's simple SD reader.  Skipping it is worth several seconds of the
# time between a power cut and a picture on screen.
#
# Raspberry Pi OS builds ext4 and the SD/MMC drivers into the kernel, so it can
# mount the root filesystem directly; the initramfs matters for setups this is
# not (encrypted root, LVM, booting from USB/NFS).  The script checks that
# before changing anything and refuses if those drivers are modules here.
#
# IF IT DOES NOT BOOT, the fix is one line, and a Mac or PC can do it:
#
#   1. Power off, take the SD card out, put it in your computer
#   2. A FAT volume appears (usually "bootfs") -- open config.txt in any editor
#   3. Change  auto_initramfs=0  back to  auto_initramfs=1
#   4. Save, eject, card back in the Pi
#
# Nothing is deleted: the initramfs files stay on the card, so that one line is
# all that is needed.  The root filesystem is ext4 and macOS cannot read it, but
# you never need to -- everything that controls booting is on the FAT partition.
set -euo pipefail

CONFIG_TXT=/boot/firmware/config.txt

DISABLE=0
[[ "${1:-}" == "--disable" ]] && DISABLE=1
[[ $# -gt 1 || ( $# -eq 1 && $DISABLE -eq 0 ) ]] && { echo "usage: $0 [--disable]" >&2; exit 2; }
[[ $EUID -eq 0 ]] || { echo "run with sudo" >&2; exit 1; }
[[ -f $CONFIG_TXT ]] || { echo "no $CONFIG_TXT -- is this a Raspberry Pi?" >&2; exit 1; }

set_config() {
  local key="$1" val="$2"
  if grep -qE "^[[:space:]]*${key}=" "$CONFIG_TXT"; then
    sed -i -E "s|^[[:space:]]*${key}=.*|${key}=${val}|" "$CONFIG_TXT"
  else
    printf '\n[all]\n%s=%s\n' "$key" "$val" >>"$CONFIG_TXT"
  fi
}

if [[ $DISABLE -eq 1 ]]; then
  echo "==> Loading the initramfs again"
  set_config auto_initramfs 1
  echo "Done. Reboot to apply."
  exit 0
fi

echo "==> Checking this kernel can mount root without an initramfs"
KREL="$(uname -r)"
missing=""
for m in ext4 mmc_block mmc_core; do
  if find "/lib/modules/$KREL" -name "${m}.ko*" 2>/dev/null | grep -q .; then
    missing="$missing $m"
  fi
done
if [[ -n "$missing" ]]; then
  echo "refusing: these are kernel modules here, so the root filesystem would" >&2
  echo "          need the initramfs to mount:$missing" >&2
  exit 1
fi
grep -qw ext4 /proc/filesystems || { echo "refusing: kernel does not list ext4 as built in" >&2; exit 1; }
ROOT_FS="$(findmnt -no FSTYPE / || true)"
[[ "$ROOT_FS" == "ext4" ]] || { echo "refusing: root is '$ROOT_FS', not ext4" >&2; exit 1; }
if grep -qE "cryptdevice|root=/dev/mapper|nfsroot" /boot/firmware/cmdline.txt 2>/dev/null; then
  echo "refusing: the kernel command line points at an encrypted/LVM/NFS root," >&2
  echo "          which needs the initramfs" >&2
  exit 1
fi
echo "    ext4 and MMC are built in, root is a plain ext4 partition -- safe to skip"

echo "==> Telling the firmware not to load the initramfs"
set_config auto_initramfs 0

cat <<EOF

Done. Reboot to apply.

The initramfs files are still on the card; only the one config line changed.

If the Pi does not come back after the reboot:
  1. Power off, SD card into your Mac or PC
  2. Open config.txt on the FAT volume (usually "bootfs")
  3. Set  auto_initramfs=1
  4. Save, eject, card back in the Pi

Or, while it is still running:  sudo $0 --disable
EOF
