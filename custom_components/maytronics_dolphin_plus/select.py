"""Cleaning-mode select for Dolphin Plus local BLE."""

from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
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
from .protocol import CLEAN_MODE_NAMES, MODE_DURATION_ORDER

_LOGGER = logging.getLogger(__name__)

# Programs the Plus app offers for IoT power supplies.  ``stairs`` (15) exists
# only on models that advertise it, so it is not offered unless the robot
# reports it in its own duration matrix.
DEFAULT_MODES: tuple[str, ...] = MODE_DURATION_ORDER

MODE_TO_CODE: dict[str, int] = {
    name: code for code, name in CLEAN_MODE_NAMES.items() if name != "empty"
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: DolphinPlusCoordinator = hass.data[DOMAIN][entry.entry_id][
        DATA_COORDINATOR
    ]
    async_add_entities([DolphinPlusCleaningModeSelect(coordinator, entry)])


class DolphinPlusCleaningModeSelect(CoordinatorEntity, SelectEntity):
    """``set_cleaning_mode`` (opcode 03 / dest FFE9).

    As in the vendor app, selecting a program starts a cleaning cycle in that
    program -- the power supply has no separate "select then start" step.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_name = "Cleaning mode"
    _attr_icon = "mdi:waves"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: DolphinPlusCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._address = entry.data[CONF_ADDRESS]
        name = entry.data.get(CONF_NAME) or "Dolphin Plus"
        self._attr_unique_id = f"{entry.entry_id}_cleaning_mode"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=name,
            manufacturer="Maytronics",
            model="Dolphin Plus (BLE)",
            connections={(dr.CONNECTION_BLUETOOTH, dr.format_mac(self._address))},
        )
        self._attr_options = list(DEFAULT_MODES)
        self._pending: str | None = None

    @property
    def _data(self) -> dict[str, Any]:
        return self.coordinator.data or {}

    def _supported_options(self) -> list[str]:
        """Prefer the programs the robot itself reports durations for."""
        durations = self._data.get("mode_durations")
        if isinstance(durations, dict) and durations:
            supported = [m for m in DEFAULT_MODES if durations.get(m, 0) > 0]
            if supported:
                return supported
        return list(DEFAULT_MODES)

    @property
    def options(self) -> list[str]:
        options = self._supported_options()
        current = self._data.get("cleaning_mode")
        # Keep the UI truthful if the robot is running a program we would not
        # otherwise offer (e.g. stairs on models that advertise it).
        if current is not None:
            name = CLEAN_MODE_NAMES.get(int(current))
            if name and name not in options and name != "empty":
                options.append(name)
        return options

    @property
    def current_option(self) -> str | None:
        if self._pending is not None:
            return self._pending
        mode = self._data.get("cleaning_mode")
        if mode is None:
            return None
        name = CLEAN_MODE_NAMES.get(int(mode))
        return name if name in self.options else None

    def _session(self) -> DolphinPlusBleConnection:
        return self.hass.data[DOMAIN][self._entry.entry_id][DATA_BLE_SESSION]

    async def async_select_option(self, option: str) -> None:
        code = MODE_TO_CODE.get(option)
        if code is None:
            raise ValueError(f"Unsupported cleaning mode: {option}")
        self._pending = option
        self.async_write_ha_state()
        try:
            await self._session().async_set_cleaning_mode(code)
            confirmed = await self.coordinator.async_refresh_until_field(
                "cleaning_mode", code
            )
            if confirmed is not None and int(confirmed) != code:
                _LOGGER.warning(
                    "Cleaning mode: %s was sent but the robot reports %s",
                    option,
                    CLEAN_MODE_NAMES.get(int(confirmed), confirmed),
                )
        finally:
            self._pending = None
            self.async_write_ha_state()
