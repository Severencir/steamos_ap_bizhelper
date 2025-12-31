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
from typing import Any, Callable, Dict, Optional

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
AP_GAMEPAD_DEBUG = "AP_BIZHELPER_DEBUG_GAMEPAD"
AP_GAMEPAD_DEBUG_SEND = "AP_BIZHELPER_DEBUG_GAMEPAD_SEND_EVENT"

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
            self._debug = os.environ.get(AP_GAMEPAD_DEBUG, "").strip() == "1"
            self._debug_send_event = os.environ.get(AP_GAMEPAD_DEBUG_SEND, "").strip() == "1"
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
            self._action_buttons: Dict[str, Optional[QtWidgets.QAbstractButton]] = {
                "affirmative": None,
                "negative": None,
                "special": None,
                "default": None,
            }
            # QFileDialog directory history for controller back/forward.
            # We track visited directories via QFileDialog.directoryEntered().
            self._fd_hist: list[str] = []
            self._fd_hist_pos: int = -1
            self._fd_hist_lock: bool = False
            self._fd_hist_pending: Optional[str] = None
            # QFileDialog: manage a dynamic "current directory" entry at the top of the sidebar.
            self._fd_sidebar_urls_tail: Optional[list] = None
            self._fd_sidebar_current_entry: Optional[str] = None
            self._fd_sidebar_frozen: bool = False
            self._fd_sidebar_anchor: Optional[str] = None
            self._fd_sidebar_pending: Optional[str] = None
            # QFileDialog: track which pane we consider "active" for styling.
            # This is used to drive selection highlight colors without relying on focus pseudo-states.
            self._fd_active_pane: str = "file"
            # QFileDialog: prevent passive sidebar selection from triggering re-entrant directory changes.
            self._fd_sidebar_passive_select_lock: bool = False
            if self._is_file_dialog():
                # IMPORTANT:
                # QFileDialog can start at a platform default (commonly Documents) and only
                # apply the requested start directory after the event loop runs. Prime our
                # history + dynamic sidebar entry after that happens.
                try:
                    QtCore.QTimer.singleShot(0, self._prime_file_dialog_initial_state)
                    QtCore.QTimer.singleShot(50, self._prime_file_dialog_initial_state)
                except Exception:
                    pass
                try:
                    self._dialog.directoryEntered.connect(self._on_file_dialog_directory_entered)  # type: ignore[union-attr]
                except Exception:
                    pass
                try:
                    self._set_file_dialog_active_pane("file")
                except Exception:
                    pass
            self._init_sdl()

        # -----------------------------
        # Debug helpers
        # -----------------------------

        def _w(self, w: Optional[QtWidgets.QWidget]) -> str:
            if w is None:
                return "<None>"
            try:
                return (
                    f"{w.__class__.__name__}(name={w.objectName()!r}, vis={w.isVisible()}, "
                    f"en={w.isEnabled()}, focus={w.hasFocus()}, fp={int(w.focusPolicy())})"
                )
            except Exception:
                return f"{w.__class__.__name__}(?)"

        def _chain(self, w: Optional[QtWidgets.QWidget], limit: int = 10) -> str:
            parts: list[str] = []
            cur = w
            for _ in range(limit):
                if cur is None:
                    break
                try:
                    parts.append(f"{cur.__class__.__name__}({cur.objectName()!r})")
                except Exception:
                    parts.append(cur.__class__.__name__)
                try:
                    cur = cur.parentWidget()
                except Exception:
                    break
            return " <- ".join(parts) if parts else "<None>"

        def _dump_itemview(self, v: Optional[QtWidgets.QWidget]) -> str:
            if v is None:
                return "itemview=<None>"
            try:
                if not isinstance(v, QtWidgets.QAbstractItemView):
                    return f"itemview=<{v.__class__.__name__} not QAbstractItemView>"
                m = v.model()
                sm = v.selectionModel()
                cur = sm.currentIndex() if sm else QtCore.QModelIndex()
                rc = m.rowCount(v.rootIndex()) if m else -1
                sel = sm.selectedIndexes() if sm else []
                sel0 = sel[0] if sel else QtCore.QModelIndex()
                return (
                    f"rows={rc} cur=({cur.row()},{cur.column()},{cur.isValid()}) "
                    f"selCount={len(sel)} sel0=({sel0.row()},{sel0.column()},{sel0.isValid()})"
                )
            except Exception as e:
                return f"itemview=<err {e!r}>"

        def _dump_focus(self, tag: str) -> None:
            if not self._debug:
                return
            try:
                app_focus = QtWidgets.QApplication.focusWidget()
            except Exception:
                app_focus = None
            dlg_focus = None
            try:
                dlg_focus = self._dialog.focusWidget() if self._dialog else None
            except Exception:
                dlg_focus = None
            target = None
            try:
                target = self._target_widget()
            except Exception:
                target = None

            APP_LOGGER.log(
                (
                    f"[{tag}] appFocus={self._w(app_focus)} | dlgFocus={self._w(dlg_focus)} | "
                    f"target={self._w(target)}\n"
                    f"    appChain: {self._chain(app_focus)}\n"
                    f"    dlgChain: {self._chain(dlg_focus)}\n"
                    f"    tgtChain: {self._chain(target)}"
                ),
                level="INFO",
                location="gp-debug",
                include_context=True,
            )

        def _normalize_target(self, w: Optional[QtWidgets.QWidget]) -> Optional[QtWidgets.QWidget]:
            """Normalize a focused child (often a viewport) back to its item view.

            QFileDialog list/sidebar/tree widgets are typically QAbstractItemView
            (a QAbstractScrollArea). Focus can land on the viewport child; key
            navigation handlers live on the view, so we route to the view.
            """

            if w is None:
                return None
            try:
                # Direct hit.
                if isinstance(w, QtWidgets.QAbstractItemView):
                    return w

                # If focus is on the viewport of a scroll area/item view, climb to parent.
                p = w.parentWidget()
                if p and isinstance(p, QtWidgets.QAbstractItemView):
                    try:
                        if hasattr(p, "viewport") and w is p.viewport():
                            return p
                    except Exception:
                        return p

                # Generic climb: walk up a few parents looking for an item view.
                cur = w
                for _ in range(6):
                    cur = cur.parentWidget() if cur is not None else None
                    if cur is None:
                        break
                    if isinstance(cur, QtWidgets.QAbstractItemView):
                        return cur
            except Exception:
                return w
            return w

        def register_action_buttons(
            self,
            *,
            affirmative: Optional[QtWidgets.QAbstractButton] = None,
            negative: Optional[QtWidgets.QAbstractButton] = None,
            special: Optional[QtWidgets.QAbstractButton] = None,
            default: Optional[QtWidgets.QAbstractButton] = None,
        ) -> None:
            self._action_buttons.update(
                {
                    "affirmative": affirmative,
                    "negative": negative,
                    "special": special,
                    "default": default or affirmative,
                }
            )

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
            if not self._dialog.isEnabled():
                return

            active_modal = QtWidgets.QApplication.activeModalWidget()
            if active_modal is not None and active_modal is not self._dialog:
                return

            active_window = QtWidgets.QApplication.activeWindow()
            if active_window is not None and active_window is not self._dialog:
                if not self._dialog.isAncestorOf(active_window):
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
                    try:
                        self._handle_axis(event.caxis)
                    except Exception as e:
                        try:
                            APP_LOGGER.log(
                                f"[poll] axis handler error: {e!r}",
                                level="ERROR",
                                location="gamepad",
                                include_context=True,
                            )
                        except Exception:
                            pass
                elif etype == SDL_CONTROLLERBUTTONDOWN:
                    try:
                        self._handle_button(event.cbutton, pressed=True)
                    except Exception as e:
                        try:
                            APP_LOGGER.log(
                                f"[poll] button-down handler error: {e!r}",
                                level="ERROR",
                                location="gamepad",
                                include_context=True,
                            )
                        except Exception:
                            pass
                elif etype == SDL_CONTROLLERBUTTONUP:
                    try:
                        self._handle_button(event.cbutton, pressed=False)
                    except Exception as e:
                        try:
                            APP_LOGGER.log(
                                f"[poll] button-up handler error: {e!r}",
                                level="ERROR",
                                location="gamepad",
                                include_context=True,
                            )
                        except Exception:
                            pass

        def _target_widget(self) -> Optional[QtWidgets.QWidget]:
            if self._dialog and self._dialog.isVisible():
                focused = self._dialog.focusWidget()
                focused = self._normalize_target(focused)
                if self._last_target and (
                    focused is self._dialog or focused is self._focus_before_toggle
                ):
                    return self._normalize_target(self._last_target)
                if focused and focused is not self._dialog:
                    return focused
                if self._last_target:
                    return self._normalize_target(self._last_target)
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

            if self._debug and pressed and qt_key in (
                QtCore.Qt.Key_Left,
                QtCore.Qt.Key_Right,
                QtCore.Qt.Key_Up,
                QtCore.Qt.Key_Down,
            ):
                self._dump_focus(f"pre key={qt_key}")

            # QFileDialog sidebar: ignore Left/Right navigation (it can jump/warp selection).
            # This also prevents D-pad Left/Right from "jumping to top" in the places list.
            if (
                self._is_file_dialog()
                and isinstance(target, QtWidgets.QAbstractItemView)
                and target.objectName() == "sidebar"
                and qt_key in (QtCore.Qt.Key_Left, QtCore.Qt.Key_Right)
            ):
                return

            if pressed and qt_key in (
                QtCore.Qt.Key_Left,
                QtCore.Qt.Key_Right,
                QtCore.Qt.Key_Up,
                QtCore.Qt.Key_Down,
            ):
                go_next = qt_key in (QtCore.Qt.Key_Right, QtCore.Qt.Key_Down)

                if not self._is_file_dialog() and self._move_between_controls(
                    go_next=go_next, origin=target
                ):
                    return

            if qt_key in (
                QtCore.Qt.Key_Left,
                QtCore.Qt.Key_Right,
                QtCore.Qt.Key_Up,
                QtCore.Qt.Key_Down,
            ):
                self._ensure_file_view_selection(target)

                # QFileDialog's sidebar is a QListView named "sidebar". It can end up
                # with a selected row but an invalid currentIndex, causing Up/Down to do nothing.
                if (
                    pressed
                    and qt_key in (QtCore.Qt.Key_Up, QtCore.Qt.Key_Down)
                    and isinstance(target, QtWidgets.QAbstractItemView)
                    and target.objectName() == "sidebar"
                ):
                    try:
                        self._force_itemview_current(target)
                        if self._step_itemview(
                            target, -1 if qt_key == QtCore.Qt.Key_Up else 1
                        ):
                            return
                    except Exception:
                        # Never break input handling on sidebar quirks.
                        pass

            event_type = QtCore.QEvent.KeyPress if pressed else QtCore.QEvent.KeyRelease
            ev = QtGui.QKeyEvent(event_type, qt_key, modifiers)

            if self._debug:
                try:
                    # PySide6 flag enums aren't always int()-castable (can be KeyboardModifier).
                    mods_val = getattr(modifiers, "value", None)
                    mods_repr = hex(int(mods_val)) if mods_val is not None else repr(modifiers)
                except Exception:
                    mods_repr = repr(modifiers)
                try:
                    APP_LOGGER.log(
                        f"[post] key={qt_key} pressed={pressed} mods={mods_repr} -> "
                        f"target={self._w(target)} {self._dump_itemview(target)}",
                        level="INFO",
                        location="gp-debug",
                        include_context=True,
                    )
                except Exception:
                    # Debug logging must never break input handling.
                    pass

            # In debug mode you can force sendEvent for immediate acceptance info.
            if self._debug and self._debug_send_event:
                QtWidgets.QApplication.sendEvent(target, ev)
                try:
                    accepted = ev.isAccepted()
                except Exception:
                    accepted = None
                APP_LOGGER.log(
                    f"[send] accepted={accepted}",
                    level="INFO",
                    location="gp-debug",
                    include_context=True,
                )
            else:
                QtWidgets.QApplication.postEvent(target, ev)

        def _force_itemview_current(self, widget: QtWidgets.QWidget) -> None:
            """Ensure the target item view has a valid currentIndex.

            QFileDialog's sidebar often ends up with a *selected* row but an
            invalid currentIndex; arrow navigation is driven by currentIndex.
            """
            if not isinstance(widget, QtWidgets.QAbstractItemView):
                return
            model = widget.model()
            selection_model = widget.selectionModel()
            if model is None or selection_model is None:
                return

            root_index = widget.rootIndex()
            try:
                cur = selection_model.currentIndex()
            except Exception:
                cur = QtCore.QModelIndex()

            index = cur if getattr(cur, "isValid", lambda: False)() else None
            if index is None:
                try:
                    sel = selection_model.selectedIndexes()
                except Exception:
                    sel = []
                if sel:
                    index = sel[0]
                else:
                    try:
                        if model.rowCount(root_index) <= 0:
                            return
                        index = model.index(0, 0, root_index)
                    except Exception:
                        return

            try:
                if not index.isValid():
                    return
            except Exception:
                return

            # Set both selection + current; different Qt internals sometimes only
            # respond to one or the other.
            flags = (
                QtCore.QItemSelectionModel.Clear
                | QtCore.QItemSelectionModel.Select
                | QtCore.QItemSelectionModel.Current
            )
            try:
                selection_model.setCurrentIndex(index, flags)
            except Exception:
                try:
                    selection_model.select(index, flags)
                except Exception:
                    pass
            try:
                widget.setCurrentIndex(index)
            except Exception:
                pass
            try:
                widget.scrollTo(index)
            except Exception:
                pass

        def _step_itemview(self, widget: QtWidgets.QWidget, delta_rows: int) -> bool:
            """Move selection/current in an item view by +/- rows.

            Used as a fallback for QFileDialog's sidebar, which can ignore arrow
            keys when currentIndex is invalid.
            """
            if not isinstance(widget, QtWidgets.QAbstractItemView):
                return False

            model = widget.model()
            selection_model = widget.selectionModel()
            if model is None or selection_model is None:
                return False

            root_index = widget.rootIndex()
            try:
                row_count = model.rowCount(root_index)
            except Exception:
                return False
            if row_count <= 0:
                return False

            # Base row: prefer currentIndex, fall back to first selected row, else 0.
            try:
                cur = selection_model.currentIndex()
            except Exception:
                cur = QtCore.QModelIndex()

            if getattr(cur, "isValid", lambda: False)():
                base_row = cur.row()
            else:
                try:
                    sel = selection_model.selectedIndexes()
                except Exception:
                    sel = []
                base_row = sel[0].row() if sel else 0

            new_row = max(0, min(row_count - 1, base_row + delta_rows))
            try:
                index = model.index(new_row, 0, root_index)
                if not index.isValid():
                    return False
            except Exception:
                return False

            flags = (
                QtCore.QItemSelectionModel.Clear
                | QtCore.QItemSelectionModel.Select
                | QtCore.QItemSelectionModel.Current
            )
            try:
                selection_model.setCurrentIndex(index, flags)
            except Exception:
                try:
                    selection_model.select(index, flags)
                except Exception:
                    return False
            try:
                widget.setCurrentIndex(index)
            except Exception:
                pass
            try:
                widget.scrollTo(index)
            except Exception:
                pass
            return True


        def _ensure_file_view_selection(self, widget: QtWidgets.QWidget) -> None:
            """Best-effort: make item views usable with D-pad/arrow navigation.

            For QFileDialog's sidebar, it's common to have a *selected* row but no
            valid currentIndex; QAbstractItemView's keyboard navigation is driven
            by currentIndex, so we force it.
            """
            if not isinstance(widget, QtWidgets.QAbstractItemView):
                return

            model = widget.model()
            selection_model = widget.selectionModel()
            if model is None or selection_model is None:
                return

            root_index = widget.rootIndex()

            try:
                has_sel = selection_model.hasSelection()
            except Exception:
                has_sel = False

            if has_sel:
                self._force_itemview_current(widget)
                return

            try:
                if model.rowCount(root_index) <= 0:
                    return
                first_index = model.index(0, 0, root_index)
                if not first_index.isValid():
                    return
            except Exception:
                return

            flags = (
                QtCore.QItemSelectionModel.Clear
                | QtCore.QItemSelectionModel.Select
                | QtCore.QItemSelectionModel.Current
            )
            try:
                # Prefer setCurrentIndex; it updates both current + selection in most views.
                selection_model.setCurrentIndex(first_index, flags)
            except Exception:
                try:
                    selection_model.select(first_index, flags)
                except Exception:
                    pass
            try:
                widget.setCurrentIndex(first_index)
            except Exception:
                pass

            self._force_itemview_current(widget)

        def _toggle_file_dialog_pane(self, *, go_right: bool) -> bool:
            if not self._dialog:
                return False

            if self._debug:
                self._dump_focus(f"before toggle go_right={go_right}")

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

            if self._debug:
                APP_LOGGER.log(
                    f"[toggle] sidebar={self._w(sidebar)} {self._dump_itemview(sidebar)} | "
                    f"tree={self._w(tree_view)} {self._dump_itemview(tree_view)} | "
                    f"list={self._w(list_view)} {self._dump_itemview(list_view)} | "
                    f"chosen={self._w(target)}",
                    level="INFO",
                    location="gp-debug",
                    include_context=True,
                )

            if self._is_file_dialog():
                try:
                    self._set_file_dialog_active_pane("file" if go_right else "sidebar")
                except Exception:
                    pass

            self._last_target = target
            target.setFocus()
            if not target.hasFocus():
                target.setFocus(QtCore.Qt.OtherFocusReason)

            # Always ensure item-view selection/currentIndex, even if focus ends up staying on the dialog.
            self._ensure_file_view_selection(target)
            # QFileDialog can update the sidebar selection/current asynchronously after focus changes.
            # Re-run on the next event-loop tick to catch that.
            QtCore.QTimer.singleShot(0, lambda w=target: self._ensure_file_view_selection(w))
            QtCore.QTimer.singleShot(0, lambda w=target: self._force_itemview_current(w))
            # When switching to the sidebar, freeze the "current directory" sidebar entry so it
            # doesn't immediately jump as the sidebar selection changes.
            if (
                self._is_file_dialog()
                and isinstance(target, QtWidgets.QAbstractItemView)
                and target.objectName() == "sidebar"
            ):
                try:
                    self._freeze_sidebar_current_dir_entry()
                    self._select_sidebar_current_dir_entry(target)
                    QtCore.QTimer.singleShot(
                        0, lambda v=target: self._select_sidebar_current_dir_entry(v)
                    )
                except Exception:
                    pass
            elif self._is_file_dialog() and go_right:
                # Leaving the sidebar: resume tracking the file pane directory and commit history.
                self._unfreeze_sidebar_current_dir_entry()
                self._commit_pending_file_dialog_history()
                # Ensure the dynamic "current directory" row is highlighted now that we're back
                # in the file pane (directoryEntered may have fired while the sidebar was active).
                self._schedule_passive_sidebar_current_dir_highlight()

            if self._debug:
                self._dump_focus("after toggle setFocus")
            if not target.hasFocus():
                # QFileDialog can keep focus on the dialog, so we fall back to routing
                # subsequent navigation events to the cached target even without focus.
                return True
            return True


        def _set_file_dialog_active_pane(self, pane: str) -> None:
            """Update QFileDialog pane state used for selection styling.

            The Qt stylesheet for the file dialog keys off a dynamic property on the
            dialog (``ap_bizhelper_active_pane``). We update that property when the
            controller toggles between the sidebar and file pane, then repolish the
            relevant widgets so the style updates immediately.
            """
            if not self._is_file_dialog():
                return
            normalized = "sidebar" if str(pane).strip().lower().startswith("side") else "file"
            try:
                prop_val = self._dialog.property("ap_bizhelper_active_pane")
            except Exception:
                prop_val = None
            same_value = getattr(self, "_fd_active_pane", None) == normalized and prop_val == normalized

            # Even if the property value hasn't changed, we may need to repaint viewports.
            self._fd_active_pane = normalized
            if not same_value:
                try:
                    self._dialog.setProperty("ap_bizhelper_active_pane", normalized)
                except Exception:
                    return

            def _repolish(w: Optional[QtWidgets.QWidget]) -> None:
                if w is None:
                    return
                try:
                    st = w.style()
                    st.unpolish(w)
                    st.polish(w)
                    w.update()
                except Exception:
                    try:
                        w.update()
                    except Exception:
                        pass

            def _poke_view(v: Optional[QtWidgets.QAbstractItemView]) -> None:
                if v is None:
                    return
                try:
                    _repolish(v)
                except Exception:
                    pass
                # Item views paint into their viewport; update that explicitly so
                # selection color changes take effect immediately.
                try:
                    vp = v.viewport()
                    if vp is not None:
                        vp.update()
                        vp.repaint()
                except Exception:
                    pass
                try:
                    v.update()
                    v.repaint()
                except Exception:
                    pass

            _repolish(self._dialog)
            _poke_view(self._dialog.findChild(QtWidgets.QAbstractItemView, "sidebar"))
            _poke_view(self._dialog.findChild(QtWidgets.QAbstractItemView, "treeView"))
            _poke_view(self._dialog.findChild(QtWidgets.QAbstractItemView, "listView"))

            # QFileDialog can apply internal style/state updates after focus changes.
            # Nudge viewports again on the next tick so the pane property wins.
            try:
                QtCore.QTimer.singleShot(0, lambda: _poke_view(self._dialog.findChild(QtWidgets.QAbstractItemView, "sidebar")))
                QtCore.QTimer.singleShot(0, lambda: _poke_view(self._dialog.findChild(QtWidgets.QAbstractItemView, "treeView")))
                QtCore.QTimer.singleShot(0, lambda: _poke_view(self._dialog.findChild(QtWidgets.QAbstractItemView, "listView")))
            except Exception:
                pass

        def _passive_select_sidebar_current_dir_entry(self) -> None:
            """Highlight the dynamic 'current directory' sidebar row without navigating."""
            if not self._is_file_dialog():
                return
            if getattr(self, "_fd_active_pane", "file") != "file":
                return
            if getattr(self, "_fd_sidebar_frozen", False):
                return
            if getattr(self, "_fd_sidebar_passive_select_lock", False):
                return

            sidebar = self._dialog.findChild(QtWidgets.QAbstractItemView, "sidebar")
            if sidebar is None:
                return
            try:
                if not sidebar.isVisible() or not sidebar.isEnabled():
                    return
            except Exception:
                pass

            model = sidebar.model()
            sm = sidebar.selectionModel()
            if model is None or sm is None:
                return

            root = sidebar.rootIndex()
            try:
                idx = model.index(0, 0, root)
            except Exception:
                return
            if not getattr(idx, "isValid", lambda: False)():
                return

            try:
                cur = sm.currentIndex()
                if cur.isValid() and cur.row() == 0:
                    return
            except Exception:
                pass

            flags = (
                QtCore.QItemSelectionModel.Clear
                | QtCore.QItemSelectionModel.Select
                | QtCore.QItemSelectionModel.Current
            )

            self._fd_sidebar_passive_select_lock = True
            try:
                blocker_sidebar = QtCore.QSignalBlocker(sidebar)
                blocker_sm = QtCore.QSignalBlocker(sm)
                sm.setCurrentIndex(idx, flags)
                sidebar.setCurrentIndex(idx)
                try:
                    sidebar.scrollTo(idx)
                except Exception:
                    pass
                _ = (blocker_sidebar, blocker_sm)  # keep blockers alive
            except Exception:
                pass
            finally:
                self._fd_sidebar_passive_select_lock = False

        def _schedule_passive_sidebar_current_dir_highlight(self) -> None:
            """Retry passive sidebar highlight briefly to account for async model population."""
            if not self._is_file_dialog():
                return
            try:
                QtCore.QTimer.singleShot(0, self._passive_select_sidebar_current_dir_entry)
                QtCore.QTimer.singleShot(60, self._passive_select_sidebar_current_dir_entry)
            except Exception:
                pass

        # -----------------------------
        # QFileDialog helpers (sidebar + directory navigation)
        # -----------------------------

        def _file_dialog_file_pane(self) -> Optional[QtWidgets.QAbstractItemView]:
            if not self._is_file_dialog():
                return None
            tree_view = self._dialog.findChild(QtWidgets.QTreeView, "treeView")
            list_view = self._dialog.findChild(QtWidgets.QListView, "listView")
            for candidate in (tree_view, list_view):
                if candidate is not None and candidate.isVisible():
                    return candidate
            return None

        def _file_dialog_sidebar_active(self) -> bool:
            if not self._is_file_dialog():
                return False
            try:
                sidebar = self._dialog.findChild(QtWidgets.QAbstractItemView, "sidebar")
            except Exception:
                sidebar = None
            if sidebar is None:
                return False
            focus = None
            try:
                focus = self._dialog.focusWidget()
            except Exception:
                focus = None
            if focus is None:
                try:
                    focus = QtWidgets.QApplication.focusWidget()
                except Exception:
                    focus = None
            try:
                if focus is not None and (focus is sidebar or sidebar.isAncestorOf(focus)):
                    return True
            except Exception:
                pass
            if self._last_target is not None:
                try:
                    if self._last_target is sidebar:
                        return True
                    if isinstance(self._last_target, QtWidgets.QWidget) and (
                        self._last_target.objectName() == "sidebar"
                    ):
                        return True
                except Exception:
                    pass
            return False

        def _prime_file_dialog_selection(self) -> None:
            pane = self._file_dialog_file_pane()
            if pane is None:
                return
            try:
                self._ensure_file_view_selection(pane)
                QtCore.QTimer.singleShot(0, lambda v=pane: self._ensure_file_view_selection(v))
                QtCore.QTimer.singleShot(0, lambda v=pane: self._force_itemview_current(v))
                self._schedule_passive_sidebar_current_dir_highlight()
            except Exception:
                return

        def sync_file_dialog_sidebar_entry(self) -> None:
            """Sync the dynamic sidebar entry to the dialog's current directory."""
            if not self._is_file_dialog():
                return
            try:
                # QFileDialog.directory() returns a QDir
                qdir = self._dialog.directory()  # type: ignore[union-attr]
                cur = qdir.absolutePath()
            except Exception:
                try:
                    cur = self._dialog.directory().path()  # type: ignore[union-attr]
                except Exception:
                    cur = ""
            if not cur:
                return
            try:
                norm = os.path.normpath(cur)
            except Exception:
                norm = cur
            if self._fd_sidebar_frozen:
                self._fd_sidebar_pending = norm
                return
            self._fd_sidebar_pending = None
            self._set_sidebar_current_dir_entry(norm)
            self._schedule_passive_sidebar_current_dir_highlight()

        def _record_file_dialog_history(self, path: str) -> None:
            if not path:
                return
            norm = os.path.normpath(path)
            if self._fd_hist_pos == -1:
                self._fd_hist = [norm]
                self._fd_hist_pos = 0
                return
            if not self._fd_hist or not (0 <= self._fd_hist_pos < len(self._fd_hist)):
                self._fd_hist = [norm]
                self._fd_hist_pos = 0
                return
            if norm == self._fd_hist[self._fd_hist_pos]:
                return
            if self._fd_hist_pos < len(self._fd_hist) - 1:
                self._fd_hist = self._fd_hist[: self._fd_hist_pos + 1]
            self._fd_hist.append(norm)
            self._fd_hist_pos = len(self._fd_hist) - 1

        def _commit_pending_file_dialog_history(self) -> None:
            if self._fd_hist_lock:
                return
            if self._fd_hist_pending is None:
                return
            pending = self._fd_hist_pending
            self._fd_hist_pending = None
            self._record_file_dialog_history(pending)

        def _on_file_dialog_directory_entered(self, path: str) -> None:
            if not self._is_file_dialog():
                return
            if not path:
                return
            try:
                norm = os.path.normpath(path)
            except Exception:
                norm = path
            self._prime_file_dialog_selection()
            if self._file_dialog_sidebar_active():
                self._fd_hist_pending = norm
                self._fd_sidebar_pending = norm
                return

            # History tracking: when we explicitly navigate back/forward we temporarily
            # lock recording to avoid duplicating entries and fighting the user's chosen
            # history position. However, we still want the *sidebar* to reflect the live
            # directory for controller navigation.
            if not self._fd_hist_lock:
                self._fd_hist_pending = None
                self._record_file_dialog_history(norm)
            else:
                self._fd_hist_pending = None

            # Track the live file-pane directory in the top sidebar entry unless frozen.
            if self._fd_sidebar_frozen:
                self._fd_sidebar_pending = norm
                return
            self._fd_sidebar_pending = None
            self._set_sidebar_current_dir_entry(norm)
            self._schedule_passive_sidebar_current_dir_highlight()

        def _file_dialog_current_dir(self) -> Optional[str]:
            if not self._is_file_dialog():
                return None

            # Prefer the dynamic *top sidebar entry* as our source of truth.
            # This avoids races where QFileDialog updates its internal directory asynchronously.
            try:
                if getattr(self, "_fd_sidebar_frozen", False) and getattr(self, "_fd_sidebar_anchor", None):
                    pending = getattr(self, "_fd_sidebar_pending", None)
                    if pending:
                        return pending
                    return getattr(self, "_fd_sidebar_anchor", None)
            except Exception:
                pass

            try:
                cur = getattr(self, "_fd_sidebar_current_entry", None)
                if cur:
                    return cur
            except Exception:
                pass

            # Fallback to our history tracking if the sidebar entry hasn't been established.
            try:
                pending = getattr(self, "_fd_hist_pending", None)
                if pending:
                    return pending
            except Exception:
                pass
            try:
                hist = getattr(self, "_fd_hist", None) or []
                pos = int(getattr(self, "_fd_hist_pos", -1))
                if 0 <= pos < len(hist):
                    return hist[pos]
            except Exception:
                pass

            # Fallback: ask the dialog.
            try:
                # QFileDialog.directory() returns a QDir
                qdir = self._dialog.directory()  # type: ignore[union-attr]
                p = qdir.absolutePath()
                return str(p) if p else None
            except Exception:
                try:
                    p = self._dialog.directory().path()  # type: ignore[union-attr]
                    return str(p) if p else None
                except Exception:
                    return None

        def _set_sidebar_current_dir_entry(self, path: str) -> None:
            """Ensure the *first* sidebar entry points at the given local directory path.

            This is used in QFileDialog to provide a stable "return to where I was browsing"
            entry when navigating the sidebar with a controller.
            """
            if not self._is_file_dialog() or not path:
                return
            try:
                norm = os.path.normpath(path)
            except Exception:
                norm = path

            # Skip redundant updates; repeatedly calling setSidebarUrls() can be disruptive.
            if getattr(self, "_fd_sidebar_current_entry", None) == norm:
                return

            try:
                url = QtCore.QUrl.fromLocalFile(norm)
                current_urls = list(self._dialog.sidebarUrls())  # type: ignore[union-attr]
            except Exception:
                return

            # Capture the "tail" once (the original sidebarUrls) so we can keep the default
            # places while replacing only the dynamic first entry.
            if self._fd_sidebar_urls_tail is None:
                self._fd_sidebar_urls_tail = current_urls

            def _local_key(u: Any) -> Optional[str]:
                try:
                    p = u.toLocalFile()
                except Exception:
                    return None
                if not p:
                    return None
                try:
                    return os.path.normpath(p)
                except Exception:
                    return p

            want = norm
            tail = self._fd_sidebar_urls_tail or []
            new_tail: list = []
            seen: set = set([want])
            for u in tail:
                k = _local_key(u)
                if k is not None and k in seen:
                    continue
                if k is not None:
                    seen.add(k)
                new_tail.append(u)

            try:
                self._dialog.setSidebarUrls([url] + new_tail)  # type: ignore[union-attr]
                self._fd_sidebar_current_entry = norm
            except Exception:
                return

        def _freeze_sidebar_current_dir_entry(self) -> None:
            """Freeze the dynamic sidebar entry while the sidebar is focused."""
            if not self._is_file_dialog():
                return
            self._fd_sidebar_frozen = True
            cur = self._file_dialog_current_dir()
            if not cur:
                return
            try:
                anchor = os.path.normpath(cur)
            except Exception:
                anchor = cur
            self._fd_sidebar_anchor = anchor
            self._set_sidebar_current_dir_entry(anchor)

        def _unfreeze_sidebar_current_dir_entry(self) -> None:
            """Resume dynamic sidebar entry tracking when leaving the sidebar."""
            if not self._is_file_dialog():
                return
            self._fd_sidebar_frozen = False
            self._fd_sidebar_anchor = None
            # If the directory changed while frozen, apply the pending value first.
            target = self._fd_sidebar_pending or self._file_dialog_current_dir()
            self._fd_sidebar_pending = None
            if target:
                self._set_sidebar_current_dir_entry(target)

        def _select_sidebar_current_dir_entry(self, sidebar: QtWidgets.QAbstractItemView) -> None:
            """Select the dynamic 'current directory' entry (row 0) in the QFileDialog sidebar."""
            if not self._is_file_dialog() or sidebar is None:
                return
            model = sidebar.model()
            sm = sidebar.selectionModel()
            if model is None or sm is None:
                return
            root = sidebar.rootIndex()
            try:
                idx = model.index(0, 0, root)
                if not idx.isValid():
                    return
                flags = (
                    QtCore.QItemSelectionModel.Clear
                    | QtCore.QItemSelectionModel.Select
                    | QtCore.QItemSelectionModel.Current
                )
                sm.setCurrentIndex(idx, flags)
                sidebar.setCurrentIndex(idx)
                sidebar.scrollTo(idx)
            except Exception:
                return

            try:
                # QFileDialog.directory() returns a QDir
                qdir = self._dialog.directory()  # type: ignore[union-attr]
                return str(qdir.absolutePath())
            except Exception:
                try:
                    return str(self._dialog.directory().path())  # type: ignore[union-attr]
                except Exception:
                    return None

        def _prime_file_dialog_initial_state(self) -> None:
            """Prime history + dynamic sidebar entry after QFileDialog applies its start directory.

            This avoids capturing Qt's transient initial default (often Documents) before the real
            start directory (Downloads / per-dialog memory) is applied.
            """
            if not self._is_file_dialog():
                return
            cur = self._file_dialog_current_dir()
            if not cur:
                return
            try:
                norm = os.path.normpath(cur)
            except Exception:
                norm = cur

            if not getattr(self, "_fd_hist", None):
                self._fd_hist = [norm]
                self._fd_hist_pos = 0

            if getattr(self, "_fd_sidebar_frozen", False):
                self._fd_sidebar_pending = norm
                return
            self._set_sidebar_current_dir_entry(norm)
            self._schedule_passive_sidebar_current_dir_highlight()


        def _select_sidebar_for_current_dir(self, sidebar: QtWidgets.QAbstractItemView) -> None:
            """Best-effort: select the sidebar place that matches the current directory.

            We compare the QFileDialog's current directory with QFileDialog.sidebarUrls()
            and pick the longest-prefix match (e.g., ~/Downloads over ~).
            """
            if not self._is_file_dialog():
                return
            cur = self._file_dialog_current_dir()
            if not cur:
                return
            try:
                urls = list(self._dialog.sidebarUrls())  # type: ignore[union-attr]
            except Exception:
                return

            cur_norm = os.path.normpath(cur)
            best_i = None
            best_len = -1

            for i, url in enumerate(urls):
                try:
                    p = url.toLocalFile()
                except Exception:
                    continue
                if not p:
                    continue
                p_norm = os.path.normpath(p)
                # Match exact or prefix with separator boundary.
                if cur_norm == p_norm or cur_norm.startswith(p_norm + os.sep):
                    if len(p_norm) > best_len:
                        best_len = len(p_norm)
                        best_i = i

            if best_i is None:
                return

            model = sidebar.model()
            sm = sidebar.selectionModel()
            if model is None or sm is None:
                return

            root = sidebar.rootIndex()
            try:
                if model.rowCount(root) <= best_i:
                    # Fallback: scan rows for matching URL/path data.
                    best_i = None
                else:
                    idx = model.index(best_i, 0, root)
                    if idx.isValid():
                        flags = (
                            QtCore.QItemSelectionModel.Clear
                            | QtCore.QItemSelectionModel.Select
                            | QtCore.QItemSelectionModel.Current
                        )
                        sm.setCurrentIndex(idx, flags)
                        sidebar.setCurrentIndex(idx)
                        sidebar.scrollTo(idx)
                        return
            except Exception:
                best_i = None

            if best_i is None:
                # Slow-path: iterate model rows and compare any URL-like data.
                try:
                    rows = model.rowCount(root)
                except Exception:
                    return
                for r in range(rows):
                    try:
                        idx = model.index(r, 0, root)
                        data = model.data(idx, QtCore.Qt.UserRole)
                        if hasattr(data, "toLocalFile"):
                            p = data.toLocalFile()
                        else:
                            p = str(data) if data is not None else ""
                        if not p:
                            continue
                        p_norm = os.path.normpath(p)
                        if cur_norm == p_norm or cur_norm.startswith(p_norm + os.sep):
                            best_i = r
                            break
                    except Exception:
                        continue
                if best_i is None:
                    return
                try:
                    idx = model.index(best_i, 0, root)
                    if not idx.isValid():
                        return
                    flags = (
                        QtCore.QItemSelectionModel.Clear
                        | QtCore.QItemSelectionModel.Select
                        | QtCore.QItemSelectionModel.Current
                    )
                    sm.setCurrentIndex(idx, flags)
                    sidebar.setCurrentIndex(idx)
                    sidebar.scrollTo(idx)
                except Exception:
                    return

        def _sidebar_activate_and_focus_file_pane(self, sidebar: QtWidgets.QAbstractItemView) -> bool:
            """In QFileDialog, treat selecting a sidebar place like 'open', then move focus right."""
            if not self._is_file_dialog():
                return False
            if sidebar is None:
                return False
            chosen_path = self._sidebar_selected_path(sidebar)
            if chosen_path:
                try:
                    self._dialog.setDirectory(chosen_path)  # type: ignore[union-attr]
                except Exception:
                    pass

            # Switch to file pane on the next tick so the directory change can propagate.
            try:
                QtCore.QTimer.singleShot(0, lambda: self._toggle_file_dialog_pane(go_right=True))
            except Exception:
                try:
                    self._toggle_file_dialog_pane(go_right=True)
                except Exception:
                    pass

            return True

        def _sidebar_selected_path(self, sidebar: QtWidgets.QAbstractItemView) -> Optional[str]:
            if not self._is_file_dialog() or sidebar is None:
                return None
            model = sidebar.model()
            sm = sidebar.selectionModel()
            if model is None or sm is None:
                return None

            root = sidebar.rootIndex()
            try:
                idx = sm.currentIndex()
            except Exception:
                idx = QtCore.QModelIndex()

            if not getattr(idx, "isValid", lambda: False)():
                try:
                    sel = sm.selectedIndexes()
                except Exception:
                    sel = []
                if sel:
                    idx = sel[0]

            if not getattr(idx, "isValid", lambda: False)():
                try:
                    if model.rowCount(root) > 0:
                        idx = model.index(0, 0, root)
                except Exception:
                    pass

            if not getattr(idx, "isValid", lambda: False)():
                return None

            # Fast path: row index corresponds to QFileDialog.sidebarUrls() ordering.
            try:
                urls = list(self._dialog.sidebarUrls())  # type: ignore[union-attr]
                if 0 <= idx.row() < len(urls):
                    p = urls[idx.row()].toLocalFile()
                    if p:
                        return p
            except Exception:
                pass

            # Slow path: try to extract a local file path from the model.
            for role in (QtCore.Qt.UserRole, QtCore.Qt.DisplayRole):
                try:
                    data = model.data(idx, role)
                    if hasattr(data, "toLocalFile"):
                        p = data.toLocalFile()
                    else:
                        p = str(data) if data is not None else ""
                    if p and os.path.isabs(p):
                        return p
                except Exception:
                    continue

            return None

        def sync_file_dialog_sidebar_entry_from_sidebar(
            self, sidebar: Optional[QtWidgets.QAbstractItemView]
        ) -> None:
            if not self._is_file_dialog() or sidebar is None:
                return
            chosen_path = self._sidebar_selected_path(sidebar)
            if not chosen_path:
                return
            try:
                norm = os.path.normpath(chosen_path)
            except Exception:
                norm = chosen_path
            self._fd_sidebar_pending = None
            if self._fd_sidebar_frozen:
                self._fd_sidebar_anchor = norm
            self._set_sidebar_current_dir_entry(norm)
            self._schedule_passive_sidebar_current_dir_highlight()

        def _file_dialog_back(self) -> bool:
            if not self._is_file_dialog():
                return False

            # Seed history if needed.
            if self._fd_hist_pos == -1:
                cur = self._file_dialog_current_dir()
                if cur:
                    self._fd_hist = [os.path.normpath(cur)]
                    self._fd_hist_pos = 0

            if self._fd_hist_pos > 0 and self._fd_hist:
                self._fd_hist_pos -= 1
                target = self._fd_hist[self._fd_hist_pos]
                try:
                    self._fd_hist_lock = True
                    self._dialog.setDirectory(target)  # type: ignore[union-attr]
                    self._fd_hist_pending = None
                    # Keep the dynamic sidebar entry in sync immediately.
                    try:
                        norm = os.path.normpath(target)
                    except Exception:
                        norm = target
                    if getattr(self, "_fd_sidebar_frozen", False):
                        self._fd_sidebar_pending = norm
                    else:
                        self._fd_sidebar_pending = None
                        self._set_sidebar_current_dir_entry(norm)
                    self._prime_file_dialog_selection()
                finally:
                    self._fd_hist_lock = False
                return True

            # Fallback: emulate a common back shortcut.
            try:
                self._post_key(QtCore.Qt.Key_Left, True, modifiers=QtCore.Qt.AltModifier)
                self._post_key(QtCore.Qt.Key_Left, False, modifiers=QtCore.Qt.AltModifier)
                return True
            except Exception:
                return False

        def _file_dialog_forward(self) -> bool:
            if not self._is_file_dialog():
                return False

            # Seed history if needed.
            if self._fd_hist_pos == -1:
                cur = self._file_dialog_current_dir()
                if cur:
                    self._fd_hist = [os.path.normpath(cur)]
                    self._fd_hist_pos = 0

            if self._fd_hist and 0 <= self._fd_hist_pos < len(self._fd_hist) - 1:
                self._fd_hist_pos += 1
                target = self._fd_hist[self._fd_hist_pos]
                try:
                    self._fd_hist_lock = True
                    self._dialog.setDirectory(target)  # type: ignore[union-attr]
                    self._fd_hist_pending = None
                    # Keep the dynamic sidebar entry in sync immediately.
                    try:
                        norm = os.path.normpath(target)
                    except Exception:
                        norm = target
                    if getattr(self, "_fd_sidebar_frozen", False):
                        self._fd_sidebar_pending = norm
                    else:
                        self._fd_sidebar_pending = None
                        self._set_sidebar_current_dir_entry(norm)
                    self._prime_file_dialog_selection()
                finally:
                    self._fd_hist_lock = False
                return True

            # Fallback: emulate a common forward shortcut.
            try:
                self._post_key(QtCore.Qt.Key_Right, True, modifiers=QtCore.Qt.AltModifier)
                self._post_key(QtCore.Qt.Key_Right, False, modifiers=QtCore.Qt.AltModifier)
                return True
            except Exception:
                return False

        def _file_dialog_up(self) -> bool:
            """Navigate to parent directory in a QFileDialog."""
            if not self._is_file_dialog():
                return False
            cur = self._file_dialog_current_dir()
            if not cur:
                return False
            cur_norm = os.path.normpath(cur)
            parent = os.path.dirname(cur_norm)
            if not parent or parent == cur_norm:
                return False
            try:
                self._dialog.setDirectory(parent)  # type: ignore[union-attr]
                # Keep the dynamic sidebar entry in sync immediately so repeated 'up'
                # presses have a stable source of truth.
                try:
                    parent_norm = os.path.normpath(parent)
                except Exception:
                    parent_norm = parent
                if getattr(self, "_fd_sidebar_frozen", False):
                    self._fd_sidebar_pending = parent_norm
                else:
                    self._fd_sidebar_pending = None
                    self._set_sidebar_current_dir_entry(parent_norm)
                self._prime_file_dialog_selection()
                return True
            except Exception:
                return False
        def _handle_axis(self, axis_event: SDL_ControllerAxisEvent) -> None:
            value = axis_event.value

            
            # Triggers: treat LT/RT as digital bumpers (L/R). This avoids relying on trigger-based
            # focus switching, and makes triggers behave like the L/R shoulder buttons everywhere.
            if axis_event.axis in (SDL_CONTROLLER_AXIS_TRIGGERLEFT, SDL_CONTROLLER_AXIS_TRIGGERRIGHT):
                is_left = axis_event.axis == SDL_CONTROLLER_AXIS_TRIGGERLEFT
                state_key = "ltrigger" if is_left else "rtrigger"
                active = value >= self.AXIS_THRESHOLD

                if self._axis_state.get(state_key) != active:
                    self._axis_state[state_key] = active
                    if active:
                        try:
                            self._handle_trigger_as_bumper(is_left=is_left)
                        except Exception:
                            pass
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


        def _handle_trigger_as_bumper(self, *, is_left: bool) -> None:
            """Treat an LT/RT activation edge like the L/R shoulder buttons."""
            if self._is_file_dialog():
                if is_left:
                    self._file_dialog_back()
                else:
                    self._file_dialog_forward()
                return

            # In other dialogs, bumpers cycle focus between actionable controls.
            self._change_focus(next_focus=not is_left)

        def _handle_button(self, button_event: SDL_ControllerButtonEvent, *, pressed: bool) -> None:
            button = int(button_event.button)
            self._button_state[button] = pressed
            action_buttons = self._action_buttons

            focus_map: Dict[int, bool] = {
                SDL_CONTROLLER_BUTTON_LEFTSHOULDER: False,
                SDL_CONTROLLER_BUTTON_RIGHTSHOULDER: True,
            }
            nav_map: Dict[int, int] = {
                SDL_CONTROLLER_BUTTON_DPAD_UP: QtCore.Qt.Key_Up,
                SDL_CONTROLLER_BUTTON_DPAD_DOWN: QtCore.Qt.Key_Down,
                SDL_CONTROLLER_BUTTON_DPAD_LEFT: QtCore.Qt.Key_Left,
                SDL_CONTROLLER_BUTTON_DPAD_RIGHT: QtCore.Qt.Key_Right,
            }

            # In file dialogs, bumpers act like directory history navigation (back/forward).
            if pressed and self._is_file_dialog() and button in (
                SDL_CONTROLLER_BUTTON_LEFTSHOULDER,
                SDL_CONTROLLER_BUTTON_RIGHTSHOULDER,
            ):
                try:
                    if button == SDL_CONTROLLER_BUTTON_LEFTSHOULDER:
                        self._file_dialog_back()
                    else:
                        self._file_dialog_forward()
                finally:
                    # Never repurpose bumpers for focus switching in file dialogs.
                    return

            # In other dialogs, bumpers cycle focus between actionable controls.
            if button in focus_map and pressed:
                self._change_focus(next_focus=focus_map[button])
                return

            if button == SDL_CONTROLLER_BUTTON_START:
                # QFileDialog: Start toggles focus between sidebar and file pane.
                if pressed and self._is_file_dialog():
                    try:
                        # If we're already on the sidebar, go right to the file pane; otherwise go left.
                        go_right = self._file_dialog_sidebar_active()
                        self._toggle_file_dialog_pane(go_right=go_right)
                    except Exception:
                        pass
                return

            if button == SDL_CONTROLLER_BUTTON_Y:
                # QFileDialog: Y = directory up.
                if pressed and self._is_file_dialog():
                    self._file_dialog_up()
                    return
                # Other dialogs: Y = special action button (if provided).
                if pressed and self._activate_button(action_buttons.get("special")):
                    return
                return

            if button in nav_map:
                self._post_key(nav_map[button], pressed)
                return

            if not pressed:
                return
            if button == SDL_CONTROLLER_BUTTON_A:
                target = self._target_widget()
                # QFileDialog: A on the sidebar should behave like selecting the place,
                # then move focus to the file pane for browsing.
                if self._is_file_dialog():
                    try:
                        if (
                            isinstance(target, QtWidgets.QAbstractItemView)
                            and target.objectName() == "sidebar"
                            and self._sidebar_activate_and_focus_file_pane(target)
                        ):
                            return
                    except Exception:
                        pass
                if isinstance(target, QtWidgets.QCheckBox):
                    try:
                        target.animateClick()
                        return
                    except Exception:
                        pass
                self._post_key(QtCore.Qt.Key_Return, True)
                return
            if button == SDL_CONTROLLER_BUTTON_B:
                if not self._activate_button(action_buttons.get("negative")):
                    self._post_key(QtCore.Qt.Key_Escape, True)
                return
            if button == SDL_CONTROLLER_BUTTON_X:
                self._activate_button(action_buttons.get("affirmative"))
                return

        def _activate_button(self, button: Optional[QtWidgets.QAbstractButton]) -> bool:
            if button is None:
                return False

            try:
                button.setFocus()
                button.animateClick()
                return True
            except Exception:
                return False

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

        def _button_targets(self) -> list[QtWidgets.QAbstractButton]:
            buttons: list[QtWidgets.QAbstractButton] = []
            for key in ("affirmative", "special", "negative", "default"):
                button = self._action_buttons.get(key)
                if (
                    button is not None
                    and button not in buttons
                    and (not hasattr(button, "isVisible") or button.isVisible())
                ):
                    buttons.append(button)
            return buttons

        def _focusable(self, widget: Optional[QtWidgets.QWidget]) -> bool:
            if widget is None:
                return False
            try:
                return (
                    widget.isVisible()
                    and widget.isEnabled()
                    and widget.focusPolicy() != QtCore.Qt.NoFocus
                )
            except Exception:
                return False

        def _focus_cycle(self) -> list[QtWidgets.QWidget]:
            actions = self._action_buttons
            buttons: list[QtWidgets.QWidget] = []

            primary = actions.get("default") or actions.get("affirmative")
            for candidate in (
                primary,
                actions.get("special"),
                actions.get("negative"),
                actions.get("affirmative"),
                actions.get("default"),
            ):
                if candidate is None or candidate in buttons or not self._focusable(candidate):
                    continue
                buttons.append(candidate)

            widgets: list[QtWidgets.QWidget] = []
            for widget in (*buttons, *self._checkbox_targets()):
                if widget not in widgets and self._focusable(widget):
                    widgets.append(widget)
            return widgets

        def _checkbox_targets(self) -> list[QtWidgets.QCheckBox]:
            if not self._dialog:
                return []

            checkboxes = [
                cb
                for cb in self._dialog.findChildren(QtWidgets.QCheckBox)
                if cb.isVisible() and cb.isEnabled()
            ]
            for checkbox in checkboxes:
                self._ensure_checkbox_focus_style(checkbox)
            return checkboxes

        def _ensure_checkbox_focus_style(self, checkbox: QtWidgets.QCheckBox) -> None:
            style = checkbox.styleSheet()
            marker = "/* ap-bizhelper-checkbox-focus */"
            if marker in style:
                return

            focus_style = (
                f"{style}\n"
                f"{marker}\n"
                "QCheckBox:focus {"
                "outline: 2px solid #3daee9;"
                "outline-offset: 2px;"
                "border-radius: 4px;"
                "}\n"
            )
            checkbox.setStyleSheet(focus_style)

        def _ensure_checkbox_visibility(self, widget: QtWidgets.QWidget) -> None:
            parent = widget.parent()
            while parent is not None and not isinstance(parent, QtWidgets.QScrollArea):
                parent = parent.parent()
            if isinstance(parent, QtWidgets.QScrollArea):
                try:
                    parent.ensureWidgetVisible(widget)
                except Exception:
                    pass

        def _move_between_controls(
            self, *, go_next: bool, origin: QtWidgets.QWidget
        ) -> bool:
            widgets = self._focus_cycle()
            if len(widgets) < 2:
                return False

            try:
                current_index = widgets.index(origin)  # type: ignore[arg-type]
            except ValueError:
                current_index = 0

            step = 1 if go_next else -1
            target_index = (current_index + step) % len(widgets)

            try:
                target_widget = widgets[target_index]
                target_widget.setFocus(QtCore.Qt.FocusReason.TabFocusReason)
                if not target_widget.hasFocus():
                    target_widget.setFocus()
                self._last_target = target_widget
                self._ensure_checkbox_visibility(target_widget)
                return True
            except Exception:
                return False

        def _is_file_dialog(self) -> bool:
            try:
                from PySide6 import QtWidgets as _QtWidgets  # type: ignore

                return isinstance(self._dialog, _QtWidgets.QFileDialog)
            except Exception:
                return False

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


def install_gamepad_navigation(
    dialog: QtWidgets.QWidget,
    *,
    actions: Optional[Dict[str, Optional[QtWidgets.QAbstractButton]]] = None,
) -> Optional[GamepadEventFilter]:
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

    if actions:
        try:
            layer.register_action_buttons(**actions)
        except Exception:
            pass

    def _set_initial_focus() -> None:
        preferred = (actions or {}).get("default") or (actions or {}).get("affirmative")
        if preferred is None:
            return
        try:
            if preferred.isVisible() and preferred.isEnabled():
                preferred.setFocus(QtCore.Qt.FocusReason.ActiveWindowFocusReason)
                try:
                    layer._last_target = preferred  # type: ignore[attr-defined]
                except Exception:
                    pass
        except Exception:
            return

    try:
        QtCore.QTimer.singleShot(0, _set_initial_focus)
    except Exception:
        pass

    try:
        did_activate = {"done": False}

        class _FocusOnceFilter(QtCore.QObject):
            def eventFilter(self, obj: QtCore.QObject, ev: QtCore.QEvent) -> bool:  # noqa: N802
                if did_activate["done"]:
                    return False
                if ev.type() in (QtCore.QEvent.Show, QtCore.QEvent.WindowActivate):
                    did_activate["done"] = True
                    QtCore.QTimer.singleShot(0, _set_initial_focus)
                return False

        focus_filter = _FocusOnceFilter(dialog)
        dialog.installEventFilter(focus_filter)
        setattr(dialog, "_ap_gamepad_focus_filter", focus_filter)
    except Exception:
        pass

    dialog.installEventFilter(layer)
    try:
        dialog.destroyed.connect(layer.shutdown)
    except Exception:
        pass
    setattr(dialog, "_ap_gamepad_layer", layer)
    APP_LOGGER.log("Gamepad navigation enabled", level="INFO", location="gamepad")
    return layer
