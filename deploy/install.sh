#!/usr/bin/env bash
# Native install on Raspberry Pi OS Lite 64-bit (primary deployment mode).
#
#   sudo ./deploy/install.sh                # install/upgrade code + unit; never touches data
#
# What this changes (and nothing else):
#   * apt packages: python3-venv libportaudio2 libsndfile1 alsa-utils chrony
#   * system user/group 'noise-collector' (member of 'audio'), no login shell
#   * /opt/noise-collector/releases/<time>/ (a new venv per install), venv -> current release,
#     previous -> prior release, docs (replaced on upgrade)
#   * /etc/noise-collector/ (created 0750 if missing; existing files are left alone)
#   * /etc/systemd/system/noise-collector.service
# Data under /var/lib/noise-collector is never modified by this script.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
PREFIX=/opt/noise-collector
ETC=/etc/noise-collector
USER_NAME=noise-collector

[[ $EUID -eq 0 ]] || { echo "run with sudo"; exit 1; }
[[ "$(uname -m)" == "aarch64" ]] || echo "warning: expected aarch64 (Raspberry Pi OS 64-bit), found $(uname -m)"

apt-get update
apt-get install -y --no-install-recommends python3-venv libportaudio2 libsndfile1 alsa-utils chrony

PY=$(command -v python3)
"$PY" - <<'PYV'
import sys
assert sys.version_info >= (3, 11), f"Python >= 3.11 required, found {sys.version}"
PYV

if ! id "$USER_NAME" >/dev/null 2>&1; then
  useradd --system --home-dir /var/lib/noise-collector --shell /usr/sbin/nologin --user-group "$USER_NAME"
fi
usermod -a -G audio "$USER_NAME"

install -d -m 0755 "$PREFIX" "$PREFIX/releases"
# Virtualenvs are not relocatable (scripts embed absolute interpreter paths), so each install
# builds a new venv in its final place, releases/<timestamp>, and then atomically repoints the
# $PREFIX/venv symlink. The previous release stays for rollback.
REL="$PREFIX/releases/$(date -u +%Y%m%dT%H%M%SZ)"
"$PY" -m venv "$REL"
"$REL/bin/pip" install --quiet --upgrade pip
# Locked, hash-checked dependencies; then the collector itself without re-resolving deps.
"$REL/bin/pip" install --quiet --require-hashes -r "$SRC/requirements.lock"
"$REL/bin/pip" install --quiet --no-deps "$SRC"
"$REL/bin/noise-collector" --version
if [[ -d "$PREFIX/venv" && ! -L "$PREFIX/venv" ]]; then
  # Layout from earlier installers (a real directory): keep it aside, never delete it here.
  rm -rf "$PREFIX/venv.legacy"; mv "$PREFIX/venv" "$PREFIX/venv.legacy"
fi
if [[ -L "$PREFIX/venv" ]]; then ln -sfn "$(readlink "$PREFIX/venv")" "$PREFIX/previous"; fi
ln -sfn "$REL" "$PREFIX/venv.next" && mv -T "$PREFIX/venv.next" "$PREFIX/venv"
# Keep the three newest releases plus whatever "previous" points to.
KEEP_PREV="$(readlink -f "$PREFIX/previous" 2>/dev/null || true)"
ls -1dt "$PREFIX"/releases/*/ 2>/dev/null | tail -n +4 | while read -r old; do
  [[ "$(readlink -f "$old")" == "$KEEP_PREV" ]] || rm -rf "$old"
done
rm -rf "$PREFIX/docs"; cp -r "$SRC/docs" "$PREFIX/docs"

install -d -m 0750 -o root -g "$USER_NAME" "$ETC"
if [[ ! -f "$ETC/collector.toml" ]]; then
  install -m 0640 -o root -g "$USER_NAME" "$SRC/deploy/collector.example.toml" "$ETC/collector.toml"
  echo "created $ETC/collector.toml from the example: edit it (or run 'noise-collector provision')"
fi
if [[ -f "$ETC/credentials.toml" ]]; then
  chown "$USER_NAME:$USER_NAME" "$ETC/credentials.toml"; chmod 0600 "$ETC/credentials.toml"
fi

install -m 0644 "$SRC/deploy/systemd/noise-collector.service" /etc/systemd/system/noise-collector.service
systemctl daemon-reload
systemctl enable --now chrony >/dev/null 2>&1 || true
echo
echo "installed. next steps:"
echo "  1. put the device token in $ETC/credentials.toml (device_token = \"...\"), owner $USER_NAME, mode 0600"
echo "  2. sudo -u $USER_NAME $PREFIX/venv/bin/noise-collector devices"
echo "  3. sudo -u $USER_NAME $PREFIX/venv/bin/noise-collector provision --bootstrap bootstrap.toml --output $ETC/collector.toml --yes"
echo "  4. sudo -u $USER_NAME $PREFIX/venv/bin/noise-collector doctor"
echo "  5. sudo systemctl enable --now noise-collector"
echo "rollback: sudo systemctl stop noise-collector && sudo ln -sfn \"\$(readlink $PREFIX/previous)\" $PREFIX/venv && sudo systemctl start noise-collector (see docs/operations.md: schema compatibility)"
