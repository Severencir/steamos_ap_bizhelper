"""SDL/evdev gamepad bridge that translates events into Qt key presses."""

from __future__ import annotations

import os
import threading
import time
from typing import Callable, Optional

from .logging_utils import get_app_logger

Logger = get_app_logger().__class__


class _GamepadThread:
    def __init__(
        self,
        logger: Logger,
        on_key: Callable[[int], None],
        *,
        context: str,
    ) -> None:
        self._logger = logger
        self._on_key = on_key
        self._context = context
        self._running = threading.Event()
        self._running.set()
        self._thread: Optional[threading.Thread] = None

    def start(self) -> bool:
        try:
            import pygame
        except Exception as exc:  # pragma: no cover - depends on optional SDL install
            self._logger.log(
                f"SDL/pygame not available for controller bridge ({exc}).",
                level="WARNING",
                location=self._context,
                include_context=True,
            )
            return False

        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
        os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

        try:
            pygame.init()
            pygame.joystick.init()
        except Exception as exc:  # pragma: no cover - runtime environment dependent
            self._logger.log(
                f"Failed to initialize SDL/pygame joystick support ({exc}).",
                level="ERROR",
                location=self._context,
                include_context=True,
            )
            return False

        if pygame.joystick.get_count() <= 0:
            self._logger.log(
                "No controllers detected by SDL/pygame; skipping bridge.",
                level="INFO",
                location=self._context,
                include_context=True,
            )
            pygame.joystick.quit()
            pygame.quit()
            return False

        joystick = pygame.joystick.Joystick(0)
        joystick.init()
        self._logger.log(
            "SDL/pygame controller bridge active for dialog navigation.",
            level="INFO",
            location=self._context,
            include_context=True,
        )

        def _loop() -> None:
            axis_state = {"x": 0.0, "y": 0.0}
            hat_state = (0, 0)
            threshold = 0.6
            button_map = {
                0: "accept",  # A / Cross
                1: "cancel",  # B / Circle
                2: "accept",  # X / Square
                3: "accept",  # Y / Triangle
                7: "accept",  # Start
                6: "cancel",  # Back/Select
            }

            def emit_direction(x: float, y: float) -> None:
                if x <= -threshold:
                    self._on_key(1)
                elif x >= threshold:
                    self._on_key(2)
                if y <= -threshold:
                    self._on_key(3)
                elif y >= threshold:
                    self._on_key(4)

            def emit_hat(dx: int, dy: int) -> None:
                if dx < 0:
                    self._on_key(1)
                elif dx > 0:
                    self._on_key(2)
                if dy > 0:
                    self._on_key(3)
                elif dy < 0:
                    self._on_key(4)

            while self._running.is_set():
                try:
                    for event in pygame.event.get():
                        if event.type == pygame.JOYBUTTONDOWN:
                            action = button_map.get(event.button)
                            if action == "accept":
                                self._on_key(5)
                            elif action == "cancel":
                                self._on_key(6)
                        elif event.type == pygame.JOYHATMOTION:
                            if event.value != hat_state:
                                hat_state = event.value
                                emit_hat(*hat_state)
                        elif event.type == pygame.JOYAXISMOTION:
                            if event.axis in (0, 1):
                                if event.axis == 0:
                                    axis_state["x"] = float(event.value)
                                else:
                                    axis_state["y"] = float(event.value)
                                emit_direction(axis_state["x"], axis_state["y"])
                except Exception:
                    # Avoid crashing the thread for transient SDL errors.
                    pass
                time.sleep(0.01)

            pygame.joystick.quit()
            pygame.quit()

        self._thread = threading.Thread(target=_loop, name="ap-bizhelper-controller", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._running.clear()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None


def start_gamepad_key_emitter(
    logger: Logger, *, on_key: Callable[[int], None], context: str
) -> Optional[Callable[[], None]]:
    """Start the SDL/evdev bridge that emits key actions for dialogs."""

    worker = _GamepadThread(logger, on_key, context=context)
    if not worker.start():
        return None
    return worker.stop
