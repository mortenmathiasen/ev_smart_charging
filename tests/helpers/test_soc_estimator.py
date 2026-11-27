"""Test ev_smart_charging/helpers/soc_estimator.py"""

from datetime import datetime, timedelta

from homeassistant.util import dt

from custom_components.ev_smart_charging.helpers.soc_estimator import SocEstimator

T0 = datetime(2026, 10, 18, 6, 0, tzinfo=dt.UTC)


def at(minutes: float) -> datetime:
    """Return T0 plus minutes"""
    return T0 + timedelta(minutes=minutes)


async def test_soc_estimator_no_report():
    """Test the estimator before any report."""
    estimator = SocEstimator()
    assert estimator.get_soc(T0, 10.0) is None
    assert estimator.charging_hours(T0) == 0.0


async def test_soc_estimator_charging():
    """Test that charging since the report is added."""
    estimator = SocEstimator()
    assert estimator.report(40.0, at(0), at(0))
    assert estimator.get_soc(at(30), 10.0) == 40.0

    estimator.set_charging(True, at(30))
    assert estimator.is_charging()
    assert estimator.get_soc(at(90), 10.0) == 50.0
    estimator.set_charging(False, at(120))
    assert not estimator.is_charging()
    assert estimator.get_soc(at(180), 10.0) == 55.0

    # Capped at the Target SOC and 100%
    assert estimator.get_soc(at(180), 100.0, 80.0) == 80.0
    assert estimator.get_soc(at(180), 100.0, 120.0) == 100.0
    # Never below the reported SOC
    assert estimator.get_soc(at(180), 10.0, 30.0) == 40.0


async def test_soc_estimator_report_restarts_measurement():
    """Test that a new report only counts charging after the report."""
    estimator = SocEstimator()
    estimator.report(40.0, at(0), at(0))
    estimator.set_charging(True, at(0))

    # The EV reports at 60 minutes, received at 70 minutes
    assert estimator.report(50.0, at(60), at(70))
    assert estimator.get_soc(at(90), 10.0) == 55.0

    # A newer report with unchanged SOC moves the measurement start
    assert estimator.report(50.0, at(90), at(91))
    assert estimator.get_soc(at(120), 10.0) == 55.0

    # The same report again is ignored
    assert not estimator.report(50.0, at(90), at(130))
    assert estimator.get_soc(at(150), 10.0) == 60.0

    # Charging periods that ended before the report are removed
    estimator.set_charging(False, at(150))
    estimator.report(60.0, at(160), at(160))
    assert not estimator.charging_periods


async def test_soc_estimator_timestamp_after_soc():
    """Test a SOC that is updated before its timestamp."""
    estimator = SocEstimator()
    estimator.report(40.0, at(0), at(0))
    estimator.set_charging(True, at(0))

    # New SOC with the old timestamp is assumed to be reported now,
    # not at the old timestamp with the charging since then added
    assert estimator.report(50.0, at(0), at(60))
    assert estimator.reported_at == at(60)
    assert estimator.get_soc(at(60), 10.0) == 50.0
    # Then the new timestamp
    assert estimator.report(50.0, at(60), at(60))
    assert estimator.get_soc(at(90), 10.0) == 55.0


async def test_soc_estimator_timestamp_corrected_after_soc():
    """Test that the report time is corrected when the timestamp arrives later."""
    estimator = SocEstimator()
    estimator.report(20.0, at(0), at(0))
    estimator.set_charging(True, at(0))

    # SOC measured at 30 minutes is received at 60 minutes with the old timestamp
    assert estimator.report(25.0, at(0), at(60))
    estimator.set_charging(False, at(70))
    assert estimator.get_soc(at(90), 10.0) == 26.7
    # Unchanged SOC with the old timestamp is ignored
    assert not estimator.report(25.0, at(0), at(80))

    # The timestamp arrives and is earlier than the time the SOC was received.
    # The charging before the SOC was received is kept and counted.
    assert estimator.report(25.0, at(30), at(90))
    assert estimator.reported_at == at(30)
    assert estimator.get_soc(at(90), 10.0) == 31.7


async def test_soc_estimator_captured_earlier_delivered_late():
    """Test a SOC with a capture time before the last report time."""
    estimator = SocEstimator()
    # 0 = 23:42, 78 = 01:00, 108 = 01:30, 155 = 02:17
    estimator.set_charging(True, at(78))
    # A SOC reported with the time of a later dataset
    assert estimator.report(35.0, at(108), at(108))

    # A new SOC captured at 23:42 is delivered at 02:17. It includes no
    # charging, so all charging since 23:42 is added.
    assert estimator.report(38.0, at(0), at(155))
    assert estimator.reported_at == at(0)
    assert estimator.get_soc(at(155), 10.0) == 50.8


async def test_soc_estimator_same_soc_earlier_capture_time():
    """Test that an earlier capture time for the same SOC corrects the report time."""
    estimator = SocEstimator()
    estimator.set_charging(True, at(78))
    # The SOC was taken as measured at the time of a later dataset
    assert estimator.report(38.0, at(200), at(200))
    assert estimator.get_soc(at(230), 10.0) == 43.0

    # The capture time of the SOC becomes known
    assert estimator.report(38.0, at(0), at(230))
    assert estimator.reported_at == at(0)
    assert estimator.get_soc(at(230), 10.0) == 63.3
    # The same capture time again is not a new report
    assert not estimator.report(38.0, at(0), at(240))


async def test_soc_estimator_invalid_timestamp():
    """Test that a missing or future timestamp is replaced by the current time."""
    estimator = SocEstimator()
    estimator.report(40.0, None, at(10))
    assert estimator.reported_at == at(10)
    estimator.report(45.0, at(60), at(20))
    assert estimator.reported_at == at(20)
    # Unchanged SOC without a valid timestamp is not a new report
    assert not estimator.report(45.0, None, at(30))
    assert estimator.reported_at == at(20)


async def test_soc_estimator_restore():
    """Test storing and restoring the estimator."""
    estimator = SocEstimator()
    estimator.report(40.0, at(0), at(0))
    estimator.set_charging(True, at(0))
    data = estimator.as_dict(at(60))

    restored = SocEstimator()
    restored.restore(data)
    assert restored.reported_soc == 40.0
    assert restored.reported_at == at(0)
    assert restored.timestamp == at(0)
    # The restored timestamp is used to detect a SOC without a new timestamp
    assert restored.report(45.0, at(0), at(100))
    assert restored.reported_at == at(100)
    restored.restore(data)
    # Charging is ended when the state was saved
    assert not restored.is_charging()
    assert restored.get_soc(at(120), 10.0) == 50.0

    # Data saved before the timestamp was stored
    del data["timestamp"]
    restored.restore(data)
    assert restored.reported_at == at(0)
    assert restored.timestamp is None
    # The same report again is ignored, but its timestamp is registered
    assert not restored.report(40.0, at(0), at(100))
    assert restored.timestamp == at(0)

    # Invalid data is ignored
    restored.restore({"reported_soc": 40.0})
    assert restored.reported_soc is None
    assert restored.charging_periods == []
