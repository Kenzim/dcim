"""NanoKVM HID framing for firmware 2.2 JSON and 2.3 binary."""
from app.services.ipmi_kvm.nanokvm import NanoKvmProfile
from app.services.ipmi_kvm.registry import get_profile
from app.services.nanokvm.hid import HidState, absolute_coord, resolve_hid_protocol, translate_input


def test_protocol_follows_application_version():
    assert resolve_hid_protocol("2.2.9") == "json"
    assert resolve_hid_protocol("2.3.0") == "binary"
    assert resolve_hid_protocol("2.5.2") == "binary"
    assert resolve_hid_protocol("2.5.2", "json") == "json"
    assert resolve_hid_protocol("", "binary") == "binary"


def test_json_keyboard_and_mouse_match_firmware_2_2():
    state = HidState()
    down = translate_input(
        "json",
        {"type": "key", "code": "Delete", "down": True, "ctrl": 1, "shift": 0, "alt": 4, "meta": 0},
        state,
    )
    up = translate_input("json", {"type": "key", "code": "Delete", "down": False}, state)
    move = translate_input("json", {"type": "mouse", "event": "move", "x": 0.5, "y": 0.25}, state)
    click = translate_input("json", {"type": "mouse", "event": "down", "button": 0}, state)
    wheel = translate_input("json", {"type": "mouse", "event": "wheel", "delta": 120}, state)
    assert down == [[1, 76, 1, 0, 4, 0]]
    assert up == [[1, 0, 0, 0, 0, 0]]
    assert click == [[2, 1, 1, 0, 0]]
    assert wheel == [[2, 4, 0, 0, 1]]
    assert move == [[2, 2, 0, absolute_coord(0.5), absolute_coord(0.25)]]
    assert move[0][3] == int(0x7FFF * 0.5) + 1


def test_binary_keyboard_rollover_and_absolute_mouse():
    state = HidState()
    first = translate_input(
        "binary",
        {"type": "key", "code": "KeyA", "down": True, "ctrl": 1, "shift": 0, "alt": 0, "meta": 0},
        state,
    )
    second = translate_input(
        "binary",
        {"type": "key", "code": "KeyB", "down": True, "ctrl": 1, "shift": 0, "alt": 0, "meta": 0},
        state,
    )
    release = translate_input(
        "binary",
        {"type": "key", "code": "KeyA", "down": False, "ctrl": 1, "shift": 0, "alt": 0, "meta": 0},
        state,
    )
    move = translate_input("binary", {"type": "mouse", "event": "move", "x": 0, "y": 1}, state)
    assert first[0][0] == 1
    assert first[0][1] == 1  # left ctrl
    assert first[0][3] == 4  # HID A
    assert second[0][3] == 4
    assert second[0][4] == 5
    assert release[0][3] == 5
    assert release[0][4] == 0
    assert move[0][0] == 2
    assert move[0][2] == 1
    assert move[0][3] == 0
    assert int.from_bytes(move[0][4:6], "little") == 0x7FFF


def test_nanokvm_profile_is_not_an_aten_stream():
    profile = get_profile("nanokvm")
    assert isinstance(profile, NanoKvmProfile)
    assert profile.packet_mode == "nanokvm"
    assert profile.decode_worker_path == ""
    assert profile.stop_frame() == b""
