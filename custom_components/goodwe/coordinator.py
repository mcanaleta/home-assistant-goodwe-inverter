"""Update coordinator for Goodwe."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import CONF_SCAN_INTERVAL
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    BaseCoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from goodwe import Inverter, InverterError, RequestFailedException

from .const import DEFAULT_SCAN_INTERVAL
from .discovery import async_recover_inverter
from .helpers import raise_if_cancelled

_LOGGER = logging.getLogger(__name__)

type GoodweConfigEntry = ConfigEntry[GoodweRuntimeData]


@dataclass
class GoodweRuntimeData:
    """Data class for runtime data."""

    inverter: Inverter
    coordinator: GoodweUpdateCoordinator
    device_info: DeviceInfo


class GoodweUpdateCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Gather data for the energy device."""

    config_entry: GoodweConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: GoodweConfigEntry,
        inverter: Inverter,
    ) -> None:
        """Initialize update coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name=entry.title,
            update_interval=timedelta(
                seconds=entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
            ),
        )
        self.inverter: Inverter = inverter
        self._last_data: dict[str, Any] = {}
        self._polled_entities: dict[BaseCoordinatorEntity, int] = {}
        self._consecutive_failures = 0

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch data from the inverter."""
        await self._update_polled_entities()

        try:
            self._last_data = self.data or {}
            data = await self.inverter.read_runtime_data()
            raise_if_cancelled()
            self._consecutive_failures = 0
            return data
        except RequestFailedException as ex:
            raise_if_cancelled()
            self._consecutive_failures = max(
                self._consecutive_failures + 1, ex.consecutive_failures_count
            )
            # UDP communication with inverter is by definition unreliable.
            # It is rather normal in many environments to fail to receive
            # proper response in usual time, so we intentionally ignore isolated
            # failures and report problem with availability only after
            # consecutive streak of 3 of failed requests.
            if self._consecutive_failures < 3:
                _LOGGER.debug(
                    "No response received (streak of %d)", ex.consecutive_failures_count
                )
                # return last known data
                return self._last_data
            # Inverter does not respond anymore (e.g. it went to sleep mode)
            _LOGGER.debug(
                "Inverter not responding (streak of %d)", ex.consecutive_failures_count
            )
            recovered = await self._async_recover()
            if recovered is not None:
                # The existing update listener reloads the entry after an IP change,
                # so entities retaining an inverter reference also get the new host.
                _, data = recovered
                return data
            raise UpdateFailed(ex) from ex
        except (InverterError, OSError) as ex:
            raise_if_cancelled()
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                recovered = await self._async_recover()
                if recovered is not None:
                    _, data = recovered
                    return data
            raise UpdateFailed(ex) from ex

    async def _async_recover(self) -> tuple[Inverter, dict[str, Any]] | None:
        """Refresh references even when the logger still uses the same endpoint."""
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return None
        before = (dict(self.config_entry.data), dict(self.config_entry.options))
        recovered = await async_recover_inverter(self.hass, self.config_entry)
        if recovered is not None:
            self.inverter = recovered[0]
            self._consecutive_failures = 0
            # Changed entries reload through their update listener. If nothing
            # changed, reload explicitly to refresh references held by controls.
            if before == (
                dict(self.config_entry.data),
                dict(self.config_entry.options),
            ):
                self.hass.async_create_task(
                    self.hass.config_entries.async_reload(self.config_entry.entry_id)
                )
        return recovered

    async def _update_polled_entities(self) -> None:
        for entity, interval in list(self._polled_entities.items()):
            if interval:
                try:
                    await entity.async_update()
                except InverterError:
                    raise_if_cancelled()
                    _LOGGER.debug("Failed to update entity %s", entity.name)

    def sensor_value(self, sensor: str) -> Any:
        """Answer current (or last known) value of the sensor."""
        val = self.data.get(sensor)
        return val if val is not None else self._last_data.get(sensor)

    def total_sensor_value(self, sensor: str) -> Any:
        """Answer current value of the 'total' (never 0) sensor."""
        val = self.data.get(sensor)
        return val or self._last_data.get(sensor)

    def reset_sensor(self, sensor: str) -> None:
        """Reset sensor value to 0.

        Intended for "daily" cumulative sensors (e.g. PV energy produced today),
        which should be explicitly reset to 0 at midnight if inverter is suspended.
        """
        self._last_data[sensor] = 0
        self.data[sensor] = 0

    def entity_state_polling(
        self, entity: BaseCoordinatorEntity, interval: int
    ) -> None:
        """Enable/disable polling of entity state."""
        if interval:
            self._polled_entities[entity] = interval
        else:
            self._polled_entities.pop(entity, None)
