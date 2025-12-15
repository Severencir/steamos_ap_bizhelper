"""Optional SDL-backed gamepad navigation for Qt dialogs.

This module stays dormant unless running under Steam (detected by common
Steam environment variables) or when explicitly enabled via the
``AP_BIZHELPER_ENABLE_GAMEPAD`` flag. It attempts to load SDL2 and hidapi
from the AppImage-bundled libraries, open the first available controller,
and translate common controller inputs into Qt key events so dialogs can
be navigated without keyboard emulation.
"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path
from typing import Callable, Dict, Optional

from .logging_utils import get_app_logger

try:  # PySide6 is optional for CLI flows
    from PySide6 import QtCore, QtGui, QtWidgets
except Exception:  # pragma: no cover - guarded import
    QtCore = None  # type: ignore
    QtGui = None  # type: ignore
    QtWidgets = None  # type: ignore

QT_AVAILABLE = QtCore is not None and QtGui is not None and QtWidgets is not None


APP_LOGGER = get_app_logger()

AP_GAMEPAD_DISABLE = "AP_BIZHELPER_DISABLE_GAMEPAD"
AP_GAMEPAD_ENABLE = "AP_BIZHELPER_ENABLE_GAMEPAD"

# Steam exposes one of these when launched from a Steam library entry.
_STEAM_ENV_KEYS = ("SteamGameId", "SteamGameID", "SteamAppId", "SteamAppID")


# SDL constants used for lightweight event parsing.
SDL_INIT_JOYSTICK = 0x00000200
SDL_INIT_HAPTIC = 0x00001000
SDL_INIT_GAMECONTROLLER = 0x00002000

SDL_CONTROLLERAXISMOTION = 0x650
SDL_CONTROLLERBUTTONDOWN = 0x651
SDL_CONTROLLERBUTTONUP = 0x652
SDL_CONTROLLERDEVICEADDED = 0x653
SDL_CONTROLLERDEVICEREMOVED = 0x654

SDL_CONTROLLER_AXIS_LEFTX = 0
SDL_CONTROLLER_AXIS_LEFTY = 1
SDL_CONTROLLER_AXIS_TRIGGERLEFT = 4
SDL_CONTROLLER_AXIS_TRIGGERRIGHT = 5

SDL_CONTROLLER_BUTTON_A = 0
SDL_CONTROLLER_BUTTON_B = 1
SDL_CONTROLLER_BUTTON_X = 2
SDL_CONTROLLER_BUTTON_Y = 3
SDL_CONTROLLER_BUTTON_BACK = 4
SDL_CONTROLLER_BUTTON_START = 6
SDL_CONTROLLER_BUTTON_LEFTSHOULDER = 9
SDL_CONTROLLER_BUTTON_RIGHTSHOULDER = 10
SDL_CONTROLLER_BUTTON_DPAD_UP = 11
SDL_CONTROLLER_BUTTON_DPAD_DOWN = 12
SDL_CONTROLLER_BUTTON_DPAD_LEFT = 13
SDL_CONTROLLER_BUTTON_DPAD_RIGHT = 14

# Typing aliases for ctypes
Uint8 = ctypes.c_uint8
Uint16 = ctypes.c_uint16
Uint32 = ctypes.c_uint32
Sint16 = ctypes.c_int16
Sint32 = ctypes.c_int32
SDL_JoystickID = Sint32


class SDL_ControllerAxisEvent(ctypes.Structure):
    _fields_ = [
        ("type", Uint32),
        ("timestamp", Uint32),
        ("which", SDL_JoystickID),
        ("axis", Uint8),
        ("padding1", Uint8),
        ("padding2", Uint8),
        ("padding3", Uint8),
        ("value", Sint16),
        ("padding4", Uint16),
    ]


class SDL_ControllerButtonEvent(ctypes.Structure):
    _fields_ = [
        ("type", Uint32),
        ("timestamp", Uint32),
        ("which", SDL_JoystickID),
        ("button", Uint8),
        ("state", Uint8),
        ("padding1", Uint8),
        ("padding2", Uint8),
    ]


class SDL_Event(ctypes.Union):
    _fields_ = [
        ("type", Uint32),
        ("caxis", SDL_ControllerAxisEvent),
        ("cbutton", SDL_ControllerButtonEvent),
        ("padding", Uint8 * 80),
    ]


class _SDLWrapper:
    """Minimal SDL2 bindings for controller polling."""

    def __init__(self, lib: ctypes.CDLL) -> None:
        self._lib = lib
        # Init / quit
        self.SDL_Init = lib.SDL_Init
        self.SDL_Init.argtypes = [Uint32]
        self.SDL_Init.restype = ctypes.c_int

        self.SDL_QuitSubSystem = lib.SDL_QuitSubSystem
        self.SDL_QuitSubSystem.argtypes = [Uint32]
        self.SDL_QuitSubSystem.restype = None

        # Controller helpers
        self.SDL_NumJoysticks = lib.SDL_NumJoysticks
        self.SDL_NumJoysticks.restype = ctypes.c_int

        self.SDL_GameControllerOpen = lib.SDL_GameControllerOpen
        self.SDL_GameControllerOpen.argtypes = [ctypes.c_int]
        self.SDL_GameControllerOpen.restype = ctypes.c_void_p

        self.SDL_GameControllerClose = lib.SDL_GameControllerClose
        self.SDL_GameControllerClose.argtypes = [ctypes.c_void_p]
        self.SDL_GameControllerClose.restype = None

        self.SDL_GameControllerEventState = lib.SDL_GameControllerEventState
        self.SDL_GameControllerEventState.argtypes = [ctypes.c_int]
        self.SDL_GameControllerEventState.restype = ctypes.c_int

        self.SDL_PollEvent = lib.SDL_PollEvent
        self.SDL_PollEvent.argtypes = [ctypes.POINTER(SDL_Event)]
        self.SDL_PollEvent.restype = ctypes.c_int

        self.SDL_GetError = lib.SDL_GetError
        self.SDL_GetError.restype = ctypes.c_char_p

    def get_error(self) -> str:
        err = self.SDL_GetError()
        return err.decode("utf-8", errors="ignore") if err else ""


if QT_AVAILABLE:

    class GamepadEventFilter(QtCore.QObject):
        """Qt event filter that posts key events based on SDL controller input."""

        AXIS_THRESHOLD = 16000

        def __init__(
            self, dialog: QtWidgets.QWidget, sdl_loader: Callable[[], Optional[_SDLWrapper]]
        ):
            super().__init__(dialog)
            self._dialog = dialog
            self._sdl_loader = sdl_loader
            self._sdl: Optional[_SDLWrapper] = None
            self._controller: Optional[int] = None
            self._axis_state: Dict[str, bool] = {
                "left": False,
                "right": False,
                "up": False,
                "down": False,
                "ltrigger": False,
                "rtrigger": False,
            }
            self._button_state: Dict[int, bool] = {}
            self._timer: Optional[QtCore.QTimer] = None
            self._active = False
            self._last_target: Optional[QtWidgets.QWidget] = None
            self._focus_before_toggle: Optional[QtWidgets.QWidget] = None
            self._init_sdl()

        @property
        def active(self) -> bool:
            return self._active

        def _init_sdl(self) -> None:
            sdl = self._sdl_loader()
            if sdl is None:
                return

            mask = SDL_INIT_GAMECONTROLLER | SDL_INIT_JOYSTICK | SDL_INIT_HAPTIC
            if sdl.SDL_Init(mask) != 0:
                APP_LOGGER.log(
                    f"SDL init failed: {sdl.get_error()}", level="WARNING", location="gamepad"
                )
                return

            if sdl.SDL_NumJoysticks() <= 0:
                APP_LOGGER.log("No controllers detected", level="INFO", location="gamepad")
                sdl.SDL_QuitSubSystem(mask)
                return

            controller = sdl.SDL_GameControllerOpen(0)
            if not controller:
                APP_LOGGER.log(
                    f"Failed to open controller 0: {sdl.get_error()}",
                    level="WARNING",
                    location="gamepad",
                )
                sdl.SDL_QuitSubSystem(mask)
                return

            sdl.SDL_GameControllerEventState(1)
            self._sdl = sdl
            self._controller = controller
            self._active = True

            timer = QtCore.QTimer(self)
            timer.timeout.connect(self._poll_controller)
            timer.start(16)
            self._timer = timer

        def shutdown(self) -> None:
            if self._timer:
                self._timer.stop()
                self._timer = None

            if self._sdl and self._controller:
                try:
                    self._sdl.SDL_GameControllerClose(self._controller)
                except Exception:
                    pass
                try:
                    self._sdl.SDL_QuitSubSystem(
                        SDL_INIT_GAMECONTROLLER | SDL_INIT_JOYSTICK | SDL_INIT_HAPTIC
                    )
                except Exception:
                    pass

            self._controller = None
            self._sdl = None
            self._active = False

        def eventFilter(self, obj: QtCore.QObject, event: QtCore.QEvent) -> bool:  # noqa: N802
            if event.type() in {QtCore.QEvent.Close, QtCore.QEvent.Destroy}:
                self.shutdown()
            return False

        def _poll_controller(self) -> None:
            if not self._sdl or not self._controller:
                return

            event = SDL_Event()
            while self._sdl.SDL_PollEvent(ctypes.byref(event)) != 0:
                etype = event.type
                if etype == SDL_CONTROLLERDEVICEADDED and not self._controller:
                    self._controller = self._sdl.SDL_GameControllerOpen(0)
                elif etype == SDL_CONTROLLERDEVICEREMOVED:
                    self.shutdown()
                    break
                elif etype == SDL_CONTROLLERAXISMOTION:
                    self._handle_axis(event.caxis)
                elif etype == SDL_CONTROLLERBUTTONDOWN:
                    self._handle_button(event.cbutton, pressed=True)
                elif etype == SDL_CONTROLLERBUTTONUP:
                    self._handle_button(event.cbutton, pressed=False)

        def _target_widget(self) -> Optional[QtWidgets.QWidget]:
            if self._dialog and self._dialog.isVisible():
                focused = self._dialog.focusWidget()
                if self._last_target and (
                    focused is self._dialog or focused is self._focus_before_toggle
                ):
                    return self._last_target
                if focused and focused is not self._dialog:
                    return focused
                if self._last_target:
                    return self._last_target
                if focused:
                    return focused
                return self._dialog
            return None

        def _post_key(
            self,
            qt_key: int,
            pressed: bool,
            *,
            modifiers: QtCore.Qt.KeyboardModifiers = QtCore.Qt.NoModifier,
        ) -> None:
            target = self._target_widget()
            if target is None:
                return

            if pressed and qt_key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right):
                toggled = self._toggle_file_dialog_pane(go_right=qt_key == QtCore.Qt.Key_Right)
                if toggled:
                    return

            if qt_key in (
                QtCore.Qt.Key_Left,
                QtCore.Qt.Key_Right,
                QtCore.Qt.Key_Up,
                QtCore.Qt.Key_Down,
            ):
                self._ensure_file_view_selection(target)

            event_type = QtCore.QEvent.KeyPress if pressed else QtCore.QEvent.KeyRelease
            ev = QtGui.QKeyEvent(event_type, qt_key, modifiers)
            QtWidgets.QApplication.postEvent(target, ev)

        def _ensure_file_view_selection(self, widget: QtWidgets.QWidget) -> None:
            if not isinstance(widget, (QtWidgets.QListView, QtWidgets.QTreeView)):
                return

            model = widget.model()
            selection_model = widget.selectionModel()
            if model is None or selection_model is None:
                return

            if selection_model.hasSelection():
                return

            root_index = widget.rootIndex()
            if model.rowCount(root_index) <= 0:
                return

            first_index = model.index(0, 0, root_index)
            if not first_index.isValid():
                return

            selection_model.select(
                first_index,
                QtCore.QItemSelectionModel.Clear
                | QtCore.QItemSelectionModel.Select
                | QtCore.QItemSelectionModel.Current,
            )
            try:
                widget.setCurrentIndex(first_index)
            except Exception:
                pass

        def _toggle_file_dialog_pane(self, *, go_right: bool) -> bool:
            if not self._dialog:
                return False

            self._focus_before_toggle = self._dialog.focusWidget()
            sidebar = self._dialog.findChild(QtWidgets.QAbstractItemView, "sidebar")
            tree_view = self._dialog.findChild(QtWidgets.QTreeView, "treeView")
            list_view = self._dialog.findChild(QtWidgets.QListView, "listView")

            file_panes: list[QtWidgets.QAbstractItemView] = []
            for candidate in (tree_view, list_view):
                if candidate is not None and candidate.isVisible():
                    file_panes.append(candidate)

            target: Optional[QtWidgets.QWidget]
            if go_right:
                target = file_panes[0] if file_panes else None
            else:
                target = sidebar if sidebar and sidebar.isVisible() else None

            if target is None:
                return False

            self._last_target = target
            target.setFocus()
            if not target.hasFocus():
                target.setFocus(QtCore.Qt.OtherFocusReason)
            if not target.hasFocus():
                # QFileDialog can keep focus on the dialog, so we fall back to routing
                # subsequent navigation events to the cached target even without focus.
                return True
            self._ensure_file_view_selection(target)
            return True

        def _handle_axis(self, axis_event: SDL_ControllerAxisEvent) -> None:
            value = axis_event.value

            trigger_map = {
                SDL_CONTROLLER_AXIS_TRIGGERLEFT: ("ltrigger", QtCore.Qt.Key_Left),
                SDL_CONTROLLER_AXIS_TRIGGERRIGHT: ("rtrigger", QtCore.Qt.Key_Right),
            }

            if axis_event.axis in trigger_map:
                state_key, qt_key = trigger_map[axis_event.axis]
                active = value >= self.AXIS_THRESHOLD
                if self._axis_state.get(state_key) != active:
                    self._axis_state[state_key] = active
                    self._post_key(qt_key, active, modifiers=QtCore.Qt.AltModifier)
                return

            direction: Optional[str] = None
            if axis_event.axis == SDL_CONTROLLER_AXIS_LEFTX:
                if value <= -self.AXIS_THRESHOLD:
                    direction = "left"
                elif value >= self.AXIS_THRESHOLD:
                    direction = "right"
            elif axis_event.axis == SDL_CONTROLLER_AXIS_LEFTY:
                if value <= -self.AXIS_THRESHOLD:
                    direction = "up"
                elif value >= self.AXIS_THRESHOLD:
                    direction = "down"

            for key, qt_key in (
                ("left", QtCore.Qt.Key_Left),
                ("right", QtCore.Qt.Key_Right),
                ("up", QtCore.Qt.Key_Up),
                ("down", QtCore.Qt.Key_Down),
            ):
                active = direction == key
                if self._axis_state.get(key) == active:
                    continue
                self._axis_state[key] = active
                self._post_key(qt_key, active)

        def _handle_button(self, button_event: SDL_ControllerButtonEvent, *, pressed: bool) -> None:
            button = int(button_event.button)
            self._button_state[button] = pressed

            focus_map: Dict[int, bool] = {
                SDL_CONTROLLER_BUTTON_LEFTSHOULDER: False,
                SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: True,
                SDL_CONTROLLER_BUTTON_X: True,
            }
            shortcut_map: Dict[int, tuple[int, QtCore.Qt.KeyboardModifiers]] = {
                SDL_CONTROLLER_BUTTON_Y: (QtCore.Qt.Key_Up, QtCore.Qt.AltModifier),
            }
            key_map: Dict[int, int] = {
                SDL_CONTROLLER_BUTTON_DPAD_UP: QtCore.Qt.Key_Up,
                SDL_CONTROLLER_BUTTON_DPAD_DOWN: QtCore.Qt.Key_Down,
                SDL_CONTROLLER_BUTTON_DPAD_LEFT: QtCore.Qt.Key_Left,
                SDL_CONTROLLER_BUTTON_DPAD_RIGHT: QtCore.Qt.Key_Right,
                SDL_CONTROLLER_BUTTON_A: QtCore.Qt.Key_Return,
                SDL_CONTROLLER_BUTTON_B: QtCore.Qt.Key_Escape,
                SDL_CONTROLLER_BUTTON_START: QtCore.Qt.Key_Return,
                SDL_CONTROLLER_BUTTON_BACK: QtCore.Qt.Key_Backspace,
            }

            if button in focus_map and pressed:
                self._change_focus(next_focus=focus_map[button])
                return

            if button in shortcut_map:
                key, modifiers = shortcut_map[button]
                self._post_key(key, pressed, modifiers=modifiers)
                return

            if button in key_map:
                self._post_key(key_map[button], pressed)

        def _change_focus(self, *, next_focus: bool) -> None:
            if not self._dialog:
                return

            target = self._target_widget()
            if target is None:
                return

            if next_focus:
                target.focusNextPrevChild(True)
            else:
                target.focusNextPrevChild(False)

else:  # pragma: no cover - PySide6 not available in environment

    class GamepadEventFilter:  # type: ignore[misc]
        def __init__(self, *args, **kwargs):
            self._active = False

        @property
        def active(self) -> bool:
            return False

        def shutdown(self) -> None:
            return


def _candidate_lib_dirs() -> list[Path]:
    locations = []
    appdir = os.environ.get("APPDIR")
    if appdir:
        locations.append(Path(appdir) / "usr" / "lib")
    try:
        here = Path(__file__).resolve().parent
        locations.append(here)
    except Exception:
        pass
    for prefix in ("/usr/lib", "/usr/local/lib", "/lib", "/lib64", "/usr/lib64"):
        locations.append(Path(prefix))
    return locations


def _load_library(name: str) -> Optional[ctypes.CDLL]:
    for lib_dir in _candidate_lib_dirs():
        candidate = lib_dir / name
        if candidate.exists():
            try:
                return ctypes.CDLL(str(candidate))
            except OSError:
                continue
    try:
        return ctypes.CDLL(name)
    except OSError:
        return None


def _load_sdl() -> Optional[_SDLWrapper]:
    if QtWidgets is None:
        return None

    sdl_lib = _load_library("libSDL2-2.0.so.0")
    hidapi = _load_library("libhidapi-hidraw.so.0") or _load_library("libhidapi-libusb.so.0")
    if sdl_lib is None or hidapi is None:
        APP_LOGGER.log(
            "SDL2 or hidapi not available; skipping gamepad navigation",
            level="INFO",
            location="gamepad",
        )
        return None

    try:
        return _SDLWrapper(sdl_lib)
    except Exception as exc:  # pragma: no cover - init guard
        APP_LOGGER.log(f"Failed to wrap SDL library: {exc}", level="WARNING", location="gamepad")
        return None


def _in_steam_mode() -> bool:
    return any(os.environ.get(key) for key in _STEAM_ENV_KEYS)


def install_gamepad_navigation(dialog: QtWidgets.QWidget) -> Optional[GamepadEventFilter]:
    """Install gamepad navigation when enabled and a controller is available."""

    if QtWidgets is None or QtCore is None:
        return None

    if os.environ.get(AP_GAMEPAD_DISABLE, "").strip():
        return None

    if not (_in_steam_mode() or os.environ.get(AP_GAMEPAD_ENABLE, "").strip()):
        return None

    layer = GamepadEventFilter(dialog, _load_sdl)
    if not layer.active:
        layer.shutdown()
        return None

    dialog.installEventFilter(layer)
    try:
        dialog.destroyed.connect(layer.shutdown)
    except Exception:
        pass
    setattr(dialog, "_ap_gamepad_layer", layer)
    APP_LOGGER.log("Gamepad navigation enabled", level="INFO", location="gamepad")
    return layer
