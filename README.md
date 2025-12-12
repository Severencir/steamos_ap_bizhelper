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
