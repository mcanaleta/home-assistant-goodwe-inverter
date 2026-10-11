"""The Goodwe inverter component."""

from homeassistant.const import (
    CONF_HOST,
    CONF_MAC,
    CONF_PORT,
    CONF_PROTOCOL,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.device_registry import (
    CONNECTION_NETWORK_MAC,
    DeviceInfo,
    format_mac,
)

from goodwe import Inverter, InverterError, connect
from goodwe.const import GOODWE_TCP_PORT, GOODWE_UDP_PORT

from .const import (
    CONF_KEEP_ALIVE,
    CONF_MODBUS_ID,
    CONF_MODEL_FAMILY,
    CONF_NETWORK_RETRIES,
    CONF_NETWORK_TIMEOUT,
    DEFAULT_MODBUS_ID,
    DEFAULT_NETWORK_RETRIES,
    DEFAULT_NETWORK_TIMEOUT,
    DOMAIN,
    PLATFORMS,
)
from .coordinator import GoodweConfigEntry, GoodweRuntimeData, GoodweUpdateCoordinator
from .discovery import DATA_REDISCOVERY, async_recover_inverter
from .helpers import (
    async_detect_inverter_port,
    async_update_endpoint,
    raise_if_cancelled,
    validate_serial,
)
from .services import async_setup_services, async_unload_services


async def async_setup_entry(hass: HomeAssistant, entry: GoodweConfigEntry) -> bool:
    """Set up the Goodwe components from a config entry."""
    hass.data.setdefault(DOMAIN, {})
    host = entry.options.get(CONF_HOST, entry.data[CONF_HOST])
    protocol = entry.options.get(CONF_PROTOCOL, entry.data.get(CONF_PROTOCOL, "UDP"))
    port = entry.options.get(
        CONF_PORT,
        entry.data.get(
            CONF_PORT, GOODWE_TCP_PORT if protocol == "TCP" else GOODWE_UDP_PORT
        ),
    )
    keep_alive = entry.options.get(CONF_KEEP_ALIVE, False)
    model_family = entry.options.get(CONF_MODEL_FAMILY, entry.data[CONF_MODEL_FAMILY])
    network_retries = entry.options.get(CONF_NETWORK_RETRIES, DEFAULT_NETWORK_RETRIES)
    network_timeout = entry.options.get(CONF_NETWORK_TIMEOUT, DEFAULT_NETWORK_TIMEOUT)
    modbus_id = entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID)

    # Connect to Goodwe inverter
    try:
        inverter = await connect(
            host=host,
            port=port,
            family=model_family,
            comm_addr=modbus_id,
            timeout=network_timeout,
            retries=network_retries,
        )
        raise_if_cancelled()
        validate_serial(entry, inverter)
        inverter.set_keep_alive(keep_alive)
    except (InverterError, OSError) as err:
        raise_if_cancelled()
        try:
            inverter = await async_check_port(hass, entry, host)
        except InverterError, OSError:
            recovered = await async_recover_inverter(hass, entry)
            if recovered is None:
                raise ConfigEntryNotReady from err
            inverter, _ = recovered
        inverter.set_keep_alive(keep_alive)

    device_info = DeviceInfo(
        configuration_url="https://semsplus.goodwe.com/",
        identifiers={(DOMAIN, inverter.serial_number)},
        name=entry.title,
        manufacturer="GoodWe",
        model=inverter.model_name,
        sw_version=f"{inverter.firmware} / {inverter.arm_firmware}",
        hw_version=f"{inverter.serial_number[5:8]} {inverter.serial_number[0:5]}",
    )
    if mac := entry.data.get(CONF_MAC):
        device_info["connections"] = {(CONNECTION_NETWORK_MAC, format_mac(mac))}

    # Create update coordinator
    coordinator = GoodweUpdateCoordinator(hass, entry, inverter)

    # Fetch initial data so we have data when entities subscribe
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = GoodweRuntimeData(
        inverter=inverter,
        coordinator=coordinator,
        device_info=device_info,
    )

    hass.data[DOMAIN][entry.entry_id] = entry.runtime_data

    entry.async_on_unload(entry.add_update_listener(update_listener))

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    await async_setup_services(hass)

    return True


async def async_check_port(
    hass: HomeAssistant, entry: GoodweConfigEntry, host: str
) -> Inverter:
    """Check the communication port of the inverter, it may have changed after a firmware update."""
    inverter, port = await async_detect_inverter_port(
        host=host,
        family=entry.options.get(CONF_MODEL_FAMILY, entry.data.get(CONF_MODEL_FAMILY)),
        comm_addr=entry.options.get(CONF_MODBUS_ID, DEFAULT_MODBUS_ID),
    )
    validate_serial(entry, inverter)
    async_update_endpoint(hass, entry, host, port)
    return inverter


async def async_unload_entry(
    hass: HomeAssistant, config_entry: GoodweConfigEntry
) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(
        config_entry, PLATFORMS
    )

    if unload_ok:
        hass.data[DOMAIN].pop(config_entry.entry_id)
        hass.data.get(DATA_REDISCOVERY, {}).pop(config_entry.entry_id, None)

        if not hass.data[DOMAIN]:
            await async_unload_services(hass)

    return unload_ok


async def update_listener(hass: HomeAssistant, config_entry: GoodweConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(config_entry.entry_id)


async def async_migrate_entry(
    hass: HomeAssistant, config_entry: GoodweConfigEntry
) -> bool:
    """Migrate old config entries."""

    if config_entry.version > 2:
        return False

    if config_entry.version == 1:
        # Update from version 1 to version 2 adding the PROTOCOL to the config entry
        host = config_entry.data[CONF_HOST]
        port = config_entry.data.get(
            CONF_PORT,
            config_entry.data.get(
                CONF_PORT,
                (
                    GOODWE_TCP_PORT
                    if config_entry.data.get(CONF_PROTOCOL) == "TCP"
                    else GOODWE_UDP_PORT
                ),
            ),
        )
        if not port:
            try:
                _, port = await async_detect_inverter_port(host=host)
            except (InverterError, OSError) as err:
                raise ConfigEntryNotReady from err
        new_data = {
            **config_entry.data,
            CONF_HOST: host,
            CONF_PORT: port,
            CONF_PROTOCOL: "TCP" if port == GOODWE_TCP_PORT else "UDP",
        }
        hass.config_entries.async_update_entry(config_entry, data=new_data, version=2)

    return True
