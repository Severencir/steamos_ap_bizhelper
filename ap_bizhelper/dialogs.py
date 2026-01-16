from __future__ import annotations

"""Shared dialog helpers built on Kivy.

This module centralizes dialog rendering so the main app, shims, and helpers can
reuse consistent widgets. Callers can opt into persistent directory tracking by
passing load/save callbacks for file dialogs.
"""

import importlib.util
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .ap_bizhelper_config import (
    get_path_setting,
    load_settings as _load_shared_settings,
    save_settings as _save_shared_settings,
)
from .constants import (
    DOWNLOADS_DIR_KEY,
    DIALOG_SHIM_ZENITY_FILENAME,
    LAST_FILE_DIALOG_DIR_KEY,
    LAST_FILE_DIALOG_DIRS_KEY,
)
from .logging_utils import AppLogger, get_app_logger

DIALOG_DEFAULTS = {
    "KIVY_FONT_SCALE": 1.5,
    "KIVY_MIN_TEXT_SP": 20,
    "KIVY_TITLE_SP": 24,
    "KIVY_DIALOG_WIDTH": 1080,
    "KIVY_DIALOG_HEIGHT": 720,
    "KIVY_DIALOG_MIN_WIDTH": 900,
    "KIVY_DIALOG_MIN_HEIGHT": 600,
    "KIVY_DIALOG_RESIZABLE": True,
    "KIVY_DIALOG_BORDERLESS": True,
    "KIVY_DIALOG_PADDING_DP": 24,
    "KIVY_DIALOG_SPACING_DP": 12,
    "KIVY_BUTTON_HEIGHT_DP": 52,
    "KIVY_LIST_ROW_HEIGHT_DP": 48,
    "KIVY_FILE_DIALOG_HEIGHT": 800,
    "KIVY_BG_RGBA": [0.12, 0.12, 0.12, 1],
    "KIVY_FOCUS_RGBA": [0.2, 0.4, 0.6, 1],
}

_FORCE_CONSOLE_DIALOGS_ENV = "AP_BIZHELPER_FORCE_CONSOLE_DIALOGS"

_KIVY_IMPORT_ERROR: Optional[BaseException] = None
_KIVY_MODULES: Optional["_KivyModules"] = None
_KIVY_CONFIGURED = False
_ACTIVE_DIALOGS = 0


DialogButtonRole = str


@dataclass
class DialogButtonSpec:
    label: str
    role: DialogButtonRole = "neutral"
    is_default: bool = False


@dataclass
class DialogResult:
    label: Optional[str]
    role: Optional[DialogButtonRole]
    checklist: List[str]
    progress_cancelled: bool = False
    radio_selection: Optional[str] = None


@dataclass
class _KivyModules:
    Clock: object
    Window: object
    Config: object
    runTouchApp: object
    stopTouchApp: object
    FocusBehavior: object
    BoxLayout: object
    Button: object
    ToggleButton: object
    Label: object
    ScrollView: object
    GridLayout: object
    TextInput: object
    ProgressBar: object
    FileChooserListView: object
    Clipboard: object
    dp: object
    sp: object
    FocusableButton: object
    FocusableToggleButton: object
    FocusableFileChooser: object


class FocusManager:
    def __init__(self) -> None:
        self._widgets: List[object] = []
        self._index = -1
        self._cancel_handler: Optional[callable] = None

    def set_cancel_handler(self, handler: callable) -> None:
        self._cancel_handler = handler

    def register(self, widget: object, *, default: bool = False) -> None:
        if widget in self._widgets:
            return
        self._widgets.append(widget)
        if default or self._index < 0:
            self.focus_index(len(self._widgets) - 1)

    def focus_index(self, idx: int) -> None:
        if not self._widgets:
            return
        self._index = max(0, min(idx, len(self._widgets) - 1))
        widget = self._widgets[self._index]
        if hasattr(widget, "focus"):
            try:
                widget.focus = True
            except Exception:
                pass

    def focus_next(self) -> None:
        if not self._widgets:
            return
        self.focus_index((self._index + 1) % len(self._widgets))

    def focus_prev(self) -> None:
        if not self._widgets:
            return
        self.focus_index((self._index - 1) % len(self._widgets))

    def activate(self) -> None:
        if not self._widgets or self._index < 0:
            return
        widget = self._widgets[self._index]
        if hasattr(widget, "trigger_action"):
            try:
                widget.trigger_action(duration=0)
                return
            except Exception:
                pass
        if hasattr(widget, "on_press"):
            try:
                widget.on_press()
            except Exception:
                pass
        if hasattr(widget, "state"):
            try:
                widget.state = "down" if widget.state != "down" else "normal"
            except Exception:
                pass

    def on_key_down(self, _window: object, keycode: tuple[int, str], *_args) -> bool:
        key = keycode[1]
        if key in {"tab", "right", "down"}:
            self.focus_next()
            return True
        if key in {"left", "up"}:
            self.focus_prev()
            return True
        if key in {"enter", "numpadenter", "spacebar"}:
            self.activate()
            return True
        if key in {"escape", "backspace", "b"}:
            if self._cancel_handler:
                self._cancel_handler()
                return True
        return False


class _DialogSession:
    def __init__(self, *, title: str, settings: Dict[str, object]) -> None:
        self.title = title
        self.settings = settings
        self.result: Optional[object] = None
        self.focus_manager = FocusManager()
        self._closed = False

    def close(self, result: Optional[object] = None) -> None:
        if self._closed:
            return
        self._closed = True
        self.result = result
        modules = _get_kivy(self.settings)
        modules.Clock.schedule_once(lambda _dt: modules.stopTouchApp())

    def run(self, build: callable) -> Optional[object]:
        modules = _get_kivy(self.settings)
        settings = self.settings
        width = _coerce_int_setting(settings, "KIVY_DIALOG_WIDTH", int(DIALOG_DEFAULTS["KIVY_DIALOG_WIDTH"]), minimum=320)
        height = _coerce_int_setting(settings, "KIVY_DIALOG_HEIGHT", int(DIALOG_DEFAULTS["KIVY_DIALOG_HEIGHT"]), minimum=240)
        min_width = _coerce_int_setting(settings, "KIVY_DIALOG_MIN_WIDTH", int(DIALOG_DEFAULTS["KIVY_DIALOG_MIN_WIDTH"]), minimum=0)
        min_height = _coerce_int_setting(settings, "KIVY_DIALOG_MIN_HEIGHT", int(DIALOG_DEFAULTS["KIVY_DIALOG_MIN_HEIGHT"]), minimum=0)
        modules.Window.set_title(self.title)
        modules.Window.size = (width, height)
        if min_width > 0:
            modules.Window.minimum_width = min_width
        if min_height > 0:
            modules.Window.minimum_height = min_height
        modules.Window.borderless = _coerce_bool_setting(
            settings, "KIVY_DIALOG_BORDERLESS", bool(DIALOG_DEFAULTS["KIVY_DIALOG_BORDERLESS"])
        )
        modules.Window.clearcolor = _coerce_rgba_setting(
            settings, "KIVY_BG_RGBA", DIALOG_DEFAULTS["KIVY_BG_RGBA"]
        )
        self.focus_manager.set_cancel_handler(lambda: self.close(self.result))
        root = build(self, modules)
        modules.Window.bind(on_key_down=self.focus_manager.on_key_down)
        modules.Window.bind(on_request_close=lambda *_args: self.close(self.result) or True)
        with _track_dialog_activity():
            modules.runTouchApp(root)
        modules.Window.unbind(on_key_down=self.focus_manager.on_key_down)
        return self.result


def _mark_dialog_open() -> None:
    global _ACTIVE_DIALOGS
    _ACTIVE_DIALOGS += 1


def _mark_dialog_closed() -> None:
    global _ACTIVE_DIALOGS
    _ACTIVE_DIALOGS = max(0, _ACTIVE_DIALOGS - 1)


@contextmanager
def _track_dialog_activity() -> "Iterable[None]":
    _mark_dialog_open()
    try:
        yield
    finally:
        _mark_dialog_closed()


def dialogs_active() -> bool:
    return _ACTIVE_DIALOGS > 0


def merge_dialog_settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    return {**DIALOG_DEFAULTS, **(settings or {})}


def _load_dialog_settings(settings: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """Return dialog settings merged with defaults and persist new defaults.

    When ``settings`` is ``None``, the on-disk settings are loaded and any
    missing dialog defaults are written back immediately so future callers pick
    up the new baseline values without needing to save a selection first.
    """

    if settings is not None:
        return merge_dialog_settings(settings)

    stored_settings = _load_shared_settings()
    merged_settings = merge_dialog_settings(stored_settings)

    needs_save = False
    for key in DIALOG_DEFAULTS:
        if key not in stored_settings:
            needs_save = True
            break
    if needs_save:
        _save_shared_settings(merged_settings)

    return merged_settings


def _coerce_font_setting(
    settings: Dict[str, object], key: str, default: float, *, minimum: Optional[float] = None
) -> float:
    value = settings.get(key, default)
    try:
        numeric_value = float(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_int_setting(
    settings: Dict[str, object], key: str, default: int, *, minimum: Optional[int] = None
) -> int:
    value = settings.get(key, default)
    try:
        numeric_value = int(value)
    except Exception:
        return default
    if minimum is not None:
        numeric_value = max(numeric_value, minimum)
    return numeric_value


def _coerce_bool_setting(settings: Dict[str, object], key: str, default: bool) -> bool:
    value = settings.get(key, default)
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        lowered = value.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    return default


def _coerce_rgba_setting(
    settings: Dict[str, object], key: str, default: Sequence[float]
) -> List[float]:
    value = settings.get(key, default)
    if isinstance(value, str):
        cleaned = value.strip().strip("[]()")
        if cleaned:
            value = [part.strip() for part in cleaned.split(",")]
    if isinstance(value, (list, tuple)):
        if len(value) != 4:
            return list(default)
        rgba: List[float] = []
        for component in value:
            try:
                numeric = float(component)
            except Exception:
                return list(default)
            rgba.append(max(0.0, min(1.0, numeric)))
        return rgba
    return list(default)


def _display_available() -> bool:
    if os.environ.get(_FORCE_CONSOLE_DIALOGS_ENV):
        return False
    if os.environ.get("KIVY_WINDOW") == "mock":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _kivy_available() -> bool:
    global _KIVY_IMPORT_ERROR
    if not _display_available():
        return False
    if _KIVY_IMPORT_ERROR is not None:
        return False
    try:
        return importlib.util.find_spec("kivy") is not None
    except Exception as exc:
        _KIVY_IMPORT_ERROR = exc
        return False


def gui_available() -> bool:
    return _kivy_available()


def _configure_kivy(settings: Dict[str, object]) -> None:
    global _KIVY_CONFIGURED
    if _KIVY_CONFIGURED:
        return
    from kivy.config import Config

    resizable = _coerce_bool_setting(
        settings, "KIVY_DIALOG_RESIZABLE", bool(DIALOG_DEFAULTS["KIVY_DIALOG_RESIZABLE"])
    )
    try:
        Config.set("graphics", "resizable", "1" if resizable else "0")
        Config.set("kivy", "exit_on_escape", "0")
    except Exception:
        pass
    _KIVY_CONFIGURED = True


def _get_kivy_modules_raw() -> _KivyModules:
    from kivy import metrics
    from kivy.base import runTouchApp, stopTouchApp
    from kivy.clock import Clock
    from kivy.config import Config
    from kivy.core.clipboard import Clipboard
    from kivy.core.window import Window
    from kivy.uix.behaviors import FocusBehavior
    from kivy.uix.boxlayout import BoxLayout
    from kivy.uix.button import Button
    from kivy.uix.filechooser import FileChooserListView
    from kivy.uix.gridlayout import GridLayout
    from kivy.uix.label import Label
    from kivy.uix.progressbar import ProgressBar
    from kivy.uix.scrollview import ScrollView
    from kivy.uix.textinput import TextInput
    from kivy.uix.togglebutton import ToggleButton

    class FocusableButton(FocusBehavior, Button):
        focus_color = [0.2, 0.5, 0.9, 1]

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._base_color = list(self.background_color)

        def on_focus(self, _instance: object, value: bool) -> None:
            if value:
                self.background_color = list(self.focus_color)
            else:
                self.background_color = list(self._base_color)

    class FocusableToggleButton(FocusBehavior, ToggleButton):
        focus_color = [0.2, 0.5, 0.9, 1]

        def __init__(self, **kwargs) -> None:
            super().__init__(**kwargs)
            self._base_color = list(self.background_color)

        def on_focus(self, _instance: object, value: bool) -> None:
            if value:
                self.background_color = list(self.focus_color)
            else:
                self.background_color = list(self._base_color)

    class FocusableFileChooser(FocusBehavior, FileChooserListView):
        pass

    return _KivyModules(
        Clock=Clock,
        Window=Window,
        Config=Config,
        runTouchApp=runTouchApp,
        stopTouchApp=stopTouchApp,
        FocusBehavior=FocusBehavior,
        BoxLayout=BoxLayout,
        Button=Button,
        ToggleButton=ToggleButton,
        Label=Label,
        ScrollView=ScrollView,
        GridLayout=GridLayout,
        TextInput=TextInput,
        ProgressBar=ProgressBar,
        FileChooserListView=FileChooserListView,
        Clipboard=Clipboard,
        dp=metrics.dp,
        sp=metrics.sp,
        FocusableButton=FocusableButton,
        FocusableToggleButton=FocusableToggleButton,
        FocusableFileChooser=FocusableFileChooser,
    )


def _get_kivy(settings: Dict[str, object]) -> _KivyModules:
    global _KIVY_MODULES, _KIVY_IMPORT_ERROR
    if _KIVY_MODULES is not None:
        focus_color = _coerce_rgba_setting(
            settings, "KIVY_FOCUS_RGBA", DIALOG_DEFAULTS["KIVY_FOCUS_RGBA"]
        )
        _KIVY_MODULES.FocusableButton.focus_color = focus_color
        _KIVY_MODULES.FocusableToggleButton.focus_color = focus_color
        return _KIVY_MODULES
    try:
        _configure_kivy(settings)
        _KIVY_MODULES = _get_kivy_modules_raw()
        focus_color = _coerce_rgba_setting(
            settings, "KIVY_FOCUS_RGBA", DIALOG_DEFAULTS["KIVY_FOCUS_RGBA"]
        )
        _KIVY_MODULES.FocusableButton.focus_color = focus_color
        _KIVY_MODULES.FocusableToggleButton.focus_color = focus_color
    except Exception as exc:  # pragma: no cover - import guard
        _KIVY_IMPORT_ERROR = exc
        raise
    return _KIVY_MODULES


def _font_sp(settings: Dict[str, object], key: str, default: int) -> float:
    scale = _coerce_font_setting(
        settings, "KIVY_FONT_SCALE", float(DIALOG_DEFAULTS["KIVY_FONT_SCALE"]), minimum=0.1
    )
    base = _coerce_font_setting(settings, key, float(default), minimum=1)
    return base * scale


def _dialog_padding(settings: Dict[str, object]) -> float:
    return _coerce_int_setting(settings, "KIVY_DIALOG_PADDING_DP", int(DIALOG_DEFAULTS["KIVY_DIALOG_PADDING_DP"]), minimum=0)


def _dialog_spacing(settings: Dict[str, object]) -> float:
    return _coerce_int_setting(settings, "KIVY_DIALOG_SPACING_DP", int(DIALOG_DEFAULTS["KIVY_DIALOG_SPACING_DP"]), minimum=0)


def _button_height(settings: Dict[str, object]) -> float:
    return _coerce_int_setting(settings, "KIVY_BUTTON_HEIGHT_DP", int(DIALOG_DEFAULTS["KIVY_BUTTON_HEIGHT_DP"]), minimum=0)


def _list_row_height(settings: Dict[str, object]) -> float:
    return _coerce_int_setting(settings, "KIVY_LIST_ROW_HEIGHT_DP", int(DIALOG_DEFAULTS["KIVY_LIST_ROW_HEIGHT_DP"]), minimum=0)


def _console_prompt(prompt: str, *, default: Optional[str] = None) -> Optional[str]:
    if not sys.stdin or not getattr(sys.stdin, "isatty", lambda: False)():
        return default
    try:
        return input(prompt)
    except EOFError:
        return default


def _console_confirm(message: str, *, default: str = "n") -> bool:
    suffix = "[y/N]" if default.lower() != "y" else "[Y/n]"
    response = _console_prompt(f"{message} {suffix} ", default=default)
    if response is None:
        return default.lower() == "y"
    value = response.strip().lower()
    if not value:
        return default.lower() == "y"
    return value.startswith("y")


def _console_select_radio(items: Sequence[str]) -> Optional[str]:
    if not items:
        return None
    if not sys.stdin or not getattr(sys.stdin, "isatty", lambda: False)():
        return None
    for idx, item in enumerate(items, start=1):
        print(f"{idx}. {item}")
    response = _console_prompt("Select an item (blank to cancel): ")
    if not response:
        return None
    try:
        selected = int(response)
    except ValueError:
        return None
    if 1 <= selected <= len(items):
        return items[selected - 1]
    return None


def _console_select_checklist(items: Sequence[Tuple[bool, str]]) -> Optional[List[str]]:
    if not items:
        return []
    if not sys.stdin or not getattr(sys.stdin, "isatty", lambda: False)():
        return None
    for idx, (_, label) in enumerate(items, start=1):
        print(f"{idx}. {label}")
    response = _console_prompt("Select items (comma separated, blank to cancel): ")
    if not response:
        return None
    selections = []
    for token in response.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            idx = int(token)
        except ValueError:
            continue
        if 1 <= idx <= len(items):
            selections.append(items[idx - 1][1])
    return selections


def _console_input_path(prompt: str) -> Optional[Path]:
    response = _console_prompt(prompt)
    if not response:
        return None
    return Path(response).expanduser()


def modular_dialog(
    *,
    title: str,
    text: Optional[str] = None,
    buttons: Sequence[DialogButtonSpec],
    checklist: Optional[Iterable[Tuple[bool, str]]] = None,
    radio_items: Optional[Iterable[str]] = None,
    progress_stream: Optional[Iterable[str]] = None,
    icon: Optional[str] = None,
    height: Optional[int] = None,
) -> DialogResult:
    if not buttons:
        raise ValueError("At least one button must be provided to modular_dialog")

    settings_obj = _load_dialog_settings(None)
    if height is not None:
        settings_obj = {**settings_obj, "KIVY_DIALOG_HEIGHT": height}
    if not _kivy_available():
        result = DialogResult(label=None, role=None, checklist=[], progress_cancelled=False, radio_selection=None)
        if checklist is not None:
            selections = _console_select_checklist(list(checklist))
            if selections is None:
                return result
            result.checklist = selections
        if radio_items is not None:
            result.radio_selection = _console_select_radio(list(radio_items))
        if progress_stream is not None:
            for _ in progress_stream:
                continue
            result.role = "positive"
            return result
        default_spec = next((spec for spec in buttons if spec.is_default), buttons[0])
        if _console_confirm(text or title, default="y" if default_spec.role == "positive" else "n"):
            result.label = default_spec.label
            result.role = default_spec.role
        else:
            negative_spec = next((spec for spec in buttons if spec.role == "negative"), default_spec)
            result.label = negative_spec.label
            result.role = negative_spec.role
        return result

    result = DialogResult(label=None, role=None, checklist=[], progress_cancelled=False, radio_selection=None)

    def _build(session: _DialogSession, modules: _KivyModules):
        settings = session.settings
        padding = modules.dp(_dialog_padding(settings))
        spacing = modules.dp(_dialog_spacing(settings))
        root = modules.BoxLayout(orientation="vertical", padding=padding, spacing=spacing)

        if title:
            title_label = modules.Label(
                text=title,
                bold=True,
                font_size=modules.sp(_font_sp(settings, "KIVY_TITLE_SP", int(DIALOG_DEFAULTS["KIVY_TITLE_SP"]))),
                size_hint_y=None,
                height=modules.dp(40),
            )
            root.add_widget(title_label)

        if text:
            text_label = modules.Label(
                text=text,
                font_size=modules.sp(_font_sp(settings, "KIVY_MIN_TEXT_SP", int(DIALOG_DEFAULTS["KIVY_MIN_TEXT_SP"]))),
                text_size=(None, None),
                size_hint_y=None,
            )
            text_label.bind(
                width=lambda instance, value: setattr(instance, "text_size", (value, None))
            )
            text_label.bind(
                texture_size=lambda instance, value: setattr(instance, "height", value[1])
            )
            root.add_widget(text_label)

        radio_buttons: List[object] = []
        if radio_items is not None:
            scroll = modules.ScrollView(size_hint=(1, 1))
            grid = modules.GridLayout(cols=1, size_hint_y=None, spacing=spacing)
            grid.bind(minimum_height=grid.setter("height"))
            group_name = f"radio-{id(grid)}"
            for item_text in radio_items:
                btn = modules.FocusableToggleButton(
                    text=str(item_text),
                    group=group_name,
                    size_hint_y=None,
                    height=modules.dp(_list_row_height(settings)),
                )
                radio_buttons.append(btn)
                session.focus_manager.register(btn, default=len(radio_buttons) == 1)
                grid.add_widget(btn)
            scroll.add_widget(grid)
            root.add_widget(scroll)

        checklist_buttons: List[Tuple[object, str]] = []
        if checklist is not None:
            scroll = modules.ScrollView(size_hint=(1, 1))
            grid = modules.GridLayout(cols=1, size_hint_y=None, spacing=spacing)
            grid.bind(minimum_height=grid.setter("height"))
            for checked, label_text in checklist:
                btn = modules.FocusableToggleButton(
                    text=str(label_text),
                    size_hint_y=None,
                    height=modules.dp(_list_row_height(settings)),
                )
                btn.state = "down" if checked else "normal"
                checklist_buttons.append((btn, str(label_text)))
                session.focus_manager.register(btn, default=len(checklist_buttons) == 1)
                grid.add_widget(btn)
            scroll.add_widget(grid)
            root.add_widget(scroll)

        progress_bar = None
        progress_label = None
        if progress_stream is not None:
            progress_label = modules.Label(
                text="",
                font_size=modules.sp(_font_sp(settings, "KIVY_MIN_TEXT_SP", int(DIALOG_DEFAULTS["KIVY_MIN_TEXT_SP"]))),
                text_size=(None, None),
                size_hint_y=None,
            )
            progress_label.bind(
                width=lambda instance, value: setattr(instance, "text_size", (value, None))
            )
            progress_label.bind(
                texture_size=lambda instance, value: setattr(instance, "height", value[1])
            )
            root.add_widget(progress_label)
            progress_bar = modules.ProgressBar(max=100, value=0)
            root.add_widget(progress_bar)

        button_row = modules.BoxLayout(
            orientation="horizontal",
            spacing=spacing,
            size_hint_y=None,
            height=modules.dp(_button_height(settings)),
        )
        for idx, spec in enumerate(buttons):
            btn = modules.FocusableButton(text=spec.label)
            if spec.is_default or idx == 0:
                session.focus_manager.register(btn, default=True)
            else:
                session.focus_manager.register(btn)

            def _make_handler(button_spec: DialogButtonSpec):
                def _handler(*_args):
                    result.label = button_spec.label
                    result.role = button_spec.role
                    result.checklist = [
                        label_text
                        for btn, label_text in checklist_buttons
                        if getattr(btn, "state", None) == "down"
                    ]
                    session.close(result)
                return _handler

            btn.bind(on_release=_make_handler(spec))
            button_row.add_widget(btn)
        root.add_widget(button_row)

        if progress_stream is not None:
            cancel_spec = next((spec for spec in buttons if spec.role == "negative"), None)
            cancel_spec = cancel_spec or buttons[-1]
            cancel_button = button_row.children[-1] if button_row.children else None

            def _cancel(*_args):
                result.progress_cancelled = True
                result.label = cancel_spec.label
                result.role = cancel_spec.role
                session.close(result)

            if cancel_button is not None:
                cancel_button.bind(on_release=_cancel)

            def _run_stream() -> None:
                for line in progress_stream:
                    if result.progress_cancelled:
                        break
                    line_text = str(line).strip()
                    if line_text.startswith("#") and progress_label is not None:
                        message = line_text.lstrip("# ").strip()
                        modules.Clock.schedule_once(
                            lambda _dt, text=message: setattr(progress_label, "text", text)
                        )
                        continue
                    try:
                        percent = int(float(line_text))
                    except Exception:
                        continue
                    modules.Clock.schedule_once(
                        lambda _dt, v=percent: setattr(progress_bar, "value", max(0, min(100, v)))
                    )
                if not result.progress_cancelled:
                    modules.Clock.schedule_once(lambda _dt: setattr(progress_bar, "value", 100))
                    result.role = "positive"
                    session.close(result)

            threading.Thread(target=_run_stream, daemon=True).start()

        return root

    session = _DialogSession(title=title, settings=settings_obj)
    return session.run(_build) or result


def question_dialog(
    *, title: str, text: str, ok_label: str, cancel_label: str, extra_label: Optional[str] = None
) -> str:
    buttons = [DialogButtonSpec(ok_label, role="positive", is_default=True)]
    if extra_label:
        buttons.append(DialogButtonSpec(extra_label, role="special"))
    buttons.append(DialogButtonSpec(cancel_label, role="negative"))

    result = modular_dialog(
        title=title,
        text=text,
        icon="question",
        buttons=buttons,
    )

    if result.role == "positive":
        return "ok"
    if result.role == "special":
        return "extra"
    return "cancel"


def info_dialog(message: str, *, title: str = "Information", logger: Optional[AppLogger] = None) -> None:
    app_logger = logger or get_app_logger()
    app_logger.log_dialog(title, message, backend="kivy", location="info-dialog")
    if not _kivy_available():
        sys.stdout.write(f"{title}: {message}\n")
        return
    modular_dialog(
        title=title,
        text=message,
        icon="info",
        buttons=[DialogButtonSpec("OK", role="positive", is_default=True)],
    )


def error_dialog(message: str, *, title: str = "Error", logger: Optional[AppLogger] = None) -> None:
    app_logger = logger or get_app_logger()
    app_logger.log_dialog(title, message, level="ERROR", backend="kivy", location="error-dialog")
    if not _kivy_available():
        sys.stderr.write(f"{title}: {message}\n")
        return
    modular_dialog(
        title=title,
        text=message,
        icon="error",
        buttons=[DialogButtonSpec("OK", role="positive", is_default=True)],
    )


def fallback_error_dialog(
    message: str, *, title: str = "Error", logger: Optional[AppLogger] = None
) -> None:
    app_logger = logger or get_app_logger()
    if _kivy_available():
        try:
            error_dialog(message, title=title, logger=app_logger)
            return
        except Exception as exc:
            app_logger.log(
                f"Kivy error dialog failed; falling back to zenity. {exc}",
                level="WARNING",
                include_context=True,
                location="error-dialog",
            )

    zenity = shutil.which(DIALOG_SHIM_ZENITY_FILENAME)
    if zenity and os.environ.get("DISPLAY"):
        try:
            app_logger.log_dialog(title, message, level="ERROR", backend="zenity", location="error-dialog")
            subprocess.run(
                [zenity, "--error", "--title", title, "--text", message],
                check=False,
            )
            return
        except Exception as exc:
            app_logger.log(
                f"Zenity error dialog failed; falling back to stderr. {exc}",
                level="WARNING",
                include_context=True,
                location="error-dialog",
            )

    app_logger.log_dialog(title, message, level="ERROR", backend="stderr", location="error-dialog")
    sys.stderr.write(f"{title}: {message}\n")


def preferred_start_dir(initial: Optional[Path], settings: Dict[str, object], dialog_key: str) -> Path:
    last_dir_setting = str(settings.get(LAST_FILE_DIALOG_DIR_KEY, "") or "")
    per_dialog_dir = str(
        settings.get(LAST_FILE_DIALOG_DIRS_KEY, {}).get(dialog_key, "") or ""
    )
    downloads_dir = get_path_setting(settings, DOWNLOADS_DIR_KEY)

    candidates = [
        initial if initial and initial.expanduser() != Path.home() else None,
        Path(per_dialog_dir) if per_dialog_dir else None,
        Path(last_dir_setting) if last_dir_setting else None,
        downloads_dir if downloads_dir.exists() else None,
        initial if initial else None,
        Path.home(),
    ]
    for candidate in candidates:
        if candidate is None:
            continue
        candidate_path = candidate.expanduser()
        if candidate_path.is_file():
            candidate_path = candidate_path.parent
        if candidate_path.exists():
            return candidate_path
    return Path.home()


def remember_file_dialog_dir(settings: Dict[str, object], selection: Path, dialog_key: str) -> None:
    parent = selection.parent if selection.is_file() else selection
    dialog_dirs = settings.get(LAST_FILE_DIALOG_DIRS_KEY, {})
    dialog_dirs[dialog_key] = str(parent)
    settings[LAST_FILE_DIALOG_DIRS_KEY] = dialog_dirs
    settings[LAST_FILE_DIALOG_DIR_KEY] = str(parent)


def _parse_filter_patterns(filter_text: Optional[str]) -> List[str]:
    if not filter_text:
        return ["*"]
    patterns: List[str] = []
    for chunk in filter_text.split(";;"):
        if "(" in chunk and ")" in chunk:
            inside = chunk.split("(", 1)[1].rsplit(")", 1)[0]
        else:
            inside = chunk
        for part in inside.split():
            part = part.strip()
            if part:
                patterns.append(part)
    return patterns or ["*"]


def file_dialog(
    *,
    title: str,
    start_dir: Path,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    select_directories: bool = False,
) -> Optional[Path]:
    settings_obj = _load_dialog_settings(settings)
    file_settings = dict(settings_obj)
    file_settings["KIVY_DIALOG_HEIGHT"] = settings_obj.get(
        "KIVY_FILE_DIALOG_HEIGHT", DIALOG_DEFAULTS["KIVY_FILE_DIALOG_HEIGHT"]
    )
    if not _kivy_available():
        prompt = f"{title} (enter path or leave blank to cancel): "
        selection = _console_input_path(prompt)
        if selection is None:
            return None
        if select_directories and not selection.is_dir():
            return None
        if not select_directories and not selection.exists():
            return None
        return selection

    filter_patterns = _parse_filter_patterns(file_filter)
    session = _DialogSession(title=title, settings=file_settings)

    def _build(session: _DialogSession, modules: _KivyModules):
        settings_local = session.settings
        padding = modules.dp(_dialog_padding(settings_local))
        spacing = modules.dp(_dialog_spacing(settings_local))
        root = modules.BoxLayout(orientation="vertical", padding=padding, spacing=spacing)

        chooser = modules.FocusableFileChooser(
            path=str(start_dir),
            filters=filter_patterns,
            dirselect=select_directories,
        )
        session.focus_manager.register(chooser, default=True)
        root.add_widget(chooser)

        button_row = modules.BoxLayout(
            orientation="horizontal",
            spacing=spacing,
            size_hint_y=None,
            height=modules.dp(_button_height(settings_local)),
        )
        ok_button = modules.FocusableButton(text="Select")
        cancel_button = modules.FocusableButton(text="Cancel")
        session.focus_manager.register(ok_button)
        session.focus_manager.register(cancel_button)

        def _accept(*_args):
            selection = chooser.selection[0] if chooser.selection else chooser.path
            session.close(Path(selection))

        def _cancel(*_args):
            session.close(None)

        ok_button.bind(on_release=_accept)
        cancel_button.bind(on_release=_cancel)
        button_row.add_widget(ok_button)
        button_row.add_widget(cancel_button)
        root.add_widget(button_row)
        return root

    result = session.run(_build)
    if isinstance(result, Path):
        return result
    return None


def select_file_dialog(
    *,
    title: str,
    dialog_key: str,
    initial: Optional[Path] = None,
    file_filter: Optional[str] = None,
    settings: Optional[Dict[str, object]] = None,
    save_settings: bool = True,
    select_directories: bool = False,
) -> Optional[Path]:
    settings_obj = _load_dialog_settings(settings)
    start_dir = preferred_start_dir(initial, settings_obj, dialog_key)

    selection = file_dialog(
        title=title,
        start_dir=start_dir,
        file_filter=file_filter,
        settings=settings_obj,
        select_directories=select_directories,
    )

    if selection:
        remember_file_dialog_dir(settings_obj, selection, dialog_key)
        if save_settings:
            _save_shared_settings(settings_obj)

    return selection


def checklist_dialog(
    title: str,
    text: Optional[str],
    items: Iterable[Tuple[bool, str]],
    *,
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
    height: Optional[int] = None,
) -> Optional[List[str]]:
    button_specs = [
        DialogButtonSpec(ok_label, role="positive", is_default=True),
        DialogButtonSpec(cancel_label, role="negative"),
    ]

    dialog = modular_dialog(
        title=title or "Select items",
        text=text,
        buttons=button_specs,
        checklist=list(items),
        height=height,
    )

    if dialog.role != "positive":
        return None
    return dialog.checklist


def radio_list_dialog(
    title: str,
    text: Optional[str],
    items: Iterable[str],
    *,
    ok_label: str = "OK",
    cancel_label: str = "Cancel",
    height: Optional[int] = None,
) -> Optional[str]:
    button_specs = [
        DialogButtonSpec(ok_label, role="positive", is_default=True),
        DialogButtonSpec(cancel_label, role="negative"),
    ]

    dialog = modular_dialog(
        title=title or "Select item",
        text=text,
        buttons=button_specs,
        radio_items=list(items),
        height=height,
    )

    if dialog.role != "positive":
        return None
    return dialog.radio_selection


def progress_dialog_from_stream(
    title: str, text: str, stream: Iterable[str], *, cancel_label: str = "Cancel"
) -> int:
    buttons = [DialogButtonSpec(cancel_label, role="negative", is_default=True)]

    result = modular_dialog(
        title=title or "Progress",
        text=text,
        buttons=buttons,
        progress_stream=stream,
    )

    if result.progress_cancelled or result.role == "negative":
        return 1
    return 0


def list_action_dialog(
    *,
    title: str,
    text: Optional[str],
    items: Sequence[str],
    actions: Sequence[DialogButtonSpec],
    cancel_label: str = "Close",
) -> Tuple[Optional[str], Optional[DialogButtonSpec]]:
    buttons = list(actions) + [DialogButtonSpec(cancel_label, role="negative")]
    result = modular_dialog(
        title=title,
        text=text,
        buttons=buttons,
        radio_items=items,
    )
    chosen_action = next((spec for spec in buttons if spec.label == result.label), None)
    return result.radio_selection, chosen_action


def copy_to_clipboard(text: str, settings: Optional[Dict[str, object]] = None) -> bool:
    if not _kivy_available():
        return False
    settings_obj = _load_dialog_settings(settings)
    try:
        modules = _get_kivy(settings_obj)
        modules.Clipboard.copy(text)
        return True
    except Exception:
        return False


def run_custom_dialog(
    *, title: str, build: callable, settings: Optional[Dict[str, object]] = None
) -> Optional[object]:
    settings_obj = _load_dialog_settings(settings)
    if not _kivy_available():
        return None
    session = _DialogSession(title=title, settings=settings_obj)
    return session.run(build)
