"""Config flow for Terma Smart integration."""
from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
try:
    from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
except ImportError:
    try:
        from homeassistant.components.zeroconf import ZeroconfServiceInfo
    except ImportError:
        from typing import Any as ZeroconfServiceInfo
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import CONF_DEVICES, CONF_SERIAL, DEFAULT_PORT, DOMAIN
from .terma_client import TermaClient, TermaError

_LOGGER = logging.getLogger(__name__)

INTEGRATION_UNIQUE_ID = "terma_devices"

STEP_USER_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): cv.string,
        vol.Required(CONF_SERIAL): cv.string,
        vol.Optional(CONF_PORT, default=DEFAULT_PORT): vol.Coerce(int),
    }
)


def _existing_entry(flow: config_entries.ConfigFlow) -> ConfigEntry | None:
    """Return the single Terma config entry if one exists."""
    entries = flow._async_current_entries()  # noqa: SLF001
    return entries[0] if entries else None


def _device_already_in_entry(entry: ConfigEntry, serial: str) -> bool:
    """Return True if `serial` is already in this entry's device list."""
    return any(
        d.get(CONF_SERIAL) == serial for d in entry.data.get(CONF_DEVICES, [])
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Terma Smart."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the config flow."""
        self._discovery_info: dict[str, Any] = {}

    async def _add_or_create(self, device: dict[str, Any]) -> FlowResult:
        """Append `device` to an existing entry or create a new entry."""
        entry = _existing_entry(self)

        if entry is not None:
            if _device_already_in_entry(entry, device[CONF_SERIAL]):
                return self.async_abort(reason="already_configured")

            new_devices = [*entry.data.get(CONF_DEVICES, []), device]
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICES: new_devices}
            )
            await self.hass.config_entries.async_reload(entry.entry_id)
            return self.async_abort(reason="device_added")

        await self.async_set_unique_id(INTEGRATION_UNIQUE_ID)
        self._abort_if_unique_id_configured()
        return self.async_create_entry(
            title="Terma",
            data={CONF_DEVICES: [device]},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: dict[str, str] = {}
        if user_input is not None:
            entry = _existing_entry(self)
            if entry is not None and _device_already_in_entry(
                entry, user_input[CONF_SERIAL]
            ):
                return self.async_abort(reason="already_configured")

            try:
                client = TermaClient(
                    host=user_input[CONF_HOST],
                    serial=user_input[CONF_SERIAL],
                    port=user_input.get(CONF_PORT, DEFAULT_PORT),
                )
                await self.hass.async_add_executor_job(client.get_telemetry)
            except TermaError:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                return await self._add_or_create(
                    {
                        CONF_HOST: user_input[CONF_HOST],
                        CONF_PORT: user_input.get(CONF_PORT, DEFAULT_PORT),
                        CONF_SERIAL: user_input[CONF_SERIAL],
                    }
                )

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_zeroconf(
        self, discovery_info: ZeroconfServiceInfo
    ) -> FlowResult:
        """Handle zeroconf discovery."""
        host = discovery_info.host
        port = discovery_info.port or DEFAULT_PORT
        # Instance name is usually serial number with 'V' instead of '#'
        instance = discovery_info.name.split(".")[0]
        serial = instance.replace("V", "#")

        entry = _existing_entry(self)
        if entry is not None and _device_already_in_entry(entry, serial):
            # Update the host/port in case the device moved on the network.
            new_devices = [
                {**d, CONF_HOST: host, CONF_PORT: port}
                if d.get(CONF_SERIAL) == serial
                else d
                for d in entry.data.get(CONF_DEVICES, [])
            ]
            self.hass.config_entries.async_update_entry(
                entry, data={**entry.data, CONF_DEVICES: new_devices}
            )
            return self.async_abort(reason="already_configured")

        self._discovery_info = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_SERIAL: serial,
        }
        self.context["title_placeholders"] = {"name": serial}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle confirmation of a discovered device."""
        if user_input is not None:
            return await self._add_or_create(self._discovery_info)

        return self.async_show_form(
            step_id="confirm",
            description_placeholders={"name": self._discovery_info[CONF_SERIAL]},
        )
