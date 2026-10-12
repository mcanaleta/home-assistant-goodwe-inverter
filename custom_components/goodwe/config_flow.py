"""Config flow to configure Goodwe inverters using their local API."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT
from homeassistant.config_entries import (
    SOURCE_IGNORE,
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

PROTOCOL_CHOICES = ["UDP", "TCP"]
CONFIG_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Required(CONF_PROTOCOL, default="UDP"): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_MODEL_FAMILY, default="none"): str,
    }
)
OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_HOST): str,
        vol.Optional(CONF_PORT): int,
        vol.Required(CONF_PROTOCOL): vol.In(PROTOCOL_CHOICES),
        vol.Required(CONF_KEEP_ALIVE): cv.boolean,
        vol.Required(CONF_MODEL_FAMILY): str,
        vol.Optional(CONF_SCAN_INTERVAL): int,
        vol.Optional(CONF_MODBUS_ID): int,
        vol.Optional(CONF_NETWORK_RETRIES): cv.positive_int,
        vol.Optional(CONF_NETWORK_TIMEOUT): cv.positive_int,
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
        if user_input is not None:
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
        self._dhcp_mac: str | None = None
        self._dhcp_candidate: tuple[Inverter, str, int] | None = None

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
        await self.async_set_unique_id(inverter.serial_number)
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=DEFAULT_NAME,
            data={
                CONF_HOST: host,
                CONF_PORT: port,
                CONF_PROTOCOL: protocol,
                CONF_MODEL_FAMILY: type(inverter).__name__,
                **({CONF_MAC: self._dhcp_mac} if self._dhcp_mac else {}),
            },
        )

    async def async_step_dhcp(
        self, discovery_info: DhcpServiceInfo
    ) -> ConfigFlowResult:
        """Verify a DHCP candidate before offering or updating an inverter."""
        self._dhcp_mac = format_mac(discovery_info.macaddress)
        known = next(
            (
                entry
                for entry in self._async_current_entries()
                if entry.data.get(CONF_MAC) == self._dhcp_mac
            ),
            None,
        )
        settings = {**known.data, **known.options} if known else {}
        result = await self._async_identify_dhcp_inverter(
            discovery_info.ip,
            family=settings.get(CONF_MODEL_FAMILY),
            preferred_port=settings.get(CONF_PORT),
            comm_addr=settings.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID),
        )
        if result is None:
            return self.async_abort(reason="not_goodwe")
        inverter, port = result
        await self.async_set_unique_id(inverter.serial_number)
        self._async_update_dhcp_entry(discovery_info.ip, port)
        self._abort_if_unique_id_configured()
        self._dhcp_candidate = (inverter, discovery_info.ip, port)
        self._set_confirm_only()
        return await self.async_step_confirm()

    @callback
    def _async_update_dhcp_entry(self, host: str, port: int) -> None:
        """Preserve serial identity and unrelated settings when an address changes."""
        entry = self.hass.config_entries.async_entry_for_domain_unique_id(
            DOMAIN, self.unique_id
        )
        if entry is None or entry.source == SOURCE_IGNORE:
            return
        endpoint = {
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PROTOCOL: "UDP" if port == GOODWE_UDP_PORT else "TCP",
        }
        data = {**entry.data, **endpoint, CONF_MAC: self._dhcp_mac}
        options = dict(entry.options)
        for key, value in endpoint.items():
            if key in options:
                options[key] = value
        self.hass.config_entries.async_update_entry(entry, data=data, options=options)

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Recheck identity before the user confirms a newly discovered device."""
        assert self._dhcp_candidate is not None
        inverter, host, port = self._dhcp_candidate
        errors = {}
        if user_input is not None:
            result = await self._async_identify_dhcp_inverter(
                host,
                family=type(inverter).__name__,
                preferred_port=port,
            )
            if result is None:
                errors["base"] = "connection_error"
            elif result[0].serial_number != self.unique_id:
                return self.async_abort(reason="device_changed")
            else:
                self._async_update_dhcp_entry(host, result[1])
                return await self.async_handle_successful_connection(
                    result[0],
                    host,
                    result[1],
                    "UDP" if result[1] == GOODWE_UDP_PORT else "TCP",
                )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema({}),
            errors=errors,
            description_placeholders={
                "model": inverter.model_name,
                "serial": inverter.serial_number,
                "host": host,
            },
        )

    @staticmethod
    async def _async_identify_dhcp_inverter(
        host: str,
        *,
        family: str | None = None,
        preferred_port: int | None = None,
        comm_addr: int = DEFAULT_MODBUS_ID,
    ) -> tuple[Inverter, int] | None:
        """Bound read-only identification of shared-vendor DHCP candidates."""
        ports = dict.fromkeys((preferred_port, GOODWE_UDP_PORT, GOODWE_TCP_PORT))
        families = [family] if family and family != "none" else ["ET", "DT", "ES"]
        task = asyncio.current_task()
        assert task is not None
        try:
            async with asyncio.timeout(8):
                for port in ports:
                    if port is None:
                        continue
                    for candidate_family in families:
                        try:
                            inverter = await connect(
                                host=host,
                                port=port,
                                family=candidate_family,
                                comm_addr=comm_addr,
                                timeout=1,
                                retries=0,
                                do_discover=False,
                            )
                        except (InverterError, OSError):
                            # goodwe 0.4.10 can convert cancellation to InverterError.
                            if task.cancelling():
                                raise asyncio.CancelledError
                            continue
                        if task.cancelling():
                            raise asyncio.CancelledError
                        if inverter.serial_number and inverter.serial_number.strip():
                            return inverter, port
        except TimeoutError:
            pass
        return None

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle a flow initialized by the user."""
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
                inverter = await connect(
                    host=host, port=port, family=model_family, retries=10
                )
            except InverterError:
                errors[CONF_HOST] = "connection_error"
            else:
                return await self.async_handle_successful_connection(
                    inverter, host, port, protocol
                )
        return self.async_show_form(
            step_id="user", data_schema=CONFIG_SCHEMA, errors=errors
        )

    @staticmethod
    async def async_detect_inverter_port(
        host: str,
    ) -> tuple[Inverter, int]:
        """Detects the port of the Inverter."""
        port = GOODWE_UDP_PORT
        try:
            inverter = await connect(host=host, port=port, retries=10)
        except InverterError:
            port = GOODWE_TCP_PORT
            inverter = await connect(host=host, port=port, retries=10)
        return inverter, port
