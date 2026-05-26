"""Base entity for the Terma Smart integration."""
from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import TermaDevice
from .const import DOMAIN


class TermaBaseEntity(CoordinatorEntity):
    """Base class for all Terma entities — owns DeviceInfo and unique_id."""

    _attr_has_entity_name = True

    def __init__(self, device: TermaDevice, key: str) -> None:
        """Initialize a Terma entity bound to one device."""
        super().__init__(device.coordinator)
        self._device = device
        self._attr_unique_id = f"{device.serial}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, device.serial)},
            name=f"Terma {device.serial}",
            manufacturer="Terma",
            model="Smart Thermostat",
        )
