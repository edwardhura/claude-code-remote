"""Cross-cutting utility helpers used by the bot layer.

Currently houses :func:`format_user_datetime`, the single source of truth for
rendering ``datetime`` values in user-facing Telegram replies. Every bot-side
timestamp render goes through this helper so the user's preferred timezone
(``paired_users.timezone``) is honoured uniformly and a single output format
is enforced — see CCR-035.
"""

from __future__ import annotations

import zoneinfo
from datetime import UTC
from typing import TYPE_CHECKING, Literal

import structlog

if TYPE_CHECKING:
    from datetime import datetime, tzinfo

    from ccr.db.models import PairedUser

log = structlog.get_logger()


_FORMAT_BY_MODE: dict[str, str] = {
    "full": "%H:%M - %d/%m/%Y",
    "time": "%H:%M",
}

_MONTH_ABBR: tuple[str, ...] = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def format_user_datetime(
    dt: datetime,
    user: PairedUser | None,
    mode: Literal["full", "short", "time"],
) -> str:
    """Render ``dt`` in the user's preferred timezone (or UTC) using ``mode``.

    Modes (24-hour, no AM/PM):

    * ``"full"`` → ``HH:MM - DD/MM/YYYY`` (e.g. ``"14:03 - 05/05/2026"``).
    * ``"short"`` → ``HH:MM - D Mon`` with no leading zero on the day and an
      English 3-letter month abbreviation (e.g. ``"14:03 - 5 May"``). The
      month abbreviation is locale-independent — we use a hardcoded English
      table so output is identical regardless of ``LC_TIME``.
    * ``"time"`` → ``HH:MM`` (e.g. ``"14:03"``).

    Timezone resolution:

    * If ``user`` is not ``None`` and ``user.timezone`` is a non-empty IANA name
      that resolves via :class:`zoneinfo.ZoneInfo`, ``dt`` is converted into
      that zone before formatting.
    * If ``user`` is ``None`` or ``user.timezone`` is ``None``, output uses UTC.
    * If ``user.timezone`` is set but :class:`zoneinfo.ZoneInfoNotFoundError`
      is raised on lookup, a structlog ``warning`` is emitted and output falls
      back to UTC. This helper never raises on a bad zone — bot replies must
      not crash because of a stale or invalid stored preference.
    """
    tz: tzinfo = UTC
    if user is not None and user.timezone is not None:
        try:
            tz = zoneinfo.ZoneInfo(user.timezone)
        except zoneinfo.ZoneInfoNotFoundError:
            log.warning(
                "format_user_datetime.unresolvable_timezone",
                tg_user_id=user.tg_user_id,
                timezone=user.timezone,
            )
            tz = UTC
    # SQLite drops tzinfo on round-trip, so a naive datetime read back from the
    # DB is the wall-clock UTC time without an attached zone. Treat naive input
    # as UTC so `astimezone` shifts correctly into the target zone.
    aware = dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)
    local = aware.astimezone(tz)
    if mode == "short":
        return f"{local:%H:%M} - {local.day} {_MONTH_ABBR[local.month - 1]}"
    return local.strftime(_FORMAT_BY_MODE[mode])


__all__ = ["format_user_datetime"]
