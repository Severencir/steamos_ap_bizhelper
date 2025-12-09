#!/usr/bin/env bash
set -euo pipefail

# ap-bizhelper + Archipelago cleanup helper (non-interactive)
#
# This is intended for DEVELOPMENT / TESTING use.
# It removes config and data directories for:
#
#   - ap-bizhelper (our helper script)
#   - Archipelago (AppImage-based config/data under $HOME)
#
# It does NOT touch:
#   - BizHawk installation outside our helper's data dir
#   - Proton, Steam, or any system packages
#
# Paths removed (if they exist):
#   - $HOME/.config/ap_bizhelper_test
#   - $HOME/.local/share/ap_bizhelper_test
#   - $HOME/.config/Archipelago
#   - $HOME/.local/share/Archipelago
#   - $HOME/.cache/Archipelago
#
# Usage:
#   chmod +x ap-bizhelper-clean.sh
#   ./ap-bizhelper-clean.sh
#
# WARNING: This script is intentionally non-interactive and will delete
# the above directories immediately when run.

HELPER_CONFIG_DIR="$HOME/.config/ap_bizhelper_test"
HELPER_DATA_DIR="$HOME/.local/share/ap_bizhelper_test"

AP_CONFIG_DIR="$HOME/.config/Archipelago"
AP_DATA_DIR="$HOME/.local/share/Archipelago"
AP_CACHE_DIR="$HOME/.cache/Archipelago"

TARGETS=(
  "$HELPER_CONFIG_DIR"
  "$HELPER_DATA_DIR"
  "$AP_CONFIG_DIR"
  "$AP_DATA_DIR"
  "$AP_CACHE_DIR"
)

echo "[ap-bizhelper-clean] Removing the following directories if they exist:"
for d in "${TARGETS[@]}"; do
  echo "  - $d"
done
echo

for d in "${TARGETS[@]}"; do
  if [[ -d "$d" ]]; then
    echo "[ap-bizhelper-clean] Removing: $d"
    rm -rf -- "$d"
  else
    echo "[ap-bizhelper-clean] Skipping (not found): $d"
  fi
done

echo
echo "[ap-bizhelper-clean] Done. Config and data for ap-bizhelper and Archipelago have been reset (where present)."
