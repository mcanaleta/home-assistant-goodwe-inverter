"""Exercise the real Home Assistant flow manager, entries and registry."""

import unittest
from contextlib import ExitStack
from tempfile import TemporaryDirectory
from types import MappingProxyType
from unittest.mock import AsyncMock, patch

from goodwe import InverterError
from homeassistant import loader
from homeassistant.config_entries import (
    SOURCE_DHCP,
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntries,
    ConfigEntry,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry, entity_registry
from homeassistant.helpers.service_info.dhcp import DhcpServiceInfo
from test_rediscovery import SERIAL, inverter

import custom_components
from custom_components.goodwe import config_flow, update_listener
from custom_components.goodwe.discovery import DiscoveredInverter

HOST = "192.168.1.120"


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = TemporaryDirectory()
        self.hass = HomeAssistant(self.directory.name)
        loader.async_setup(self.hass)
        self.hass.config_entries = ConfigEntries(self.hass, {})
        await self.hass.config_entries.async_initialize()
        self.stack = ExitStack()
        integration = loader.Integration.resolve_from_root(
            self.hass, custom_components, "goodwe"
        )
        self.stack.enter_context(
            patch.object(
                loader,
                "async_get_custom_components",
                AsyncMock(return_value={"goodwe": integration}),
            )
        )
        self.stack.enter_context(
            patch("homeassistant.config_entries.async_process_deps_reqs", AsyncMock())
        )
        self.stack.enter_context(
            patch.object(
                self.hass.config_entries, "async_setup", AsyncMock(return_value=True)
            )
        )
        self.reload = self.stack.enter_context(
            patch.object(
                self.hass.config_entries, "async_reload", AsyncMock(return_value=True)
            )
        )

    async def asyncTearDown(self):
        await self.hass.async_block_till_done()
        await self.hass.async_stop(force=True)
        self.stack.close()
        self.directory.cleanup()

    async def existing(self, **kwargs):
        values = {
            "domain": "goodwe",
            "data": {
                "host": "192.168.1.50",
                "port": 8899,
                "protocol": "UDP",
                "model_family": "ET",
                "extra": 7,
            },
            "options": {
                "host": "192.168.1.50",
                "port": 8899,
                "protocol": "UDP",
                "scan_interval": 90,
            },
            "title": "Solar",
            "version": 2,
            "minor_version": 1,
            "unique_id": SERIAL,
            "source": SOURCE_USER,
            "discovery_keys": MappingProxyType({}),
            "subentries_data": [],
        }
        values.update(kwargs)
        entry = ConfigEntry(**values)
        await self.hass.config_entries.async_add(entry)
        entry.add_update_listener(update_listener)
        return entry

    async def test_initial_discovery_creates_real_entry_after_confirmation(self):
        inv = inverter()
        inv.model_name = "GW10K-ET"
        with (
            patch.object(
                config_flow,
                "async_discover_inverters",
                AsyncMock(
                    return_value={
                        HOST: DiscoveredInverter(inv, 8899, "00:11:22:33:44:55")
                    }
                ),
            ),
            patch.object(
                config_flow, "async_probe_inverter", AsyncMock(return_value=(inv, 8899))
            ),
        ):
            result = await self.hass.config_entries.flow.async_init(
                "goodwe", context={"source": SOURCE_USER}
            )
            self.assertEqual(result["step_id"], "select")
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {"device": HOST}
            )
            self.assertEqual(result["step_id"], "confirm")
            self.assertEqual(self.hass.config_entries.async_entries("goodwe"), [])
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"], {}
            )
        self.assertEqual(result["type"], "create_entry")
        entry = result["result"]
        self.assertEqual(entry.unique_id, SERIAL)
        self.assertEqual(entry.data["mac"], "00:11:22:33:44:55")
        self.assertEqual(len(self.hass.config_entries.async_entries("goodwe")), 1)

    async def test_dhcp_merges_endpoint_reloads_and_preserves_entity_registry(self):
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
        with patch.object(
            config_flow,
            "async_probe_inverter",
            AsyncMock(return_value=(inverter(), 502)),
        ):
            result = await self.hass.config_entries.flow.async_init(
                "goodwe",
                context={"source": SOURCE_DHCP},
                data=DhcpServiceInfo(
                    ip=HOST, hostname="Solar-WiFi", macaddress="001122334455"
                ),
            )
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "already_configured")
        self.assertEqual(entry.data["host"], HOST)
        self.assertEqual(entry.options["port"], 502)
        self.assertEqual(entry.options["protocol"], "TCP")
        self.assertEqual(entry.data["extra"], 7)
        self.assertEqual(entry.options["scan_interval"], 90)
        self.assertEqual(entry.unique_id, SERIAL)
        self.assertIs(registry.async_get(entity.entity_id), entity)
        self.reload.assert_awaited_once_with(entry.entry_id)
        self.assertEqual(len(self.hass.config_entries.async_entries("goodwe")), 1)

    async def test_reconfigure_rejects_another_serial(self):
        entry = await self.existing()
        with patch.object(
            config_flow,
            "async_detect_inverter_port",
            AsyncMock(return_value=(inverter("OTHER"), 8899)),
        ):
            result = await self.hass.config_entries.flow.async_init(
                "goodwe",
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            self.assertEqual(result["step_id"], "reconfigure")
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": HOST, "protocol": "UDP", "model_family": "ET"},
            )
        self.assertEqual(result["reason"], "unique_id_mismatch")
        self.assertEqual(entry.data["host"], "192.168.1.50")
        self.reload.assert_not_awaited()

    async def test_reconfigure_retries_connection_failure_then_updates(self):
        entry = await self.existing()
        with patch.object(
            config_flow,
            "async_detect_inverter_port",
            AsyncMock(side_effect=[InverterError(), (inverter(), 502)]),
        ):
            result = await self.hass.config_entries.flow.async_init(
                "goodwe",
                context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
            )
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": HOST, "protocol": "TCP", "model_family": "ET"},
            )
            self.assertEqual(result["errors"], {"host": "connection_error"})
            result = await self.hass.config_entries.flow.async_configure(
                result["flow_id"],
                {"host": HOST, "protocol": "TCP", "model_family": "ET"},
            )
        await self.hass.async_block_till_done()
        self.assertEqual(result["reason"], "reconfigure_successful")
        self.assertEqual(entry.options["host"], HOST)
        self.assertEqual(entry.options["port"], 502)
        self.reload.assert_awaited_once_with(entry.entry_id)

    async def test_coordinator_refreshes_connection_when_endpoint_is_unchanged(self):
        from homeassistant.config_entries import ConfigEntryState

        from custom_components.goodwe.coordinator import GoodweUpdateCoordinator

        entry = await self.existing(state=ConfigEntryState.LOADED)
        old, new = inverter(), inverter()
        old.read_runtime_data.side_effect = InverterError("offline")
        coordinator = GoodweUpdateCoordinator(self.hass, entry, old)
        coordinator.data = {"ppv": 5}
        coordinator._consecutive_failures = 2
        with patch(
            "custom_components.goodwe.coordinator.async_recover_inverter",
            AsyncMock(return_value=(new, {"ppv": 1234})),
        ):
            self.assertEqual(await coordinator._async_update_data(), {"ppv": 1234})
        await self.hass.async_block_till_done()
        self.assertIs(coordinator.inverter, new)
        self.assertEqual(coordinator._consecutive_failures, 0)
        self.reload.assert_awaited_once_with(entry.entry_id)
        self.assertEqual(await coordinator._async_update_data(), {"ppv": 1234})

    async def test_coordinator_changed_endpoint_reloads_only_once(self):
        from homeassistant.config_entries import ConfigEntryState

        from custom_components.goodwe.coordinator import GoodweUpdateCoordinator
        from custom_components.goodwe.helpers import async_update_endpoint

        entry = await self.existing(state=ConfigEntryState.LOADED)
        new = inverter()

        async def recover(hass, configured):
            async_update_endpoint(hass, configured, HOST, 8899)
            return new, {"ppv": 1234}

        coordinator = GoodweUpdateCoordinator(self.hass, entry, inverter())
        with patch(
            "custom_components.goodwe.coordinator.async_recover_inverter",
            side_effect=recover,
        ):
            result = await coordinator._async_recover()
        await self.hass.async_block_till_done()
        self.assertIs(result[0], new)
        self.reload.assert_awaited_once_with(entry.entry_id)

    async def test_coordinator_marks_unavailable_if_recovery_fails(self):
        from goodwe import RequestFailedException
        from homeassistant.config_entries import ConfigEntryState
        from homeassistant.helpers.update_coordinator import UpdateFailed

        from custom_components.goodwe.coordinator import GoodweUpdateCoordinator

        entry = await self.existing(state=ConfigEntryState.LOADED)
        inv = inverter()
        coordinator = GoodweUpdateCoordinator(self.hass, entry, inv)
        coordinator.data = {"ppv": 99}
        with patch(
            "custom_components.goodwe.coordinator.async_recover_inverter",
            AsyncMock(return_value=None),
        ) as recover:
            for failure in [
                RequestFailedException("lost", 3),
                InverterError("offline"),
                OSError("network gone"),
            ]:
                inv.read_runtime_data.side_effect = failure
                with self.assertRaises(UpdateFailed):
                    await coordinator._async_update_data()
            self.assertEqual(recover.await_count, 3)
        self.reload.assert_not_awaited()
        # Entity polling failures do not prevent the inverter telemetry request.
        entity = type(
            "Entity",
            (),
            {
                "name": "configuration",
                "async_update": AsyncMock(side_effect=InverterError()),
            },
        )()
        coordinator.entity_state_polling(entity, 5)
        inv.read_runtime_data.side_effect = None
        self.assertEqual(await coordinator._async_update_data(), {"ppv": 1234})
        coordinator.entity_state_polling(entity, 0)
        self.assertEqual(coordinator._polled_entities, {})
        coordinator._last_data = {"total": 42, "daily": 10}
        coordinator.data = {"total": 0, "daily": None}
        self.assertEqual(coordinator.total_sensor_value("total"), 42)
        self.assertEqual(coordinator.sensor_value("daily"), 10)
        self.assertEqual(coordinator.sensor_value("total"), 0)
        coordinator.reset_sensor("daily")
        self.assertEqual(coordinator.sensor_value("daily"), 0)
