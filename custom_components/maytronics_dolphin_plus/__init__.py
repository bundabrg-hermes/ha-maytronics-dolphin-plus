"""Maytronics Dolphin Plus — local BLE (MyDolphin Plus app protocol)."""

from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv, entity_registry as er

from .connection import DolphinPlusBleConnection, async_ble_periodic_release
from .const import (
    ATTR_DIRECTION,
    ATTR_DURATION,
    ATTR_SPEED,
    CONF_ADDRESS,
    DRIVE_MAX_DURATION,
    SERVICE_DRIVE,
    SERVICE_QUIT_MANUAL_DRIVE,
    SERVICE_RESET_FILTER_INDICATOR,
    CONF_PROFILE,
    CONF_TRANSPORT,
    DATA_BLE_SESSION,
    DATA_COORDINATOR,
    DATA_KEEPALIVE_TASK,
    DEFAULT_PROFILE,
    DEFAULT_TRANSPORT,
    DOMAIN,
)
from .coordinator import DolphinPlusCoordinator
from .protocol import DRIVE_DIRECTIONS

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SWITCH,
    Platform.SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
]

_TARGET_SCHEMA = {vol.Optional(ATTR_ENTITY_ID): cv.comp_entity_ids}

DRIVE_SCHEMA = vol.Schema(
    {
        **_TARGET_SCHEMA,
        vol.Optional(ATTR_DIRECTION, default="forward"): vol.In(list(DRIVE_DIRECTIONS)),
        vol.Optional(ATTR_SPEED, default=50): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=100)
        ),
        vol.Optional(ATTR_DURATION, default=1.0): vol.All(
            vol.Coerce(float), vol.Range(min=0.2, max=DRIVE_MAX_DURATION)
        ),
    }
)


def _async_context_for_call(
    hass: HomeAssistant, call: ServiceCall
) -> dict[str, Any]:
    """Resolve the device for ``entity_id``, else the first loaded device."""
    entity_ids = call.data.get(ATTR_ENTITY_ID) or []
    if entity_ids:
        registry = er.async_get(hass)
        for entity_id in entity_ids:
            registered = registry.async_get(entity_id)
            entry_id = registered.config_entry_id if registered else None
            context = hass.data.get(DOMAIN, {}).get(entry_id or "")
            if context is None:
                raise HomeAssistantError(
                    f"{entity_id} is not an entity of a loaded Dolphin Plus device"
                )
            return context
    for entry in hass.config_entries.async_entries(DOMAIN):
        context = hass.data.get(DOMAIN, {}).get(entry.entry_id)
        if context:
            return context
    raise HomeAssistantError("No loaded Maytronics Dolphin Plus device")


async def _async_handle_drive(hass: HomeAssistant, call: ServiceCall) -> None:
    """Drive for a bounded time, then always stop and leave RC mode."""
    session: DolphinPlusBleConnection = _async_context_for_call(hass, call)[
        DATA_BLE_SESSION
    ]
    await session.async_manual_drive(call.data[ATTR_DIRECTION], call.data[ATTR_SPEED])
    try:
        await asyncio.sleep(call.data[ATTR_DURATION])
    finally:
        await session.async_manual_drive("stop", 0)
        await session.async_quit_manual_drive()


async def _async_handle_quit_manual_drive(hass: HomeAssistant, call: ServiceCall) -> None:
    session: DolphinPlusBleConnection = _async_context_for_call(hass, call)[
        DATA_BLE_SESSION
    ]
    await session.async_quit_manual_drive()


async def _async_handle_reset_filter_indicator(
    hass: HomeAssistant, call: ServiceCall
) -> None:
    context = _async_context_for_call(hass, call)
    session: DolphinPlusBleConnection = context[DATA_BLE_SESSION]
    coordinator: DolphinPlusCoordinator = context[DATA_COORDINATOR]
    await session.async_reset_filter_indicator()
    await coordinator.async_request_refresh()


def _async_register_services(hass: HomeAssistant) -> None:
    if hass.services.has_service(DOMAIN, SERVICE_DRIVE):
        return
    hass.services.async_register(
        DOMAIN, SERVICE_DRIVE, _async_handle_drive, schema=DRIVE_SCHEMA
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_QUIT_MANUAL_DRIVE,
        _async_handle_quit_manual_drive,
        schema=vol.Schema(_TARGET_SCHEMA),
    )
    hass.services.async_register(
        DOMAIN,
        SERVICE_RESET_FILTER_INDICATOR,
        _async_handle_reset_filter_indicator,
        schema=vol.Schema(_TARGET_SCHEMA),
    )


def _async_unregister_services(hass: HomeAssistant) -> None:
    for service in (
        SERVICE_DRIVE,
        SERVICE_QUIT_MANUAL_DRIVE,
        SERVICE_RESET_FILTER_INDICATOR,
    ):
        if hass.services.has_service(DOMAIN, service):
            hass.services.async_remove(DOMAIN, service)


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    if entry.state is not ConfigEntryState.LOADED:
        return
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    hass.data.setdefault(DOMAIN, {})
    profile = entry.data.get(CONF_PROFILE, DEFAULT_PROFILE)
    transport = entry.data.get(CONF_TRANSPORT, DEFAULT_TRANSPORT)
    session = DolphinPlusBleConnection(
        hass,
        entry.data[CONF_ADDRESS],
        entry.entry_id,
        profile=profile,
        transport=transport,
    )
    coordinator = DolphinPlusCoordinator(hass, session, entry)
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    release_task = hass.async_create_background_task(
        async_ble_periodic_release(hass, entry.entry_id),
        f"{DOMAIN}_ble_release_{entry.entry_id[:8]}",
    )
    entry.async_on_unload(release_task.cancel)

    hass.data[DOMAIN][entry.entry_id] = {
        DATA_BLE_SESSION: session,
        DATA_COORDINATOR: coordinator,
        DATA_KEEPALIVE_TASK: release_task,
    }

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    _async_register_services(hass)
    hass.async_create_background_task(
        coordinator.async_refresh(),
        f"{DOMAIN}_status_{entry.entry_id[:8]}",
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    entry_data = hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    if entry_data:
        task: asyncio.Task | None = entry_data.get(DATA_KEEPALIVE_TASK)
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if coord := entry_data.get(DATA_COORDINATOR):
            with suppress(Exception):
                await coord.async_shutdown()
        if session := entry_data.get(DATA_BLE_SESSION):
            session.mark_shutting_down()
            try:
                await asyncio.wait_for(session.async_disconnect(), timeout=15.0)
            except TimeoutError:
                _LOGGER.warning("Dolphin Plus BLE disconnect timed out during unload")
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    # Services are device-agnostic; drop them once no device is loaded.
    if not hass.data.get(DOMAIN):
        _async_unregister_services(hass)
    return unloaded
