"""HID events for NanoKVM firmware 2.2 (JSON) and 2.3+ (binary reports).

The browser sends one logical event. This module turns it into the frame the
device's ``/api/ws`` expects for the firmware that was detected at login.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# USB HID usage IDs keyed by KeyboardEvent.code. Matches NanoKVM 2.2 mappings.
KEYBOARD_CODES: dict[str, int] = {
    "KeyA": 4, "KeyB": 5, "KeyC": 6, "KeyD": 7, "KeyE": 8, "KeyF": 9,
    "KeyG": 10, "KeyH": 11, "KeyI": 12, "KeyJ": 13, "KeyK": 14, "KeyL": 15,
    "KeyM": 16, "KeyN": 17, "KeyO": 18, "KeyP": 19, "KeyQ": 20, "KeyR": 21,
    "KeyS": 22, "KeyT": 23, "KeyU": 24, "KeyV": 25, "KeyW": 26, "KeyX": 27,
    "KeyY": 28, "KeyZ": 29,
    "Digit1": 30, "Digit2": 31, "Digit3": 32, "Digit4": 33, "Digit5": 34,
    "Digit6": 35, "Digit7": 36, "Digit8": 37, "Digit9": 38, "Digit0": 39,
    "Enter": 40, "Escape": 41, "Backspace": 42, "Tab": 43, "Space": 44,
    "Minus": 45, "Equal": 46, "BracketLeft": 47, "BracketRight": 48,
    "Backslash": 49, "IntlBackslash": 49, "Semicolon": 51, "Quote": 52,
    "Backquote": 53, "Comma": 54, "Period": 55, "Slash": 56, "CapsLock": 57,
    "F1": 58, "F2": 59, "F3": 60, "F4": 61, "F5": 62, "F6": 63,
    "F7": 64, "F8": 65, "F9": 66, "F10": 67, "F11": 68, "F12": 69,
    "PrintScreen": 70, "ScrollLock": 71, "Pause": 72, "Insert": 73,
    "Home": 74, "PageUp": 75, "Delete": 76, "End": 77, "PageDown": 78,
    "ArrowRight": 79, "ArrowLeft": 80, "ArrowDown": 81, "ArrowUp": 82,
    "NumLock": 83, "NumpadDivide": 84, "NumpadMultiply": 85, "NumpadSubtract": 86,
    "NumpadAdd": 87, "NumpadEnter": 88, "Numpad1": 89, "Numpad2": 90,
    "Numpad3": 91, "Numpad4": 92, "Numpad5": 93, "Numpad6": 94, "Numpad7": 95,
    "Numpad8": 96, "Numpad9": 97, "Numpad0": 98, "NumpadDecimal": 99,
    "ControlLeft": 224, "ShiftLeft": 225, "AltLeft": 226, "MetaLeft": 227,
    "ControlRight": 228, "ShiftRight": 229, "AltRight": 230, "MetaRight": 231,
}

_MODIFIER_CODES = frozenset(range(224, 232))

# Browser button index -> NanoKVM 2.2 button byte (left=1, right=2, middle=4).
_JSON_BUTTONS = {0: 1, 1: 4, 2: 2}
# Same buttons as bits in a 2.3 absolute report.
_BINARY_BUTTONS = {0: 1, 1: 4, 2: 2}


def parse_application_version(value: str) -> tuple[int, int, int]:
    parts: list[int] = []
    for piece in (value or "").strip().split("."):
        if not piece.isdigit():
            break
        parts.append(int(piece))
    while len(parts) < 3:
        parts.append(0)
    return parts[0], parts[1], parts[2]


def resolve_hid_protocol(application: str, override: str = "auto") -> str:
    """``json`` for firmware before 2.3, ``binary`` from 2.3 onward."""
    choice = (override or "auto").strip().lower()
    if choice in ("json", "binary"):
        return choice
    return "binary" if parse_application_version(application) >= (2, 3, 0) else "json"


def absolute_coord(norm: float) -> int:
    """Map a 0..1 position onto NanoKVM's 1..32767 absolute range."""
    try:
        value = float(norm)
    except (TypeError, ValueError):
        return 1
    if value <= 0:
        return 1
    return min(0x7FFF, int(0x7FFF * min(value, 1.0)) + 1)


@dataclass
class HidState:
    """Rollover state for the binary HID dialect. JSON events are stateless."""

    keys: list[int] = field(default_factory=list)
    buttons: int = 0
    x: int = 1
    y: int = 1


def translate_input(protocol: str, message: dict, state: HidState) -> list[list | bytes]:
    """Turn one viewer event into device WebSocket frames. Empty means ignore."""
    kind = str(message.get("type") or "")
    if kind == "key":
        frame = _key_frame(protocol, message, state)
        return [frame] if frame is not None else []
    if kind == "mouse":
        frame = _mouse_frame(protocol, message, state)
        return [frame] if frame is not None else []
    return []


def _modifier_bits(message: dict) -> int:
    bits = 0
    for name in ("ctrl", "shift", "alt", "meta"):
        try:
            bits |= int(message.get(name) or 0)
        except (TypeError, ValueError):
            continue
    return bits & 0xFF


def _json_key_frame(message: dict, down: bool, hid: int | None) -> list | None:
    if not down:
        return [1, 0, 0, 0, 0, 0]
    if hid is None:
        return None
    return [
        1,
        hid,
        _named_bit(message, "ctrl"),
        _named_bit(message, "shift"),
        _named_bit(message, "alt"),
        _named_bit(message, "meta"),
    ]


def _track_binary_key(state: HidState, down: bool, code: str, hid: int | None) -> bool:
    """Update rollover state. Return False when the key code is unknown."""
    if not down and not code:
        state.keys.clear()
        return True
    if down and code and hid is None:
        return False
    if down and hid is not None and hid not in _MODIFIER_CODES and hid not in state.keys and len(state.keys) < 6:
        state.keys.append(hid)
    elif not down and hid is not None and hid in state.keys:
        state.keys.remove(hid)
    return True


def _key_frame(protocol: str, message: dict, state: HidState) -> list | bytes | None:
    down = bool(message.get("down"))
    code = str(message.get("code") or "")
    hid = KEYBOARD_CODES.get(code)
    if protocol == "json":
        return _json_key_frame(message, down, hid)
    if not _track_binary_key(state, down, code, hid):
        return None
    report = bytearray(8)
    report[0] = _modifier_bits(message)
    for index, key in enumerate(state.keys[:6]):
        report[2 + index] = key
    return bytes([1]) + bytes(report)


def _named_bit(message: dict, name: str) -> int:
    try:
        return int(message.get(name) or 0) & 0xFF
    except (TypeError, ValueError):
        return 0


def _json_mouse_frame(message: dict, event: str) -> list | None:
    if event == "move":
        return [2, 2, 0, absolute_coord(message.get("x")), absolute_coord(message.get("y"))]
    if event == "down":
        button = _JSON_BUTTONS.get(_button_index(message), 0)
        if not button:
            return None
        return [2, 1, button, 0, 0]
    if event == "up":
        return [2, 0, 0, 0, 0]
    if event != "wheel":
        return None
    delta = _wheel_delta(message)
    if delta == 0:
        return None
    return [2, 4, 0, 0, delta]


def _track_binary_mouse(state: HidState, message: dict, event: str) -> int | None:
    """Update absolute-mouse state. ``None`` means the event should be dropped."""
    if event == "move":
        state.x = absolute_coord(message.get("x"))
        state.y = absolute_coord(message.get("y"))
        return 0
    if event == "down":
        state.buttons |= _BINARY_BUTTONS.get(_button_index(message), 0)
        return 0
    if event == "up":
        button = _button_index(message)
        bit = _BINARY_BUTTONS.get(button, 0) if button is not None else 0
        if bit:
            state.buttons &= ~bit
        else:
            state.buttons = 0
        return 0
    if event != "wheel":
        return None
    wheel = _wheel_delta(message)
    return wheel or None


def _binary_mouse_report(state: HidState, wheel: int) -> bytes:
    report = bytearray(6)
    report[0] = state.buttons & 0xFF
    report[1] = state.x & 0xFF
    report[2] = (state.x >> 8) & 0xFF
    report[3] = state.y & 0xFF
    report[4] = (state.y >> 8) & 0xFF
    report[5] = wheel & 0xFF
    return bytes([2]) + bytes(report)


def _mouse_frame(protocol: str, message: dict, state: HidState) -> list | bytes | None:
    event = str(message.get("event") or "")
    if protocol == "json":
        return _json_mouse_frame(message, event)
    wheel = _track_binary_mouse(state, message, event)
    if wheel is None:
        return None
    return _binary_mouse_report(state, wheel)


def _button_index(message: dict) -> int | None:
    raw = message.get("button")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _wheel_delta(message: dict) -> int:
    try:
        delta = float(message.get("delta") or 0)
    except (TypeError, ValueError):
        return 0
    if delta > 0:
        return 1
    if delta < 0:
        return -1
    return 0
