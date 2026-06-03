from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import total_ordering
from typing import Union
from zoneinfo import ZoneInfo, available_timezones


@total_ordering
class TimezoneAware:
    """Timezone-aware datetime wrapper with operator overloading for arithmetic and comparisons."""

    __slots__ = ("_tz", "_tz_name", "_anchor")

    def __init__(self, tz_name: str, dt: datetime | None = None) -> None:
        if tz_name not in available_timezones() and tz_name != "UTC":
            raise ValueError(
                f"Unknown timezone: {tz_name!r}. "
                f"Use a valid IANA timezone name (e.g., 'America/Sao_Paulo')."
            )

        self._tz_name: str = tz_name
        self._tz: ZoneInfo = ZoneInfo(tz_name)

        if dt is not None:
            self._anchor: datetime | None = (
                dt.replace(tzinfo=self._tz) if dt.tzinfo is None else dt.astimezone(self._tz)
            )
        else:
            self._anchor = None

    @property
    def now(self) -> datetime:
        """Current wall-clock time in the configured timezone (always live)."""
        return datetime.now(self._tz)

    @property
    def time(self) -> datetime:
        """Anchored time if set, otherwise live current time."""
        return self._anchor if self._anchor is not None else self.now

    @property
    def timestamp(self) -> float:
        """UNIX timestamp of the effective datetime."""
        return self.time.timestamp()

    @property
    def timezone_name(self) -> str:
        return self._tz_name

    @property
    def tz(self) -> ZoneInfo:
        return self._tz

    @property
    def is_anchored(self) -> bool:
        return self._anchor is not None

    @property
    def utc(self) -> datetime:
        """The effective datetime converted to UTC."""
        return self.time.astimezone(timezone.utc)

    def to(self, tz_name: str) -> TimezoneAware:
        """Convert the effective time to another timezone, returning a new anchored instance."""
        converted = self.time.astimezone(ZoneInfo(tz_name))
        return TimezoneAware(tz_name, converted)

    def __add__(self, other: timedelta) -> TimezoneAware:
        if not isinstance(other, timedelta):
            return NotImplemented
        return TimezoneAware(self._tz_name, self.time + other)

    def __radd__(self, other: timedelta) -> TimezoneAware:
        return self.__add__(other)

    def __sub__(
        self, other: Union[TimezoneAware, timedelta]
    ) -> Union[timedelta, TimezoneAware]:
        if isinstance(other, TimezoneAware):
            return self.time - other.time
        if isinstance(other, timedelta):
            return TimezoneAware(self._tz_name, self.time - other)
        return NotImplemented

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TimezoneAware):
            return NotImplemented
        return self.time == other.time

    def __lt__(self, other: TimezoneAware) -> bool:
        if not isinstance(other, TimezoneAware):
            return NotImplemented
        return self.time < other.time

    def __hash__(self) -> int:
        return hash(self.time)

    def diff(self, other: TimezoneAware) -> timedelta:
        """Absolute time difference between two instances (always positive)."""
        return abs(self.time - other.time)

    def __repr__(self) -> str:
        mode = "anchored" if self.is_anchored else "live"
        return f"TimezoneAware({self._tz_name!r}, time={self.time.isoformat()}, mode={mode})"

    def __str__(self) -> str:
        return f"{self._tz_name} @ {self.time.strftime('%Y-%m-%d %H:%M:%S %Z')}"
