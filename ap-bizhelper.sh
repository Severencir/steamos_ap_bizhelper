#!/usr/bin/env bash
set -euo pipefail

########################################
# Config locations
########################################
CONFIG_DIR="$HOME/.config/ap_bizhelper_test"
CONFIG_FILE="$CONFIG_DIR/settings.conf"
EXT_CONFIG_FILE="$CONFIG_DIR/ext_behavior.conf"
DATA_DIR="$HOME/.local/share/ap_bizhelper_test"
mkdir -p "$CONFIG_DIR" "$DATA_DIR"

AP_APPIMAGE_DEFAULT="$DATA_DIR/Archipelago.AppImage"

# Windows BizHawk + Proton
BIZHAWK_WIN_DIR="$DATA_DIR/bizhawk_win"
BIZHAWK_EXE=""          # path to EmuHawk.exe (Linux side)
PROTON_BIN=""           # path to Proton "proton" script
PROTON_PREFIX="$DATA_DIR/proton_prefix"
BIZHAWK_RUNNER="$DATA_DIR/run_bizhawk_proton.sh"

# Lua for SNES (Windows-style path used inside BizHawk, e.g. "SNI\\lua\\Connector.lua")
SFC_LUA_PATH=""

# Version tracking for update checks
AP_VERSION=""
AP_SKIP_VERSION=""
BIZHAWK_VERSION=""
BIZHAWK_SKIP_VERSION=""

# Latest version info (filled from GitHub API when needed)
AP_LATEST_URL=""
AP_LATEST_VERSION=""
BIZHAWK_LATEST_URL=""
BIZHAWK_LATEST_VERSION=""

# Desktop shortcut flag
AP_DESKTOP_SHORTCUT=""

########################################
# Helpers
########################################

load_config() {
  if [[ -f "$CONFIG_FILE" ]]; then
    # shellcheck disable=SC1090
    source "$CONFIG_FILE"
  fi
}

save_config() {
  cat >"$CONFIG_FILE" <<EOF_CFG
AP_APPIMAGE="${AP_APPIMAGE:-}"
BIZHAWK_EXE="${BIZHAWK_EXE:-}"
PROTON_BIN="${PROTON_BIN:-}"
BIZHAWK_RUNNER="${BIZHAWK_RUNNER:-}"
SFC_LUA_PATH="${SFC_LUA_PATH:-}"
AP_VERSION="${AP_VERSION:-}"
AP_SKIP_VERSION="${AP_SKIP_VERSION:-}"
BIZHAWK_VERSION="${BIZHAWK_VERSION:-}"
BIZHAWK_SKIP_VERSION="${BIZHAWK_SKIP_VERSION:-}"
AP_DESKTOP_SHORTCUT="${AP_DESKTOP_SHORTCUT:-}"
EOF_CFG
}

error_dialog() {
  local msg="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --error --text="$msg" || true
  else
    echo "ERROR: $msg" >&2
  fi
}

info_dialog() {
  local msg="$1"
  if command -v zenity >/dev/null 2>&1; then
    zenity --info --text="$msg" || true
  else
    echo "$msg"
  fi
}

# Button chooser: returns one of "Download" / "Select" / "Cancel"
choose_install_action() {
  local title="$1"
  local text="$2"
  local select_label="${3:-Select}"

  if ! command -v zenity >/dev/null 2>&1; then
    echo "Cancel"
    return 0
  fi

  local output status
  output=$(zenity --question \
      --title="$title" \
      --text="$text" \
      --ok-label="Download" \
      --cancel-label="Cancel" \
      --extra-button="$select_label")
  status=$?

  if [[ $status -eq 0 || $status -eq 5 ]]; then
    if [[ "$output" == "$select_label" ]]; then
      echo "Select"
    else
      echo "Download"
    fi
  else
    echo "Cancel"
  fi
}

########################################
# Download helper with visual progress + cancel
########################################

download_file_with_progress() {
  local url="$1"
  local dest="$2"
  local title="$3"
  local text="$4"

  if ! command -v wget >/dev/null 2>&1 && ! command -v curl >/dev/null 2>&1; then
    error_dialog "Neither wget nor curl is available to download files.\nInstall wget or curl and try again."
    return 1
  fi

  # If zenity isn't available, just download without UI.
  if ! command -v zenity >/dev/null 2>&1; then
    if command -v wget >/dev/null 2>&1; then
      wget -O "$dest" "$url"
    else
      curl -L -o "$dest" "$url"
    fi
    return $?
  fi

  local fifo
  fifo=$(mktemp -u)
  mkfifo "$fifo"

  # Writer: fake progress 1..95 in a loop, keeps the bar moving.
  (
    local p=1
    while :; do
      echo "$p"
      echo "#$text"
      sleep 0.3
      p=$((p + 2))
      if (( p > 95 )); then
        p=1
      fi
    done
  ) >"$fifo" &
  local writer_pid=$!

  # Zenity progress dialog with auto-close at 100 and a Cancel button.
  zenity --progress \
         --title="$title" \
         --percentage=0 \
         --auto-close \
         <"$fifo" &
  local zpid=$!

  # Start the actual download.
  if command -v wget >/dev/null 2>&1; then
    wget -O "$dest" "$url" &
  else
    curl -L -o "$dest" "$url" &
  fi
  local dpid=$!

  local status=0

  # Monitor both zenity and downloader.
  while :; do
    # If download finished, break and send 100%.
    if ! kill -0 "$dpid" 2>/dev/null; then
      wait "$dpid" || status=$?
      break
    fi

    # If zenity closed (user Cancel/close), kill download and fail.
    if ! kill -0 "$zpid" 2>/dev/null; then
      kill "$dpid" 2>/dev/null || true
      wait "$dpid" 2>/dev/null || true
      kill "$writer_pid" 2>/dev/null || true
      rm -f "$fifo"
      return 1
    fi

    sleep 0.2
  done

  # Stop the writer and send a final 100% to close zenity cleanly.
  kill "$writer_pid" 2>/dev/null || true
  echo "100" >"$fifo" 2>/dev/null || true
  echo "#Finished: $text" >"$fifo" 2>/dev/null || true

  # Give zenity a moment to consume the final lines.
  sleep 0.3
  kill "$zpid" 2>/dev/null || true

  rm -f "$fifo"
  return $status
}

########################################
# Extension behavior config
########################################

get_ext_behavior() {
  local ext="$1"
  if [[ ! -f "$EXT_CONFIG_FILE" ]]; then
    echo ""
    return 0
  fi
  local line
  line=$(grep -E "^${ext}=" "$EXT_CONFIG_FILE" 2>/dev/null | head -n1 || true)
  if [[ -z "$line" ]]; then
    echo ""
  else
    echo "${line#*=}"
  fi
}

set_ext_behavior() {
  local ext="$1"
  local val="$2"
  mkdir -p "$CONFIG_DIR"
  if [[ -f "$EXT_CONFIG_FILE" ]]; then
    if grep -q -E "^${ext}=" "$EXT_CONFIG_FILE"; then
      sed -i -E "s/^${ext}=.*/${ext}=${val}/" "$EXT_CONFIG_FILE"
    else
      echo "${ext}=${val}" >>"$EXT_CONFIG_FILE"
    fi
  else
    echo "${ext}=${val}" >"$EXT_CONFIG_FILE"
  fi
}

########################################
# GitHub API helpers for "latest" versions
########################################

resolve_ap_latest_info() {
  local api="https://api.github.com/repos/ArchipelagoMW/Archipelago/releases/latest"

  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    return 1
  fi

  local json
  json=$(curl -s "$api" 2>/dev/null || true)
  [[ -z "$json" ]] && return 1

  local line
  line=$(printf '%s\n' "$json" | jq -r '
    .tag_name as $tag
    | .assets[]
    | select(.name | test("Archipelago_.*_linux-x86_64\\.AppImage$"))
    | "\(.browser_download_url)\t\($tag)"' | head -n1)

  if [[ -z "$line" || "$line" == "null" ]]; then
    return 1
  fi

  AP_LATEST_URL=${line%%$'\t'*}
  AP_LATEST_VERSION=${line##*$'\t'}
  return 0
}

resolve_bizhawk_latest_info() {
  local api="https://api.github.com/repos/TASEmulators/BizHawk/releases/latest"

  if ! command -v curl >/dev/null 2>&1 || ! command -v jq >/dev/null 2>&1; then
    return 1
  fi

  local json
  json=$(curl -s "$api" 2>/dev/null || true)
  [[ -z "$json" ]] && return 1

  local line
  line=$(printf '%s\n' "$json" | jq -r '
    .tag_name as $tag
    | .assets[]
    | select(.name | test("win-x64.*\\.zip$"))
    | "\(.browser_download_url)\t\($tag)"' | head -n1)

  if [[ -z "$line" || "$line" == "null" ]]; then
    return 1
  fi

  BIZHAWK_LATEST_URL=${line%%$'\t'*}
  BIZHAWK_LATEST_VERSION=${line##*$'\t'}
  return 0
}

########################################
# Archipelago install / update
########################################

AP_APPIMAGE=""

install_ap_appimage() {
  local url="$1"
  local ver="$2"

  mkdir -p "$(dirname "$AP_APPIMAGE_DEFAULT")"
  AP_APPIMAGE="$AP_APPIMAGE_DEFAULT"
  rm -f "$AP_APPIMAGE"

  if ! download_file_with_progress "$url" "$AP_APPIMAGE" \
        "Archipelago download" "Downloading Archipelago $ver..."; then
    error_dialog "Archipelago download failed or was cancelled."
    return 1
  fi

  chmod +x "$AP_APPIMAGE" || true
  AP_VERSION="$ver"
  AP_SKIP_VERSION=""
  save_config
  return 0
}

download_ap_appimage() {
  if ! resolve_ap_latest_info; then
    error_dialog "Could not determine the latest Archipelago release."
    exit 1
  fi
  if ! install_ap_appimage "$AP_LATEST_URL" "$AP_LATEST_VERSION"; then
    exit 1
  fi
  info_dialog "Archipelago AppImage downloaded to:\n$AP_APPIMAGE"
}

check_update_ap() {
  # Only auto-update if using our default managed AppImage
  if [[ "${AP_APPIMAGE:-}" != "$AP_APPIMAGE_DEFAULT" ]]; then
    return
  fi
  if ! resolve_ap_latest_info; then
    return
  fi

  if [[ -n "${AP_VERSION:-}" && "$AP_VERSION" == "$AP_LATEST_VERSION" ]]; then
    return
  fi
  if [[ -n "${AP_SKIP_VERSION:-}" && "$AP_SKIP_VERSION" == "$AP_LATEST_VERSION" ]]; then
    return
  fi
  if ! command -v zenity >/dev/null 2>&1; then
    return
  fi

  local choice
  if choice=$(zenity --question \
       --title="Archipelago update" \
       --text="A new Archipelago version is available:\nCurrent: ${AP_VERSION:-unknown}\nLatest: $AP_LATEST_VERSION\n\nUpdate now?" \
       --ok-label="Update" \
       --cancel-label="Later" \
       --extra-button="Skip this version"); then
    if [[ "$choice" == "Skip this version" ]]; then
      AP_SKIP_VERSION="$AP_LATEST_VERSION"
      save_config
      return
    else
      install_ap_appimage "$AP_LATEST_URL" "$AP_LATEST_VERSION" || return
      info_dialog "Archipelago updated to $AP_LATEST_VERSION."
    fi
  else
    # "Later"
    return
  fi
}

select_ap_appimage() {
  local selected
  selected=$(zenity --file-selection \
    --title="Select Archipelago AppImage" \
    --filename="$HOME/") || exit 0

  if [[ ! -f "$selected" ]]; then
    error_dialog "Selected file does not exist."
    exit 1
  fi

  chmod +x "$selected" || true
  AP_APPIMAGE="$selected"
}

ensure_ap_desktop_shortcut() {
  # Only after AP_APPIMAGE is known and executable
  if [[ -z "${AP_APPIMAGE:-}" || ! -x "$AP_APPIMAGE" ]]; then
    return
  fi

  # Already decided
  if [[ -n "${AP_DESKTOP_SHORTCUT:-}" ]]; then
    return
  fi

  if ! command -v zenity >/dev/null 2>&1; then
    AP_DESKTOP_SHORTCUT="no"
    save_config
    return
  fi

  if zenity --question \
      --title="Archipelago shortcut" \
      --text="Create a desktop shortcut for the Archipelago AppImage?\n\nThis will add an 'Archipelago.desktop' launcher to your Desktop." \
      --ok-label="Create" \
      --cancel-label="Skip"; then
    local desktop_dir="$HOME/Desktop"
    mkdir -p "$desktop_dir"
    local shortcut="$desktop_dir/Archipelago.desktop"
    cat >"$shortcut" <<EOF_DESK
[Desktop Entry]
Type=Application
Name=Archipelago
Exec=${AP_APPIMAGE}
Terminal=false
EOF_DESK
    chmod +x "$shortcut" || true
    AP_DESKTOP_SHORTCUT="yes"
    save_config
  else
    AP_DESKTOP_SHORTCUT="no"
    save_config
  fi
}

ensure_ap_appimage() {
  load_config

  # Try stored path
  if [[ -n "${AP_APPIMAGE:-}" && -f "$AP_APPIMAGE" ]]; then
    chmod +x "$AP_APPIMAGE" || true
  elif [[ -x "$AP_APPIMAGE_DEFAULT" ]]; then
    AP_APPIMAGE="$AP_APPIMAGE_DEFAULT"
  fi

  if [[ -z "${AP_APPIMAGE:-}" || ! -x "$AP_APPIMAGE" ]]; then
    # Need to set up AP
    local action
    action=$(choose_install_action \
      "Archipelago setup" \
      "Archipelago AppImage is not configured.\n\nDownload latest AppImage from GitHub, select an existing one, or cancel?") || action="Cancel"

    case "$action" in
      "Download")
        download_ap_appimage
        ;;
      "Select")
        select_ap_appimage
        ;;
      "Cancel"|*)
        exit 0
        ;;
    esac
  fi

  if [[ -z "${AP_APPIMAGE:-}" || ! -x "$AP_APPIMAGE" ]]; then
    error_dialog "Archipelago AppImage was not configured correctly."
    exit 1
  fi

  # Now that AP_APPIMAGE is known, optionally check for updates
  check_update_ap
  save_config
  ensure_ap_desktop_shortcut
}

########################################
# BizHawk (Windows) + Proton setup
########################################

auto_detect_bizhawk_exe() {
  if [[ -d "$BIZHAWK_WIN_DIR" ]]; then
    local emu
    emu=$(find "$BIZHAWK_WIN_DIR" -maxdepth 4 -type f -iname "EmuHawk.exe" | head -n1 || true)
    if [[ -n "$emu" ]]; then
      BIZHAWK_EXE="$emu"
      echo "[ap-bizhelper] Auto-detected BizHawk at: $BIZHAWK_EXE"
      return 0
    fi
  fi
  return 1
}

install_bizhawk_win() {
  local url="$1"
  local ver="$2"

  if ! command -v unzip >/dev/null 2>&1; then
    error_dialog "unzip is required to extract BizHawk.\nInstall unzip or use 'Select existing EmuHawk.exe'."
    return 1
  fi

  mkdir -p "$BIZHAWK_WIN_DIR"
  rm -rf "$BIZHAWK_WIN_DIR"/*
  local tmpzip="$BIZHAWK_WIN_DIR/bizhawk_win.zip"

  if ! download_file_with_progress "$url" "$tmpzip" \
        "BizHawk download" "Downloading BizHawk $ver (Windows win-x64)..."; then
    error_dialog "BizHawk download failed or was cancelled."
    return 1
  fi

  if ! unzip -o "$tmpzip" -d "$BIZHAWK_WIN_DIR" >/dev/null; then
    error_dialog "Failed to extract BizHawk archive."
    return 1
  fi

  rm -f "$tmpzip"

  if ! auto_detect_bizhawk_exe; then
    error_dialog "Could not find EmuHawk.exe in extracted BizHawk."
    return 1
  fi

  BIZHAWK_VERSION="$ver"
  BIZHAWK_SKIP_VERSION=""
  save_config
  return 0
}

download_bizhawk_win() {
  if ! resolve_bizhawk_latest_info; then
    error_dialog "Could not determine the latest BizHawk release."
    exit 1
  fi
  if ! install_bizhawk_win "$BIZHAWK_LATEST_URL" "$BIZHAWK_LATEST_VERSION"; then
    exit 1
  fi
  info_dialog "BizHawk Windows configured.\nEmuHawk.exe:\n$BIZHAWK_EXE"
}

check_update_bizhawk() {
  # Only auto-update if BizHawk is living under our managed directory
  if [[ -z "${BIZHAWK_EXE:-}" || "${BIZHAWK_EXE#$BIZHAWK_WIN_DIR}" == "$BIZHAWK_EXE" ]]; then
    return
  fi
  if ! resolve_bizhawk_latest_info; then
    return
  fi
  if [[ -n "${BIZHAWK_VERSION:-}" && "$BIZHAWK_VERSION" == "$BIZHAWK_LATEST_VERSION" ]]; then
    return
  fi
  if [[ -n "${BIZHAWK_SKIP_VERSION:-}" && "$BIZHAWK_SKIP_VERSION" == "$BIZHAWK_LATEST_VERSION" ]]; then
    return
  fi
  if ! command -v zenity >/dev/null 2>&1; then
    return
  fi

  local choice
  if choice=$(zenity --question \
       --title="BizHawk update" \
       --text="A new BizHawk version is available:\nCurrent: ${BIZHAWK_VERSION:-unknown}\nLatest: $BIZHAWK_LATEST_VERSION\n\nUpdate now?" \
       --ok-label="Update" \
       --cancel-label="Later" \
       --extra-button="Skip this version"); then
    if [[ "$choice" == "Skip this version" ]]; then
      BIZHAWK_SKIP_VERSION="$BIZHAWK_LATEST_VERSION"
      save_config
      return
    else
      if install_bizhawk_win "$BIZHAWK_LATEST_URL" "$BIZHAWK_LATEST_VERSION"; then
        build_bizhawk_runner
        info_dialog "BizHawk updated to $BIZHAWK_LATEST_VERSION."
      fi
    fi
  else
    return
  fi
}

select_bizhawk_exe() {
  local selected
  selected=$(zenity --file-selection \
    --title="Select BizHawk EmuHawk.exe (Windows)" \
    --filename="$HOME/") || exit 0

  if [[ ! -f "$selected" ]]; then
    error_dialog "Selected file does not exist."
    exit 1
  fi

  BIZHAWK_EXE="$selected"
  info_dialog "Using existing BizHawk:\n$BIZHAWK_EXE"
}

auto_detect_proton() {
  local base="$HOME/.steam/steam/steamapps/common"
  if [[ ! -d "$base" ]]; then
    return 1
  fi
  local candidates
  candidates=$(find "$base" -maxdepth 2 -type f -name "proton" 2>/dev/null | sort || true)
  if [[ -z "$candidates" ]]; then
    return 1
  fi
  local chosen
  chosen=$(echo "$candidates" | grep -i "Experimental/proton" | head -n1 || true)
  if [[ -z "$chosen" ]]; then
    chosen=$(echo "$candidates" | tail -n1)
  fi
  if [[ -n "$chosen" && -f "$chosen" ]]; then
    PROTON_BIN="$chosen"
    echo "[ap-bizhelper] Auto-detected Proton at: $PROTON_BIN"
    return 0
  fi
  return 1
}

ensure_proton_bin() {
  if [[ -n "${PROTON_BIN:-}" && -x "$PROTON_BIN" ]]; then
    return
  fi
  if auto_detect_proton; then
    return
  fi
  local selected
  selected=$(zenity --file-selection \
    --title="Select Proton launcher script (the 'proton' file)" \
    --filename="$HOME/.steam/steam/steamapps/common/") || exit 0

  if [[ ! -f "$selected" ]]; then
    error_dialog "Selected Proton script does not exist."
    exit 1
  fi

  PROTON_BIN="$selected"
  info_dialog "Proton launcher set to:\n$PROTON_BIN"
}

build_bizhawk_runner() {
  if [[ -z "${BIZHAWK_EXE:-}" || ! -f "$BIZHAWK_EXE" ]]; then
    error_dialog "EmuHawk.exe is not set or does not exist."
    return 1
  fi

  if [[ -z "${PROTON_BIN:-}" || ! -x "$PROTON_BIN" ]]; then
    error_dialog "Proton launcher is not set or not executable."
    return 1
  fi

  mkdir -p "$(dirname "$BIZHAWK_RUNNER")" "$PROTON_PREFIX"

  cat >"$BIZHAWK_RUNNER" <<EOF_RUN
#!/usr/bin/env bash
set -euo pipefail

# Run BizHawk from its own directory so relative paths like "SNI\\lua\\Connector.lua"
# resolve inside the BizHawk folder instead of the Desktop.
cd "$(dirname "$BIZHAWK_EXE")"

export STEAM_COMPAT_CLIENT_INSTALL_PATH="\$HOME/.steam/steam"
export STEAM_COMPAT_DATA_PATH="$PROTON_PREFIX"

exec "$PROTON_BIN" run "$BIZHAWK_EXE" "\$@"
EOF_RUN

  chmod +x "$BIZHAWK_RUNNER"
  echo "[ap-bizhelper] BizHawk runner created at: $BIZHAWK_RUNNER"
  return 0
}

ensure_bizhawk_proton() {
  load_config

  if [[ -n "${BIZHAWK_RUNNER:-}" && -x "$BIZHAWK_RUNNER" && \
        -n "${BIZHAWK_EXE:-}" && -f "$BIZHAWK_EXE" && \
        -n "${PROTON_BIN:-}" && -x "$PROTON_BIN" ]]; then
    echo "[ap-bizhelper] Using existing BizHawk runner: $BIZHAWK_RUNNER"
    # Also see if an update is available
    check_update_bizhawk
    return
  fi

  if [[ -z "${BIZHAWK_EXE:-}" || ! -f "$BIZHAWK_EXE" ]]; then
    auto_detect_bizhawk_exe || true
  fi

  if [[ -z "${BIZHAWK_EXE:-}" || ! -f "$BIZHAWK_EXE" ]]; then
    local action
    action=$(choose_install_action \
      "BizHawk (Proton) setup" \
      "BizHawk (Windows) is not configured.\n\nDownload latest BizHawk for Windows (win-x64), select an existing EmuHawk.exe, or cancel?") || action="Cancel"

    case "$action" in
      "Download")
        download_bizhawk_win
        ;;
      "Select")
        select_bizhawk_exe
        ;;
      "Cancel"|*)
        echo "[ap-bizhelper] BizHawk setup cancelled; no auto-launch will be attempted."
        BIZHAWK_RUNNER=""
        save_config
        return
        ;;
    esac
  fi

  ensure_proton_bin
  if ! build_bizhawk_runner; then
    echo "[ap-bizhelper] Failed to build BizHawk runner; auto-launch will be disabled."
    BIZHAWK_RUNNER=""
    save_config
    return
  fi

  # After we have a runner and known EXE, we can check for updates
  check_update_bizhawk
  save_config
}

ensure_sfc_lua_path() {
  load_config

  # If user already set a custom Windows-style path (no /), trust it.
  if [[ -n "${SFC_LUA_PATH:-}" && "$SFC_LUA_PATH" != */* ]]; then
    return
  fi

  if [[ -z "${BIZHAWK_EXE:-}" || ! -f "$BIZHAWK_EXE" ]]; then
    echo "[ap-bizhelper] Cannot set Lua path: BizHawk not configured."
    return
  fi

  local bizdir
  bizdir="$(dirname "$BIZHAWK_EXE")"

  if [[ -f "$bizdir/SNI/lua/Connector.lua" ]]; then
    SFC_LUA_PATH="SNI\\lua\\Connector.lua"
    save_config
    return
  fi

  local connector
  connector=$(
    find /tmp "$HOME" \
      -maxdepth 8 \
      -type f \
      -path "*/.mount_Archip*/opt/Archipelago/SNI/lua/Connector.lua" 2>/dev/null \
      | head -n1 || true
  )

  if [[ -z "$connector" || ! -f "$connector" ]]; then
    echo "[ap-bizhelper] Could not auto-locate Connector.lua in Archipelago AppImage."
    return
  fi

  local ap_sni_root
  ap_sni_root="$(dirname "$(dirname "$connector")")"

  mkdir -p "$bizdir/SNI"
  if ! cp -a "$ap_sni_root"/. "$bizdir/SNI/"; then
    echo "[ap-bizhelper] Failed to copy Archipelago SNI files into BizHawk directory."
    return
  fi

  SFC_LUA_PATH="SNI\\lua\\Connector.lua"
  echo "[ap-bizhelper] Installed Archipelago SNI files from AppImage."
  echo "[ap-bizhelper] Using Lua path inside BizHawk: $SFC_LUA_PATH"
  save_config
}

launch_bizhawk_for_rom() {
  local rom="$1"

  if [[ -z "${BIZHAWK_RUNNER:-}" || ! -x "$BIZHAWK_RUNNER" ]]; then
    echo "[ap-bizhelper] launch_bizhawk_for_rom called but runner is not configured."
    return
  fi

  local rom_ext rom_ext_lc
  rom_ext="${rom##*.}"
  rom_ext_lc="${rom_ext,,}"

  local args=()

  if [[ "$rom_ext_lc" == "sfc" ]]; then
    ensure_sfc_lua_path
    if [[ -n "${SFC_LUA_PATH:-}" ]]; then
      echo "[ap-bizhelper] Adding Lua script for .sfc: $SFC_LUA_PATH"
      args+=( "--lua=$SFC_LUA_PATH" )
    else
      echo "[ap-bizhelper] No Lua script configured for .sfc; launching without Lua."
    fi
  fi

  "$BIZHAWK_RUNNER" "$rom" "${args[@]}" &
}

########################################
# Patch selection + APWorld helper
########################################

select_patch_file() {
  local patch
  patch=$(zenity --file-selection \
    --title="Select Archipelago patch file" \
    --filename="$HOME/") || exit 0

  if [[ ! -f "$patch" ]]; then
    error_dialog "Selected patch file does not exist."
    exit 1
  fi

  echo "$patch"
}

ensure_apworld_for_extension() {
  local ext_lc="$1"

  # Only care about "new" extensions (no behavior stored yet)
  local behavior
  behavior=$(get_ext_behavior "$ext_lc")
  if [[ -n "$behavior" ]]; then
    return
  fi

  if ! command -v zenity >/dev/null 2>&1; then
    echo "[ap-bizhelper] zenity not available; skipping APWorld prompt for .$ext_lc."
    return
  fi

  local worlds_dir="$HOME/.local/share/Archipelago/worlds"

  if zenity --question \
      --title="APWorld for .$ext_lc" \
      --text="This looks like a new Archipelago patch extension (.$ext_lc).\n\nIf this game requires an external .apworld file and it isn't already installed, you can select it now to copy into:\n${worlds_dir}\n\nDo you want to select a .apworld file for this extension now?" \
      --ok-label="Select .apworld" \
      --cancel-label="Skip"; then
    local apworld
    apworld=$(zenity --file-selection \
      --title="Select .apworld file for .$ext_lc" \
      --file-filter="*.apworld" \
      --filename="$HOME/") || return

    if [[ -f "$apworld" ]]; then
      mkdir -p "$worlds_dir"
      cp -f "$apworld" "$worlds_dir"/
      info_dialog "Copied $(basename "$apworld") to:\n$worlds_dir"
    else
      error_dialog "Selected .apworld file does not exist."
    fi
  else
    echo "[ap-bizhelper] User skipped APWorld selection for .$ext_lc."
  fi
}

run_archipelago_with_patch() {
  local patch="$1"

  if [[ -z "${AP_APPIMAGE:-}" || ! -x "$AP_APPIMAGE" ]]; then
    error_dialog "Archipelago AppImage is not set or not executable."
    exit 1
  fi

  echo "[ap-bizhelper] Launching Archipelago with patch: $patch"
  "$AP_APPIMAGE" "$patch" &
}

is_new_bizhawk_running() {
  local baseline="$1"
  local current
  current=$(pgrep -f 'EmuHawk.exe' || true)

  [[ -z "$current" ]] && return 1

  local pid
  for pid in $current; do
    if ! grep -q -w "$pid" <<<"$baseline"; then
      return 0
    fi
  done

  return 1
}

handle_bizhawk_for_patch() {
  local patch="$1"
  local baseline_pids="$2"

  if [[ -z "${BIZHAWK_RUNNER:-}" || ! -x "$BIZHAWK_RUNNER" ]]; then
    echo "[ap-bizhelper] BizHawk runner not configured or not executable; skipping auto-launch."
    return
  fi

  local dir base rom ext ext_lc behavior
  dir=$(dirname "$patch")
  base=$(basename "$patch")
  base="${base%.*}"
  rom="$dir/$base.sfc"
  ext="${patch##*.}"
  ext_lc="${ext,,}"

  echo "[ap-bizhelper] Patch: $patch"
  echo "[ap-bizhelper] Detected extension: .$ext_lc"
  echo "[ap-bizhelper] Expected ROM: $rom"

  # Wait for ROM to appear (up to ~60s)
  local i
  for i in $(seq 1 60); do
    if [[ -f "$rom" ]]; then
      echo "[ap-bizhelper] ROM detected: $rom"
      break
    fi
    sleep 1
  done

  if [[ ! -f "$rom" ]]; then
    echo "[ap-bizhelper] Timed out waiting for ROM; not launching BizHawk."
    return
  fi

  behavior=$(get_ext_behavior "$ext_lc")
  echo "[ap-bizhelper] Saved behavior for .$ext_lc: ${behavior:-<none>}"

  case "$behavior" in
    auto)
      echo "[ap-bizhelper] Behavior 'auto': not launching BizHawk; assuming AP/user handles it."
      return
      ;;
    fallback)
      echo "[ap-bizhelper] Behavior 'fallback': launching BizHawk via Proton."
      launch_bizhawk_for_rom "$rom"
      return
      ;;
    "")
      echo "[ap-bizhelper] No behavior stored for .$ext_lc yet; entering learning mode."
      ;;
    *)
      echo "[ap-bizhelper] Unknown behavior value '$behavior' for .$ext_lc; doing nothing for safety."
      return
      ;;
  esac

  # Unknown behavior: see if BizHawk appears by itself within a timeout.
  local waited=0
  local timeout=10
  while (( waited < timeout )); do
    if is_new_bizhawk_running "$baseline_pids"; then
      echo "[ap-bizhelper] Detected new BizHawk instance; recording .$ext_lc as 'auto'."
      set_ext_behavior "$ext_lc" "auto"
      return
    fi
    sleep 1
    waited=$((waited + 1))
  done

  # Still no BizHawk. Automatically fall back without prompting:
  echo "[ap-bizhelper] No BizHawk detected for .$ext_lc after ${timeout}s; switching this extension to 'fallback' and launching BizHawk."
  set_ext_behavior "$ext_lc" "fallback"
  launch_bizhawk_for_rom "$rom"
}

########################################
# Main
########################################

ensure_ap_appimage
ensure_bizhawk_proton

BASELINE_BIZHAWK_PIDS=$(pgrep -f 'EmuHawk.exe' || true)

PATCH_FILE=$(select_patch_file)

# Front-load APWorld install prompt for new extensions
PATCH_EXT="${PATCH_FILE##*.}"
PATCH_EXT_LC="${PATCH_EXT,,}"
ensure_apworld_for_extension "$PATCH_EXT_LC"

run_archipelago_with_patch "$PATCH_FILE"

handle_bizhawk_for_patch "$PATCH_FILE" "$BASELINE_BIZHAWK_PIDS"
