# ap-bizhelper

This package bundles the SteamOS Archipelago/BizHawk helper as a Python package with a single-file AppImage release target.

## Development

Run the helper directly from source with:

```bash
python -m ap_bizhelper
```

## Building a one-file release

1. Ensure `build` is available (e.g., `python -m pip install build`).
2. Build the wheel and AppImage artifact (Python, PySide6, and the app are bundled inside the AppImage so the target system just downloads and runs one file):

```bash
python tools/build_appimage.py
```

The script writes the wheel and `dist/ap-bizhelper.AppImage`. The AppImage can be double-clicked on a SteamOS system to launch the full flow without command-line arguments.

## File associations

* When ap-bizhelper sees a new patch file extension, it prompts to register itself as the handler.
* Accepting the prompt creates a per-user desktop entry and MIME globs under `~/.local/share/applications/ap-bizhelper.desktop` and `~/.local/share/mime/packages/ap-bizhelper-*.xml`. New extensions are added automatically after you opt in once.
* Decisions are stored in `~/.config/ap_bizhelper_test/ext_associations.json` (mode plus per-extension choices) so the prompt is not repeated for the same extension.
* Disable or revoke associations with the config helper, for example:

```bash
python -m ap_bizhelper.ap_bizhelper_config set-association-mode disabled
python -m ap_bizhelper.ap_bizhelper_config clear-ext-association apbp
```

Setting the mode to `disabled` stops future prompts and removes the handler entries on the next run; changing back to `prompt` or `enabled` will recreate them for stored extensions.

## Steam gamepad navigation in the file picker

When launched through Steam (for overlay/controller support), the PySide6 file picker supports direct gamepad navigation. Default button mappings:

* **A**: select/accept
* **B**: cancel/back out of the dialog
* **L1 / R1**: go back or forward in history
* **Y**: move up a directory
* **X**: open the context menu
* **D-pad Up/Down/Left/Right** or left stick: move the selection
* **D-pad Left**: toggle focus to the sidebar; **D-pad Right**: toggle focus back to the file list

To disable gamepad control, set `"ENABLE_GAMEPAD_FILE_DIALOG": false` in `~/.config/ap_bizhelper_test/settings.json` and restart the helper.
