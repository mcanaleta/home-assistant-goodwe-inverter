"""Shared connection and entry-update logic, independent of the config flow."""

from __future__ import annotations

import asyncio
from typing import Any

from homeassistant.config_entries import SOURCE_IGNORE, ConfigEntry
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_PORT, CONF_PROTOCOL
from homeassistant.core import HomeAssistant, callback

from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT

CONNECTION_SECONDS = 8


def raise_if_cancelled() -> None:
    """Restore cancellation if goodwe 0.4.10 converted it to an API error."""
    if (task := asyncio.current_task()) is not None and task.cancelling():
        raise asyncio.CancelledError


def validate_serial(entry: ConfigEntry | None, inverter: Inverter) -> None:
    """Require a usable serial and preserve the identity of existing entries."""
    if not inverter.serial_number or not inverter.serial_number.strip():
        raise InverterError("The inverter did not report a serial number")
    if entry and entry.unique_id and inverter.serial_number != entry.unique_id:
        raise InverterError("The inverter serial does not match this config entry")


async def async_detect_inverter_port(
    host: str,
    *,
    family: str | None = None,
    preferred_port: int | None = None,
    comm_addr: int = 0,
    allow_fallback: bool = True,
) -> tuple[Inverter, int]:
    """Bound read-only identification and keep library retries from hiding cancellation."""
    ports = (
        list(dict.fromkeys([preferred_port, GOODWE_UDP_PORT, GOODWE_TCP_PORT]))
        if allow_fallback
        else [preferred_port]
    )
    families = [family] if family and family != "none" else ["ET", "DT", "ES"]
    try:
        async with asyncio.timeout(CONNECTION_SECONDS):
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
                        )
                        raise_if_cancelled()
                        validate_serial(None, inverter)
                        return inverter, port
                    except InverterError, OSError:
                        raise_if_cancelled()
    except TimeoutError as err:
        raise InverterError("Inverter identification timed out") from err
    raise InverterError("No supported inverter responded")


@callback
def entry_endpoint_data(
    entry: ConfigEntry,
    host: str,
    port: int | None = None,
    mac: str | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge network information without dropping unrelated data or options."""
    data = {**entry.data, CONF_HOST: host}
    options = dict(entry.options)
    updates: dict[str, Any] = {CONF_HOST: host}
    if port is not None:
        updates.update(
            {
                CONF_PORT: port,
                CONF_PROTOCOL: "UDP" if port == GOODWE_UDP_PORT else "TCP",
            }
        )
    if mac is not None:
        updates[CONF_MAC] = mac
    data.update(updates)
    for key in (CONF_HOST, CONF_PORT, CONF_PROTOCOL):
        if key in options and key in updates:
            options[key] = updates[key]
    return data, options


@callback
def async_update_endpoint(
    hass: HomeAssistant,
    entry: ConfigEntry,
    host: str,
    port: int | None = None,
    mac: str | None = None,
) -> bool:
    """Update a known device; ignored entries remain untouched."""
    if entry.source == SOURCE_IGNORE:
        return False
    data, options = entry_endpoint_data(entry, host, port, mac)
    return hass.config_entries.async_update_entry(entry, data=data, options=options)
