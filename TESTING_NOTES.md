# Testing limitations

This repository depends on a desktop environment with Zenity dialogs, Proton, BizHawk, and the Archipelago AppImage. The current container does not include those components or a graphical session, so full end-to-end testing (downloads, dialogs, Proton launches, and AppImage execution) cannot be simulated here. Only static checks such as syntax validation can be run in this environment.

To validate behavior, run the updated scripts on a target device with those dependencies installed and confirm dialogs, downloads, and BizHawk launches operate as expected.
