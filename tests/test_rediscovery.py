"""Run with unittest inside a Home Assistant 2026.10 Python environment."""

import asyncio
import unittest
from ipaddress import IPv4Interface
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from goodwe import InverterError, RequestFailedException
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import UpdateFailed

from custom_components.goodwe import async_check_port, async_setup_entry, discovery
from custom_components.goodwe.coordinator import GoodweUpdateCoordinator

SERIAL = "EXAMPLE-ET-SERIAL"


def entry(**kwargs):
    values = {
        "entry_id": "existing-entry",
        "unique_id": SERIAL,
        "data": {
            "host": "192.168.1.50",
            "port": 8899,
            "model_family": "ET",
            "extra": 7,
        },
        "options": {"scan_interval": 30},
        "state": ConfigEntryState.LOADED,
    }
    values.update(kwargs)
    return SimpleNamespace(**values)


def inverter(serial=SERIAL):
    inv = MagicMock()
    inv.serial_number = serial
    inv.read_runtime_data = AsyncMock(return_value={"ppv": 1234})
    return inv


class DiscoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_reply_validation_and_multiple_candidates(self):
        protocol = discovery._DiscoveryProtocol(IPv4Interface("192.168.1.115/24"))
        source = ("192.168.1.120", 48899)
        for data, addr in [
            (b"192.168.1.120,001122334455,Solar-WiFi", source),
            (b"192.168.1.121,AABBCCDDEEFF,Other", ("192.168.1.121", 48899)),
            (b"192.168.1.120,001122334455,Solar-WiFi", source),
            (b"192.168.1.122,001122334455,Spoofed", source),
            (b"192.168.2.120,001122334455,Other-LAN", ("192.168.2.120", 48899)),
            (b"192.168.1.120,not-a-mac,Solar-WiFi", source),
            (b"invalid", source),
            (b"192.168.1.120,001122334455,Solar-WiFi", (source[0], 80)),
        ]:
            protocol.datagram_received(data, addr)
        self.assertEqual(protocol.hosts, {"192.168.1.120", "192.168.1.121"})

    async def test_only_enabled_interfaces(self):
        hass = MagicMock()
        adapters = [
            {
                "enabled": True,
                "ipv4": [{"address": "192.168.1.115", "network_prefix": 24}],
            },
            {
                "enabled": False,
                "ipv4": [{"address": "192.168.10.254", "network_prefix": 24}],
            },
        ]
        with (
            patch.object(
                discovery, "async_get_adapters", AsyncMock(return_value=adapters)
            ),
            patch.object(
                discovery,
                "_async_search_interface",
                AsyncMock(return_value={"192.168.1.120"}),
            ) as search,
        ):
            self.assertEqual(
                await discovery.async_discover_hosts(hass), ["192.168.1.120"]
            )
        search.assert_awaited_once_with(IPv4Interface("192.168.1.115/24"))

    async def test_broadcast_enabled_and_transport_closed(self):
        transport = MagicMock()
        loop = MagicMock()
        loop.create_datagram_endpoint = AsyncMock(return_value=(transport, None))
        with (
            patch.object(discovery.asyncio, "get_running_loop", return_value=loop),
            patch.object(discovery.asyncio, "sleep", AsyncMock()),
        ):
            await discovery._async_search_interface(IPv4Interface("192.168.1.115/24"))
        self.assertTrue(
            loop.create_datagram_endpoint.call_args.kwargs["allow_broadcast"]
        )
        transport.sendto.assert_called_once_with(
            discovery.DISCOVERY_REQUEST, ("192.168.1.255", 48899)
        )
        transport.close.assert_called_once()

    async def test_recover_correct_serial_preserve_entry_and_options(self):
        hass = MagicMock(data={})
        configured = entry(
            options={"host": "192.168.1.50", "keep_alive": True, "scan_interval": 30}
        )
        wrong, right = inverter("OTHER-SERIAL"), inverter()
        with (
            patch.object(
                discovery,
                "async_discover_hosts",
                AsyncMock(return_value=["192.168.1.119", "192.168.1.120"]),
            ),
            patch.object(discovery, "connect", AsyncMock(side_effect=[wrong, right])),
        ):
            recovered = await discovery.async_recover_inverter(hass, configured)
        self.assertIs(recovered[0], right)
        self.assertEqual(recovered[1], {"ppv": 1234})
        wrong.read_runtime_data.assert_not_awaited()
        right.write_setting.assert_not_called()
        right.set_keep_alive.assert_called_once_with(True)
        hass.config_entries.async_update_entry.assert_called_once_with(
            configured,
            data={**configured.data, "host": "192.168.1.120"},
            options={**configured.options, "host": "192.168.1.120"},
        )
        self.assertEqual(configured.unique_id, SERIAL)
        self.assertEqual(configured.entry_id, "existing-entry")

    async def test_wrong_serial_or_unreadable_telemetry_does_not_update(self):
        for candidate in [inverter("OTHER"), inverter()]:
            candidate.read_runtime_data.side_effect = InverterError("offline")
            hass = MagicMock(data={})
            with (
                patch.object(
                    discovery,
                    "async_discover_hosts",
                    AsyncMock(return_value=["192.168.1.120"]),
                ),
                patch.object(discovery, "connect", AsyncMock(return_value=candidate)),
            ):
                self.assertIsNone(await discovery.async_recover_inverter(hass, entry()))
            hass.config_entries.async_update_entry.assert_not_called()

    async def test_missing_serial_and_cooldown_do_not_search(self):
        hass = MagicMock(data={})
        configured = entry()
        with patch.object(
            discovery, "async_discover_hosts", AsyncMock(return_value=[])
        ) as search:
            self.assertIsNone(
                await discovery.async_recover_inverter(hass, entry(unique_id=None))
            )
            await discovery.async_recover_inverter(hass, configured)
            await discovery.async_recover_inverter(hass, configured)
        search.assert_awaited_once()

    async def test_cancellation_is_not_swallowed(self):
        with (
            patch.object(
                discovery,
                "async_discover_hosts",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await discovery.async_recover_inverter(MagicMock(data={}), entry())

    async def test_port_fallback_checks_serial_before_updating(self):
        hass = MagicMock()
        with (
            patch(
                "custom_components.goodwe.GoodweFlowHandler.async_detect_inverter_port",
                AsyncMock(return_value=(inverter("OTHER"), 502)),
            ),
            self.assertRaises(InverterError),
        ):
            await async_check_port(hass, entry(), "192.168.1.50")
        hass.config_entries.async_update_entry.assert_not_called()

    async def test_startup_discovers_after_old_host_fails(self):
        configured = entry(
            title="GoodWe", async_on_unload=MagicMock(), add_update_listener=MagicMock()
        )
        hass = MagicMock(data={})
        hass.config_entries.async_forward_entry_setups = AsyncMock()
        coordinator = MagicMock(async_config_entry_first_refresh=AsyncMock())
        found = inverter()
        with (
            patch(
                "custom_components.goodwe.connect",
                AsyncMock(side_effect=InverterError("offline")),
            ),
            patch(
                "custom_components.goodwe.async_check_port",
                AsyncMock(side_effect=InverterError("offline")),
            ),
            patch(
                "custom_components.goodwe.async_recover_inverter",
                AsyncMock(return_value=(found, {})),
            ),
            patch(
                "custom_components.goodwe.GoodweUpdateCoordinator",
                return_value=coordinator,
            ),
            patch("custom_components.goodwe.async_setup_services", AsyncMock()),
        ):
            self.assertTrue(await async_setup_entry(hass, configured))
        self.assertIs(configured.runtime_data.inverter, found)

    async def test_startup_no_match_stays_retryable(self):
        with (
            patch(
                "custom_components.goodwe.connect",
                AsyncMock(side_effect=InverterError("offline")),
            ),
            patch(
                "custom_components.goodwe.async_check_port",
                AsyncMock(side_effect=InverterError("offline")),
            ),
            patch(
                "custom_components.goodwe.async_recover_inverter",
                AsyncMock(return_value=None),
            ),
            self.assertRaises(ConfigEntryNotReady),
        ):
            await async_setup_entry(MagicMock(data={}), entry())

    async def test_generic_failures_recover_only_after_three(self):
        obj = object.__new__(GoodweUpdateCoordinator)
        obj.hass = MagicMock()
        obj.config_entry = entry()
        obj.inverter = inverter()
        obj.data = {}
        obj._polled_entities = {}
        obj._generic_failures = 0
        obj.inverter.read_runtime_data.side_effect = InverterError("offline")
        with patch(
            "custom_components.goodwe.coordinator.async_recover_inverter",
            AsyncMock(return_value=(inverter(), {"ppv": 1234})),
        ) as recover:
            for _ in range(2):
                with self.assertRaises(UpdateFailed):
                    await obj._async_update_data()
            recover.assert_not_awaited()
            self.assertEqual(await obj._async_update_data(), {"ppv": 1234})
            recover.assert_awaited_once()
            obj.inverter.read_runtime_data.side_effect = None
            await obj._async_update_data()
            self.assertEqual(obj._generic_failures, 0)

    async def test_runtime_recovery_and_isolated_packet_loss(self):
        obj = object.__new__(GoodweUpdateCoordinator)
        obj.hass = MagicMock()
        obj.config_entry = entry()
        obj.inverter = inverter()
        obj.data = {"ppv": 99}
        obj._polled_entities = {}
        obj._generic_failures = 0
        obj.inverter.read_runtime_data.side_effect = RequestFailedException(
            "offline", 1
        )
        with patch(
            "custom_components.goodwe.coordinator.async_recover_inverter",
            AsyncMock(return_value=(inverter(), {"ppv": 1234})),
        ) as recover:
            self.assertEqual(await obj._async_update_data(), {"ppv": 99})
            recover.assert_not_awaited()
            obj.inverter.read_runtime_data.side_effect = RequestFailedException(
                "offline", 3
            )
            self.assertEqual(await obj._async_update_data(), {"ppv": 1234})
            recover.assert_awaited_once_with(obj.hass, obj.config_entry)

    async def test_first_refresh_does_not_trigger_reload(self):
        obj = object.__new__(GoodweUpdateCoordinator)
        obj.hass = MagicMock()
        obj.config_entry = entry(state=ConfigEntryState.SETUP_IN_PROGRESS)
        with patch(
            "custom_components.goodwe.coordinator.async_recover_inverter", AsyncMock()
        ) as recover:
            self.assertIsNone(await obj._async_recover())
        recover.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
