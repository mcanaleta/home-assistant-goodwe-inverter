"""Focused regressions for DHCP discovery and serial-preserving IP changes."""

import asyncio
import unittest
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

from goodwe import InverterError
from homeassistant import loader
from homeassistant.config_entries import (
    SOURCE_DHCP,
    SOURCE_IGNORE,
    SOURCE_USER,
    ConfigEntries,
    ConfigEntry,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo

import custom_components
from custom_components.goodwe import async_setup_entry, config_flow, update_listener

HOST = "192.168.1.120"
OLD_HOST = "192.168.1.50"
MAC = "e8:fd:f8:12:34:56"
SERIAL = "EXAMPLE-ET-SERIAL"
INFO = DhcpServiceInfo(ip=HOST, hostname="Solar-WiFi", macaddress="E8FDF8123456")


class ET(MagicMock):
    """Use the same family name as a real ET inverter, without hardware access."""


def inverter(serial=SERIAL):
    result = ET()
    result.serial_number = serial
    result.model_name = "GW10K-ET"
    return result


class DhcpFlowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        loader.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        self.patches = ExitStack()
        integration = loader.Integration.resolve_from_root(
            self.hass, custom_components, "goodwe"
        )
        self.patches.enter_context(
            patch.object(
                loader,
                "async_get_custom_components",
                AsyncMock(return_value={"goodwe": integration}),
            )
        )
        self.patches.enter_context(
            patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock())
        )
        self.patches.enter_context(
            patch.object(
                self.hass.config_entries, "async_setup", AsyncMock(return_value=True)
            )
        )
        self.reload = self.patches.enter_context(
            patch.object(
                self.hass.config_entries, "async_reload", AsyncMock(return_value=True)
            )
        )

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.patches.close()
        self.directory.cleanup()

    async def existing(self, source=SOURCE_USER, **kwargs):
        fields = {
            "domain": "goodwe",
            "title": "Solar",
            "version": 2,
            "minor_version": 1,
            "source": source,
            "unique_id": SERIAL,
            "data": {
                "host": OLD_HOST,
                "port": 8899,
                "protocol": "UDP",
                "model_family": "ET",
                "extra": 7,
            },
            "options": {
                "host": OLD_HOST,
                "port": 8899,
                "protocol": "UDP",
                "scan_interval": 90,
            },
            "discovery_keys": MappingProxyType({}),
            "subentries_data": [],
        }
        fields.update(kwargs)
        entry = ConfigEntry(**fields)
        await self.hass.config_entries.async_add(entry)
        entry.add_update_listener(update_listener)
        return entry

    async def dhcp(self):
        return await self.hass.config_entries.flow.async_init(
            "goodwe", context={"source": SOURCE_DHCP}, data=INFO
        )

    async def test_new_device_requires_confirmation_and_registers_logger_mac(self):
        inv = inverter()
        with patch.object(config_flow, "connect", AsyncMock(return_value=inv)):
            result = await self.dhcp()
            self.assertEqual(result["step_id"], "confirm")
            self.assertEqual(result["description_placeholders"]["serial"], SERIAL)
            self.assertTrue(self.hass.config_entries.flow.async_progress()[0]["context"]["confirm_only"])
            self.assertEqual(self.hass.config_entries.async_entries("goodwe"), [])
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
        self.assertEqual(result["type"], "create_entry")
        entry = result["result"]
        self.assertEqual(entry.unique_id, SERIAL)
        self.assertEqual(entry.data["host"], HOST)
        self.assertEqual(entry.data["mac"], MAC)
        coordinator = MagicMock(async_config_entry_first_refresh=AsyncMock())
        with (
            patch("custom_components.goodwe.connect", AsyncMock(return_value=inv)),
            patch(
                "custom_components.goodwe.GoodweUpdateCoordinator",
                return_value=coordinator,
            ),
            patch("custom_components.goodwe.async_setup_services", AsyncMock()),
            patch.object(
                self.hass.config_entries, "async_forward_entry_setups", AsyncMock()
            ),
        ):
            self.assertTrue(await async_setup_entry(self.hass, entry))
        self.assertEqual(entry.runtime_data.device_info["connections"], {("mac", MAC)})
        inv.write_setting.assert_not_called()

    async def test_existing_serial_updates_endpoint_once_and_preserves_entities(self):
        entry = await self.existing()
        device_registry.async_setup(self.hass)
        await device_registry.async_load(self.hass)
        registry = entity_registry.async_get(self.hass)
        await registry.async_load()
        entity = registry.async_get_or_create(
            "sensor",
            "goodwe",
            f"{SERIAL}-ppv",
            config_entry=entry,
            suggested_object_id="solar_power",
        )
        # Three failed UDP families, followed by a successful TCP identification.
        with patch.object(
            config_flow,
            "connect",
            AsyncMock(
                side_effect=[
                    InverterError(),
                    InverterError(),
                    InverterError(),
                    inverter(),
                ]
            ),
        ):
            result = await self.dhcp()
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(
            entry.data,
            {
                "host": HOST,
                "port": 502,
                "protocol": "TCP",
                "mac": MAC,
                "model_family": "ET",
                "extra": 7,
            },
        )
        self.assertEqual(
            entry.options,
            {
                "host": HOST,
                "port": 502,
                "protocol": "TCP",
                "scan_interval": 90,
            },
        )
        self.assertEqual(entry.unique_id, SERIAL)
        self.assertIs(registry.async_get(entity.entity_id), entity)
        self.assertEqual(len(self.hass.config_entries.async_entries("goodwe")), 1)
        self.reload.assert_awaited_once_with(entry.entry_id)

    async def test_registered_mac_uses_existing_custom_connection_settings(self):
        entry = await self.existing(
            data={
                "host": OLD_HOST,
                "port": 502,
                "protocol": "TCP",
                "model_family": "ET",
                "mac": MAC,
            },
            options={"port": 1502, "model_family": "EH", "modbus_id": 42},
        )
        with patch.object(
            config_flow, "connect", AsyncMock(return_value=inverter())
        ) as connect:
            result = await self.dhcp()
        self.assertEqual(result["reason"], "already_configured")
        connect.assert_awaited_once_with(
            host=HOST,
            port=1502,
            family="EH",
            comm_addr=42,
            timeout=1,
            retries=0,
            do_discover=False,
        )
        self.assertEqual(
            entry.options, {"port": 1502, "model_family": "EH", "modbus_id": 42}
        )

    async def test_other_serial_never_updates_an_existing_inverter(self):
        entry = await self.existing()
        before = dict(entry.data), dict(entry.options)
        with patch.object(
            config_flow, "connect", AsyncMock(return_value=inverter("OTHER"))
        ):
            result = await self.dhcp()
        self.assertEqual(result["step_id"], "confirm")
        self.assertEqual((dict(entry.data), dict(entry.options)), before)
        self.reload.assert_not_awaited()

    async def test_shared_vendor_candidate_without_goodwe_response_is_rejected(self):
        with patch.object(
            config_flow, "connect", AsyncMock(side_effect=InverterError())
        ):
            result = await self.dhcp()
        self.assertEqual(result["reason"], "not_goodwe")
        self.assertEqual(self.hass.config_entries.async_entries("goodwe"), [])

    async def test_ignored_serial_stays_ignored(self):
        entry = await self.existing(source=SOURCE_IGNORE)
        before = dict(entry.data), dict(entry.options)
        with patch.object(config_flow, "connect", AsyncMock(return_value=inverter())):
            result = await self.dhcp()
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual((dict(entry.data), dict(entry.options)), before)
        self.reload.assert_not_awaited()

    async def test_confirmation_retries_offline_device_and_rejects_reassigned_ip(self):
        with patch.object(config_flow, "connect", AsyncMock(return_value=inverter())):
            result = await self.dhcp()
        flow_id = result["flow_id"]
        with patch.object(config_flow, "connect", AsyncMock(side_effect=OSError())):
            result = await self.hass.config_entries.flow.async_configure(flow_id, {})
        self.assertEqual(result["errors"], {"base": "connection_error"})
        with patch.object(
            config_flow, "connect", AsyncMock(return_value=inverter("OTHER"))
        ):
            result = await self.hass.config_entries.flow.async_configure(flow_id, {})
        self.assertEqual(result["reason"], "device_changed")
        self.assertEqual(self.hass.config_entries.async_entries("goodwe"), [])

    async def test_duplicate_discovery_does_not_create_two_flows(self):
        with patch.object(config_flow, "connect", AsyncMock(return_value=inverter())):
            first = await self.dhcp()
            second = await self.dhcp()
        self.assertEqual(first["step_id"], "confirm")
        self.assertEqual(second["reason"], "already_in_progress")

    async def test_repeated_identical_event_does_not_reload_again(self):
        await self.existing(
            data={
                "host": HOST,
                "port": 8899,
                "protocol": "UDP",
                "model_family": "ET",
                "mac": MAC,
            },
            options={"scan_interval": 90},
        )
        with patch.object(config_flow, "connect", AsyncMock(return_value=inverter())):
            result = await self.dhcp()
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "already_configured")
        self.reload.assert_not_awaited()

    async def test_manual_configuration_keeps_the_existing_path(self):
        result = await self.hass.config_entries.flow.async_init(
            "goodwe", context={"source": SOURCE_USER}
        )
        self.assertEqual(result["step_id"], "user")
        with patch.object(
            config_flow, "connect", AsyncMock(return_value=inverter())
        ) as connect:
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"],
                {
                    "host": HOST,
                    "protocol": "UDP",
                    "model_family": "ET",
                },
            )
        connect.assert_awaited_once_with(host=HOST, port=8899, family="ET", retries=10)
        self.assertEqual(result["type"], "create_entry")
        self.assertNotIn("mac", result["data"])

    async def test_manifest_registers_dhcp_matchers_with_home_assistant(self):
        matchers = await loader.async_get_dhcp(self.hass)
        self.assertIn({"domain": "goodwe", "hostname": "solar-wifi*"}, matchers)
        self.assertIn({"domain": "goodwe", "macaddress": "E8FDF8*"}, matchers)
        self.assertIn({"domain": "goodwe", "registered_devices": True}, matchers)


class IdentificationTests(unittest.IsolatedAsyncioTestCase):
    async def test_blank_serial_and_network_errors_do_not_identify_a_device(self):
        for response in [inverter(""), inverter("   ")]:
            with patch.object(config_flow, "connect", AsyncMock(return_value=response)):
                self.assertIsNone(
                    await config_flow.GoodweFlowHandler._async_identify_dhcp_inverter(
                        HOST
                    )
                )
        with patch.object(config_flow, "connect", AsyncMock(side_effect=OSError())):
            self.assertIsNone(
                await config_flow.GoodweFlowHandler._async_identify_dhcp_inverter(HOST)
            )

    async def test_library_swallowed_cancellation_is_restored(self):
        started = asyncio.Event()

        async def cancelled_library(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise InverterError("Protocol wrapped cancellation") from None

        with patch.object(config_flow, "connect", side_effect=cancelled_library):
            task = asyncio.create_task(
                config_flow.GoodweFlowHandler._async_identify_dhcp_inverter(HOST)
            )
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_identification_obeys_deadline_when_library_swallows_cancellation(
        self,
    ):
        original_timeout = asyncio.timeout

        async def cancelled_library(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise InverterError("Protocol wrapped cancellation") from None

        with (
            patch.object(
                config_flow.asyncio,
                "timeout",
                side_effect=lambda delay: original_timeout(0.01),
            ),
            patch.object(
                config_flow, "connect", side_effect=cancelled_library
            ) as connect,
        ):
            self.assertIsNone(
                await config_flow.GoodweFlowHandler._async_identify_dhcp_inverter(HOST)
            )
        connect.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
