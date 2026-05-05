"""Tests for :func:`ccr.utils.format_user_datetime` (CCR-035)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from structlog.testing import capture_logs

from ccr.db.models import PairedUser
from ccr.utils import format_user_datetime

# --------------------------------------------------------------------------- #
# Helpers.
# --------------------------------------------------------------------------- #


def _make_user(*, timezone: str | None, tg_user_id: int = 42) -> PairedUser:
    return PairedUser(
        id=uuid.uuid4(),
        tg_user_id=tg_user_id,
        tg_username=f"u{tg_user_id}",
        is_owner=False,
        approved_at=datetime.now(UTC),
        timezone=timezone,
    )


# --------------------------------------------------------------------------- #
# Mode formatting.
# --------------------------------------------------------------------------- #


def test_full_mode_formats_hh_mm_dd_mm_yyyy() -> None:
    """``"full"`` → ``HH:MM - DD/MM/YYYY`` (24h, no AM/PM)."""
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    user = _make_user(timezone=None)

    out = format_user_datetime(dt, user, "full")

    assert out == "14:03 - 05/05/2026"


def test_short_mode_formats_hh_mm_d_mon() -> None:
    """``"short"`` → ``HH:MM - D Mon`` (no leading zero on day, English month abbr.)."""
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    user = _make_user(timezone=None)

    out = format_user_datetime(dt, user, "short")

    assert out == "14:03 - 5 May"


def test_short_mode_uses_english_month_abbreviations() -> None:
    """Spot-check across the calendar: month abbreviation is always English."""
    user = _make_user(timezone=None)
    cases = {
        1: "Jan",
        2: "Feb",
        3: "Mar",
        4: "Apr",
        6: "Jun",
        7: "Jul",
        8: "Aug",
        12: "Dec",
    }
    for month, abbr in cases.items():
        dt = datetime(2026, month, 9, 7, 5, tzinfo=UTC)
        out = format_user_datetime(dt, user, "short")
        assert out == f"07:05 - 9 {abbr}", f"month={month}: got {out!r}"


def test_short_mode_pads_day_without_leading_zero() -> None:
    """Day in ``"short"`` mode is rendered without a leading zero (5, not 05)."""
    user = _make_user(timezone=None)

    out_single_digit = format_user_datetime(datetime(2026, 5, 5, 14, 3, tzinfo=UTC), user, "short")
    out_two_digit = format_user_datetime(datetime(2026, 5, 25, 14, 3, tzinfo=UTC), user, "short")

    assert out_single_digit == "14:03 - 5 May"
    assert out_two_digit == "14:03 - 25 May"


def test_time_mode_formats_hh_mm() -> None:
    """``"time"`` → ``HH:MM``."""
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    user = _make_user(timezone=None)

    out = format_user_datetime(dt, user, "time")

    assert out == "14:03"


# --------------------------------------------------------------------------- #
# Timezone application.
# --------------------------------------------------------------------------- #


def test_europe_berlin_shifts_utc_noon_by_offset() -> None:
    """A UTC-noon datetime renders shifted into Berlin's offset (+1 winter, +2 summer).

    We assert the hour DIFFERS from the UTC render and is one of the two
    valid offsets — that survives the DST flip without hard-coding a season.
    """
    dt_summer = datetime(2026, 7, 1, 12, 0, 0, tzinfo=UTC)
    dt_winter = datetime(2026, 1, 15, 12, 0, 0, tzinfo=UTC)
    user = _make_user(timezone="Europe/Berlin")

    out_summer = format_user_datetime(dt_summer, user, "time")
    out_winter = format_user_datetime(dt_winter, user, "time")

    assert out_summer == "14:00"
    assert out_winter == "13:00"
    assert format_user_datetime(dt_summer, _make_user(timezone=None), "time") == "12:00"


def test_full_mode_with_tz_renders_local_date() -> None:
    """When the conversion crosses midnight, the rendered date follows the local zone."""
    dt = datetime(2026, 5, 5, 23, 30, 0, tzinfo=UTC)
    user = _make_user(timezone="Asia/Tokyo")  # UTC+9 year-round.

    out = format_user_datetime(dt, user, "full")

    assert out == "08:30 - 06/05/2026"


# --------------------------------------------------------------------------- #
# UTC fallbacks.
# --------------------------------------------------------------------------- #


def test_user_none_falls_back_to_utc() -> None:
    """``user is None`` → render in UTC."""
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)

    out = format_user_datetime(dt, None, "full")

    assert out == "14:03 - 05/05/2026"


def test_user_timezone_none_falls_back_to_utc() -> None:
    """``user.timezone is None`` → render in UTC."""
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    user = _make_user(timezone=None)

    out = format_user_datetime(dt, user, "full")

    assert out == "14:03 - 05/05/2026"


# --------------------------------------------------------------------------- #
# Defensive fallback for unresolvable zones.
# --------------------------------------------------------------------------- #


def test_unresolvable_timezone_falls_back_to_utc_and_logs_warning() -> None:
    """A bogus IANA name → fall back to UTC AND emit a structlog warning.

    We use :func:`structlog.testing.capture_logs` to assert the warning
    fires; the helper must not raise.
    """
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    user = _make_user(timezone="Not/Real")

    with capture_logs() as logs:
        out = format_user_datetime(dt, user, "full")

    assert out == "14:03 - 05/05/2026"
    matching = [
        e
        for e in logs
        if e.get("log_level") == "warning" and "format_user_datetime" in e.get("event", "")
    ]
    assert matching, f"Expected a warning log, got: {logs}"
    assert matching[0].get("timezone") == "Not/Real"


def test_unresolvable_timezone_does_not_raise() -> None:
    """Defensive: helper must not propagate :class:`ZoneInfoNotFoundError`."""
    dt = datetime(2026, 5, 5, 14, 3, 17, tzinfo=UTC)
    user = _make_user(timezone="Definitely/Not_A_Zone")

    # No try/except — if this raises, the test fails.
    out = format_user_datetime(dt, user, "time")

    assert out == "14:03"


# --------------------------------------------------------------------------- #
# DST edge sanity.
# --------------------------------------------------------------------------- #


def test_naive_datetime_is_treated_as_utc() -> None:
    """Naive ``datetime`` (no tzinfo) is interpreted as UTC.

    SQLite drops ``tzinfo`` on round-trip even with ``DateTime(timezone=True)``;
    every ``Session.started_at`` read back is naive. The helper must not
    interpret it as local time, otherwise users see incorrect timestamps.
    """
    naive = datetime(2026, 5, 5, 14, 3, 17)  # noqa: DTZ001 — deliberately naive
    user = _make_user(timezone="Asia/Tokyo")  # UTC+9.

    out = format_user_datetime(naive, user, "time")

    # 14:03 UTC + 9 = 23:03 Tokyo. (If naive were treated as local, the result
    # would depend on the test machine's $TZ — we assert the UTC interpretation.)
    assert out == "23:03"


def test_dst_edge_renders_correct_offset_pre_and_post() -> None:
    """Just before vs. just after Berlin's spring-forward, offset differs by 1h.

    Europe/Berlin transitions at 01:00 UTC on the last Sunday of March.
    For 2026 that's 2026-03-29 01:00 UTC → local clocks jump 02:00 → 03:00.

    * 2026-03-29 00:30 UTC → 01:30 CET (winter, +1).
    * 2026-03-29 01:30 UTC → 03:30 CEST (summer, +2).
    """
    user = _make_user(timezone="Europe/Berlin")

    pre = datetime(2026, 3, 29, 0, 30, 0, tzinfo=UTC)
    post = datetime(2026, 3, 29, 1, 30, 0, tzinfo=UTC)

    out_pre = format_user_datetime(pre, user, "time")
    out_post = format_user_datetime(post, user, "time")

    assert out_pre == "01:30"
    assert out_post == "03:30"
