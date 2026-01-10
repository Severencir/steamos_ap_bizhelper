# TODO

- Add a settings-map dialog helper function for the UI.
- Fix inconsistent file select dialog behavior between app based and shim based.
- Normalize log names.
- Modify migration helper and launcher to support saveram dirs not named SaveRAM. Ensure that save dir tree reflects this.
- Fix BizHawk SaveRAM migration logic.
- Implement rollback support.
- Make BizHawk deps separate checkboxes in the downloads dialog and prompt to select independently for each.
- Make handler registration only occur if AP accepts the patch file successfully.
- Implement snapshots of the saves dir and add it to the utils dialog options.
- Remove dialog references to Linux, Windows, or Proton; everything is assumed to be Linux now.
- Solve main app staying open past AP closure. Consider decoupling app from AP by staging shim + shim deps. If done, consider having staged components use same deps.
- Ensure all staged components are present independently every launch and copy/download them as necessary.
