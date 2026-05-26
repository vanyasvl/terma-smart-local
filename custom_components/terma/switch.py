"""Support for Terma Smart switches."""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import TermaDevice
from .const import DOMAIN
from .entity import TermaBaseEntity
from .terma_client import TermaClient


@dataclass(frozen=True, kw_only=True)
class TermaSwitchEntityDescription(SwitchEntityDescription):
    """Describes a Terma switch."""

    telemetry_key: str
    set_fn: Callable[[TermaClient, bool], None]


SWITCH_DESCRIPTIONS: tuple[TermaSwitchEntityDescription, ...] = (
    TermaSwitchEntityDescription(
        key="power",
        name="Power",
        icon="mdi:power",
        telemetry_key="isEnabled",
        set_fn=TermaClient.set_enabled,
    ),
    TermaSwitchEntityDescription(
        key="dry",
        name="Dry",
        icon="mdi:heating-coil",
        telemetry_key="isDryerOn",
        set_fn=TermaClient.set_dryer,
    ),
    TermaSwitchEntityDescription(
        key="parental_lock",
        name="Parental Lock",
        icon="mdi:lock",
        telemetry_key="isParentalControlOn",
        set_fn=TermaClient.set_parental_control,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Terma Smart switches for all devices in this entry."""
    devices: list[TermaDevice] = hass.data[DOMAIN][entry.entry_id]

    async_add_entities(
        TermaSwitch(device, description)
        for device in devices
        for description in SWITCH_DESCRIPTIONS
    )


class TermaSwitch(TermaBaseEntity, SwitchEntity):
    """Representation of a Terma Smart switch."""

    entity_description: TermaSwitchEntityDescription

    def __init__(
        self,
        device: TermaDevice,
        description: TermaSwitchEntityDescription,
    ) -> None:
        """Initialize the switch."""
        super().__init__(device, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        """Return true if the switch is on."""
        if telemetry := self.coordinator.data:
            return bool(telemetry.get(self.entity_description.telemetry_key))
        return None

    async def _set(self, value: bool) -> None:
        await self.hass.async_add_executor_job(
            self.entity_description.set_fn, self._device.client, value
        )
        await self.coordinator.async_request_refresh()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn the switch on."""
        await self._set(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn the switch off."""
        await self._set(False)
