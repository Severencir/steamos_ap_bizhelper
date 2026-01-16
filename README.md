# ap-bizhelper

This package bundles the SteamOS Archipelago/BizHawk helper as a Python package with a single-file AppImage release target.

## Development

Run the helper directly from source with:

```bash
python -m ap_bizhelper
```

CLI subcommands that never open a GUI (currently `ensure`, `uninstall-all`, and `uninstall-core`) default to running
with `--nosteam`. Use `--steam` to force a Steam relaunch for those commands.

## Supported platform

ap-bizhelper is only supported on Steam Deck–like devices running SteamOS. Other platforms are untested and unsupported.

## BizHawk runtime and connectors

ap-bizhelper now launches the native Linux BizHawk build (no Proton). BizHawk dependencies are staged locally under the
configured `runtime_root` and never installed system-wide. The default flow downloads and extracts Arch Linux packages
for `mono`, `libgdiplus`, and `lua` into that runtime root, but users can disable downloads and point at an existing
runtime folder.

Connector Lua scripts and SNI are no longer downloaded separately. The BizHawk runner discovers the active Archipelago
AppImage mount under `/tmp/.mount_*` and uses the connector/SNI resources shipped inside that mount to ensure version
compatibility.

## Building a one-file release

1. Ensure `build` is available (e.g., `python -m pip install build`).
2. Build the wheel and AppImage artifact (Python, Kivy, and the app are bundled inside the AppImage so the target system just downloads and runs one file):

```bash
python tools/build_appimage.py
```

The script writes the wheel and `dist/ap-bizhelper.AppImage`. The AppImage can be double-clicked on a SteamOS system to launch the full flow without command-line arguments.

### Manual dialog smoke test

Use these steps after a dialog/UI change to verify controller-friendly input:

1. Launch the helper and trigger a prompt (e.g., run `python -m ap_bizhelper` with missing dependencies).
2. Confirm you can navigate with arrow keys or D-pad and activate buttons with Enter/A.
3. Press Escape/B to cancel and verify the caller handles the cancel path.
4. Resize the window and confirm the layout adapts without clipping.

## File associations

* When ap-bizhelper sees a new patch file extension, it prompts to register itself as the handler.
* Accepting the prompt creates a per-user desktop entry and MIME globs under `~/.local/share/applications/ap-bizhelper.desktop` and `~/.local/share/mime/packages/ap-bizhelper-*.xml`. New extensions are added automatically after you opt in once.
* Decisions are stored in `~/.config/ap_bizhelper/ext_associations.json` (mode plus per-extension choices) so the prompt is not repeated for the same extension.
* Disable or revoke associations with the config helper, for example:

```bash
python -m ap_bizhelper.ap_bizhelper_config set-association-mode disabled
python -m ap_bizhelper.ap_bizhelper_config clear-ext-association apbp
```

Setting the mode to `disabled` stops future prompts and removes the handler entries on the next run; changing back to `prompt` or `enabled` will recreate them for stored extensions.
