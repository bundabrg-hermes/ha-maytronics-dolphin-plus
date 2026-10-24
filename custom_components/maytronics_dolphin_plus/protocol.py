"""Plus app IoT BLE frame builder (from ``ble_iot_protocol.json`` + APK 3.4).

IoT checksum: 16-bit unsigned sum of all preceding bytes, big-endian (``Lq/f.a``).

Outbound IoT GATT notify payloads are ASCII hex envelopes (not raw binary), e.g.
``03:ab03fff806000002ab``. Inbound robot notifications use the same ``:`` envelope
with a variable prefix; long frames are split across notify chunks.
"""

from __future__ import annotations

import json
import logging
import re
from enum import IntEnum
from functools import lru_cache
from pathlib import Path
from typing import Any

from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

SOP_IOT = 0xAB
SRC_IOT = 0x03
IOT_HEADER_LEN = 7  # sop + src + dest(2) + opcode + data_len(2)
IOT_CHECKSUM_LEN = 2

# Host→robot notify envelope used by Plus IoT PSUs (ESPHome + APK capture).
IOT_NOTIFY_PREFIX = "03:"

CMD_START_UP = "start_up_dolphin"
CMD_SHUTDOWN = "shutdown_dolphin"
CMD_SYSTEM_STATUS = "system_status"
CMD_SET_CLEANING_MODE = "set_cleaning_mode"
CMD_SET_CYCLE_TIME = "set_cycle_time"
CMD_MANUAL_DRIVE = "manual_drive"
CMD_QUIT_MANUAL_DRIVE = "quit_manual_drive"
CMD_RESET_FILTER_INDICATOR = "reset_filter_indicator"

_HEX_CHARS = re.compile(rb"^[0-9a-fA-F]+$")


class PowerState(IntEnum):
    """Inferred from ``system_status.sm_state`` (0 = off, non-zero = on)."""

    OFF = 0
    ON = 1
    UNKNOWN = -1


def _protocol_json_path(profile: str) -> Path:
    base = Path(__file__).resolve().parent / "protocols"
    name = {
        "iot": "ble_iot_protocol.json",
        "pop": "ble_pop_protocol.json",
        "buoy": "ble_buoy_protocol.json",
    }.get(profile, "ble_iot_protocol.json")
    return base / name


@lru_cache(maxsize=4)
def load_protocol_spec(profile: str) -> dict[str, Any]:
    path = _protocol_json_path(profile)
    with path.open(encoding="utf-8-sig") as f:
        return json.load(f)


async def async_load_protocol_spec(
    hass: HomeAssistant, profile: str
) -> dict[str, Any]:
    """Load protocol JSON off the event loop (avoids blocking-call warnings)."""
    return await hass.async_add_executor_job(load_protocol_spec, profile)


def iot_checksum(data: bytes) -> bytes:
    total = sum(b & 0xFF for b in data) & 0xFFFF
    return bytes([(total >> 8) & 0xFF, total & 0xFF])


def _hex_to_bytes(value: str) -> bytes:
    cleaned = value.strip().replace(" ", "")
    if len(cleaned) % 2:
        cleaned = "0" + cleaned
    return bytes.fromhex(cleaned)


def build_iot_command(
    spec: dict[str, Any],
    command_name: str,
    payload: bytes = b"",
) -> bytes:
    """Build a single IoT-protocol request frame."""
    commands = spec.get("commands") or {}
    if command_name not in commands:
        raise ValueError(f"Unknown command: {command_name}")
    cmd = commands[command_name]
    dest = _hex_to_bytes(str(cmd["destination"]))
    opcode = _hex_to_bytes(str(cmd["opcode"]))
    if len(dest) != 2:
        raise ValueError(f"destination must be 2 bytes: {cmd['destination']}")
    if len(opcode) != 1:
        raise ValueError(f"opcode must be 1 byte: {cmd['opcode']}")

    data_len = len(payload)
    header = bytes(
        [
            SOP_IOT,
            SRC_IOT,
            dest[0],
            dest[1],
            opcode[0],
            (data_len >> 8) & 0xFF,
            data_len & 0xFF,
        ]
    )
    body = header + payload
    return body + iot_checksum(body)


def build_startup(spec: dict[str, Any]) -> bytes:
    return build_iot_command(spec, CMD_START_UP)


def build_shutdown(spec: dict[str, Any]) -> bytes:
    return build_iot_command(spec, CMD_SHUTDOWN)


def build_system_status_request(spec: dict[str, Any]) -> bytes:
    return build_iot_command(spec, CMD_SYSTEM_STATUS)


# --- Control commands -------------------------------------------------------
#
# Cleaning mode and cycle time target the drive controller (``FFE9``); steering
# and the filter-indicator reset target ``FFF7``.  Steering frames take effect
# physically but never return a clean ACK, so callers must send them without
# waiting for a response (waiting can abort the ACL link on some firmwares).

DRIVE_DIRECTIONS = {
    "stop": 0x01,
    "forward": 0x02,
    "backward": 0x03,
    "right": 0x04,
    "left": 0x05,
}


def build_set_cleaning_mode(spec: dict[str, Any], mode: int) -> bytes:
    """Start a cleaning cycle in ``mode`` (see :data:`CLEAN_MODE_NAMES`)."""
    code = int(mode)
    if code == 254 or code not in CLEAN_MODE_NAMES:
        raise ValueError(f"Unsupported cleaning mode: {mode}")
    return build_iot_command(spec, CMD_SET_CLEANING_MODE, bytes([code]))


def build_set_cycle_time(spec: dict[str, Any], minutes: int) -> bytes:
    """Set the cycle length in minutes (2-byte big-endian payload)."""
    value = int(minutes)
    if not 1 <= value <= 600:
        raise ValueError(f"Cycle time out of range: {minutes}")
    return build_iot_command(
        spec, CMD_SET_CYCLE_TIME, bytes([(value >> 8) & 0xFF, value & 0xFF])
    )


def build_manual_drive(spec: dict[str, Any], direction: str | int, speed: int) -> bytes:
    """Steering frame: a direction from :data:`DRIVE_DIRECTIONS` + speed 0-100."""
    if isinstance(direction, str):
        if direction not in DRIVE_DIRECTIONS:
            raise ValueError(f"Unknown drive direction: {direction}")
        code = DRIVE_DIRECTIONS[direction]
    else:
        code = int(direction)
    rate = max(0, min(100, int(speed)))
    return build_iot_command(spec, CMD_MANUAL_DRIVE, bytes([code, rate]))


def build_quit_manual_drive(spec: dict[str, Any]) -> bytes:
    """Leave remote-control mode (the robot parks its drive outputs)."""
    return build_iot_command(spec, CMD_QUIT_MANUAL_DRIVE)


def build_reset_filter_indicator(spec: dict[str, Any]) -> bytes:
    """Clear the "filter full" indicator on the power supply."""
    return build_iot_command(spec, CMD_RESET_FILTER_INDICATOR)



def encode_iot_notify_text(frame: bytes) -> bytes:
    """Wrap a binary IoT frame for GATT-server notify (ASCII ``03:<hex>``)."""
    return (IOT_NOTIFY_PREFIX + frame.hex()).encode("ascii")


def looks_like_iot_ascii_chunk(data: bytes) -> bool:
    """True if a notify chunk looks like Plus ASCII envelope text."""
    if not data:
        return False
    if b":" in data:
        return True
    sample = data[:min(len(data), 32)]
    return bool(_HEX_CHARS.match(sample))


def _verify_iot_checksum(frame: bytes) -> bool:
    if len(frame) < IOT_HEADER_LEN + IOT_CHECKSUM_LEN:
        return False
    body, cs = frame[:-IOT_CHECKSUM_LEN], frame[-IOT_CHECKSUM_LEN:]
    return iot_checksum(body) == cs


def iter_iot_frames(buffer: bytes) -> tuple[list[bytes], bytes]:
    """Split accumulated *binary* notify data into complete IoT frames."""
    frames: list[bytes] = []
    i = 0
    while i < len(buffer):
        if buffer[i] != SOP_IOT:
            i += 1
            continue
        if i + IOT_HEADER_LEN > len(buffer):
            break
        data_len = (buffer[i + 5] << 8) | buffer[i + 6]
        total = IOT_HEADER_LEN + data_len + IOT_CHECKSUM_LEN
        if i + total > len(buffer):
            # Some firmwares (e.g. AW5) count the 2-byte checksum *inside*
            # data_len and their trailing checksum does not match the app's
            # sum. Accept a full 7+data_len frame unverified in that case.
            total_lenient = IOT_HEADER_LEN + data_len
            if (
                data_len >= IOT_CHECKSUM_LEN
                and total_lenient >= IOT_HEADER_LEN + IOT_CHECKSUM_LEN
                and i + total_lenient <= len(buffer)
            ):
                frame = buffer[i : i + total_lenient]
                _LOGGER.debug(
                    "Plus BLE: accepting unverified frame (dlen includes cs): %s",
                    frame.hex(),
                )
                frames.append(frame)
                i += total_lenient
            break
        frame = buffer[i : i + total]
        if _verify_iot_checksum(frame):
            frames.append(frame)
        else:
            _LOGGER.debug("Plus BLE: dropping frame with bad checksum: %s", frame.hex())
        i += total
    return frames, buffer[i:]


class IotAsciiRxBuffer:
    """Reassemble IoT ASCII notify chunks (``[prefix]:<hex>``) into binary frames."""

    def __init__(self) -> None:
        self._text = ""

    def feed(self, chunk: bytes) -> list[bytes]:
        try:
            text = chunk.decode("ascii", errors="ignore")
        except Exception:  # noqa: BLE001
            return []
        if not text:
            return []

        if not self._text:
            colon = text.find(":")
            if colon < 0:
                return []
            self._text = text[colon:]
        else:
            self._text += text

        return self._drain()

    @property
    def pending(self) -> bool:
        return bool(self._text)

    def _drain(self) -> list[bytes]:
        frames: list[bytes] = []
        while self._text:
            colon = self._text.find(":")
            if colon < 0:
                self._text = ""
                break
            if colon > 0:
                self._text = self._text[colon:]

            hex_part = self._text[1:]
            if len(hex_part) < IOT_HEADER_LEN * 2:
                break
            try:
                header = bytes.fromhex(hex_part[: IOT_HEADER_LEN * 2])
            except ValueError:
                _LOGGER.debug("Plus BLE: bad ASCII header; clearing rx buffer")
                self._text = ""
                break
            if len(header) < IOT_HEADER_LEN or header[0] != SOP_IOT:
                self._text = ""
                break

            data_len = (header[5] << 8) | header[6]
            frame_len = IOT_HEADER_LEN + data_len + IOT_CHECKSUM_LEN
            need_hex = frame_len * 2
            if len(hex_part) < need_hex:
                # Firmware quirk (AW5): data_len includes the checksum bytes
                # and the trailing checksum itself is bogus. Accept a full
                # 7+data_len frame unverified.
                need_lenient = (IOT_HEADER_LEN + data_len) * 2
                if (
                    data_len >= IOT_CHECKSUM_LEN
                    and len(hex_part) >= need_lenient
                ):
                    try:
                        frame = bytes.fromhex(hex_part[:need_lenient])
                    except ValueError:
                        self._text = ""
                        break
                    self._text = hex_part[need_lenient:]
                    _LOGGER.debug(
                        "Plus BLE: accepting unverified ASCII frame "
                        "(dlen includes cs): %s",
                        frame.hex(),
                    )
                    frames.append(frame)
                    continue
                break
            try:
                frame = bytes.fromhex(hex_part[:need_hex])
            except ValueError:
                _LOGGER.debug("Plus BLE: bad ASCII frame hex; clearing rx buffer")
                self._text = ""
                break
            self._text = hex_part[need_hex:]
            if _verify_iot_checksum(frame):
                frames.append(frame)
            else:
                _LOGGER.debug(
                    "Plus BLE: dropping ASCII frame with bad checksum: %s",
                    frame.hex(),
                )
        return frames


def parse_iot_frame_payload(frame: bytes) -> tuple[int, int, bytes]:
    """Return (opcode, data_len, payload including leading ACK when present)."""
    if len(frame) < IOT_HEADER_LEN + IOT_CHECKSUM_LEN:
        raise ValueError("frame too short")
    opcode = frame[4]
    data_len = (frame[5] << 8) | frame[6]
    payload = frame[IOT_HEADER_LEN : IOT_HEADER_LEN + data_len]
    return opcode, data_len, payload


PWS_STATE_NAMES = {
    0: "off",
    1: "on",
    2: "hold_weekly",
    3: "hold_delay",
    4: "programming",
    5: "cleaning",
    6: "sleep",
}

ROBOT_STATE_NAMES = {
    0: "init",
    1: "mapping",
    2: "scanning",
    3: "recovery",
    4: "finished",
    5: "fault",
    6: "programming",
    7: "not_connected",
}

CLEAN_MODE_NAMES = {
    1: "regular",
    2: "fast",
    3: "cove",
    4: "floor",
    5: "waterline",
    6: "ultra",
    7: "spot",
    8: "wall",
    9: "tictac",
    10: "custom",
    11: "pickup",
    15: "stairs",
    254: "empty",
}

# Order of the 2-byte duration matrix in system_status bytes 30+ (verified
# against the reference SM profile durations on a live M7 unit).
MODE_DURATION_ORDER = (
    "regular",
    "fast",
    "cove",
    "floor",
    "waterline",
    "ultra",
    "spot",
    "wall",
    "tictac",
    "custom",
    "pickup",
)


def pws_state_name(value: int | None) -> str | None:
    if value is None:
        return None
    return PWS_STATE_NAMES.get(int(value), f"unknown_{value}")


def robot_state_name(value: int | None) -> str | None:
    if value is None:
        return None
    return ROBOT_STATE_NAMES.get(int(value), f"unknown_{value}")


def clean_mode_name(value: int | None) -> str | None:
    if value is None:
        return None
    val = int(value)
    if val == 254:
        return "empty"
    return CLEAN_MODE_NAMES.get(val, f"unknown_{val}")


def filter_band(value: int | None) -> str | None:
    """Map the Advanced FBI filter byte (0-102) to a named band."""
    if value is None:
        return None
    val = int(value)
    if val == 0:
        return "empty"
    if val <= 25:
        return "partially_full"
    if val <= 74:
        return "getting_full"
    if val <= 99:
        return "almost_full"
    if val == 100:
        return "full"
    if val == 101:
        return "sensor_fault"
    if val == 102:
        return "not_available"
    return "unknown"


def parse_system_status(payload: bytes) -> dict[str, Any]:
    """Map ``system_status`` response bytes after removing the leading ACK.

    Layout (community spec + live M7 capture, data bytes after the ACK):
    0 mu_state, 1 sm_state, 2 filter, 3 cleaning_mode,
    4-5 configured cycle minutes, 6-9 cycle start local epoch,
    10-13 cycle start UTC epoch, 14 is_smart, 15 next cycle mode,
    16-17 delay-to-next minutes, 18-29 active fault block,
    30+ per-mode duration matrix.
    Some firmwares (M7) also include the checksum bytes inside the payload.
    """
    # Wire layout: ACK + mu_state + sm_state + filter + cleaning_mode + ...
    data = payload[1:] if payload else b""

    def u8(idx: int) -> int | None:
        return data[idx] if len(data) > idx else None

    def u16(idx: int) -> int | None:
        if len(data) >= idx + 2:
            return (data[idx] << 8) | data[idx + 1]
        return None

    def epoch(idx: int) -> int | None:
        if len(data) < idx + 4:
            return None
        val = int.from_bytes(data[idx : idx + 4], "big")
        return val if 1_600_000_000 <= val <= 2_600_000_000 else None

    out: dict[str, Any] = {
        "mu_state": u8(0),
        "sm_state": u8(1),
        "cleaning_mode": u8(3) if len(data) > 3 else None,
    }
    if len(data) > 2:
        out["filter_state"] = data[2]
    if len(data) >= 14:
        out["cycle_info_raw"] = data[4:14].hex()
        dur = u16(4)
        if dur is not None and dur <= 1440:
            out["cycle_time_min"] = dur
        local_ts = epoch(6)
        if local_ts is not None:
            out["cycle_start_local"] = local_ts
        utc_ts = epoch(10)
        if utc_ts is not None:
            out["cycle_start_utc"] = utc_ts
    if len(data) > 14:
        out["is_smart"] = data[14]
    if len(data) >= 18:
        # Bytes 15-17 are documented as the next scheduled program (mode +
        # delay). Observed on an M7 PWS (2026-10): the pair mirrors the
        # *currently configured* program (mode byte == running mode, value ==
        # configured cycle minutes) and does not count down while a cycle runs,
        # so it is exposed as the armed program rather than a wall-clock ETA.
        out["armed_program_mode"] = u8(15)
        out["armed_program_minutes"] = u16(16)
    fault_code = u8(18)
    if fault_code is not None:
        out["faults_code"] = fault_code
        out["has_faults"] = fault_code not in (0x00, 0xFF)
        if out["has_faults"] and len(data) >= 30:
            out["faults_pcb_hours"] = u16(19)
            out["faults_pcb_minutes"] = u8(21)
            out["faults_cycle_counter"] = u16(22)
            out["faults_value_1"] = u16(24)
            out["faults_value_2"] = u16(26)
            out["faults_value_3"] = u16(28)
    if len(data) >= 51:
        durations = {
            name: int.from_bytes(data[30 + 2 * i : 32 + 2 * i], "big")
            for i, name in enumerate(MODE_DURATION_ORDER)
        }
        # Sanity: plausible minutes only, else expose raw hex.
        if all(0 < v <= 1440 for v in durations.values()):
            out["mode_durations"] = durations
        else:
            out["cleaning_modes_raw"] = data[30:52].hex()
    elif len(data) > 30:
        out["cleaning_modes_raw"] = data[30:].hex()
    return out


def sm_state_implies_power_on(sm_state: int | None) -> bool | None:
    if sm_state is None:
        return None
    return sm_state != 0


def sm_state_to_power_state(sm_state: int | None) -> PowerState:
    inferred = sm_state_implies_power_on(sm_state)
    if inferred is None:
        return PowerState.UNKNOWN
    return PowerState.ON if inferred else PowerState.OFF
