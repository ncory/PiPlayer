#!/usr/bin/env bash
# PiPlayer one-command installer for a fresh Raspberry Pi OS (Lite) install.
#
#   curl -sL "https://github.com/ncory/PiPlayer/raw/refs/heads/main/install.sh" | bash
#
# Run it as your normal login user, not with sudo: it calls sudo itself where
# needed (on Trixie that means typing your password once). It updates the
# system, clones PiPlayer into ~/piplayer, and runs scripts/install.sh, which
# installs the packages and the boot service. Run the same command again to
# update PiPlayer; playlists, settings and media are kept.
#
# Options (environment variables):
#   PIPLAYER_DIR=~/piplayer      where the source checkout lives
#   PIPLAYER_BRANCH=main         branch to install
#   PIPLAYER_SKIP_UPGRADE=1      don't run apt full-upgrade
#   PIPLAYER_OPTS="--port 8080 --no-quiet-boot"   passed to scripts/install.sh
#
# Everything is wrapped in main(), called on the last line, so bash reads the
# whole script before running any of it (important when piped from curl).

set -euo pipefail

REPO_URL="${PIPLAYER_REPO:-https://github.com/ncory/PiPlayer.git}"
BRANCH="${PIPLAYER_BRANCH:-main}"
DIR="${PIPLAYER_DIR:-$HOME/piplayer}"

say() { printf '\n\033[1;35m==>\033[0m \033[1m%s\033[0m\n' "$*"; }
die() { printf '\n\033[1;31mError:\033[0m %s\n' "$*" >&2; exit 1; }

main() {
  [[ $EUID -ne 0 ]] || die "run this as your normal user, not as root or with sudo."
  [[ -r /etc/os-release ]] && . /etc/os-release
  [[ "${ID:-}" == "debian" || "${ID:-}" == "raspbian" || "${ID_LIKE:-}" == *debian* ]] \
    || die "this installer is for Raspberry Pi OS (Debian)."
  if [[ "${VERSION_CODENAME:-}" != "trixie" ]]; then
    printf '\nNote: PiPlayer is tested on Raspberry Pi OS Trixie; this is %s.\n' \
      "${PRETTY_NAME:-unknown}"
  fi
  local model
  model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
  [[ "$model" == Raspberry\ Pi* ]] || printf '\nNote: this does not look like a Raspberry Pi (%s).\n' "${model:-unknown}"

  say "Checking sudo access (you may be asked for your password)"
  sudo -v || die "sudo access is required."

  say "Updating package lists"
  sudo apt-get update </dev/null
  if [[ "${PIPLAYER_SKIP_UPGRADE:-}" != 1 ]]; then
    say "Upgrading the system (set PIPLAYER_SKIP_UPGRADE=1 to skip)"
    sudo DEBIAN_FRONTEND=noninteractive apt-get -y \
      -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold full-upgrade </dev/null
  fi
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends git ca-certificates </dev/null

  if [[ -d "$DIR/.git" ]]; then
    say "Updating PiPlayer in $DIR"
    git -C "$DIR" fetch --quiet origin "$BRANCH" </dev/null
    git -C "$DIR" checkout --quiet "$BRANCH" </dev/null
    git -C "$DIR" reset --quiet --hard "origin/$BRANCH" </dev/null
  elif [[ -e "$DIR" ]]; then
    die "$DIR exists but is not a git checkout. Move it aside or set PIPLAYER_DIR."
  else
    say "Downloading PiPlayer into $DIR"
    git clone --quiet --branch "$BRANCH" "$REPO_URL" "$DIR" </dev/null \
      || die "could not clone $REPO_URL (is the repository public?)"
  fi
  printf 'Version: %s\n' "$(git -C "$DIR" log -1 --format='%h %s')"

  say "Installing the PiPlayer service"
  # shellcheck disable=SC2086  # PIPLAYER_OPTS is intentionally word-split
  sudo "$DIR/scripts/install.sh" --deploy-user "$USER" ${PIPLAYER_OPTS:-} </dev/null

  local port_opt="" url
  [[ " ${PIPLAYER_OPTS:-} " =~ --port\ ([0-9]+) ]] && port_opt="${BASH_REMATCH[1]}"
  url="http://$(hostname).local/"
  [[ -n "$port_opt" && "$port_opt" != 80 ]] && url="http://$(hostname).local:$port_opt/"
  say "Done"
  cat <<EOF
PiPlayer is installed and starts at boot.

  Web UI:        $url
  Media folder:  /var/lib/piplayer/media
  Logs:          journalctl -u piplayer -f
  Update:        run the same curl command again

Reboot once to apply the boot settings:  sudo reboot
EOF
}

main "$@"
