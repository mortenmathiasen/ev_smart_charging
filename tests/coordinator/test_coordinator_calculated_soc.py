"""Test ev_smart_charging coordinator with calculated SOC."""

# From HA 2026.11, pytest_homeassistant_custom_component is deprecated and replaced with pytest_homeassistant.
try:
    from pytest_homeassistant.common import (  # type: ignore
        MockConfigEntry,
        mock_restore_cache_with_extra_data,
    )
except ImportError:
    from pytest_homeassistant_custom_component.common import (  # type: ignore
        MockConfigEntry,
        mock_restore_cache_with_extra_data,
    )

from datetime import datetime

from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant, State
from homeassistant.const import STATE_OFF, STATE_ON, MAJOR_VERSION, MINOR_VERSION
from homeassistant.helpers.entity_registry import async_get as async_entity_registry_get
from homeassistant.helpers.entity_registry import EntityRegistry
from homeassistant.util import dt

from custom_components.ev_smart_charging import async_setup_entry, async_unload_entry
from custom_components.ev_smart_charging.const import DOMAIN

from tests.helpers.helpers import (
    MockChargerEntity,
    MockPriceEntity,
    MockSOCEntity,
    MockTargetSOCEntity,
)
from tests.price import PRICE_20220930
from tests.const import MOCK_CONFIG_ALL, MOCK_CONFIG_CALCULATED_SOC

CHARGING_STATE = "binary_sensor.ocpp_charging_state"
LAST_REPORTED = "sensor.volkswagen_we_connect_id_last_reported"
CALCULATED_SOC = "sensor.ev_smart_charging_calculated_soc"


async def setup_coordinator(hass: HomeAssistant, config: dict):
    """Set up the integration and return the coordinator."""
    config_entry = MockConfigEntry(domain=DOMAIN, data=config, entry_id="test")
    if MAJOR_VERSION > 2024 or (MAJOR_VERSION == 2024 and MINOR_VERSION >= 7):
        config_entry.mock_state(hass=hass, state=ConfigEntryState.LOADED)
    config_entry.add_to_hass(hass)
    assert await async_setup_entry(hass, config_entry)
    await hass.async_block_till_done()
    return config_entry, hass.data[DOMAIN][config_entry.entry_id]


# pylint: disable=unused-argument
async def test_coordinator_calculated_soc(
    hass: HomeAssistant, skip_service_calls, set_cet_timezone, freezer
):
    """Test the calculated SOC."""

    freezer.move_to("2022-09-30T10:00:00+02:00")

    entity_registry: EntityRegistry = async_entity_registry_get(hass)
    MockSOCEntity.create(hass, entity_registry, "40")
    MockTargetSOCEntity.create(hass, entity_registry, "80")
    MockPriceEntity.create(hass, entity_registry, 123)
    MockPriceEntity.set_state(hass, PRICE_20220930, None)
    MockChargerEntity.create(hass, entity_registry, STATE_OFF)
    hass.states.async_set(CHARGING_STATE, STATE_OFF)
    # A timestamp without time zone is in local time
    hass.states.async_set(LAST_REPORTED, "2022-09-30T09:30:00")

    config_entry, coordinator = await setup_coordinator(
        hass, MOCK_CONFIG_CALCULATED_SOC
    )
    assert coordinator.ev_soc == 40.0
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "40.0"
    assert state.attributes["reported_at"] == "2022-09-30T09:30:00+02:00"

    # Charge for one hour at 6 %/h
    hass.states.async_set(CHARGING_STATE, STATE_ON)
    await hass.async_block_till_done()
    freezer.move_to("2022-09-30T11:00:00+02:00")
    await coordinator.update_calculated_soc()
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 46.0
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "46.0"
    assert state.attributes["reported_soc"] == 40.0
    assert state.attributes["charging_hours"] == 1.0
    assert coordinator.sensor.ev_soc == 46.0

    # The EV reports at 10:30, which includes 30 minutes of charging
    MockSOCEntity.set_state(hass, "43")
    hass.states.async_set(LAST_REPORTED, "2022-09-30T10:30:00+02:00")
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 46.0

    # Charging stops. The SOC is not changed while not charging.
    hass.states.async_set(CHARGING_STATE, STATE_OFF)
    await hass.async_block_till_done()
    freezer.move_to("2022-09-30T12:00:00+02:00")
    await coordinator.update_calculated_soc()
    await coordinator.update_sensors()
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 46.0

    # Capped at the Target SOC
    MockTargetSOCEntity.set_state(hass, "45")
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 45.0

    assert await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()


async def test_coordinator_calculated_soc_restore(
    hass: HomeAssistant, skip_service_calls, set_cet_timezone, freezer
):
    """Test that charging before a restart is kept."""

    freezer.move_to("2022-09-30T10:00:00+02:00")
    mock_restore_cache_with_extra_data(
        hass,
        [
            (
                State(CALCULATED_SOC, "43.0"),
                {
                    "reported_soc": 40.0,
                    "reported_at": "2022-09-30T09:00:00+02:00",
                    "charging_periods": [["2022-09-30T09:00:00+02:00", None]],
                    "saved_at": "2022-09-30T09:30:00+02:00",
                },
            )
        ],
    )

    entity_registry: EntityRegistry = async_entity_registry_get(hass)
    MockSOCEntity.create(hass, entity_registry, "40")
    MockTargetSOCEntity.create(hass, entity_registry, "80")
    MockPriceEntity.create(hass, entity_registry, 123)
    MockPriceEntity.set_state(hass, PRICE_20220930, None)
    MockChargerEntity.create(hass, entity_registry, STATE_OFF)
    hass.states.async_set(CHARGING_STATE, STATE_OFF)
    hass.states.async_set(LAST_REPORTED, "2022-09-30T09:00:00+02:00")

    config_entry, coordinator = await setup_coordinator(
        hass, MOCK_CONFIG_CALCULATED_SOC
    )
    # 30 minutes of charging at 6 %/h before the restart
    assert coordinator.ev_soc == 43.0
    assert hass.states.get(CALCULATED_SOC).state == "43.0"

    # The state to be stored
    sensor = coordinator.sensor_calculated_soc
    stored = sensor.extra_restore_state_data.as_dict()
    assert stored["reported_soc"] == 40.0
    assert stored["saved_at"] == "2022-09-30T10:00:00+02:00"

    # Restore when the coordinator is already set up
    await sensor.async_added_to_hass()
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 43.0

    assert await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()


async def test_coordinator_calculated_soc_own_charging_state(
    hass: HomeAssistant, skip_service_calls, set_cet_timezone, freezer
):
    """Test the calculated SOC using the integration's own charging state."""

    freezer.move_to("2022-09-30T10:00:00+02:00")

    entity_registry: EntityRegistry = async_entity_registry_get(hass)
    MockSOCEntity.create(hass, entity_registry, "40")
    MockTargetSOCEntity.create(hass, entity_registry, "80")
    MockPriceEntity.create(hass, entity_registry, 123)
    MockPriceEntity.set_state(hass, PRICE_20220930, None)
    MockChargerEntity.create(hass, entity_registry, STATE_OFF)
    hass.states.async_set(LAST_REPORTED, "invalid")

    config = MOCK_CONFIG_CALCULATED_SOC | {"charging_state_entity": ""}
    config_entry, coordinator = await setup_coordinator(hass, config)
    assert coordinator.ev_soc == 40.0

    await coordinator.turn_on_charging()
    freezer.move_to("2022-09-30T10:30:00+02:00")
    await coordinator.update_calculated_soc()
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 43.0

    await coordinator.turn_off_charging()
    freezer.move_to("2022-09-30T11:00:00+02:00")
    await coordinator.update_sensors()
    assert coordinator.ev_soc == 43.0

    # A timestamp that cannot be parsed is ignored
    hass.states.async_set(LAST_REPORTED, "2022-13-45T10:00:00")
    await hass.async_block_till_done()
    assert coordinator.ev_soc == 43.0

    assert await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()


async def test_coordinator_no_calculated_soc(
    hass: HomeAssistant, skip_service_calls, set_cet_timezone, freezer
):
    """Test that the calculated SOC is not used without a timestamp entity."""

    freezer.move_to("2022-09-30T10:00:00+02:00")

    entity_registry: EntityRegistry = async_entity_registry_get(hass)
    MockSOCEntity.create(hass, entity_registry, "40")
    MockTargetSOCEntity.create(hass, entity_registry, "80")
    MockPriceEntity.create(hass, entity_registry, 123)
    MockPriceEntity.set_state(hass, PRICE_20220930, None)
    MockChargerEntity.create(hass, entity_registry, STATE_OFF)

    config_entry, coordinator = await setup_coordinator(hass, MOCK_CONFIG_ALL)
    assert coordinator.soc_estimator is None
    assert hass.states.get(CALCULATED_SOC) is None

    await coordinator.turn_on_charging()
    freezer.move_to("2022-09-30T11:00:00+02:00")
    await coordinator.update_sensors()
    assert coordinator.ev_soc == 40.0

    assert await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()


SOC = "sensor.volkswagen_we_connect_id_state_of_charge"


async def test_coordinator_calculated_soc_data_captured_at(
    hass: HomeAssistant, skip_service_calls, set_cet_timezone, freezer
):
    """Test that the data_captured_at attribute of the SOC sensor is preferred."""

    freezer.move_to("2022-09-30T10:00:00+02:00")

    entity_registry: EntityRegistry = async_entity_registry_get(hass)
    MockSOCEntity.create(hass, entity_registry, "40")
    hass.states.async_set(
        SOC,
        "40",
        {"data_captured_at": "2022-09-30T09:00:00+02:00", "freshness_source": "point"},
    )
    MockTargetSOCEntity.create(hass, entity_registry, "80")
    MockPriceEntity.create(hass, entity_registry, 123)
    MockPriceEntity.set_state(hass, PRICE_20220930, None)
    MockChargerEntity.create(hass, entity_registry, STATE_OFF)
    hass.states.async_set(CHARGING_STATE, STATE_OFF)
    hass.states.async_set(LAST_REPORTED, "2022-09-30T09:30:00+02:00")

    config_entry, _ = await setup_coordinator(hass, MOCK_CONFIG_CALCULATED_SOC)
    state = hass.states.get(CALCULATED_SOC)
    assert state.attributes["reported_at"] == "2022-09-30T09:00:00+02:00"

    # Without the attribute, the timestamp entity is used
    hass.states.async_set(SOC, "41", {"freshness_source": "dataset"})
    hass.states.async_set(LAST_REPORTED, "2022-09-30T09:40:00+02:00")
    await hass.async_block_till_done()
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "41.0"
    assert state.attributes["reported_at"] == "2022-09-30T09:40:00+02:00"

    # An attribute that cannot be parsed is ignored
    hass.states.async_set(SOC, "42", {"data_captured_at": "not a time"})
    hass.states.async_set(LAST_REPORTED, "2022-09-30T09:50:00+02:00")
    await hass.async_block_till_done()
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "42.0"
    assert state.attributes["reported_at"] == "2022-09-30T09:50:00+02:00"

    # An attribute without time zone is in local time
    hass.states.async_set(SOC, "43", {"data_captured_at": "2022-09-30T09:55:00"})
    await hass.async_block_till_done()
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "43.0"
    assert state.attributes["reported_at"] == "2022-09-30T09:55:00+02:00"

    # An attribute that is a datetime object
    captured_at = datetime(2022, 9, 30, 7, 58, tzinfo=dt.UTC)
    hass.states.async_set(SOC, "44", {"data_captured_at": captured_at})
    await hass.async_block_till_done()
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "44.0"
    assert state.attributes["reported_at"] == "2022-09-30T07:58:00+00:00"

    assert await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()


async def test_coordinator_calculated_soc_stale_soc_in_new_dataset(
    hass: HomeAssistant, skip_service_calls, set_cet_timezone, freezer
):
    """Test new datasets that carry an old SOC with its capture time.

    Replays a night with a VW via vag_connect. The timestamp entity moves with
    every dataset while the SOC and its capture time stay the same.
    """

    freezer.move_to("2022-09-29T23:50:00+00:00")

    entity_registry: EntityRegistry = async_entity_registry_get(hass)
    MockSOCEntity.create(hass, entity_registry, "38")
    hass.states.async_set(
        SOC,
        "38",
        {"data_captured_at": "2022-09-29T23:42:27+00:00", "freshness_source": "point"},
    )
    MockTargetSOCEntity.create(hass, entity_registry, "80")
    MockPriceEntity.create(hass, entity_registry, 123)
    MockPriceEntity.set_state(hass, PRICE_20220930, None)
    MockChargerEntity.create(hass, entity_registry, STATE_OFF)
    hass.states.async_set(CHARGING_STATE, STATE_OFF)
    hass.states.async_set(LAST_REPORTED, "2022-09-29T23:45:00+00:00")

    config_entry, coordinator = await setup_coordinator(
        hass, MOCK_CONFIG_CALCULATED_SOC
    )
    coordinator.charging_pct_per_hour = 14.3
    assert coordinator.ev_soc == 38.0

    async def set_charging(time: str, state: str):
        freezer.move_to(time)
        hass.states.async_set(CHARGING_STATE, state)
        await hass.async_block_till_done()

    async def new_dataset(time: str):
        freezer.move_to(time)
        hass.states.async_set(LAST_REPORTED, time)
        await hass.async_block_till_done()

    await set_charging("2022-09-30T01:00:00+00:00", STATE_ON)
    await set_charging("2022-09-30T02:00:00+00:00", STATE_OFF)
    await set_charging("2022-09-30T02:17:00+00:00", STATE_ON)
    await new_dataset("2022-09-30T02:18:26+00:00")
    # 1 h 1 min 26 s of charging since the SOC was captured
    assert coordinator.ev_soc == 52.6
    await new_dataset("2022-09-30T03:20:03+00:00")
    await set_charging("2022-09-30T04:29:00+00:00", STATE_OFF)

    freezer.move_to("2022-09-30T04:56:00+00:00")
    await coordinator.update_sensors()
    await hass.async_block_till_done()
    # 38 + 3.2 h * 14.3 %/h is capped at the Target SOC
    assert coordinator.ev_soc == 80.0
    state = hass.states.get(CALCULATED_SOC)
    assert state.state == "80.0"
    assert state.attributes["reported_soc"] == 38.0
    assert state.attributes["reported_at"] == "2022-09-29T23:42:27+00:00"

    assert await async_unload_entry(hass, config_entry)
    await hass.async_block_till_done()
