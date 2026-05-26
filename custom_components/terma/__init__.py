"""The Terma Smart integration."""
from __future__ import annotations

from dataclasses import dataclass
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_DEVICES, CONF_SERIAL, DEFAULT_PORT, DOMAIN, UPDATE_INTERVAL
from .terma_client import TermaClient, TermaError

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.CLIMATE,
    Platform.NUMBER,
    Platform.SENSOR,
    Platform.SWITCH,
]


@dataclass
class TermaDevice:
    """A single Terma device managed under a config entry."""

    serial: str
    host: str
    port: int
    client: TermaClient
    coordinator: DataUpdateCoordinator
    # Cached manual setpoint — the device accepts setTemperature but does not
    # echo it back in telemetry, so the climate entity remembers what it wrote.
    manual_target_c: float | None = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Terma Smart from a config entry."""
    devices_data = entry.data.get(CONF_DEVICES, [])
    devices: list[TermaDevice] = []

    def make_update(client: TermaClient):
        async def async_update_data():
            try:
                return await hass.async_add_executor_job(client.get_telemetry)
            except TermaError as err:
                raise UpdateFailed(f"Error communicating with API: {err}") from err

        return async_update_data

    for device_data in devices_data:
        host = device_data[CONF_HOST]
        serial = device_data[CONF_SERIAL]
        port = device_data.get(CONF_PORT, DEFAULT_PORT)

        client = TermaClient(host, serial, port)

        coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name=f"Terma {serial}",
            update_method=make_update(client),
            update_interval=UPDATE_INTERVAL,
        )

        await coordinator.async_config_entry_first_refresh()

        devices.append(
            TermaDevice(
                serial=serial,
                host=host,
                port=port,
                client=client,
                coordinator=coordinator,
            )
        )

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = devices

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok
