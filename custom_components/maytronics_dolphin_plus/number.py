"""Cycle-time number for Dolphin Plus local BLE."""

from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .connection import DolphinPlusBleConnection
from .const import (
    CONF_ADDRESS,
    CONF_NAME,
    DATA_BLE_SESSION,
    DATA_COORDINATOR,
    DOMAIN,
)
from .coordinator import DolphinPlusCoordinator

_LOGGER = logging.getLogger(__name__)

# The Plus app exposes cycle length as a coarse choice; keep a wide but sane
# range and a 5-minute step so the UI stays usable.
CYCLE_STEP_MIN = 5
CYCLE_MIN_MIN = 5
CYCLE_MAX_MIN = 240


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DolphinPlusCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    async_add_entities([DolphinPlusCycleTimeNumber(coordinator, entry)])


class DolphinPlusCycleTimeNumber(CoordinatorEntity, NumberEntity):
    """``set_cycle_time`` (opcode 01 / dest FFE9), length of a cleaning cycle."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Cycle time"
    _attr_icon = "mdi:timer-marker-outline"
    _attr_entity_category = EntityCategory.CONFIG
    _attr_native_unit_of_measurement = UnitOfTime.MINUTES
    _attr_native_min_value = CYCLE_MIN_MIN
    _attr_native_max_value = CYCLE_MAX_MIN
    _attr_native_step = CYCLE_STEP_MIN
    _attr_mode = NumberMode.BOX

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._address = entry.data[CONF_ADDRESS]
        name = entry.data.get(CONF_NAME) or "Dolphin Plus"
        self._attr_unique_id = f"{entry.entry_id}_cycle_time"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=name,
            manufacturer="Maytronics",
            model="Dolphin Plus (BLE)",
            connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(self._address))},
        )
        self._pending: float | None = None

    @property
    def available(self) -> bool:
        """Only useful on power supplies that report a cycle length."""
        return bool(self.coordinator.last_update_success) and (
            self._pending is not None
            or isinstance((self.coordinator.data or {}).get("cycle_time_min"), int)
        )

    @property
    def native_value(self) -> float | None:
        if self._pending is not None:
            return self._pending
        value = (self.coordinator.data or {}).get("cycle_time_min")
        return float(value) if isinstance(value, int) else None

    def _session(self) -> DolphinPlusBleConnection:
        return self.hass.data[DOMAIN][self._entry.entry_id][DATA_BLE_SESSION]

    async def async_set_native_value(self, value: float) -> None:
        minutes = int(
            max(
                CYCLE_MIN_MIN,
                min(CYCLE_MAX_MIN, round(value / CYCLE_STEP_MIN) * CYCLE_STEP_MIN),
            )
        )
        self._pending = float(minutes)
        self.async_write_ha_state()
        try:
            await self._session().async_set_cycle_time(minutes)
            confirmed = await self.coordinator.async_refresh_until_field(
                "cycle_time_min", minutes
            )
            if confirmed != minutes:
                _LOGGER.warning(
                    "Cycle time: %s min was sent but the robot reports %s; this"
                    " power supply may express the field as a cycle type rather"
                    " than minutes",
                    minutes,
                    confirmed,
                )
        finally:
            self._pending = None
            self.async_write_ha_state()
