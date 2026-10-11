"""Read-only local discovery and recovery of GoodWe inverter addresses."""

from __future__ import annotations

import asyncio
import logging
import re
import socket
from dataclasses import dataclass
from ipaddress import IPv4Address, IPv4Interface
from time import monotonic
from typing import Any

from homeassistant.components.network import async_get_adapters
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import format_mac

from goodwe import Inverter, InverterError

from .const import CONF_MODBUS_ID, CONF_MODEL_FAMILY, DEFAULT_MODBUS_ID, DOMAIN
from .helpers import (
    async_detect_inverter_port,
    async_update_endpoint,
    raise_if_cancelled,
    validate_serial,
)

_LOGGER = logging.getLogger(__name__)
DISCOVERY_PORT = 48899
DISCOVERY_REQUEST = b"WIFIKIT-214028-READ"
DISCOVERY_SECONDS = 2
RECOVERY_SECONDS = 20
CANDIDATE_SECONDS = 4
RECOVERY_INTERVAL = 60
MAX_CANDIDATES = 16
MAX_CONCURRENT_PROBES = 4
DATA_REDISCOVERY = f"{DOMAIN}_rediscovery"


@dataclass(frozen=True, slots=True)
class LoggerInfo:
    """A logger reply is a hint; the inverter API must verify its identity."""

    host: str
    mac: str
    name: str


@dataclass(frozen=True, slots=True)
class DiscoveredInverter:
    """A verified inverter endpoint and its optional network connection."""

    inverter: Inverter
    port: int
    mac: str | None = None


async def async_probe_inverter(
    host: str,
    *,
    family: str | None = None,
    preferred_port: int | None = None,
) -> tuple[Inverter, int] | None:
    """Bound identification of a candidate without sending control commands."""
    try:
        return await async_detect_inverter_port(
            host, family=family, preferred_port=preferred_port
        )
    except InverterError, OSError:
        raise_if_cancelled()
        return None


class _DiscoveryProtocol(asyncio.DatagramProtocol):
    """Collect well-formed replies from the interface's local IPv4 network."""

    def __init__(self, interface: IPv4Interface) -> None:
        self.interface = interface
        self.loggers: dict[str, LoggerInfo] = {}

    @property
    def hosts(self) -> set[str]:
        """Return deduplicated reply addresses."""
        return set(self.loggers)

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        if len(data) > 256 or len(self.loggers) >= MAX_CANDIDATES:
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
        except UnicodeError, ValueError:
            return
        self.loggers[str(host)] = LoggerInfo(
            str(host), format_mac(fields[1]), fields[2]
        )


async def _async_search_interface(interface: IPv4Interface) -> dict[str, LoggerInfo]:
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
    return protocol.loggers


async def async_discover_loggers(hass: HomeAssistant) -> dict[str, LoggerInfo]:
    """Search only discovery interfaces explicitly enabled in HA networking."""
    interfaces: set[IPv4Interface] = set()
    for adapter in await async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for address in adapter["ipv4"]:
            try:
                interface = IPv4Interface(
                    f"{address['address']}/{address['network_prefix']}"
                )
            except ValueError:
                continue
            if (
                interface.network.prefixlen < 31
                and not interface.ip.is_loopback
                and not interface.ip.is_link_local
                and not interface.ip.is_multicast
                and not interface.ip.is_unspecified
            ):
                interfaces.add(interface)
    results = await asyncio.gather(
        *(_async_search_interface(interface) for interface in interfaces)
    )
    loggers: dict[str, LoggerInfo] = {}
    for result in results:
        loggers.update(result)
    return {host: loggers[host] for host in sorted(loggers)[:MAX_CANDIDATES]}


async def async_discover_hosts(hass: HomeAssistant) -> list[str]:
    """Return addresses for callers that do not need logger metadata."""
    return list(await async_discover_loggers(hass))


async def async_discover_inverters(
    hass: HomeAssistant,
) -> dict[str, DiscoveredInverter]:
    """Limit concurrency and total duration while identifying logger replies."""
    verified: dict[str, DiscoveredInverter] = {}
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)

    async def identify(logger: LoggerInfo) -> None:
        async with semaphore:
            if result := await async_probe_inverter(logger.host):
                verified[logger.host] = DiscoveredInverter(*result, logger.mac)

    try:
        async with asyncio.timeout(RECOVERY_SECONDS):
            await asyncio.gather(
                *(
                    identify(logger)
                    for logger in (await async_discover_loggers(hass)).values()
                )
            )
    except TimeoutError, OSError:
        _LOGGER.debug("GoodWe discovery ended before all candidates responded")
    return verified


async def async_recover_inverter(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> tuple[Inverter, dict[str, Any]] | None:
    """Verify serial and telemetry, then atomically merge the endpoint of this entry."""
    if not entry.unique_id:
        return None
    attempts: dict[str, float] = hass.data.setdefault(DATA_REDISCOVERY, {})
    now = monotonic()
    if now - attempts.get(entry.entry_id, float("-inf")) < RECOVERY_INTERVAL:
        return None
    attempts[entry.entry_id] = now
    before_data, before_options = dict(entry.data), dict(entry.options)
    family = entry.options.get(CONF_MODEL_FAMILY, entry.data.get(CONF_MODEL_FAMILY))
    port = entry.options.get(CONF_PORT, entry.data.get(CONF_PORT))
    comm_addr = entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)
    semaphore = asyncio.Semaphore(MAX_CONCURRENT_PROBES)
    tasks: list[
        asyncio.Task[tuple[LoggerInfo, Inverter, int, dict[str, Any]] | None]
    ] = []

    async def verify(
        logger: LoggerInfo,
    ) -> tuple[LoggerInfo, Inverter, int, dict[str, Any]] | None:
        async with semaphore:
            try:
                async with asyncio.timeout(CANDIDATE_SECONDS):
                    inverter, new_port = await async_detect_inverter_port(
                        logger.host,
                        family=family,
                        preferred_port=port,
                        comm_addr=comm_addr,
                    )
                    validate_serial(entry, inverter)
                    data = await inverter.read_runtime_data()
                    raise_if_cancelled()
                    return logger, inverter, new_port, data
            except InverterError, OSError, TimeoutError:
                raise_if_cancelled()
                return None

    try:
        async with asyncio.timeout(RECOVERY_SECONDS):
            loggers = await async_discover_loggers(hass)
            tasks = [asyncio.create_task(verify(logger)) for logger in loggers.values()]
            for pending in asyncio.as_completed(tasks):
                if (result := await pending) is None:
                    continue
                logger, inverter, new_port, data = result
                if (
                    dict(entry.data) != before_data
                    or dict(entry.options) != before_options
                ):
                    return None
                async_update_endpoint(hass, entry, logger.host, new_port, logger.mac)
                _LOGGER.debug(
                    "Verified the configured GoodWe inverter at %s", logger.host
                )
                return inverter, data
    except TimeoutError, OSError:
        _LOGGER.debug("GoodWe rediscovery did not find the configured inverter")
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return None
