# ap-bizhelper

This package bundles the SteamOS Archipelago/BizHawk helper as a Python package with a single-file zipapp release target.

## Development

Run the helper directly from source with:

```bash
python -m ap_bizhelper
```

## Building a one-file release

1. Ensure `build` is available (e.g., `python -m pip install build`).
2. Build the wheel and zipapp artifact (dependencies, including PySide6, are bundled alongside the zipapp in `dist/ap-bizhelper.deps`
   so the target system does not need to download anything at runtime):

```bash
python tools/build_zipapp.py
```

The script writes the wheel and `dist/ap-bizhelper.pyz`. The `.pyz` can be double-clicked on a SteamOS system with Python 3 available to launch the full flow without command-line arguments.
