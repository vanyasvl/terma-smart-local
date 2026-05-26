"""Support for the Terma Smart climate entity."""
from __future__ import annotations

import time
from typing import Any

from homeassistant.components.climate import (
    ClimateEntity,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_TEMPERATURE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.restore_state import RestoreEntity

from . import TermaDevice
from .const import DOMAIN, MAX_TEMP_C, MIN_TEMP_C, TEMP_STEP_C
from .entity import TermaBaseEntity
from .terma_client import MODE_MANUAL, decode_schedule, k_to_c


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terma climate entities for all devices in this entry."""
    devices: list[TermaDevice] = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(TermaClimate(device) for device in devices)


def _schedule_setpoint_c(
    schedule_str: str | None, device_timestamp: float | None
) -> float | None:
    """Return the schedule's active setpoint at the given device timestamp."""
    if not schedule_str or device_timestamp is None:
        return None
    try:
        entries = decode_schedule(schedule_str)
    except (ValueError, TypeError):
        return None
    if not entries:
        return None
    lt = time.localtime(device_timestamp)
    week_sec = (
        lt.tm_wday * 86400 + lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
    )
    # Schedule entries are ordered; pick the latest whose week_sec <= now,
    # else wrap to the last entry from the prior week.
    active_c = entries[-1][1]
    for ws, c in entries:
        if ws <= week_sec:
            active_c = c
        else:
            break
    return active_c


class TermaClimate(TermaBaseEntity, ClimateEntity, RestoreEntity):
    """Terma thermostat exposed as an HA climate entity."""

    _attr_name = None
    _attr_icon = "mdi:radiator"
    _attr_temperature_unit = UnitOfTemperature.CELSIUS
    _attr_target_temperature_step = TEMP_STEP_C
    _attr_min_temp = MIN_TEMP_C
    _attr_max_temp = MAX_TEMP_C
    _attr_hvac_modes = [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    _attr_supported_features = (
        ClimateEntityFeature.TARGET_TEMPERATURE
        | ClimateEntityFeature.TURN_ON
        | ClimateEntityFeature.TURN_OFF
    )

    def __init__(self, device: TermaDevice) -> None:
        """Initialize the climate entity."""
        super().__init__(device, "climate")

    async def async_added_to_hass(self) -> None:
        """Restore the manual setpoint cache across HA restarts.

        The device does not echo setTemperature, so once HA restarts and the
        in-memory cache is gone, we'd otherwise show the schedule value even
        when the device is actively driving toward a manual setpoint. Seed
        the cache from the last persisted target_temperature attribute.
        """
        await super().async_added_to_hass()
        if self._device.manual_target_c is not None:
            return
        last_state = await self.async_get_last_state()
        if last_state is None:
            return
        last_temp = last_state.attributes.get(ATTR_TEMPERATURE)
        if last_temp is None:
            return
        try:
            self._device.manual_target_c = float(last_temp)
        except (TypeError, ValueError):
            pass

    @property
    def current_temperature(self) -> float | None:
        """Return the room temperature in °C."""
        telemetry = self.coordinator.data
        if not telemetry:
            return None
        val = telemetry.get("temperature")
        if val is None or val <= 0:
            return None
        return k_to_c(val)

    @property
    def target_temperature(self) -> float | None:
        """Return the active setpoint in °C.

        Device does not echo the manual setpoint, so when in manual override
        we use the cached value we last wrote; on cache miss (e.g. HA restart)
        we fall back to the schedule entry — better than showing nothing.
        """
        telemetry = self.coordinator.data
        if not telemetry:
            return None
        if telemetry.get("manualMode") in (0, 1, 2):
            if self._device.manual_target_c is not None:
                return self._device.manual_target_c
        return _schedule_setpoint_c(
            telemetry.get("schedule"), telemetry.get("timestamp")
        )

    @property
    def hvac_mode(self) -> HVACMode | None:
        """Return the current HVAC mode."""
        telemetry = self.coordinator.data
        if not telemetry:
            return None
        if not telemetry.get("isEnabled"):
            return HVACMode.OFF
        if telemetry.get("manualMode") == 3:
            return HVACMode.AUTO
        return HVACMode.HEAT

    @property
    def hvac_action(self) -> HVACAction | None:
        """Return whether the element is currently energised."""
        telemetry = self.coordinator.data
        if not telemetry:
            return None
        if not telemetry.get("isEnabled"):
            return HVACAction.OFF
        if telemetry.get("isHeating"):
            return HVACAction.HEATING
        return HVACAction.IDLE

    async def async_set_temperature(self, **kwargs: Any) -> None:
        """Set a new manual target temperature."""
        temperature = kwargs.get(ATTR_TEMPERATURE)
        if temperature is None:
            return
        temp = float(temperature)
        client = self._device.client
        await self.hass.async_add_executor_job(
            client.set_target_temperature, temp, MODE_MANUAL
        )
        self._device.manual_target_c = temp
        await self.coordinator.async_request_refresh()

    async def async_set_hvac_mode(self, hvac_mode: HVACMode) -> None:
        """Switch HVAC mode (OFF / HEAT / AUTO)."""
        client = self._device.client
        telemetry = self.coordinator.data or {}

        if hvac_mode == HVACMode.OFF:
            await self.hass.async_add_executor_job(client.set_enabled, False)
        elif hvac_mode == HVACMode.HEAT:
            if not telemetry.get("isEnabled"):
                await self.hass.async_add_executor_job(client.set_enabled, True)
            # Transitioning from AUTO → HEAT without a user-chosen setpoint:
            # seed the manual target with the current schedule entry so the
            # device gets a concrete target and the card shows a value.
            if telemetry.get("manualMode") == 3:
                target = _schedule_setpoint_c(
                    telemetry.get("schedule"), telemetry.get("timestamp")
                )
                if target is not None:
                    await self.hass.async_add_executor_job(
                        client.set_target_temperature, target, MODE_MANUAL
                    )
                    self._device.manual_target_c = target
        elif hvac_mode == HVACMode.AUTO:
            if not telemetry.get("isEnabled"):
                await self.hass.async_add_executor_job(client.set_enabled, True)
            await self.hass.async_add_executor_job(client.exit_manual_mode)
            self._device.manual_target_c = None

        await self.coordinator.async_request_refresh()
