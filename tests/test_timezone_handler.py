"""
Tests for TimezoneAware.

Covers instantiation, live vs anchored mode, arithmetic operators,
comparison operators, timezone conversion, diff, and edge cases.
"""

from datetime import datetime, timedelta, timezone

import pytest

from modules.timezone_handler import TimezoneAware


# ──────────────────────────────────────────────
# Instantiation tests
# ──────────────────────────────────────────────

class TestInstantiation:
    def test_live_mode(self):
        """Without dt, instance is in live mode."""
        tz = TimezoneAware("America/Sao_Paulo")
        assert not tz.is_anchored
        assert tz.timezone_name == "America/Sao_Paulo"

    def test_anchored_mode_naive_dt(self):
        """Naive datetime is localized to the given timezone."""
        dt = datetime(2026, 6, 2, 14, 30)
        tz = TimezoneAware("America/Sao_Paulo", dt)
        assert tz.is_anchored
        assert tz.time.hour == 14
        assert tz.time.minute == 30
        assert tz.time.tzinfo is not None

    def test_anchored_mode_aware_dt(self):
        """Aware datetime is converted to the given timezone."""
        # 17:00 UTC → 14:00 São Paulo (UTC-3)
        dt_utc = datetime(2026, 6, 2, 17, 0, tzinfo=timezone.utc)
        tz = TimezoneAware("America/Sao_Paulo", dt_utc)
        assert tz.is_anchored
        assert tz.time.hour == 14

    def test_invalid_timezone_raises(self):
        """Invalid timezone name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown timezone"):
            TimezoneAware("Not/A/Timezone")

    def test_utc_is_valid(self):
        """'UTC' is accepted as a timezone name."""
        tz = TimezoneAware("UTC")
        assert tz.timezone_name == "UTC"


# ──────────────────────────────────────────────
# Core properties tests
# ──────────────────────────────────────────────

class TestProperties:
    def test_now_returns_current_time(self):
        """`.now` returns approximately the current time."""
        tz = TimezoneAware("UTC")
        now = datetime.now(timezone.utc)
        diff = abs((tz.now - now).total_seconds())
        assert diff < 1  # Within 1 second

    def test_time_returns_anchor_when_set(self):
        """`.time` returns the anchored datetime."""
        dt = datetime(2026, 1, 15, 10, 0)
        tz = TimezoneAware("UTC", dt)
        assert tz.time == dt.replace(tzinfo=timezone.utc)

    def test_time_returns_now_when_live(self):
        """`.time` returns live time when not anchored."""
        tz = TimezoneAware("UTC")
        now = datetime.now(timezone.utc)
        diff = abs((tz.time - now).total_seconds())
        assert diff < 1

    def test_timestamp(self):
        """`.timestamp` returns correct UNIX timestamp."""
        dt = datetime(2026, 6, 2, 12, 0, tzinfo=timezone.utc)
        tz = TimezoneAware("UTC", dt)
        assert tz.timestamp == dt.timestamp()

    def test_utc_property(self):
        """`.utc` converts to UTC."""
        dt = datetime(2026, 6, 2, 14, 0)
        tz = TimezoneAware("America/Sao_Paulo", dt)
        utc_time = tz.utc
        assert utc_time.tzinfo == timezone.utc
        # SP is UTC-3
        assert utc_time.hour == 17


# ──────────────────────────────────────────────
# Arithmetic operator tests
# ──────────────────────────────────────────────

class TestArithmetic:
    def test_add_timedelta(self):
        """``tz + timedelta`` returns shifted anchored instance."""
        dt = datetime(2026, 6, 2, 10, 0)
        tz = TimezoneAware("America/Sao_Paulo", dt)
        result = tz + timedelta(hours=3)

        assert isinstance(result, TimezoneAware)
        assert result.is_anchored
        assert result.time.hour == 13
        assert result.timezone_name == "America/Sao_Paulo"

    def test_radd_timedelta(self):
        """``timedelta + tz`` also works."""
        dt = datetime(2026, 6, 2, 10, 0)
        tz = TimezoneAware("America/Sao_Paulo", dt)
        result = timedelta(hours=2) + tz

        assert isinstance(result, TimezoneAware)
        assert result.time.hour == 12

    def test_sub_timezoneaware(self):
        """``tz1 - tz2`` returns a timedelta."""
        t1 = TimezoneAware("America/Sao_Paulo", datetime(2026, 6, 2, 11, 0))
        t2 = TimezoneAware("America/Sao_Paulo", datetime(2026, 6, 2, 13, 0))
        delta = t2 - t1

        assert isinstance(delta, timedelta)
        assert delta.total_seconds() == 7200  # 2 hours

    def test_sub_timezoneaware_cross_timezone(self):
        """Subtraction works across timezones (UTC-normalized)."""
        # 14:00 SP = 17:00 UTC
        sp = TimezoneAware("America/Sao_Paulo", datetime(2026, 6, 2, 14, 0))
        # 17:00 UTC
        utc = TimezoneAware("UTC", datetime(2026, 6, 2, 17, 0))

        delta = sp - utc
        assert abs(delta.total_seconds()) < 1  # Same instant

    def test_sub_timedelta(self):
        """``tz - timedelta`` returns shifted anchored instance."""
        dt = datetime(2026, 6, 2, 15, 0)
        tz = TimezoneAware("UTC", dt)
        result = tz - timedelta(hours=5)

        assert isinstance(result, TimezoneAware)
        assert result.time.hour == 10

    def test_add_non_timedelta_returns_not_implemented(self):
        """Adding a non-timedelta returns NotImplemented."""
        tz = TimezoneAware("UTC", datetime(2026, 1, 1))
        assert tz.__add__(42) is NotImplemented

    def test_sub_non_supported_returns_not_implemented(self):
        """Subtracting a non-supported type returns NotImplemented."""
        tz = TimezoneAware("UTC", datetime(2026, 1, 1))
        assert tz.__sub__(42) is NotImplemented


# ──────────────────────────────────────────────
# The user's exact use case
# ──────────────────────────────────────────────

class TestUserUseCase:
    def test_sao_paulo_11am_minus_1pm_equals_2_hours(self):
        """
        'I want to pass 11am sao paulo tz and 13pm sao paulo tz
        in a minus comparator and got 2 hours.'
        """
        morning = TimezoneAware(
            "America/Sao_Paulo", datetime(2026, 6, 2, 11, 0)
        )
        afternoon = TimezoneAware(
            "America/Sao_Paulo", datetime(2026, 6, 2, 13, 0)
        )

        result = afternoon - morning
        assert isinstance(result, timedelta)
        assert result == timedelta(hours=2)
        assert result.total_seconds() == 7200


# ──────────────────────────────────────────────
# Comparison operator tests
# ──────────────────────────────────────────────

class TestComparisons:
    def test_equal(self):
        """Same instant in different timezones compares equal."""
        sp = TimezoneAware("America/Sao_Paulo", datetime(2026, 6, 2, 14, 0))
        utc = TimezoneAware("UTC", datetime(2026, 6, 2, 17, 0))
        assert sp == utc

    def test_not_equal(self):
        t1 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        t2 = TimezoneAware("UTC", datetime(2026, 6, 2, 11, 0))
        assert t1 != t2

    def test_less_than(self):
        earlier = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        later = TimezoneAware("UTC", datetime(2026, 6, 2, 12, 0))
        assert earlier < later

    def test_greater_than(self):
        earlier = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        later = TimezoneAware("UTC", datetime(2026, 6, 2, 12, 0))
        assert later > earlier

    def test_less_than_or_equal(self):
        t1 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        t2 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        assert t1 <= t2

    def test_greater_than_or_equal(self):
        t1 = TimezoneAware("UTC", datetime(2026, 6, 2, 12, 0))
        t2 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        assert t1 >= t2

    def test_eq_with_non_timezoneaware(self):
        tz = TimezoneAware("UTC", datetime(2026, 1, 1))
        assert tz.__eq__("not a tz") is NotImplemented

    def test_hashable(self):
        """Instances can be used in sets and as dict keys."""
        t1 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        t2 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        assert hash(t1) == hash(t2)
        assert len({t1, t2}) == 1


# ──────────────────────────────────────────────
# Conversion and diff tests
# ──────────────────────────────────────────────

class TestConversionAndDiff:
    def test_to_converts_timezone(self):
        """`.to()` converts to another timezone."""
        sp = TimezoneAware("America/Sao_Paulo", datetime(2026, 6, 2, 14, 0))
        ny = sp.to("America/New_York")

        assert ny.timezone_name == "America/New_York"
        # SP 14:00 (UTC-3) = NY 13:00 (UTC-4, EDT)
        # Same UTC instant, different local times
        assert sp == ny  # Same instant

    def test_diff_returns_absolute(self):
        """`.diff()` always returns a positive timedelta."""
        t1 = TimezoneAware("UTC", datetime(2026, 6, 2, 10, 0))
        t2 = TimezoneAware("UTC", datetime(2026, 6, 2, 14, 0))

        assert t1.diff(t2) == timedelta(hours=4)
        assert t2.diff(t1) == timedelta(hours=4)  # Same result, always positive

    def test_diff_cross_timezone(self):
        """`.diff()` works across timezones."""
        sp = TimezoneAware("America/Sao_Paulo", datetime(2026, 6, 2, 14, 0))
        utc = TimezoneAware("UTC", datetime(2026, 6, 2, 17, 0))
        assert sp.diff(utc) == timedelta(0)  # Same instant


# ──────────────────────────────────────────────
# String representation tests
# ──────────────────────────────────────────────

class TestStringRepresentation:
    def test_repr_anchored(self):
        tz = TimezoneAware("UTC", datetime(2026, 6, 2, 12, 0))
        r = repr(tz)
        assert "UTC" in r
        assert "anchored" in r

    def test_repr_live(self):
        tz = TimezoneAware("UTC")
        r = repr(tz)
        assert "live" in r

    def test_str(self):
        tz = TimezoneAware("UTC", datetime(2026, 6, 2, 12, 0))
        s = str(tz)
        assert "UTC" in s
        assert "2026-06-02" in s
