"""Deduplicate incoming-call alerts independently of the AI handoff setting."""
from datetime import datetime, timedelta, timezone
import re

from app.storage import connect

PREFIX = "kspa:incoming-notification:"


def valid_call_sid(call_sid: str) -> bool:
    return re.fullmatch(r"CA[0-9a-fA-F]{32}", call_sid) is not None


def claim_notification(call_sid: str) -> bool:
    if not valid_call_sid(call_sid):
        return False
    now = datetime.now(timezone.utc)
    with connect() as connection:
        connection.execute(
            "DELETE FROM system_state WHERE key LIKE ? AND value < ?",
            (PREFIX + "%", (now - timedelta(days=2)).isoformat()),
        )
        cursor = connection.execute(
            "INSERT OR IGNORE INTO system_state (key, value) VALUES (?, ?)",
            (PREFIX + call_sid, now.isoformat()),
        )
        return cursor.rowcount == 1


def release_notification(call_sid: str) -> None:
    with connect() as connection:
        connection.execute("DELETE FROM system_state WHERE key = ?", (PREFIX + call_sid,))
