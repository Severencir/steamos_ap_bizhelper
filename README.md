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
* Decisions are stored in `~/.config/ap_bizhelper/ext_associations.json` (mode plus per-extension choices) so the prompt is not repeated for the same extension.
* Disable or revoke associations with the config helper, for example:

```bash
python -m ap_bizhelper.ap_bizhelper_config set-association-mode disabled
python -m ap_bizhelper.ap_bizhelper_config clear-ext-association apbp
```

Setting the mode to `disabled` stops future prompts and removes the handler entries on the next run; changing back to `prompt` or `enabled` will recreate them for stored extensions.

## Licensing and AppImage obligations

The AppImage bundles PySide6 (Qt for Python) libraries, which are offered under the LGPLv3 with the Qt LGPL Exception 1.1 or under the GPLv3. This package uses the LGPLv3 option. A full licensing notice, including the Qt LGPL Exception 1.1 text plus the complete LGPLv3 and GPLv3 licenses, is stored in `NOTICE` at the repository root and copied into the AppImage.

When running or redistributing the AppImage you may replace or relink the bundled Qt/PySide6 libraries, provided you follow the LGPLv3 requirements (e.g., supplying source or a relinking mechanism and preserving notices). You can extract the AppImage (`./ap-bizhelper.AppImage --appimage-extract`), adjust the libraries under `squashfs-root/usr/lib`, and rebuild the AppImage to run against your modified copies.

