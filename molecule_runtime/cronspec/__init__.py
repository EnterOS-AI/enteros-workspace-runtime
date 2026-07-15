"""cronspec — the runtime's cron evaluator, conformant to the SDK cron contract.

SSOT: ``molecule-ai-sdk`` ``contracts/cron/`` (grammar + semantics + the
canonical fixture set). This module is the Python side of the cross-language
equivalence gate: it MUST return, for every fixture, the exact instant the Go
reference (``robfig/cron`` v3, the shipping scheduler) returns. The vendored
fixture mirror is ``molecule_runtime/contracts/cron.fixtures.json`` (kept honest
by ``scripts/check-schemas-in-sync.sh``); ``tests/test_cronspec_contract.py``
asserts conformance.

Semantics pinned to the contract (== robfig standard parser):
  * **5 fields** — ``minute hour day-of-month month day-of-week``. No seconds
    field (fires at second 0). No ``@``-descriptors.
  * ``*`` ``a`` ``a-b`` ``a,b`` ``*/n`` ``a-b/n`` ``a/n`` (``a/n`` == ``a-max/n``).
  * **Names** — months ``JAN..DEC``, weekdays ``SUN..SAT`` (case-insensitive).
  * **Day-of-week is 0-6, 0 = Sunday.** ``7`` is rejected (robfig does too).
  * **dom/dow OR rule** — if BOTH day-of-month and day-of-week are restricted
    (neither is ``*``), a day matches when EITHER matches; if either field is
    ``*``, both must match. (Vixie/robfig semantics.)
  * **Next is strictly after** the input and returned in UTC.
  * **DST** — a fire time in a spring-forward gap is skipped; a fall-back
    (ambiguous) fire time uses the EARLIER of the two instants.
"""
from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from typing import Optional
from zoneinfo import ZoneInfo

__all__ = ["compute_next_run", "validate", "CronError"]

_MONTH_NAMES = {
    "JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
    "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12,
}
_DOW_NAMES = {"SUN": 0, "MON": 1, "TUE": 2, "WED": 3, "THU": 4, "FRI": 5, "SAT": 6}

_DIM = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)
_YEAR_LIMIT = 6  # robfig gives up after 5 years past start; we mirror with a margin


class CronError(ValueError):
    """A malformed cron expression / timezone (mirrors robfig's parse errors)."""


@dataclass(frozen=True)
class _Spec:
    minutes: frozenset
    hours: frozenset
    doms: frozenset
    months: frozenset
    dows: frozenset
    dom_star: bool
    dow_star: bool


def _parse_field(field: str, lo: int, hi: int, names: Optional[dict]) -> tuple[frozenset, bool]:
    """Parse one cron field into (allowed-values, is_star). Names resolved first."""
    is_star = field == "*"
    out: set[int] = set()

    def _num(tok: str) -> int:
        t = tok.upper()
        if names and t in names:
            return names[t]
        try:
            v = int(tok)
        except ValueError as e:
            raise CronError(f"invalid value {tok!r} in field {field!r}") from e
        return v

    for part in field.split(","):
        step = 1
        rng = part
        if "/" in part:
            rng, step_s = part.split("/", 1)
            try:
                step = int(step_s)
            except ValueError as e:
                raise CronError(f"invalid step {step_s!r} in {field!r}") from e
            if step <= 0:
                raise CronError(f"step must be positive in {field!r}")
        if rng == "*":
            start, end = lo, hi
        elif "-" in rng:
            a, b = rng.split("-", 1)
            start, end = _num(a), _num(b)
        else:
            start = _num(rng)
            # `a/n` (a step with a bare start) means a..max; a bare `a` is just a.
            end = hi if "/" in part else start
        if start < lo or end > hi or start > end:
            raise CronError(
                f"field {field!r} value {start}-{end} out of range [{lo},{hi}]"
            )
        for v in range(start, end + 1, step):
            out.add(v)
    return frozenset(out), is_star


def _parse(expr: str) -> _Spec:
    fields = expr.split()
    if len(fields) != 5:
        raise CronError(
            f"expected 5 fields (min hour dom month dow), got {len(fields)}: {expr!r}"
        )
    mi, _ = _parse_field(fields[0], 0, 59, None)
    hr, _ = _parse_field(fields[1], 0, 23, None)
    dom, dom_star = _parse_field(fields[2], 1, 31, None)
    mon, _ = _parse_field(fields[3], 1, 12, _MONTH_NAMES)
    dow, dow_star = _parse_field(fields[4], 0, 6, _DOW_NAMES)
    return _Spec(mi, hr, dom, mon, dow, dom_star, dow_star)


def _days_in_month(y: int, m: int) -> int:
    if m == 2 and (y % 4 == 0 and (y % 100 != 0 or y % 400 == 0)):
        return 29
    return _DIM[m - 1]


def _cron_dow(y: int, m: int, d: int) -> int:
    # date.weekday(): Mon=0..Sun=6 -> cron Sun=0..Sat=6
    return (_dt.date(y, m, d).weekday() + 1) % 7


def _day_matches(y: int, m: int, d: int, s: _Spec) -> bool:
    dom_ok = d in s.doms
    dow_ok = _cron_dow(y, m, d) in s.dows
    if s.dom_star or s.dow_star:
        return dom_ok and dow_ok
    return dom_ok or dow_ok


def _incr_minute(y, m, d, h, mi):
    mi += 1
    if mi == 60:
        mi = 0
        return _incr_hour(y, m, d, h) + (mi,)
    return (y, m, d, h, mi)


def _incr_hour(y, m, d, h):
    h += 1
    if h == 24:
        h = 0
        y, m, d = _incr_day(y, m, d)
    return (y, m, d, h)


def _incr_day(y, m, d):
    d += 1
    if d > _days_in_month(y, m):
        d = 1
        m += 1
        if m == 13:
            m = 1
            y += 1
    return (y, m, d)


def _next_month(y, m):
    m += 1
    if m == 13:
        m = 1
        y += 1
    return (y, m)


def _resolve(y, m, d, h, mi, loc) -> Optional[_dt.datetime]:
    """Resolve a wall-clock candidate to a UTC instant, or None if it is a
    spring-forward gap (nonexistent). Fall-back (ambiguous) -> earlier instant."""
    naive = _dt.datetime(y, m, d, h, mi)
    dt0 = naive.replace(tzinfo=loc, fold=0)
    # gap: round-trip through UTC changes the wall clock -> the time doesn't exist
    rt = dt0.astimezone(_dt.timezone.utc).astimezone(loc)
    if (rt.year, rt.month, rt.day, rt.hour, rt.minute) != (y, m, d, h, mi):
        return None
    dt1 = naive.replace(tzinfo=loc, fold=1)
    if dt0.utcoffset() != dt1.utcoffset():
        # ambiguous fall-back -> the earlier absolute instant
        u0 = dt0.astimezone(_dt.timezone.utc)
        u1 = dt1.astimezone(_dt.timezone.utc)
        return min(u0, u1)
    return dt0.astimezone(_dt.timezone.utc)


def compute_next_run(expr: str, tz: str, after: _dt.datetime) -> _dt.datetime:
    """Next fire time strictly after ``after``, as a UTC-aware datetime.

    Mirrors Go ``scheduler.ComputeNextRun`` / robfig ``Schedule.Next`` exactly
    (see the SDK cron contract + fixtures). Raises :class:`CronError` on a bad
    expression, timezone, or when no fire exists within ~6 years.
    """
    spec = _parse(expr)
    try:
        loc = ZoneInfo(tz)
    except Exception as e:  # noqa: BLE001 - unknown tz db key
        raise CronError(f"invalid timezone {tz!r}: {e}") from e
    if after.tzinfo is None:
        after = after.replace(tzinfo=_dt.timezone.utc)
    t = after.astimezone(loc)

    # Earliest minute-aligned wall time strictly after `after` is always
    # floor(after, minute) + 1 minute (whether or not `after` had sub-minute parts).
    y, m, d, h, mi = _incr_minute(t.year, t.month, t.day, t.hour, t.minute)

    year_limit = y + _YEAR_LIMIT
    while True:
        if y > year_limit:
            raise CronError(f"no fire time for {expr!r} within {_YEAR_LIMIT} years")
        if m not in spec.months:
            y, m = _next_month(y, m)
            d, h, mi = 1, 0, 0
            continue
        if not _day_matches(y, m, d, spec):
            y, m, d = _incr_day(y, m, d)
            h, mi = 0, 0
            continue
        if h not in spec.hours:
            y, m, d, h = _incr_hour(y, m, d, h)
            mi = 0
            continue
        if mi not in spec.minutes:
            y, m, d, h, mi = _incr_minute(y, m, d, h, mi)
            continue
        inst = _resolve(y, m, d, h, mi, loc)
        if inst is None:  # spring-forward gap -> this wall time never happens
            y, m, d, h, mi = _incr_minute(y, m, d, h, mi)
            continue
        return inst.astimezone(_dt.timezone.utc)


def validate(expr: str, tz: str = "UTC") -> None:
    """Raise :class:`CronError` if the expression or timezone is invalid."""
    _parse(expr)
    try:
        ZoneInfo(tz)
    except Exception as e:  # noqa: BLE001
        raise CronError(f"invalid timezone {tz!r}: {e}") from e
