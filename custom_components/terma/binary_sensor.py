"""Support for Terma Smart binary sensors."""
from __future__ import annotations

from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TermaDevice
from .const import DOMAIN
from .entity import TermaBaseEntity


@dataclass(frozen=True, kw_only=True)
class TermaBinarySensorEntityDescription(BinarySensorEntityDescription):
    """Describes a Terma binary sensor."""

    telemetry_key: str


BINARY_SENSOR_DESCRIPTIONS: tuple[TermaBinarySensorEntityDescription, ...] = (
    TermaBinarySensorEntityDescription(
        key="heating",
        name="Heating",
        icon="mdi:fire",
        telemetry_key="isHeating",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terma Smart binary sensors for all devices in this entry."""
    devices: list[TermaDevice] = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        TermaBinarySensor(device, description)
        for device in devices
        for description in BINARY_SENSOR_DESCRIPTIONS
    )


class TermaBinarySensor(TermaBaseEntity, BinarySensorEntity):
    """Representation of a Terma Smart binary sensor."""

    entity_description: TermaBinarySensorEntityDescription

    def __init__(
        self,
        device: TermaDevice,
        description: TermaBinarySensorEntityDescription,
    ) -> None:
        """Initialize the binary sensor."""
        super().__init__(device, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return true if the binary sensor is on."""
        if telemetry := self.coordinator.data:
            return bool(telemetry.get(self.entity_description.telemetry_key))
        return None
