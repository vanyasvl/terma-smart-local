"""Support for Terma Smart number entities."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.number import NumberEntity, NumberEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TermaDevice
from .const import DOMAIN
from .entity import TermaBaseEntity
from .terma_client import TermaClient


@dataclass(frozen=True, kw_only=True)
class TermaNumberEntityDescription(NumberEntityDescription):
    """Describes a Terma number entity."""

    telemetry_key: str
    set_fn: Callable[[TermaClient, float], None]
    value_transform: Callable[[float], float] = lambda v: v


NUMBER_DESCRIPTIONS: tuple[TermaNumberEntityDescription, ...] = (
    TermaNumberEntityDescription(
        key="heating_level",
        name="Heating Level",
        native_unit_of_measurement=PERCENTAGE,
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        icon="mdi:thermometer-lines",
        telemetry_key="heatingCoefficient",
        set_fn=TermaClient.set_heating_level,
        value_transform=lambda v: v * 100,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terma Smart number entities for all devices in this entry."""
    devices: list[TermaDevice] = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        TermaNumber(device, description)
        for device in devices
        for description in NUMBER_DESCRIPTIONS
    )


class TermaNumber(TermaBaseEntity, NumberEntity):
    """Representation of a Terma Smart number entity."""

    entity_description: TermaNumberEntityDescription

    def __init__(
        self,
        device: TermaDevice,
        description: TermaNumberEntityDescription,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(device, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | None:
        """Return the value of the number entity."""
        if telemetry := self.coordinator.data:
            val = telemetry.get(self.entity_description.telemetry_key)
            if val is not None:
                return float(self.entity_description.value_transform(val))
        return None

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        await self.hass.async_add_executor_job(
            self.entity_description.set_fn, self._device.client, value
        )
        await self.coordinator.async_request_refresh()
