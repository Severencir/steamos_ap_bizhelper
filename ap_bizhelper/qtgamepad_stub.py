"""Fallback QtGamepad shim for environments without native bindings.

The stub mirrors the minimal subset of the QtGamepad API used by
``ap_bizhelper`` so gamepad-dependent code can still log discovery steps
inside headless test/build environments.
"""

from __future__ import annotations

from typing import Callable, Iterable, List


class _Signal:
    def __init__(self) -> None:
        self._callbacks: List[Callable[[bool], None]] = []

    def connect(self, callback: Callable[[bool], None]) -> None:  # pragma: no cover - trivial passthrough
        self._callbacks.append(callback)

    def emit(self, pressed: bool) -> None:  # pragma: no cover - manual triggering only used in tests
        for callback in self._callbacks:
            callback(pressed)


class QGamepadManager:
    _INSTANCE: "QGamepadManager" | None = None

    @classmethod
    def instance(cls) -> "QGamepadManager":  # pragma: no cover - deterministic singleton
        if cls._INSTANCE is None:
            cls._INSTANCE = cls()
        return cls._INSTANCE

    def __init__(self) -> None:
        self._connected: List[int] = []

    def connectedGamepads(self) -> Iterable[int]:  # pragma: no cover - deterministic collection
        return tuple(self._connected)


class QGamepad:
    def __init__(self, device_id: int, parent=None) -> None:  # pragma: no cover - behaviour is deterministic
        self._device_id = device_id
        self._signals = {
            "buttonAChanged": _Signal(),
            "buttonBChanged": _Signal(),
            "buttonXChanged": _Signal(),
            "buttonYChanged": _Signal(),
            "buttonStartChanged": _Signal(),
            "buttonSelectChanged": _Signal(),
            "buttonLeftChanged": _Signal(),
            "buttonRightChanged": _Signal(),
            "buttonUpChanged": _Signal(),
            "buttonDownChanged": _Signal(),
        }

    def __getattr__(self, name: str):  # pragma: no cover - passthrough
        if name in self._signals:
            return self._signals[name]
        raise AttributeError(name)

    def deviceId(self) -> int:
        return self._device_id

    def name(self) -> str:
        return "Stub Gamepad"

    def manufacturer(self) -> str:
        return "ap-bizhelper"

    def profile(self) -> str:
        return "stub"
