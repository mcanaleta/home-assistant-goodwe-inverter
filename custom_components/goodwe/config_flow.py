"""Config flow to configure Goodwe inverters using their local API."""

from __future__ import annotations

import logging
from typing import Any

import voluptuous as vol
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_PORT,
    CONF_PROTOCOL,
    CONF_SCAN_INTERVAL,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.device_registry import format_mac
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

from goodwe import Inverter, InverterError
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT

from .const import (
    CONF_KEEP_ALIVE,
    CONF_MODBUS_ID,
    CONF_MODEL_FAMILY,
    CONF_NETWORK_RETRIES,
    CONF_NETWORK_TIMEOUT,
    DEFAULT_MODBUS_ID,
    DEFAULT_NAME,
    DEFAULT_NETWORK_RETRIES,
    DEFAULT_NETWORK_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .discovery import (
    DiscoveredInverter,
    async_discover_inverters,
    async_probe_inverter,
)
from .helpers import (
    async_detect_inverter_port,
    async_update_endpoint,
    entry_endpoint_data,
    validate_serial,
)

PROTOCOL_CHOICES = ["UDP", "TCP"]
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PROTOCOL, default="UDP"): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_MODEL_FAMILY, default="none"): vol.In(
            ["none", "ET", "DT", "ES"]
        ),
    }
)
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT): vol.All(cv.positive_int, vol.Range(min=1, max=65535)),
        vol.Required(CONF_PROTOCOL): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_KEEP_ALIVE): cv.boolean,
        vol.Required(CONF_MODEL_FAMILY): vol.In(["none", "ET", "DT", "ES"]),
        vol.Optional(CONF_SCAN_INTERVAL): vol.All(cv.positive_int, vol.Range(min=1)),
        vol.Optional(CONF_MODBUS_ID): vol.All(cv.positive_int, vol.Range(max=247)),
        vol.Optional(CONF_NETWORK_RETRIES): cv.positive_int,
        vol.Optional(CONF_NETWORK_TIMEOUT): vol.All(cv.positive_int, vol.Range(min=1)),
    }
)

_LOGGER = logging.getLogger(__name__)


class OptionsFlowHandler(OptionsFlow):
    """Options for the component."""

    def __init__(self, config_entry: ConfigEntry) -> None:
        """Init object."""
        self.entry = config_entry

    async def async_step_init(self, user_input: dict | None = None) -> ConfigFlowResult:
        """Manage the options."""
        errors = {}
        if user_input is not None:
            effective = {**self.entry.data, **self.entry.options}
            effective.setdefault(CONF_PROTOCOL, "UDP")
            effective.setdefault(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)
            effective[CONF_PORT] = effective.get(CONF_PORT) or (
                GOODWE_TCP_PORT
                if effective[CONF_PROTOCOL] == "TCP"
                else GOODWE_UDP_PORT
            )
            if any(
                user_input.get(key) != effective.get(key)
                for key in (
                    CONF_HOST,
                    CONF_PORT,
                    CONF_PROTOCOL,
                    CONF_MODEL_FAMILY,
                    CONF_MODBUS_ID,
                )
                if key in user_input
            ):
                port = user_input.get(CONF_PORT) or (
                    GOODWE_TCP_PORT
                    if user_input[CONF_PROTOCOL] == "TCP"
                    else GOODWE_UDP_PORT
                )
                try:
                    inverter, _ = await async_detect_inverter_port(
                        user_input[CONF_HOST],
                        family=user_input[CONF_MODEL_FAMILY],
                        preferred_port=port,
                        allow_fallback=False,
                        comm_addr=user_input.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID),
                    )
                    validate_serial(self.entry, inverter)
                except InverterError, OSError:
                    errors[CONF_HOST] = "connection_error"
                else:
                    return self.async_create_entry(title="", data=user_input)
            else:
                return self.async_create_entry(title="", data=user_input)

        host = self.entry.options.get(CONF_HOST, self.entry.data[CONF_HOST])
        port = self.entry.options.get(CONF_PORT, self.entry.data.get(CONF_PORT))
        protocol = self.entry.options.get(
            CONF_PROTOCOL, self.entry.data.get(CONF_PROTOCOL, "UDP")
        )
        keep_alive = self.entry.options.get(CONF_KEEP_ALIVE, False)
        model_family = self.entry.options.get(
            CONF_MODEL_FAMILY, self.entry.data[CONF_MODEL_FAMILY]
        )
        network_retries = self.entry.options.get(
            CONF_NETWORK_RETRIES, DEFAULT_NETWORK_RETRIES
        )
        network_timeout = self.entry.options.get(
            CONF_NETWORK_TIMEOUT, DEFAULT_NETWORK_TIMEOUT
        )
        modbus_id = self.entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)

        return self.async_show_form(
            step_id="init",
            errors=errors,
            data_schema=self.add_suggested_values_to_schema(
                OPTIONS_SCHEMA,
                {
                    CONF_HOST: host,
                    CONF_PORT: port,
                    CONF_PROTOCOL: protocol,
                    CONF_KEEP_ALIVE: keep_alive,
                    CONF_MODEL_FAMILY: model_family,
                    CONF_SCAN_INTERVAL: self.entry.options.get(
                        CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
                    ),
                    CONF_NETWORK_RETRIES: network_retries,
                    CONF_NETWORK_TIMEOUT: network_timeout,
                    CONF_MODBUS_ID: modbus_id,
                },
            ),
        )


class GoodweFlowHandler(ConfigFlow, domain=DOMAIN):
    """Handle a Goodwe config flow."""

    VERSION = 2

    def __init__(self) -> None:
        self._discovered: dict[str, DiscoveredInverter] = {}
        self._candidate: tuple[Inverter, str, int] | None = None
        self._mac: str | None = None

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> OptionsFlowHandler:
        """Get the options flow."""
        return OptionsFlowHandler(config_entry)

    async def async_handle_successful_connection(
        self,
        inverter: Inverter,
        host: str,
        port: int,
        protocol: str,
    ) -> ConfigFlowResult:
        """Handle a successful connection storing it's values on the entry data."""
        validate_serial(None, inverter)
        await self.async_set_unique_id(inverter.serial_number)
        self._update_existing_entry(host, port)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=DEFAULT_NAME,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_PROTOCOL: protocol,
                CONF_MODEL_FAMILY: type(inverter).__name__,
                **({CONF_MAC: self._mac} if self._mac else {}),
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Discover first, with manual configuration available as a fallback."""
        if user_input is not None:
            return await self.async_step_manual(user_input)
        self._discovered = await async_discover_inverters(self.hass)
        if not self._discovered:
            return await self.async_step_manual()
        return await self.async_step_select()

    async def async_step_select(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user choose among verified devices without typing an IP."""
        if user_input is not None:
            host = user_input["device"]
            if host == "manual":
                return await self.async_step_manual()
            discovered = self._discovered[host]
            self._mac = discovered.mac
            return await self._async_offer(discovered.inverter, host, discovered.port)
        choices = {
            host: f"{found.inverter.model_name} — {found.inverter.serial_number} ({host})"
            for host, found in self._discovered.items()
        }
        choices["manual"] = "Manual"
        return self.async_show_form(
            step_id="select",
            data_schema=vol.Schema({vol.Required("device"): vol.In(choices)}),
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Handle HA's automatic discovery, verifying the actual inverter first."""
        result = await async_probe_inverter(discovery_info.ip)
        if result is None:
            return self.async_abort(reason="not_goodwe")
        self._mac = format_mac(discovery_info.macaddress)
        return await self._async_offer(result[0], discovery_info.ip, result[1])

    def _update_existing_entry(self, host: str, port: int) -> None:
        """Merge verified network information without altering serial identity."""
        if entry := self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, self.unique_id
        ):
            async_update_endpoint(self.hass, entry, host, port, self._mac)

    async def _async_offer(
        self, inverter: Inverter, host: str, port: int
    ) -> ConfigFlowResult:
        """Deduplicate by serial and ask for confirmation before creating an entry."""
        validate_serial(None, inverter)
        await self.async_set_unique_id(inverter.serial_number)
        self._update_existing_entry(host, port)
        self._abort_if_unique_id_configured()
        self._candidate = (inverter, host, port)
        self._set_confirm_only()
        self.context["title_placeholders"] = {"name": inverter.model_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm the device, checking that DHCP has not reassigned its address."""
        assert self._candidate is not None
        inverter, host, port = self._candidate
        if user_input is not None:
            result = await async_probe_inverter(
                host, family=type(inverter).__name__, preferred_port=port
            )
            if result is None:
                return self.async_show_form(
                    step_id="confirm",
                    data_schema=vol.Schema({}),
                    errors={"base": "connection_error"},
                    description_placeholders={
                        "model": inverter.model_name,
                        "serial": inverter.serial_number,
                        "host": host,
                    },
                )
            if result[0].serial_number != self.unique_id:
                return self.async_abort(reason="device_changed")
            return await self.async_handle_successful_connection(
                result[0],
                host,
                result[1],
                "UDP" if result[1] == GOODWE_UDP_PORT else "TCP",
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            description_placeholders={
                "model": inverter.model_name,
                "serial": inverter.serial_number,
                "host": host,
            },
        )

    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Keep explicit host configuration for loggers without discovery support."""
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            protocol = user_input[CONF_PROTOCOL]
            model_family = user_input[CONF_MODEL_FAMILY]
            port = user_input.get(
                CONF_PORT, GOODWE_UDP_PORT if protocol == "UDP" else GOODWE_TCP_PORT
            )

            try:
                _LOGGER.debug(
                    "Goodwe connecting to %s:%s protocol=%s family=%s",
                    host,
                    port,
                    protocol,
                    model_family,
                )
                inverter, port = await async_detect_inverter_port(
                    host, family=model_family, preferred_port=port, allow_fallback=False
                )
            except InverterError, OSError:
                errors[CONF_HOST] = "connection_error"
            else:
                return await self.async_handle_successful_connection(
                    inverter, host, port, protocol
                )
        return self.async_show_form(
            step_id="manual", data_schema=CONFIG_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Verify a replacement endpoint while preserving the configured serial."""
        entry = self._get_reconfigure_entry()
        errors = {}
        if user_input is not None:
            host = user_input[CONF_HOST]
            port = (
                GOODWE_UDP_PORT
                if user_input[CONF_PROTOCOL] == "UDP"
                else GOODWE_TCP_PORT
            )
            try:
                inverter, port = await async_detect_inverter_port(
                    host,
                    family=user_input[CONF_MODEL_FAMILY],
                    preferred_port=port,
                    comm_addr=entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID),
                    allow_fallback=False,
                )
            except InverterError, OSError:
                errors[CONF_HOST] = "connection_error"
            else:
                await self.async_set_unique_id(inverter.serial_number)
                self._abort_if_unique_id_mismatch()
                data, options = entry_endpoint_data(entry, host, port)
                return self.async_update_and_abort(entry, data=data, options=options)
        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                CONFIG_SCHEMA, {**entry.data, **entry.options}
            ),
            errors=errors,
        )
