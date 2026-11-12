"""When account tokens expire, and the last expiry seen for each account.

An expiry is one of three things: an aware ``datetime`` in UTC, ``NEVER`` for
a token without an expiry date, or ``None`` if it is not known. The last known
expiry is kept per account in a small file without secrets, so a token that
has already expired can still say when that happened.
"""

from __future__ import annotations

import json
import math
import os
import re
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .i18n import _, ngettext

NEVER = "never"
# A token this close to its expiry is shown as a warning, and the window mentions it on start.
WARN_DAYS = 14
TOAST_DAYS = 7

_OFFSET = re.compile(r"(.*\d)\s*([+-])(\d{2}):?(\d{2})")
_lock = threading.Lock()


# --- parsing and arithmetic -------------------------------------------------


def parse(value: object) -> datetime | None:
    """Read an expiry as servers send it; returns an aware UTC datetime or None.

    Accepts GitHub's header format (``2026-12-31 08:15:00 UTC``), ISO 8601 with
    or without an offset, and a bare date as GitLab sends it, which means
    midnight UTC at the start of that day.
    """
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.upper().endswith(" UTC"):
        text = text[:-4] + "+00:00"
    elif text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    # Python before 3.11 cannot read "+0000" or a space before the offset.
    offset = _OFFSET.fullmatch(text)
    if offset:
        text = f"{offset.group(1)}{offset.group(2)}{offset.group(3)}:{offset.group(4)}"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def to_iso(expires: datetime | str | None) -> str | None:
    """ISO 8601 in UTC for a date; None for NEVER and unknown."""
    if not isinstance(expires, datetime):
        return None
    return expires.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _now(now: datetime | None) -> datetime:
    return now or datetime.now(timezone.utc)


def days_left(expires: datetime | str | None, now: datetime | None = None) -> int | None:
    """Whole days until the expiry, rounded up; negative once it has passed, None without a date.

    A token that expires in three hours has 1 day left, one that expired three
    hours ago -1, so 0 only occurs at the exact moment of expiry.
    """
    if not isinstance(expires, datetime):
        return None
    seconds = (expires - _now(now)).total_seconds()
    days = math.ceil(abs(seconds) / 86400)
    return days if seconds >= 0 else -days


def is_expired(expires: datetime | str | None, now: datetime | None = None) -> bool:
    return isinstance(expires, datetime) and expires <= _now(now)


def expires_soon(expires: datetime | str | None, within: int = WARN_DAYS, now: datetime | None = None) -> bool:
    """Whether the token is still valid but expires within ``within`` days."""
    return isinstance(expires, datetime) and not is_expired(expires, now) and expires - _now(now) <= timedelta(days=within)


def describe(expires: datetime | str | None, format_date, now: datetime | None = None) -> tuple[str, str] | None:
    """Text for the expiry and its state ("ok", "warning", "expired"), or None if unknown.

    ``format_date`` turns the aware datetime into a date in the user's locale.
    """
    if expires == NEVER:
        return _("No expiry date"), "ok"
    if not isinstance(expires, datetime):
        return None
    if is_expired(expires, now):
        return _("Expired on {date}").format(date=format_date(expires)), "expired"
    if expires_soon(expires, WARN_DAYS, now):
        days = days_left(expires, now)
        return ngettext("Expires in {days} day", "Expires in {days} days", days).format(days=days), "warning"
    return _("Valid until {date}").format(date=format_date(expires)), "ok"


# --- last known expiry per account ------------------------------------------


def store_file() -> Path:
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return Path(base) / "gitswitch" / "expiry.json"


def _load() -> dict[str, dict[str, str]]:
    """Return ``{host: {user: ISO date | NEVER}}``."""
    try:
        stored = json.loads(store_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(stored, dict):
        return {}
    return {
        str(host): {str(user): value for user, value in users.items() if isinstance(value, str)}
        for host, users in stored.items()
        if isinstance(users, dict)
    }


def _save(stored: dict[str, dict[str, str]]) -> None:
    path = store_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(stored, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # The file names the user's servers and accounts.
    temporary.chmod(0o600)
    os.replace(temporary, path)


def recall(host: str, user: str) -> datetime | str | None:
    value = _load().get(host, {}).get(user)
    return NEVER if value == NEVER else parse(value)


def remember(host: str, user: str, expires: datetime | str | None) -> None:
    """Keep ``expires`` as the last known expiry of the account; unknown values change nothing."""
    value = NEVER if expires == NEVER else to_iso(expires)
    if value is None:
        return
    with _lock:
        stored = _load()
        if stored.get(host, {}).get(user) == value:
            return
        stored.setdefault(host, {})[user] = value
        try:
            _save(stored)
        except OSError:
            pass


def forget(host: str, user: str) -> None:
    with _lock:
        stored = _load()
        if user not in stored.get(host, {}):
            return
        del stored[host][user]
        if not stored[host]:
            del stored[host]
        try:
            _save(stored)
        except OSError:
            pass
