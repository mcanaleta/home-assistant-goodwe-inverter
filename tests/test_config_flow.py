"""Exercise initial discovery and DHCP flows with HA's real ConfigFlow class."""

import asyncio
import json
import unittest
from fnmatch import fnmatch
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

from goodwe import InverterError
from homeassistant import loader
from homeassistant.config_entries import SOURCE_DHCP, SOURCE_USER
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from test_rediscovery import SERIAL, entry, inverter

import custom_components
from custom_components.goodwe import config_flow, discovery, helpers

HOST = "192.168.1.120"


def flow(source=SOURCE_USER, entries=()):
    handler = config_flow.GoodweFlowHandler()
    handler.handler = "goodwe"
    handler.context = {"source": source}
    handler.hass = MagicMock()
    handler.hass.config_entries.async_entries.return_value = list(entries)
    handler.hass.config_entries.async_entry_for_domain_unique_id.side_effect = (
        lambda domain, serial: next(
            (entry for entry in entries if entry.unique_id == serial), None
        )
    )
    handler.hass.config_entries.flow.async_progress_by_handler.return_value = []
    return handler


class ConfigFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_initial_discovery_select_confirm_without_host_input(self):
        handler = flow()
        inv = inverter()
        inv.model_name = "GW10K-ET"
        with (
            patch.object(
                config_flow,
                "async_discover_inverters",
                AsyncMock(return_value={HOST: discovery.DiscoveredInverter(inv, 8899)}),
            ),
            patch.object(
                config_flow,
                "async_probe_inverter",
                AsyncMock(return_value=(inv, 8899)),
            ),
        ):
            result = await handler.async_step_user()
            self.assertEqual(result["step_id"], "select")
            self.assertEqual(result["data_schema"]({"device": HOST}), {"device": HOST})
            result = await handler.async_step_select({"device": HOST})
            self.assertEqual(result["step_id"], "confirm")
            self.assertEqual(result["description_placeholders"]["serial"], SERIAL)
            handler.hass.config_entries.async_update_entry.assert_not_called()
            result = await handler.async_step_confirm({})
        self.assertEqual(result["type"], "create_entry")
        self.assertEqual(result["data"]["host"], HOST)
        self.assertEqual(handler.unique_id, SERIAL)

    async def test_no_candidates_and_manual_choice_keep_manual_setup(self):
        handler = flow()
        with patch.object(
            config_flow, "async_discover_inverters", AsyncMock(return_value={})
        ):
            result = await handler.async_step_user()
        self.assertEqual(result["step_id"], "manual")
        result = await handler.async_step_select({"device": "manual"})
        self.assertEqual(result["step_id"], "manual")
        with patch.object(
            config_flow,
            "async_detect_inverter_port",
            AsyncMock(return_value=(inverter(), 8899)),
        ):
            result = await handler.async_step_manual(
                {"host": HOST, "protocol": "UDP", "model_family": "ET"}
            )
        self.assertEqual(result["type"], "create_entry")

    async def test_dhcp_offers_confirmation_and_stores_network_mac(self):
        handler = flow(SOURCE_DHCP)
        with patch.object(
            config_flow,
            "async_probe_inverter",
            AsyncMock(return_value=(inverter(), 502)),
        ):
            result = await handler.async_step_dhcp(
                DhcpServiceInfo(ip=HOST, hostname="", macaddress="E8FDF8123456")
            )
            self.assertEqual(result["step_id"], "confirm")
            handler.hass.config_entries.async_update_entry.assert_not_called()
            result = await handler.async_step_confirm({})
        self.assertEqual(result["data"]["mac"], "e8:fd:f8:12:34:56")
        self.assertEqual(result["data"]["protocol"], "TCP")
        self.assertEqual(result["data"]["port"], 502)

    async def test_dhcp_same_serial_updates_existing_entry_and_host_override(self):
        existing = entry(options={"host": "192.168.1.50", "scan_interval": 90})
        existing.source = SOURCE_USER
        handler = flow(SOURCE_DHCP, [existing])
        with (
            patch.object(
                config_flow,
                "async_probe_inverter",
                AsyncMock(return_value=(inverter(), 8899)),
            ),
            self.assertRaises(AbortFlow) as abort,
        ):
            await handler.async_step_dhcp(
                DhcpServiceInfo(
                    ip=HOST, hostname="solar-wifi", macaddress="E8FDF8123456"
                )
            )
        self.assertEqual(abort.exception.reason, "already_configured")
        handler.hass.config_entries.async_update_entry.assert_called_once_with(
            existing,
            data={
                **existing.data,
                "host": HOST,
                "protocol": "UDP",
                "mac": "e8:fd:f8:12:34:56",
            },
            options={**existing.options, "host": HOST},
        )

    async def test_dhcp_other_serial_does_not_update_existing(self):
        existing = entry()
        handler = flow(SOURCE_DHCP, [existing])
        with patch.object(
            config_flow,
            "async_probe_inverter",
            AsyncMock(return_value=(inverter("OTHER-SERIAL"), 8899)),
        ):
            result = await handler.async_step_dhcp(
                DhcpServiceInfo(
                    ip=HOST, hostname="solar-wifi", macaddress="E8FDF8123456"
                )
            )
        self.assertEqual(result["step_id"], "confirm")
        handler.hass.config_entries.async_update_entry.assert_not_called()

    async def test_shared_vendor_device_is_rejected_if_not_goodwe(self):
        handler = flow(SOURCE_DHCP)
        with patch.object(
            config_flow, "async_probe_inverter", AsyncMock(return_value=None)
        ):
            result = await handler.async_step_dhcp(
                DhcpServiceInfo(ip=HOST, hostname="", macaddress="E8FDF8123456")
            )
        self.assertEqual(result["reason"], "not_goodwe")
        handler.hass.config_entries.async_update_entry.assert_not_called()

    async def test_confirmation_rejects_reassigned_address_and_allows_retry(self):
        handler = flow(SOURCE_DHCP)
        with patch.object(
            config_flow,
            "async_probe_inverter",
            AsyncMock(return_value=(inverter(), 8899)),
        ):
            await handler.async_step_dhcp(
                DhcpServiceInfo(ip=HOST, hostname="", macaddress="E8FDF8123456")
            )
        with patch.object(
            config_flow, "async_probe_inverter", AsyncMock(return_value=None)
        ):
            result = await handler.async_step_confirm({})
        self.assertEqual(result["errors"], {"base": "connection_error"})
        with patch.object(
            config_flow,
            "async_probe_inverter",
            AsyncMock(return_value=(inverter("OTHER-SERIAL"), 8899)),
        ):
            result = await handler.async_step_confirm({})
        self.assertEqual(result["reason"], "device_changed")
        handler.hass.config_entries.async_update_entry.assert_not_called()

    async def test_simultaneous_flow_for_same_serial_is_deduplicated(self):
        handler = flow(SOURCE_DHCP)
        handler.hass.config_entries.flow.async_progress_by_handler.return_value = [
            {
                "flow_id": "other-flow",
                "context": {"source": SOURCE_USER, "unique_id": SERIAL},
            }
        ]
        with (
            patch.object(
                config_flow,
                "async_probe_inverter",
                AsyncMock(return_value=(inverter(), 8899)),
            ),
            self.assertRaises(AbortFlow) as abort,
        ):
            await handler.async_step_dhcp(
                DhcpServiceInfo(ip=HOST, hostname="", macaddress="E8FDF8123456")
            )
        self.assertEqual(abort.exception.reason, "already_in_progress")


class ProbeTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_tries_tcp_after_udp_read_failure(self):
        inv = inverter()
        with patch.object(
            helpers, "connect", AsyncMock(side_effect=[InverterError(), inv])
        ) as connect:
            self.assertEqual(
                await discovery.async_probe_inverter(HOST, family="ET"), (inv, 502)
            )
        self.assertEqual(
            [call.kwargs["port"] for call in connect.await_args_list], [8899, 502]
        )
        self.assertTrue(
            all(call.kwargs["retries"] == 0 for call in connect.await_args_list)
        )

    async def test_unidentified_candidate_and_cancellation(self):
        with patch.object(helpers, "connect", AsyncMock(return_value=inverter(""))):
            self.assertIsNone(await discovery.async_probe_inverter(HOST))
        with (
            patch.object(
                helpers, "connect", AsyncMock(side_effect=asyncio.CancelledError)
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await discovery.async_probe_inverter(HOST)

    async def test_udp_search_filters_non_goodwe_candidates(self):
        inv = inverter()
        with (
            patch.object(
                discovery,
                "async_discover_loggers",
                AsyncMock(
                    return_value={
                        host: discovery.LoggerInfo(
                            host, "00:11:22:33:44:55", "Solar-WiFi"
                        )
                        for host in [HOST, "192.168.1.121"]
                    }
                ),
            ),
            patch.object(
                discovery,
                "async_probe_inverter",
                AsyncMock(side_effect=[(inv, 8899), None]),
            ),
        ):
            self.assertEqual(
                await discovery.async_discover_inverters(MagicMock()),
                {HOST: discovery.DiscoveredInverter(inv, 8899, "00:11:22:33:44:55")},
            )

    async def test_manifest_has_matching_dhcp_and_translations(self):
        root = Path(__file__).resolve().parents[1] / "custom_components/goodwe"
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertTrue(
            any(
                fnmatch("E8FDF8123456", matcher.get("macaddress", "!"))
                for matcher in manifest["dhcp"]
            )
        )
        self.assertIn({"registered_devices": True}, manifest["dhcp"])
        for path in [root / "strings.json", *(root / "translations").glob("*.json")]:
            strings = json.loads(path.read_text())
            self.assertTrue(
                {"select", "confirm", "manual"} <= strings["config"]["step"].keys()
            )

    async def test_home_assistant_loader_registers_custom_dhcp_matchers(self):
        hass = MagicMock()
        integration = loader.Integration.resolve_from_root(
            hass, custom_components, "goodwe"
        )
        self.assertIsNotNone(integration)
        with patch.object(
            loader,
            "async_get_custom_components",
            AsyncMock(return_value={"goodwe": integration}),
        ):
            matchers = await loader.async_get_dhcp(hass)
        self.assertIn({"domain": "goodwe", "macaddress": "E8FDF8*"}, matchers)
        self.assertIn({"domain": "goodwe", "registered_devices": True}, matchers)


if __name__ == "__main__":
    unittest.main()
