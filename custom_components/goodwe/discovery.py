"""Recover an existing inverter by its serial number after an IP change."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from ipaddress import IPv4Address, IPv4Interface
from time import monotonic
from typing import Any

from homeassistant.components.network import async_get_adapters
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_PROTOCOL
from homeassistant.core import HomeAssistant

from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT

from .const import (
    CONF_KEEP_ALIVE,
    CONF_MODBUS_ID,
    CONF_MODEL_FAMILY,
    DEFAULT_MODBUS_ID,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)
DISCOVERY_PORT = 48899
DISCOVERY_REQUEST = b"WIFIKIT-214028-READ"
DISCOVERY_SECONDS = 2
RECOVERY_SECONDS = 20
RECOVERY_INTERVAL = 60
MAX_CANDIDATES = 16


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collect well-formed replies from the interface's local IPv4 network."""

    def __init__(self, interface: IPv4Interface) -> None:
        self.interface = interface
        self.hosts: set[str] = set()

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > 256 or len(self.hosts) >= MAX_CANDIDATES:
            return
        try:
            fields = data.decode("ascii").strip().split(",")
            if len(fields) != 3 or not re.fullmatch(r"[0-9a-fA-F]{12}", fields[1]):
                return
            host = IPv4Address(fields[0])
            if (
                str(host) != addr[0]
                or addr[1] != DISCOVERY_PORT
                or host not in self.interface.network
                or host
                in (
                    self.interface.network.network_address,
                    self.interface.network.broadcast_address,
                    self.interface.ip,
                )
                or host.is_multicast
                or host.is_unspecified
            ):
                return
        except (UnicodeError, ValueError):
            return
        self.hosts.add(str(host))


async def _async_search_interface(interface: IPv4Interface) -> set[str]:
    protocol = _DiscoveryProtocol(interface)
    transport = None
    try:
        transport, _ = await asyncio.get_running_loop().create_datagram_endpoint(
            lambda: protocol,
            family=socket.AF_INET,
            local_addr=(str(interface.ip), 0),
            allow_broadcast=True,
        )
        transport.sendto(
            DISCOVERY_REQUEST,
            (str(interface.network.broadcast_address), DISCOVERY_PORT),
        )
        await asyncio.sleep(DISCOVERY_SECONDS)
    except OSError:
        _LOGGER.debug("GoodWe discovery unavailable on %s", interface, exc_info=True)
    finally:
        if transport is not None:
            transport.close()
    return protocol.hosts


async def async_discover_hosts(hass: HomeAssistant) -> list[str]:
    """Search only discovery interfaces enabled in Home Assistant networking."""
    interfaces = set()
    for adapter in await async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for address in adapter["ipv4"]:
            interface = IPv4Interface(
                f"{address['address']}/{address['network_prefix']}"
            )
            if (
                interface.network.prefixlen < 31
                and not interface.ip.is_loopback
                and not interface.ip.is_link_local
            ):
                interfaces.add(interface)
    results = await asyncio.gather(
        *(_async_search_interface(interface) for interface in interfaces)
    )
    return sorted(set().union(*results))[:MAX_CANDIDATES]


def validate_serial(entry: ConfigEntry, inverter: Inverter) -> None:
    """Never reuse an existing entry for a different physical inverter."""
    if entry.unique_id and inverter.serial_number != entry.unique_id:
        raise InverterError("The inverter serial does not match this config entry")


async def async_recover_inverter(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[Inverter, dict[str, Any]] | None:
    """Find the existing serial, verify telemetry, then update only its host."""
    if not entry.unique_id:
        return None
    attempts = hass.data.setdefault(f"{DOMAIN}_rediscovery", {})
    now = monotonic()
    if now - attempts.get(entry.entry_id, float("-inf")) < RECOVERY_INTERVAL:
        return None
    attempts[entry.entry_id] = now

    protocol = entry.options.get(CONF_PROTOCOL, entry.data.get(CONF_PROTOCOL, "UDP"))
    port = entry.options.get(
        CONF_PORT,
        entry.data.get(
            CONF_PORT, GOODWE_TCP_PORT if protocol == "TCP" else GOODWE_UDP_PORT
        ),
    )
    family = entry.options.get(CONF_MODEL_FAMILY, entry.data[CONF_MODEL_FAMILY])
    comm_addr = entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)
    try:
        async with asyncio.timeout(RECOVERY_SECONDS):
            for host in await async_discover_hosts(hass):
                try:
                    inverter = await connect(
                        host=host,
                        port=port,
                        family=family,
                        comm_addr=comm_addr,
                        timeout=2,
                        retries=0,
                    )
                    validate_serial(entry, inverter)
                    data = await inverter.read_runtime_data()
                except (InverterError, OSError):
                    continue
                inverter.set_keep_alive(entry.options.get(CONF_KEEP_ALIVE, False))
                options = dict(entry.options)
                if CONF_HOST in options:
                    options[CONF_HOST] = host
                old_host = entry.options.get(CONF_HOST, entry.data[CONF_HOST])
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, CONF_HOST: host},
                    options=options,
                )
                if old_host != host:
                    _LOGGER.info("Rediscovered existing GoodWe inverter at %s", host)
                return inverter, data
    except (TimeoutError, OSError):
        _LOGGER.debug("GoodWe rediscovery did not find the configured inverter")
    return None
