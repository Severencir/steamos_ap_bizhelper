import io
import sys


def test_import_no_pyside() -> None:
    __import__("ap_bizhelper")
    assert "PySide6" not in sys.modules


def test_console_question_dialog_yes(monkeypatch) -> None:
    monkeypatch.setenv("AP_BIZHELPER_FORCE_CONSOLE_DIALOGS", "1")
    import ap_bizhelper.dialogs as dialogs

    fake_stdin = io.StringIO("y\n")
    fake_stdin.isatty = lambda: True  # type: ignore[attr-defined]
    monkeypatch.setattr(sys, "stdin", fake_stdin)

    choice = dialogs.question_dialog(
        title="Confirm",
        text="Proceed?",
        ok_label="Yes",
        cancel_label="No",
    )
    assert choice == "ok"
