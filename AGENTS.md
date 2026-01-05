SteamOS/Steam Deck–like compatibility > minimal dependencies > simple modification to behavior.
Simplicity of code > readability of code > all else.

All dynamic/configurable values should be initialized to settings from a default value if they are not present, and read from settings when used. this supercedes the constants preference. This includes things like window/font size, paths, or anything that is likely to need some flexibility, but should also persist across runs
settings should be partitioned by category with one file being dedicated to values a typical user may wish to change themselves, and several to various categories of persistent storage. Any value for which changing could break behavior should be separated into one of these partitions.
Prefer using shared constants to literals where reasonable which should be consolidated to constants.py where possible.
Prefer reductive changes that solve a problem where reasonable without affecting the default behavior.
Ask clarifying questions before beginning if instructions are unclear, conflicting, or seem to disagree with the goal.
Ask about any cleanup that might be possible for touched areas of code rather than just piling more code on top of it.

Do not worry about preserving legacy code or compatibility. always assume that we are installing and running each version on a fresh machine.
Offer unsolicited suggestions or alternatives where reasonable.
If the repo could benefit from a change to the agents.md file, please make suggestions.
Prefer modularity where it makes sense.
When adding new helper scripts or subprocess entrypoints, emit a startup log entry that follows the existing timestamped/contextual logging format so empty logs are avoided on early failure.

This repo is designed to facilitate the use of Archipelago with BizHawk on a Steam Deck–like device.
The goal is to remove as many user actions as is reasonable, but keep it functional, flexible, and safe.
The only supported platform is a Steam Deck–like running SteamOS.

Repo structure & low-level flow (minimal highlights)

- Entrypoint & main flow
  - python -m ap_bizhelper -> ap_bizhelper/__init__.py::console_main -> ap_bizhelper/ap_bizhelper.py::main.
  - main() orchestrates: settings load -> Qt init -> (optional) Steam relaunch -> prereq downloads -> patch
    selection -> Archipelago launch -> optional BizHawk auto-launch -> shutdown/SaveRAM sync.
  - Other commands (ensure, utils, uninstall) are handled in ap_bizhelper.py and do not run the full flow.

- Settings persistence & partitioning (non-obvious but central)
  - Settings are split across multiple JSON files under ~/.config/ap_bizhelper/ by
    ap_bizhelper/ap_bizhelper_config.py.
    - settings.json (safe user prefs), path_settings.json (paths), install_state.json (download/install
      state), state_settings.json (internal state like relaunch args), plus ext_behavior.json,
      ext_associations.json, and apworld_cache.json.
  - load_settings() merges these with defaults; save_settings() re-splits on write. Any new dynamic value
    should follow this split.

- Archipelago lifecycle
  - ap_bizhelper/ap_bizhelper_ap.py handles Archipelago AppImage discovery/download and Qt dialog
    defaults.
  - AppImage default path is DATA_DIR/Archipelago.AppImage.

- BizHawk + Proton lifecycle
  - ap_bizhelper/ap_bizhelper_bizhawk.py owns BizHawk download, connectors, and Proton (local copy)
    management.
  - run_bizhawk_proton.py is the helper used to launch BizHawk under Proton.

- Auto-launch logic & file associations
  - Patch arguments can be paths or file:// URIs (_parse_patch_arg).
  - Patch handling can trigger:
    - .apworld prompt/copy (in ap_bizhelper_worlds.py),
    - file association prompting and desktop/mime registration,
    - extension-based BizHawk auto-launch fallback behavior (ext_behavior.json).
  - BizHawk auto-launch only happens after Archipelago appears and a ROM is detected; extension behavior
    controls fallback vs. do-nothing.

- Steam relaunch & shutdown behavior
  - If not running under Steam, ap_bizhelper.py attempts relaunch via steam://rungameid/<appid> using
    cached settings.
  - Non-GUI CLI commands (`ensure`, `uninstall-all`, `uninstall-core`) default to `--nosteam`; pass
    `--steam` to force a relaunch through Steam.
  - Shutdown handlers attempt clean BizHawk termination and SaveRAM sync before ending the Steam
    session.

- SaveRAM sync (confusing from top level)
  - sync_bizhawk_saveram() centralizes per-instance SaveRAM directories into a shared path (defaults
    under ~/Documents/bizhawk-saveram) and replaces per-instance SaveRAM dirs with symlinks.
