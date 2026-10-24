"""Sensors for Dolphin Plus BLE status."""

from __future__ import annotations

from datetime import datetime, timezone as dt_timezone

UTC = dt_timezone.utc

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import CONF_ADDRESS, CONF_NAME, DATA_COORDINATOR, DOMAIN
from .coordinator import DolphinPlusCoordinator
from .protocol import (
    PowerState,
    clean_mode_name,
    filter_band,
    pws_state_name,
    robot_state_name,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DolphinPlusCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    async_add_entities(
        [
            DolphinPlusSmStateSensor(coordinator, entry),
            DolphinPlusMuStateSensor(coordinator, entry),
            DolphinPlusCleaningModeSensor(coordinator, entry),
            DolphinPlusCycleProgressSensor(coordinator, entry),
            DolphinPlusFilterSensor(coordinator, entry),
            DolphinPlusFaultSensor(coordinator, entry),
        ]
    )


class _DolphinPlusSensorBase(CoordinatorEntity[DolphinPlusCoordinator], SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        coordinator: DolphinPlusCoordinator,
        entry: ConfigEntry,
        *,
        key: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        dev_name = entry.data.get(CONF_NAME) or "Dolphin Plus"
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_name = name
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=dev_name,
            manufacturer="Maytronics",
            model="Dolphin Plus (BLE)",
            connections={
                (dr.CONNECTION_BLUETOOTH, dr.format_mac(entry.data[CONF_ADDRESS]))
            },
        )


class DolphinPlusSmStateSensor(_DolphinPlusSensorBase):
    """Power-supply state (PwsState) from ``system_status``."""

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, key="sm_state", name="PWS state")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        if not data.get("poll_ok"):
            return None
        sm = data.get("sm_state")
        if sm is None:
            return None
        name = pws_state_name(sm)
        power = data.get("power_state")
        if power in (PowerState.ON, PowerState.OFF):
            return f"{name} ({'on' if power == PowerState.ON else 'off'})"
        return name


class DolphinPlusMuStateSensor(_DolphinPlusSensorBase):
    """Robot/motor-unit state (RobotState) from ``system_status``."""

    _attr_icon = "mdi:robot"

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, key="mu_state", name="Robot state")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        if not data.get("poll_ok"):
            return None
        return robot_state_name(data.get("mu_state"))


class DolphinPlusCleaningModeSensor(_DolphinPlusSensorBase):
    """Active cleaning program as a readable name."""

    _attr_icon = "mdi:wash-variant"

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(
            coordinator, entry, key="cleaning_mode", name="Cleaning mode"
        )

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        if not data.get("poll_ok"):
            return None
        return clean_mode_name(data.get("cleaning_mode"))

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        attrs: dict = {}
        if data.get("cleaning_mode") is not None:
            attrs["mode_code"] = data["cleaning_mode"]
        for key in ("cycle_time_min", "cycle_info_raw", "is_smart"):
            if key in data:
                attrs[key] = data[key]
        if "mode_durations" in data:
            attrs["mode_durations"] = data["mode_durations"]
        if "cleaning_modes_raw" in data:
            attrs["cleaning_modes_raw"] = data["cleaning_modes_raw"]
        armed_mode = clean_mode_name(data.get("armed_program_mode"))
        if armed_mode:
            attrs["armed_program_mode"] = armed_mode
        if isinstance(data.get("armed_program_minutes"), int):
            attrs["armed_program_minutes"] = data["armed_program_minutes"]
        return attrs


class DolphinPlusCycleProgressSensor(_DolphinPlusSensorBase):
    """Progress of the active cleaning cycle, percent complete."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:clock-outline"

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, key="cycle_progress", name="Cycle progress")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        if not data.get("poll_ok"):
            return None
        start = data.get("cycle_start_utc")
        duration = data.get("cycle_time_min")
        if not start or not duration:
            return None
        elapsed = datetime.now(UTC).timestamp() - float(start)
        total = duration * 60
        if elapsed < 0 or elapsed > total * 2:
            # Stale timestamp (cycle finished a while ago).
            return None
        return max(0, min(100, round(elapsed / total * 100)))

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        attrs: dict = {}
        start = data.get("cycle_start_utc")
        duration = data.get("cycle_time_min")
        if start:
            started = dt_util.as_local(datetime.fromtimestamp(start, tz=UTC))
            attrs["started_at"] = started.isoformat()
            elapsed_min = max(0, int((datetime.now(UTC).timestamp() - start) / 60))
            attrs["elapsed_min"] = elapsed_min
            if duration:
                attrs["duration_min"] = duration
                attrs["remaining_min"] = max(0, duration - elapsed_min)
        if data.get("cycle_start_local"):
            attrs["robot_clock_local"] = datetime.fromtimestamp(
                data["cycle_start_local"], tz=UTC
            ).isoformat()
        return attrs


class DolphinPlusFilterSensor(_DolphinPlusSensorBase):
    """Filter canister blockage as a percentage (Advanced FBI scale)."""

    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_icon = "mdi:air-filter"

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, key="filter_state", name="Filter state")

    @property
    def native_value(self) -> int | None:
        data = self.coordinator.data or {}
        if not data.get("poll_ok"):
            return None
        val = data.get("filter_state")
        if not isinstance(val, int) or val >= 101:
            # 101 = sensor fault, 102 = no filter sensor on this model.
            return None
        return val

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        band = filter_band(data.get("filter_state"))
        return {"band": band} if band else {}


class DolphinPlusFaultSensor(_DolphinPlusSensorBase):
    """Active fault code from ``system_status`` (0 / 0xFF = no fault)."""

    _attr_icon = "mdi:alert-circle-outline"

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, key="fault_status", name="Fault status")

    @property
    def native_value(self) -> str | None:
        data = self.coordinator.data or {}
        if not data.get("poll_ok"):
            return None
        code = data.get("faults_code")
        if code is None:
            return None
        if not data.get("has_faults"):
            return "ok"
        return f"fault (0x{code:02x})"

    @property
    def extra_state_attributes(self) -> dict:
        data = self.coordinator.data or {}
        attrs: dict = {}
        for key in (
            "faults_code",
            "has_faults",
            "faults_pcb_hours",
            "faults_pcb_minutes",
            "faults_cycle_counter",
            "faults_value_1",
            "faults_value_2",
            "faults_value_3",
        ):
            if key in data:
                attrs[key] = data[key]
        return attrs
