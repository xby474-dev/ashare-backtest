"""Explicit exchange sessions. A weekday approximation is never a live calendar."""
from bisect import bisect_left, bisect_right
from datetime import date
from typing import Iterable

from .models import as_date


class TradingCalendar:
    def __init__(self, sessions: Iterable[date | str], *, coverage_start: date | str | None = None,
                 coverage_end: date | str | None = None):
        values = tuple(as_date(d) for d in sessions)
        if not values or len(set(values)) != len(values):
            raise ValueError("Calendar must be nonempty and contain unique sessions")
        self.sessions = tuple(sorted(values))
        self._members = frozenset(self.sessions)
        self.coverage_start = as_date(coverage_start) if coverage_start is not None else self.sessions[0]
        self.coverage_end = as_date(coverage_end) if coverage_end is not None else self.sessions[-1]
        if self.coverage_start > self.sessions[0] or self.coverage_end < self.sessions[-1]:
            raise ValueError("Calendar coverage must contain all sessions")

    def __contains__(self, day: date) -> bool:
        return day in self._members

    def next_session(self, day: date) -> date | None:
        i = bisect_right(self.sessions, as_date(day))
        return self.sessions[i] if i < len(self.sessions) else None

    def previous_session(self, day: date) -> date | None:
        i = bisect_left(self.sessions, as_date(day)) - 1
        return self.sessions[i] if i >= 0 else None

    def between(self, start: date, end: date) -> tuple[date, ...]:
        return self.sessions[bisect_left(self.sessions, as_date(start)):bisect_right(self.sessions, as_date(end))]

    def is_month_end(self, day: date) -> bool:
        """Requires the following session: a truncated tail is NOT a month end."""
        day = as_date(day)
        nxt = self.next_session(day)
        return day in self and nxt is not None and (day.year, day.month) != (nxt.year, nxt.month)
