"""Network, concurrency and lifecycle regressions for rediscovery."""

import asyncio
import unittest
from ipaddress import IPv4Interface
from unittest.mock import AsyncMock, MagicMock, patch

from goodwe import InverterError
from homeassistant.config_entries import SOURCE_IGNORE
from test_config_flow import HOST, flow
from test_rediscovery import entry, inverter, loggers

from custom_components.goodwe import (
    async_migrate_entry,
    async_unload_entry,
    config_flow,
    discovery,
    helpers,
)


class EdgeCaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_slow_candidate_does_not_block_matching_inverter(self):
        cancelled = asyncio.Event()

        async def detect(host, **kwargs):
            if host.endswith("119"):
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()
            return inverter(), 502

        hass = MagicMock(data={})
        with (
            patch.object(
                discovery,
                "async_discover_loggers",
                AsyncMock(return_value=loggers("192.168.1.119", HOST)),
            ),
            patch.object(discovery, "async_detect_inverter_port", side_effect=detect),
        ):
            async with asyncio.timeout(0.2):
                result = await discovery.async_recover_inverter(
                    hass, entry(options={"port": 8899, "protocol": "UDP"})
                )
        self.assertEqual(result[1], {"ppv": 1234})
        self.assertTrue(cancelled.is_set())
        self.assertEqual(
            hass.config_entries.async_update_entry.call_args.kwargs["options"],
            {"port": 502, "protocol": "TCP"},
        )

    async def test_recovery_does_not_overwrite_a_concurrent_user_change(self):
        configured = entry()

        async def detect(*args, **kwargs):
            configured.options = {"host": "192.168.1.130"}
            return inverter(), 8899

        hass = MagicMock(data={})
        with (
            patch.object(
                discovery,
                "async_discover_loggers",
                AsyncMock(return_value=loggers(HOST)),
            ),
            patch.object(discovery, "async_detect_inverter_port", side_effect=detect),
        ):
            self.assertIsNone(await discovery.async_recover_inverter(hass, configured))
        hass.config_entries.async_update_entry.assert_not_called()

    async def test_swallowed_library_cancellation_still_obeys_deadline(self):
        async def bad_library(**kwargs):
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise InverterError("Cancelled by protocol wrapper") from None

        with (
            patch.object(helpers, "CONNECTION_SECONDS", 0.01),
            patch.object(helpers, "connect", side_effect=bad_library) as connect,
        ):
            async with asyncio.timeout(0.2):
                with self.assertRaisesRegex(InverterError, "timed out"):
                    await helpers.async_detect_inverter_port(HOST)
        connect.assert_awaited_once()

    async def test_external_cancellation_is_not_converted_to_failure(self):
        started = asyncio.Event()

        async def bad_library(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                raise InverterError("Cancelled by protocol wrapper") from None

        with patch.object(helpers, "connect", side_effect=bad_library):
            task = asyncio.create_task(helpers.async_detect_inverter_port(HOST))
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_recovery_deadline_and_per_candidate_failure(self):
        for error in [
            OSError("network gone"),
            InverterError("offline"),
            TimeoutError(),
        ]:
            hass = MagicMock(data={})
            with (
                patch.object(
                    discovery,
                    "async_discover_loggers",
                    AsyncMock(return_value=loggers(HOST)),
                ),
                patch.object(
                    discovery,
                    "async_detect_inverter_port",
                    AsyncMock(side_effect=error),
                ),
            ):
                self.assertIsNone(await discovery.async_recover_inverter(hass, entry()))
            hass.config_entries.async_update_entry.assert_not_called()

        async def slow(*args):
            await asyncio.Event().wait()

        with (
            patch.object(discovery, "RECOVERY_SECONDS", 0.01),
            patch.object(discovery, "async_discover_loggers", side_effect=slow),
        ):
            self.assertIsNone(
                await discovery.async_recover_inverter(MagicMock(data={}), entry())
            )
            self.assertEqual(await discovery.async_discover_inverters(MagicMock()), {})

    async def test_broadcast_failure_and_cancellation_close_transports(self):
        transport = MagicMock()
        loop = MagicMock(
            create_datagram_endpoint=AsyncMock(return_value=(transport, None))
        )
        with (
            patch.object(discovery.asyncio, "get_running_loop", return_value=loop),
            patch.object(
                discovery.asyncio,
                "sleep",
                AsyncMock(side_effect=asyncio.CancelledError),
            ),
            self.assertRaises(asyncio.CancelledError),
        ):
            await discovery._async_search_interface(IPv4Interface("192.168.1.2/24"))
        transport.close.assert_called_once()
        loop.create_datagram_endpoint.side_effect = OSError("interface gone")
        with patch.object(discovery.asyncio, "get_running_loop", return_value=loop):
            self.assertEqual(
                await discovery._async_search_interface(
                    IPv4Interface("192.168.1.2/24")
                ),
                {},
            )

    async def test_packet_limits_and_invalid_interfaces(self):
        proto = discovery._DiscoveryProtocol(IPv4Interface("192.168.1.2/24"))
        proto.datagram_received(b"x" * 257, (HOST, 48899))
        proto.datagram_received(b"\xff", (HOST, 48899))
        for host in ["192.168.1.0", "192.168.1.255", "192.168.1.2", "bad-host"]:
            proto.datagram_received(
                f"{host},001122334455,Solar-WiFi".encode(), (host, 48899)
            )
        self.assertEqual(proto.loggers, {})
        adapters = [
            {
                "enabled": True,
                "ipv4": [
                    {"address": ip, "network_prefix": prefix}
                    for ip, prefix in [
                        ("invalid", 24),
                        ("127.0.0.1", 8),
                        ("169.254.1.1", 16),
                        ("0.0.0.0", 24),
                        ("192.168.1.2", 31),
                        ("224.0.0.1", 24),
                    ]
                ],
            }
        ]
        with (
            patch.object(
                discovery, "async_get_adapters", AsyncMock(return_value=adapters)
            ),
            patch.object(discovery, "_async_search_interface", AsyncMock()) as search,
        ):
            self.assertEqual(await discovery.async_discover_loggers(MagicMock()), {})
        search.assert_not_awaited()

    async def test_ignored_entry_does_not_update(self):
        hass = MagicMock()
        self.assertFalse(
            helpers.async_update_endpoint(hass, entry(source=SOURCE_IGNORE), HOST, 8899)
        )
        hass.config_entries.async_update_entry.assert_not_called()
        data, options = helpers.entry_endpoint_data(entry(), HOST)
        self.assertNotIn("mac", data)
        self.assertEqual(options, {"scan_interval": 30})

    async def test_migration_keeps_extras_without_network_or_null_settings(self):
        hass = MagicMock()
        configured = entry(version=1)
        self.assertTrue(await async_migrate_entry(hass, configured))
        kwargs = hass.config_entries.async_update_entry.call_args.kwargs
        self.assertEqual(kwargs["version"], 2)
        self.assertEqual(kwargs["data"], {**configured.data, "protocol": "UDP"})
        self.assertFalse(await async_migrate_entry(hass, entry(version=3)))
        self.assertTrue(await async_migrate_entry(hass, entry(version=2)))

    async def test_migration_detects_missing_port_and_retries_unavailable_host(self):
        configured = entry(version=1, data={"host": HOST, "port": None})
        with patch(
            "custom_components.goodwe.async_detect_inverter_port",
            AsyncMock(return_value=(inverter(), 502)),
        ):
            hass = MagicMock()
            self.assertTrue(await async_migrate_entry(hass, configured))
        self.assertEqual(
            hass.config_entries.async_update_entry.call_args.kwargs["data"]["protocol"],
            "TCP",
        )
        from homeassistant.exceptions import ConfigEntryNotReady

        with (
            patch(
                "custom_components.goodwe.async_detect_inverter_port",
                AsyncMock(side_effect=InverterError()),
            ),
            self.assertRaises(ConfigEntryNotReady),
        ):
            await async_migrate_entry(hass, configured)

    async def test_unload_clears_cooldown_only_when_platforms_unload(self):
        for successful in [True, False]:
            hass = MagicMock(
                data={
                    "goodwe": {"existing-entry": object()},
                    discovery.DATA_REDISCOVERY: {"existing-entry": 0},
                }
            )
            hass.config_entries.async_unload_platforms = AsyncMock(
                return_value=successful
            )
            with patch(
                "custom_components.goodwe.async_unload_services", AsyncMock()
            ) as unload:
                self.assertEqual(await async_unload_entry(hass, entry()), successful)
            self.assertEqual(
                "existing-entry" in hass.data[discovery.DATA_REDISCOVERY],
                not successful,
            )
            self.assertEqual(unload.await_count, int(successful))

    async def test_manual_failure_and_direct_user_input(self):
        handler = flow()
        with patch.object(
            config_flow, "async_detect_inverter_port", AsyncMock(side_effect=OSError())
        ):
            result = await handler.async_step_user(
                {"host": HOST, "protocol": "UDP", "model_family": "ET"}
            )
        self.assertEqual(result["errors"], {"host": "connection_error"})
        with patch.object(
            config_flow,
            "async_detect_inverter_port",
            AsyncMock(return_value=(inverter(), 8899)),
        ):
            result = await handler.async_step_user(
                {"host": HOST, "protocol": "UDP", "model_family": "ET"}
            )
        self.assertEqual(result["type"], "create_entry")

    async def test_options_validate_endpoint_identity_and_keep_offline_interval_edits(
        self,
    ):
        configured = entry()
        handler = config_flow.GoodweFlowHandler.async_get_options_flow(configured)
        handler.hass = MagicMock()
        result = await handler.async_step_init()
        self.assertEqual(result["step_id"], "init")
        with patch.object(
            config_flow, "async_detect_inverter_port", AsyncMock()
        ) as detect:
            result = await handler.async_step_init({"scan_interval": 60})
        detect.assert_not_awaited()
        self.assertEqual(result["data"], {"scan_interval": 60})
        values = {"host": HOST, "protocol": "TCP", "model_family": "ET", "port": 502}
        with patch.object(
            config_flow,
            "async_detect_inverter_port",
            AsyncMock(side_effect=[(inverter("OTHER"), 502), (inverter(), 502)]),
        ):
            result = await handler.async_step_init(values)
            self.assertEqual(result["errors"], {"host": "connection_error"})
            result = await handler.async_step_init(values)
        self.assertEqual(result["data"], values)

    async def test_polling_only_options_form_accepts_default_modbus_address_offline(
        self,
    ):
        handler = config_flow.OptionsFlowHandler(entry())
        handler.hass = MagicMock()
        result = await handler.async_step_init()
        values = {
            "host": "192.168.1.50",
            "port": 8899,
            "protocol": "UDP",
            "model_family": "ET",
            "modbus_id": 0,
            "keep_alive": False,
            "scan_interval": 60,
            "network_timeout": 1,
            "network_retries": 10,
        }
        values = result["data_schema"](values)
        with patch.object(
            config_flow,
            "async_detect_inverter_port",
            AsyncMock(side_effect=InverterError("offline")),
        ) as detect:
            result = await handler.async_step_init(values)
        detect.assert_not_awaited()
        self.assertEqual(result["type"], "create_entry")


class DiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_diagnostics_redact_identity_and_preserve_useful_network_details(
        self,
    ):
        from custom_components.goodwe.diagnostics import (
            async_get_config_entry_diagnostics,
        )

        inv = inverter()
        inv.read_setting = AsyncMock(side_effect=[InverterError(), *[42] * 6])
        configured = entry()
        configured.runtime_data = type("Runtime", (), {"inverter": inv})()
        configured.as_dict = MagicMock(
            return_value={
                "unique_id": "PRIVATE-SERIAL",
                "data": {"host": HOST, "mac": "00:11:22:33:44:55", "port": 8899},
                "options": {"host": HOST},
            }
        )
        result = await async_get_config_entry_diagnostics(MagicMock(), configured)
        self.assertEqual(result["config_entry"]["unique_id"], "**REDACTED**")
        self.assertEqual(result["config_entry"]["data"]["mac"], "**REDACTED**")
        self.assertEqual(result["config_entry"]["options"]["host"], "**REDACTED**")
        self.assertEqual(result["config_entry"]["data"]["port"], 8899)
        self.assertIsNone(result["inverter"]["modbus_address"])
        inv.write_setting.assert_not_called()
