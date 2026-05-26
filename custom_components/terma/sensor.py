"""Support for Terma Smart sensors."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE, UnitOfTemperature
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TermaDevice
from .const import DOMAIN
from .entity import TermaBaseEntity
from .terma_client import k_to_c


@dataclass(frozen=True, kw_only=True)
class TermaSensorEntityDescription(SensorEntityDescription):
    """Describes a Terma sensor."""

    telemetry_key: str
    transform: Callable[[float], float] | None = None


SENSOR_DESCRIPTIONS: tuple[TermaSensorEntityDescription, ...] = (
    TermaSensorEntityDescription(
        key="temperature",
        name="Room Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:home-thermometer",
        telemetry_key="temperature",
        transform=k_to_c,
    ),
    TermaSensorEntityDescription(
        key="heater_temperature",
        name="Heater Temperature",
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        device_class=SensorDeviceClass.TEMPERATURE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:thermometer-high",
        telemetry_key="heaterTemperature",
        transform=k_to_c,
    ),
    TermaSensorEntityDescription(
        key="humidity",
        name="Humidity",
        native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.HUMIDITY,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:water-percent",
        telemetry_key="humidity",
        entity_registry_enabled_default=False,
    ),
    TermaSensorEntityDescription(
        key="power_usage",
        name="Power Usage",
        native_unit_of_measurement="Wh",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        icon="mdi:flash",
        telemetry_key="powerUsage",
        transform=lambda x: x * 1000,
    ),
    TermaSensorEntityDescription(
        key="heating_coefficient",
        name="Heating Level",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        icon="mdi:heat-wave",
        telemetry_key="heatingCoefficient",
        transform=lambda x: x * 100,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terma Smart sensors for all devices in this entry."""
    devices: list[TermaDevice] = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        TermaSensor(device, description)
        for device in devices
        for description in SENSOR_DESCRIPTIONS
    )


class TermaSensor(TermaBaseEntity, SensorEntity):
    """Representation of a Terma Smart sensor."""

    entity_description: TermaSensorEntityDescription

    def __init__(
        self,
        device: TermaDevice,
        description: TermaSensorEntityDescription,
    ) -> None:
        """Initialize the sensor."""
        super().__init__(device, description.key)
        self.entity_description = description
        self._attr_entity_registry_enabled_default = (
            description.entity_registry_enabled_default
        )

    @property
    def native_value(self) -> float | None:
        """Return the state of the sensor."""
        if telemetry := self.coordinator.data:
            val = telemetry.get(self.entity_description.telemetry_key)
            if val is not None:
                if (
                    self.entity_description.device_class
                    == SensorDeviceClass.TEMPERATURE
                    and val <= 0
                ):
                    return None
                if self.entity_description.transform:
                    return self.entity_description.transform(val)
                return val
        return None
