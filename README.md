# ap-bizhelper

This package bundles the SteamOS Archipelago/BizHawk helper as a Python package with a single-file AppImage release target.

## Development

Run the helper directly from source with:

```bash
python -m ap_bizhelper
```

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
2. Build the wheel and AppImage artifact (Python, PySide6, and the app are bundled inside the AppImage so the target system just downloads and runs one file):

```bash
python tools/build_appimage.py
```

The script writes the wheel and `dist/ap-bizhelper.AppImage`. The AppImage can be double-clicked on a SteamOS system to launch the full flow without command-line arguments.

### Qt/PySide6 runtime relinking (LGPL)

The AppImage bundles the PySide6 Qt runtime as shared libraries under `usr/qt-runtime` to preserve dynamic linking. You can swap in a compatible Qt build without rebuilding the whole application:

* At runtime, set `AP_BIZHELPER_QT_RUNTIME=/absolute/path/to/Qt` to point the launcher at an alternative Qt tree that contains `lib`, `plugins`, and (optionally) `qml` directories.
* To replace the bundled copy, extract the AppImage (`./ap-bizhelper.AppImage --appimage-extract`), replace `squashfs-root/usr/qt-runtime` with the desired Qt runtime, and repack with `appimagetool squashfs-root ap-bizhelper.AppImage`.

### Rebuilding or relinking Qt/PySide6 from source

PySide6 and the embedded Qt runtime are LGPL-licensed. To obtain matching sources and build or relink them:

1. Download the PySide6 sources that correspond to the packaged wheel, for example:

   ```bash
   python -m pip download --no-binary :all: PySide6==<version>
   ```

   Replace `<version>` with the version pinned in `pyproject.toml`.
2. Fetch the corresponding Qt source release from [https://download.qt.io/official_releases/qt/](https://download.qt.io/official_releases/qt/) or from Qt Project mirrors. Build Qt and PySide6 following the Qt for Python build guide.
3. After building the runtime, point `AP_BIZHELPER_QT_RUNTIME` at the new `Qt` output directory or replace `usr/qt-runtime` inside the AppImage (see above) so the application relinks against your rebuilt libraries.

These steps satisfy the LGPL requirement to allow relinking against user-provided versions of Qt/PySide6.

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
