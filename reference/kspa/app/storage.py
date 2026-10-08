import sqlite3
import json
import os
from contextlib import contextmanager
from datetime import datetime, time, timedelta, timezone
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Iterator
from zoneinfo import ZoneInfo

from app.kspa_business import KSPA_TEMPORARY_CAPACITY_OVERRIDES


DATA_DIR = Path(
    os.getenv("DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or (Path(__file__).resolve().parent.parent / "data")
)
DATABASE_PATH = DATA_DIR / "receptionist.db"
LATEST_APPOINTMENT_START_MINUTE = 21 * 60 + 30
KSPA_PUBLIC_THERAPISTS = ("Staff A", "Staff B", "Capacity 3")
KSPA_BASE_CAPACITY = 2
KSPA_SIMULTANEOUS_CAPACITY = len(KSPA_PUBLIC_THERAPISTS)
KSPA_THERAPIST_DAYS_OFF = {
    "Staff B": frozenset({"2026-09-04", "2026-09-05"}),
}
KNOWN_THERAPISTS = ("Staff A", "Former Staff", "Staff B")
DEFAULT_STAFF_SHIFTS = {
    "Former Staff": ("10:00", "22:00"),
    "Staff B": ("10:00", "22:00"),
    "Staff A": ("18:30", "22:00"),
}
TEMPORARY_STAFF_OVERRIDES = {
    "2026-08-26": {
        "Former Staff": ("10:00", "22:00"),
        "Staff A": ("18:30", "22:00"),
    },
}
TEMPORARY_AVAILABILITY_STARTS = {
    "2026-08-26": {
        "Former Staff": "12:50",
    },
}
MANUAL_STAFFING_KEY_PREFIX = "manual_staffing:"
DAILY_CAPACITY_KEY_PREFIX = "kspa_daily_capacity:"
MAX_DAILY_CAPACITY = 20
_APPOINTMENT_SAVE_LOCK = RLock()


def _serialize_appointment_change(function):
    """Prevent simultaneous calls from claiming the same anonymous capacity slot."""
    @wraps(function)
    def locked(*args, **kwargs):
        with _APPOINTMENT_SAVE_LOCK:
            return function(*args, **kwargs)
    return locked


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(DATABASE_PATH)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def _get_system_state(connection: sqlite3.Connection, key: str) -> str | None:
    row = connection.execute(
        "SELECT value FROM system_state WHERE key = ?", (key,)
    ).fetchone()
    return row["value"] if row else None


def _set_system_state(connection: sqlite3.Connection, key: str, value: str) -> None:
    connection.execute(
        """
        INSERT INTO system_state (key, value) VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        (key, value),
    )


def _manual_staffing_exists(connection: sqlite3.Connection, schedule_date: str) -> bool:
    return _get_system_state(
        connection, f"{MANUAL_STAFFING_KEY_PREFIX}{schedule_date}"
    ) is not None


def get_daily_capacity(schedule_date: str) -> dict:
    with connect() as connection:
        raw = _get_system_state(connection, DAILY_CAPACITY_KEY_PREFIX + schedule_date)
    return json.loads(raw) if raw else {"date": schedule_date, "count": KSPA_BASE_CAPACITY, "override": False}


@_serialize_appointment_change
def set_daily_capacity(schedule_date: str, count: int | None, *, actor: str = "", now_local: datetime | None = None) -> dict:
    target = datetime.strptime(schedule_date, "%Y-%m-%d").date()
    current = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    if target < current.astimezone(ZoneInfo("America/Los_Angeles")).date():
        raise ValueError("不能修改已過去日期的人數。")
    if count is not None and (type(count) is not int or not 0 <= count <= MAX_DAILY_CAPACITY):
        raise ValueError(f"人數必須是 0 到 {MAX_DAILY_CAPACITY} 的整數。")
    with connect() as connection:
        key = DAILY_CAPACITY_KEY_PREFIX + target.isoformat()
        if count is None:
            connection.execute("DELETE FROM system_state WHERE key = ?", (key,))
        else:
            _set_system_state(connection, key, json.dumps({
                "date": target.isoformat(), "count": count, "override": True,
                "updated_by": actor, "updated_at": datetime.now(timezone.utc).isoformat(),
            }))
    return get_daily_capacity(target.isoformat())


def _capacity_name(index: int) -> str:
    return KSPA_PUBLIC_THERAPISTS[index] if index < len(KSPA_PUBLIC_THERAPISTS) else f"Capacity {index + 1}"


def _daily_capacity_source_ready(requested_date: str) -> bool:
    daily = get_daily_capacity(requested_date)
    if not daily["override"] or daily["count"] == 0:
        return True
    with connect() as connection:
        return _get_system_state(connection, "smiley_capacity_source") is not None


def _smiley_rows_for_date(connection: sqlite3.Connection, requested_date: str):
    raw = _get_system_state(connection, DAILY_CAPACITY_KEY_PREFIX + requested_date)
    if not raw:
        rows = connection.execute(
            "SELECT capacity_slot, start_time, end_time FROM smiley_capacity_availability "
            "WHERE date = ? ORDER BY capacity_slot, start_time", (requested_date,),
        ).fetchall()
        return rows, _unsynced_local_requests(connection, requested_date)
    count = json.loads(raw)["count"]
    source_raw = _get_system_state(connection, "smiley_capacity_source")
    if not source_raw or count == 0:
        return [], []
    # Reassign all occupied intervals using the new count. Simply hiding higher
    # numbered lanes can incorrectly free a time occupied on one of those lanes.
    from app.smiley import derive_free_intervals, merge_local_bookings
    source = json.loads(source_raw)
    local = [dict(row) for row in connection.execute(
        "SELECT id,requested_date,requested_time,duration_minutes,service,smiley_remote_id "
        "FROM appointment_requests WHERE requested_date=? AND status IN ('pending','confirmed','needs_review')",
        (requested_date,),
    )]
    busy, _ = merge_local_bookings(source["appointments"], local, source["timezone"])
    busy += source["blockers"]
    target = datetime.strptime(requested_date, "%Y-%m-%d").date()
    rows, _ = derive_free_intervals(source["business_hours"], busy, target, target,
        source["timezone"], capacity=count, base_capacity=count, temporary_capacity_overrides={})
    return rows, []


def _staff_shifts(
    connection: sqlite3.Connection, schedule_date: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT therapist, start_time, end_time
        FROM staff_shifts WHERE date = ? ORDER BY therapist
        """,
        (schedule_date,),
    ).fetchall()


def init_database() -> None:
    with connect() as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS availability (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                date TEXT NOT NULL,
                therapist TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                status TEXT NOT NULL,
                uploaded_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS appointment_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                customer_name TEXT NOT NULL,
                customer_phone TEXT,
                requested_date TEXT NOT NULL,
                requested_time TEXT NOT NULL,
                service TEXT NOT NULL,
                duration_minutes INTEGER NOT NULL,
                therapist TEXT,
                notes TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                cancelled_at TEXT
            );

            CREATE TABLE IF NOT EXISTS staff_shifts (
                date TEXT NOT NULL,
                therapist TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                PRIMARY KEY (date, therapist)
            );

            CREATE TABLE IF NOT EXISTS pending_schedule_uploads (
                user_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                rows_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS unanswered_questions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                caller_phone TEXT,
                question TEXT NOT NULL,
                context TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                answer TEXT,
                answered_at TEXT
            );

            CREATE TABLE IF NOT EXISTS knowledge_entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL,
                source_question_id INTEGER UNIQUE
            );

            CREATE TABLE IF NOT EXISTS call_sessions (
                stream_sid TEXT PRIMARY KEY,
                caller_phone TEXT,
                started_at TEXT NOT NULL,
                ended_at TEXT
            );

            CREATE TABLE IF NOT EXISTS call_transcripts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                stream_sid TEXT NOT NULL,
                created_at TEXT NOT NULL,
                speaker TEXT NOT NULL,
                text TEXT NOT NULL,
                item_id TEXT,
                UNIQUE(stream_sid, speaker, item_id)
            );

            CREATE TABLE IF NOT EXISTS blocked_callers (
                phone_number TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                reason TEXT NOT NULL,
                category TEXT NOT NULL,
                confidence REAL NOT NULL,
                source_stream_sid TEXT,
                active INTEGER NOT NULL DEFAULT 1
            );

            CREATE TABLE IF NOT EXISTS call_spam_reviews (
                stream_sid TEXT PRIMARY KEY,
                reviewed_at TEXT NOT NULL,
                category TEXT NOT NULL,
                confidence REAL NOT NULL,
                reason TEXT NOT NULL,
                action TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS system_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS smiley_availability (
                date TEXT NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (date, start_time, end_time)
            );

            CREATE TABLE IF NOT EXISTS smiley_capacity_availability (
                date TEXT NOT NULL,
                capacity_slot INTEGER NOT NULL,
                start_time TEXT NOT NULL,
                end_time TEXT NOT NULL,
                synced_at TEXT NOT NULL,
                PRIMARY KEY (date, capacity_slot, start_time, end_time)
            );
            """
        )
        appointment_columns = {
            row["name"]
            for row in connection.execute("PRAGMA table_info(appointment_requests)")
        }
        if "cancelled_at" not in appointment_columns:
            connection.execute(
                "ALTER TABLE appointment_requests ADD COLUMN cancelled_at TEXT"
            )
        for column in ("smiley_remote_id", "smiley_write_state"):
            if column not in appointment_columns:
                connection.execute(
                    f"ALTER TABLE appointment_requests ADD COLUMN {column} TEXT"
                )
        # Backfill shifts for schedules saved before staff_shifts was introduced.
        existing_dates = [
            row["date"]
            for row in connection.execute(
                "SELECT DISTINCT date FROM availability"
            ).fetchall()
        ]
        connection.executemany(
            """
            INSERT OR IGNORE INTO staff_shifts
                (date, therapist, start_time, end_time)
            VALUES (?, ?, ?, ?)
            """,
            [
                (date, therapist, shift[0], shift[1])
                for date in existing_dates
                for therapist, shift in DEFAULT_STAFF_SHIFTS.items()
            ],
        )


def replace_schedule(rows: list[dict[str, str]]) -> int:
    uploaded_at = datetime.now(timezone.utc).isoformat()
    schedule_dates = sorted({row["date"] for row in rows})
    with connect() as connection:
        manual_shifts = {
            schedule_date: _staff_shifts(connection, schedule_date)
            for schedule_date in schedule_dates
            if _manual_staffing_exists(connection, schedule_date)
        }
        connection.execute("DELETE FROM availability")
        connection.execute("DELETE FROM staff_shifts")
        connection.executemany(
            """
            INSERT INTO availability
                (date, therapist, start_time, end_time, status, uploaded_at)
            VALUES
                (:date, :therapist, :start_time, :end_time, :status, :uploaded_at)
            """,
            [{**row, "uploaded_at": uploaded_at} for row in rows],
        )
        connection.executemany(
            """
            INSERT INTO staff_shifts (date, therapist, start_time, end_time)
            VALUES (?, ?, ?, ?)
            """,
            [
                (date, therapist, shift[0], shift[1])
                for date in schedule_dates
                for therapist, shift in DEFAULT_STAFF_SHIFTS.items()
            ],
        )
        for schedule_date, shifts in manual_shifts.items():
            connection.execute(
                "DELETE FROM staff_shifts WHERE date = ?", (schedule_date,)
            )
            connection.executemany(
                """
                INSERT INTO staff_shifts (date, therapist, start_time, end_time)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (
                        schedule_date,
                        shift["therapist"],
                        shift["start_time"],
                        shift["end_time"],
                    )
                    for shift in shifts
                ],
            )
    return len(rows)


def default_schedule_rows(schedule_date: str) -> list[dict[str, str]]:
    datetime.strptime(schedule_date, "%Y-%m-%d")
    staff_shifts = TEMPORARY_STAFF_OVERRIDES.get(
        schedule_date, DEFAULT_STAFF_SHIFTS
    )
    availability_starts = TEMPORARY_AVAILABILITY_STARTS.get(schedule_date, {})
    return [
        {
            "date": schedule_date,
            "therapist": therapist,
            "start_time": availability_starts.get(therapist, shift[0]),
            "end_time": shift[1],
            "status": "available",
        }
        for therapist, shift in staff_shifts.items()
    ]


def apply_temporary_staff_override(schedule_date: str) -> bool:
    """Keep a date-specific staffing exception authoritative for that day."""
    staff_shifts = TEMPORARY_STAFF_OVERRIDES.get(schedule_date)
    if not staff_shifts:
        return False
    availability_starts = TEMPORARY_AVAILABILITY_STARTS.get(schedule_date, {})

    allowed_therapists = tuple(staff_shifts)
    placeholders = ", ".join("?" for _ in allowed_therapists)
    with connect() as connection:
        if _manual_staffing_exists(connection, schedule_date):
            return False
        connection.execute(
            f"DELETE FROM availability WHERE date = ? AND therapist NOT IN ({placeholders})",
            (schedule_date, *allowed_therapists),
        )
        connection.execute(
            f"DELETE FROM staff_shifts WHERE date = ? AND therapist NOT IN ({placeholders})",
            (schedule_date, *allowed_therapists),
        )
        for therapist, (start_time, end_time) in staff_shifts.items():
            available_from = availability_starts.get(therapist, start_time)
            connection.execute(
                """
                INSERT INTO staff_shifts (date, therapist, start_time, end_time)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(date, therapist) DO UPDATE SET
                    start_time = excluded.start_time,
                    end_time = excluded.end_time
                """,
                (schedule_date, therapist, start_time, end_time),
            )
            connection.execute(
                """
                UPDATE availability
                SET start_time = MAX(start_time, ?), end_time = MIN(end_time, ?)
                WHERE date = ? AND therapist = ?
                """,
                (available_from, end_time, schedule_date, therapist),
            )
            connection.execute(
                """
                DELETE FROM availability
                WHERE date = ? AND therapist = ? AND start_time >= end_time
                """,
                (schedule_date, therapist),
            )
    return True


def set_daily_staffing(schedule_date: str, shifts: list[dict[str, str]]) -> int:
    """Set the staff physically in store without deleting schedule bookings."""
    datetime.strptime(schedule_date, "%Y-%m-%d")
    normalized: dict[str, tuple[str, str]] = {}
    for shift in shifts:
        therapist = str(shift.get("therapist", "")).strip()
        start_time = str(shift.get("start_time", "")).strip()
        end_time = str(shift.get("end_time", "")).strip()
        if therapist not in KNOWN_THERAPISTS:
            raise ValueError(f"Unknown therapist: {therapist}")
        start_minute = _minutes(start_time)
        end_minute = _minutes(end_time)
        if start_minute >= end_minute:
            raise ValueError(f"{therapist}'s end time must be after the start time.")
        normalized[therapist] = (start_time, end_time)

    uploaded_at = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        connection.execute("DELETE FROM staff_shifts WHERE date = ?", (schedule_date,))
        connection.executemany(
            """
            INSERT INTO staff_shifts (date, therapist, start_time, end_time)
            VALUES (?, ?, ?, ?)
            """,
            [
                (schedule_date, therapist, start_time, end_time)
                for therapist, (start_time, end_time) in normalized.items()
            ],
        )
        connection.executemany(
            """
            INSERT INTO availability
                (date, therapist, start_time, end_time, status, uploaded_at)
            SELECT ?, ?, ?, ?, 'available', ?
            WHERE NOT EXISTS (
                SELECT 1 FROM availability WHERE date = ? AND therapist = ?
            )
            """,
            [
                (
                    schedule_date, therapist, start_time, end_time, uploaded_at,
                    schedule_date, therapist,
                )
                for therapist, (start_time, end_time) in normalized.items()
            ],
        )
        _set_system_state(
            connection, f"{MANUAL_STAFFING_KEY_PREFIX}{schedule_date}", uploaded_at
        )
    return len(normalized)


def reset_daily_schedule_if_due(
    *,
    now_local: datetime | None = None,
) -> dict:
    current = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    schedule_date = current.date().isoformat()
    if current.time() < time(5, 0):
        return {"reset": False, "reason": "before_5am", "date": schedule_date}

    with connect() as connection:
        last_reset_date = _get_system_state(connection, "daily_schedule_reset_date")
    if last_reset_date == schedule_date:
        return {"reset": False, "reason": "already_reset", "date": schedule_date}

    with connect() as connection:
        manual_staffing = _manual_staffing_exists(connection, schedule_date)
        if manual_staffing:
            _set_system_state(connection, "daily_schedule_reset_date", schedule_date)
            return {
                "reset": False,
                "reason": "manual_staffing",
                "date": schedule_date,
            }

    count = replace_schedule(default_schedule_rows(schedule_date))
    with connect() as connection:
        _set_system_state(connection, "daily_schedule_reset_date", schedule_date)
    return {"reset": True, "date": schedule_date, "rows": count}


def cleanup_expired_data(
    *,
    now_local: datetime | None = None,
    force: bool = False,
) -> dict:
    """Delete operational records only after their 48-hour retention window."""
    current = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    if current.tzinfo is None:
        current = current.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    with connect() as connection:
        last_cleanup = _get_system_state(connection, "last_retention_cleanup_at")
        if last_cleanup and not force:
            last_run = datetime.fromisoformat(last_cleanup)
            if current - last_run < timedelta(hours=1):
                return {"cleaned": False, "reason": "less_than_one_hour"}

        local_cutoff = current - timedelta(hours=48)
        utc_cutoff = local_cutoff.astimezone(timezone.utc).isoformat()
        appointment_cutoff = local_cutoff.strftime("%Y-%m-%d %H:%M")

        cleanup_queries = {
            "cancelled": (
                "DELETE FROM appointment_requests WHERE status = 'cancelled' "
                "AND COALESCE(cancelled_at, created_at) < ?", utc_cutoff,
            ),
            "completed": (
                "DELETE FROM appointment_requests WHERE status != 'cancelled' "
                "AND (requested_date || ' ' || requested_time) < ?", appointment_cutoff,
            ),
            "availability": (
                "DELETE FROM availability WHERE (date || ' ' || end_time) < ?",
                appointment_cutoff,
            ),
            "staff_shifts": (
                "DELETE FROM staff_shifts WHERE (date || ' ' || end_time) < ?",
                appointment_cutoff,
            ),
            "staged_uploads": (
                "DELETE FROM pending_schedule_uploads WHERE created_at < ?", utc_cutoff,
            ),
            "call_transcripts": (
                "DELETE FROM call_transcripts WHERE created_at < ?", utc_cutoff,
            ),
            "call_sessions": (
                "DELETE FROM call_sessions WHERE started_at < ?", utc_cutoff,
            ),
            "call_spam_reviews": (
                "DELETE FROM call_spam_reviews WHERE reviewed_at < ?", utc_cutoff,
            ),
        }
        deleted = {
            name: connection.execute(sql, (cutoff,)).rowcount
            for name, (sql, cutoff) in cleanup_queries.items()
        }
        _set_system_state(connection, "last_retention_cleanup_at", current.isoformat())
    return {
        "cleaned": True,
        "appointments": deleted["cancelled"] + deleted["completed"],
        **{key: value for key, value in deleted.items() if key not in {"cancelled", "completed"}},
    }


def start_call_session(stream_sid: str, caller_phone: str = "") -> None:
    now = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO call_sessions (stream_sid, caller_phone, started_at)
            VALUES (?, ?, ?)
            ON CONFLICT(stream_sid) DO UPDATE SET caller_phone = excluded.caller_phone
            """,
            (stream_sid, caller_phone, now),
        )


def finish_call_session(stream_sid: str) -> None:
    if not stream_sid:
        return
    with connect() as connection:
        connection.execute(
            "UPDATE call_sessions SET ended_at = ? WHERE stream_sid = ?",
            (datetime.now(timezone.utc).isoformat(), stream_sid),
        )


def normalize_phone_number(phone_number: str) -> str:
    cleaned = phone_number.strip()
    if not cleaned:
        return ""
    digits = "".join(character for character in cleaned if character.isdigit())
    if not digits:
        return ""
    return f"+{digits}"


def is_caller_blocked(phone_number: str) -> bool:
    normalized = normalize_phone_number(phone_number)
    if not normalized:
        return False
    with connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM blocked_callers WHERE phone_number = ? AND active = 1",
            (normalized,),
        ).fetchone()
    return row is not None


def block_caller(
    phone_number: str,
    reason: str,
    category: str,
    confidence: float,
    source_stream_sid: str = "",
) -> bool:
    normalized = normalize_phone_number(phone_number)
    if not normalized:
        return False
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO blocked_callers (
                phone_number, created_at, reason, category, confidence,
                source_stream_sid, active
            ) VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(phone_number) DO UPDATE SET
                created_at = excluded.created_at,
                reason = excluded.reason,
                category = excluded.category,
                confidence = excluded.confidence,
                source_stream_sid = excluded.source_stream_sid,
                active = 1
            """,
            (
                normalized,
                datetime.now(timezone.utc).isoformat(),
                reason.strip(),
                category.strip(),
                float(confidence),
                source_stream_sid or None,
            ),
        )
    return True


def list_blocked_callers() -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT * FROM blocked_callers
            WHERE active = 1
            ORDER BY created_at DESC
            """
        ).fetchall()


def unblock_caller_by_suffix(phone_suffix: str) -> dict:
    digits = "".join(character for character in phone_suffix if character.isdigit())
    if len(digits) != 4 and len(digits) < 10:
        return {"unblocked": False, "reason": "last4_required"}
    with connect() as connection:
        if len(digits) == 4:
            rows = connection.execute(
                """
                SELECT phone_number FROM blocked_callers
                WHERE active = 1 AND substr(phone_number, -4) = ?
                """,
                (digits,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT phone_number FROM blocked_callers
                WHERE active = 1 AND phone_number = ?
                """,
                (f"+{digits}",),
            ).fetchall()
        if not rows:
            return {"unblocked": False, "reason": "not_found"}
        if len(rows) > 1:
            return {"unblocked": False, "reason": "ambiguous", "matches": len(rows)}
        connection.execute(
            "UPDATE blocked_callers SET active = 0 WHERE phone_number = ?",
            (rows[0]["phone_number"],),
        )
    return {"unblocked": True, "phone_number": rows[0]["phone_number"]}


def save_call_spam_review(
    stream_sid: str,
    category: str,
    confidence: float,
    reason: str,
    action: str,
) -> bool:
    if not stream_sid:
        return False
    with connect() as connection:
        result = connection.execute(
            """
            INSERT OR IGNORE INTO call_spam_reviews (
                stream_sid, reviewed_at, category, confidence, reason, action
            ) VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                stream_sid,
                datetime.now(timezone.utc).isoformat(),
                category,
                float(confidence),
                reason.strip(),
                action,
            ),
        )
    return result.rowcount == 1


def is_call_spam_reviewed(stream_sid: str) -> bool:
    if not stream_sid:
        return False
    with connect() as connection:
        row = connection.execute(
            "SELECT 1 FROM call_spam_reviews WHERE stream_sid = ?",
            (stream_sid,),
        ).fetchone()
    return row is not None


def append_call_transcript(
    stream_sid: str,
    speaker: str,
    text: str,
    *,
    item_id: str = "",
) -> None:
    cleaned = " ".join(text.split()).strip()
    if not stream_sid or not cleaned or speaker not in {"customer", "kiki"}:
        return
    with connect() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO call_transcripts
                (stream_sid, created_at, speaker, text, item_id)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                stream_sid,
                datetime.now(timezone.utc).isoformat(),
                speaker,
                cleaned,
                item_id or None,
            ),
        )


def list_recent_calls(
    limit: int = 10,
    *,
    started_at_or_after: str = "",
) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT c.*, COUNT(t.id) AS transcript_count
            FROM call_sessions c
            LEFT JOIN call_transcripts t ON t.stream_sid = c.stream_sid
            WHERE (? = '' OR c.started_at >= ?)
            GROUP BY c.stream_sid
            ORDER BY c.started_at DESC
            LIMIT ?
            """,
            (started_at_or_after, started_at_or_after, limit),
        ).fetchall()


def find_recent_call_by_phone_suffix(phone_suffix: str) -> sqlite3.Row | None:
    digits = "".join(character for character in phone_suffix if character.isdigit())
    if len(digits) != 4:
        return None
    with connect() as connection:
        return connection.execute(
            """
            SELECT c.*, COUNT(t.id) AS transcript_count
            FROM call_sessions c
            LEFT JOIN call_transcripts t ON t.stream_sid = c.stream_sid
            WHERE substr(c.caller_phone, -4) = ?
            GROUP BY c.stream_sid
            ORDER BY c.started_at DESC
            LIMIT 1
            """,
            (digits,),
        ).fetchone()


def get_call_transcript(stream_sid_or_suffix: str) -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT t.* FROM call_transcripts t
            JOIN call_sessions c ON c.stream_sid = t.stream_sid
            WHERE t.stream_sid = ? OR substr(t.stream_sid, -8) = ?
            ORDER BY id
            """,
            (stream_sid_or_suffix, stream_sid_or_suffix[-8:]),
        ).fetchall()


def set_appointment_notification_target(target_id: str | None) -> None:
    with connect() as connection:
        if target_id and target_id.startswith(("C", "R")):
            _set_system_state(connection, "appointment_notification_target", target_id)
        else:
            connection.execute(
                "DELETE FROM system_state WHERE key = 'appointment_notification_target'"
            )


def get_appointment_notification_target() -> str:
    with connect() as connection:
        target_id = _get_system_state(
            connection, "appointment_notification_target"
        ) or ""
        return target_id if target_id.startswith(("C", "R")) else ""


def _minutes(value: str) -> int:
    parsed = datetime.strptime(value, "%H:%M")
    return parsed.hour * 60 + parsed.minute


def _smiley_therapist_choices(
    requested_date: str,
    therapist_preference: str = "",
    requested_time: str = "",
) -> tuple[list[str], list[str], list[str], str]:
    daily = get_daily_capacity(requested_date)
    if daily["override"]:
        choices = [_capacity_name(index) for index in range(daily["count"])]
        return choices, choices, [], "" if choices else "No booking capacity for this date."
    capacity_slots = KSPA_PUBLIC_THERAPISTS
    temporary_capacity = KSPA_TEMPORARY_CAPACITY_OVERRIDES.get(requested_date)
    if (
        not temporary_capacity
        or not requested_time
        or _minutes(requested_time) < _minutes(temporary_capacity["start_time"])
    ):
        capacity_slots = KSPA_PUBLIC_THERAPISTS[:KSPA_BASE_CAPACITY]
    off = [
        name for name in capacity_slots
        if requested_date in KSPA_THERAPIST_DAYS_OFF.get(name, frozenset())
    ]
    available = [name for name in capacity_slots if name not in off]
    requested_name = therapist_preference.strip()
    if not requested_name:
        return available, available, off, ""

    canonical = next(
        (
            name for name in capacity_slots
            if name.casefold() == requested_name.casefold()
        ),
        "",
    )
    if not canonical:
        return [], available, off, "That therapist is not on K Spa's current staff list."
    if canonical in off:
        return [], available, off, f"{canonical} is not working on {requested_date}."
    return [canonical], available, off, ""


def get_local_smiley_bookings() -> list[dict]:
    with connect() as connection:
        return [dict(row) for row in connection.execute(
            """SELECT id, requested_date, requested_time, duration_minutes,
                      service, smiley_remote_id
               FROM appointment_requests
               WHERE status IN ('pending', 'confirmed', 'needs_review')"""
        )]


def claim_smiley_write(request_id: int) -> dict | None:
    """Persist the claim before networking; never retry an uncertain POST."""
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM appointment_requests WHERE id = ?", (request_id,)
        ).fetchone()
        if not row or row["status"] != "confirmed":
            return None
        result = dict(row)
        if row["smiley_write_state"]:
            result["claimed"] = False
            return result
        connection.execute(
            "UPDATE appointment_requests SET smiley_write_state = 'writing' WHERE id = ?",
            (request_id,),
        )
        result["claimed"] = True
        return result


def finish_smiley_write(request_id: int, state: str, remote_id: str = "") -> None:
    if state not in {"written", "needs_review"}:
        raise ValueError("Invalid Smiley write state")
    with connect() as connection:
        connection.execute(
            """UPDATE appointment_requests
               SET smiley_write_state = ?, smiley_remote_id = ? WHERE id = ?""",
            (state, remote_id or None, request_id),
        )


def _unsynced_local_requests(connection, requested_date):
    raw = _get_system_state(connection, "smiley_snapshot_local")
    represented = json.loads(raw) if raw else {}
    rows = connection.execute(
        """SELECT id, requested_time, duration_minutes, therapist
           FROM appointment_requests
           WHERE requested_date = ? AND status IN ('pending', 'confirmed', 'needs_review')""",
        (requested_date,),
    ).fetchall()
    return [row for row in rows if represented.get(str(row["id"])) != [
        requested_date, row["requested_time"], row["duration_minutes"],
    ]]


def replace_smiley_availability(sync_result: dict) -> int:
    rows = list(sync_result.get("free_rows") or [])
    synced_at = str(sync_result["synced_at"])
    normalized_rows = []
    for row in rows:
        slots = (
            [int(row["capacity_slot"])]
            if row.get("capacity_slot")
            else list(range(1, KSPA_BASE_CAPACITY + 1))
        )
        normalized_rows.extend(
            {**row, "capacity_slot": slot, "synced_at": synced_at}
            for slot in slots
        )
    state = {
        "status": "ok",
        "synced_at": synced_at,
        "window_start": str(sync_result["window_start"]),
        "window_end": str(sync_result["window_end"]),
        "timezone": str(sync_result.get("timezone") or "America/Los_Angeles"),
        "row_count": len(normalized_rows),
        # Store business operating intervals independently from remaining capacity.
        "business_hours": list(sync_result.get("shift_rows") or []),
        "simultaneous_capacity_default": KSPA_BASE_CAPACITY,
        "temporary_capacity_overrides": KSPA_TEMPORARY_CAPACITY_OVERRIDES,
        "error": "",
    }
    with connect() as connection:
        connection.execute("DELETE FROM smiley_capacity_availability")
        connection.executemany(
            """
            INSERT INTO smiley_capacity_availability
                (date, capacity_slot, start_time, end_time, synced_at)
            VALUES (:date, :capacity_slot, :start_time, :end_time, :synced_at)
            """,
            normalized_rows,
        )
        _set_system_state(connection, "smiley_sync", json.dumps(state))
        _set_system_state(connection, "smiley_snapshot_local", json.dumps(
            sync_result.get("represented_local_bookings") or {}
        ))
        if sync_result.get("capacity_source") is not None:
            _set_system_state(connection, "smiley_capacity_source", json.dumps(sync_result["capacity_source"]))
        else:
            connection.execute("DELETE FROM system_state WHERE key = 'smiley_capacity_source'")
    return len(normalized_rows)


def mark_smiley_sync_error(message: str) -> None:
    with connect() as connection:
        existing = _get_system_state(connection, "smiley_sync")
        try:
            state = json.loads(existing) if existing else {}
        except json.JSONDecodeError:
            state = {}
        state.update({
            "status": "error",
            "error": message[:300],
            "last_attempt_at": datetime.now(timezone.utc).isoformat(),
        })
        _set_system_state(connection, "smiley_sync", json.dumps(state))


def get_smiley_sync_status() -> dict:
    with connect() as connection:
        raw = _get_system_state(connection, "smiley_sync")
    if not raw:
        return {"status": "not_configured"}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"status": "error", "error": "Invalid Smiley sync state."}


def get_smiley_business_hours(requested_date: str = "", *, now_local: datetime | None = None) -> dict:
    state = get_smiley_sync_status()
    zone = ZoneInfo(str(state.get("timezone") or "America/Los_Angeles"))
    now = now_local or datetime.now(zone)
    if now.tzinfo is None:
        now = now.replace(tzinfo=zone)
    now = now.astimezone(zone)
    requested_date = requested_date or now.date().isoformat()
    result = {"topic": "hours", "hours_loaded": False,
              "requested_date": requested_date, "source": "Smiley business hours"}
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
        synced = datetime.fromisoformat(state.get("synced_at", ""))
        if synced.tzinfo is None:
            synced = synced.replace(tzinfo=timezone.utc)
        fresh = now.astimezone(timezone.utc) - synced <= timedelta(minutes=15)
    except (TypeError, ValueError):
        fresh = False
    hours = state.get("business_hours")
    if (not fresh or not hours
            or not state.get("window_start", "") <= requested_date <= state.get("window_end", "")):
        return {**result, "error": "Business hours cannot be confirmed right now."}
    periods = sorted(
        [{"start_time": row["start_time"], "end_time": row["end_time"]}
         for row in hours if row["date"] == requested_date],
        key=lambda row: row["start_time"],
    )
    return {**result, "hours_loaded": True, "closed": not periods, "periods": periods,
            "appointment_availability_confirmed": False,
            "last_synced_at": state["synced_at"]}


def check_smiley_availability(
    requested_date: str,
    requested_time: str,
    duration_minutes: int | None = None,
    therapist_preference: str = "",
    *,
    now_local: datetime | None = None,
    max_age_minutes: int = 15,
) -> dict:
    if duration_minutes is None:
        # Thirty minutes is the shortest supported appointment, not an assumed
        # customer choice. Reuse all freshness, capacity, and local conflict checks.
        result = check_smiley_availability(
            requested_date, requested_time, 30, therapist_preference,
            now_local=now_local, max_age_minutes=max_age_minutes,
        )
        result.update({
            "availability_scope": "start_time",
            "duration_minutes": None,
            "minimum_checked_minutes": 30,
            "requires_duration_recheck": True,
            "suggested_times": [],
        })
        for match in result.get("matches", []):
            match.pop("end_time", None)
        return result
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
        datetime.strptime(requested_time, "%H:%M")
    except ValueError:
        return {"schedule_loaded": False, "available": False, "error": "Use YYYY-MM-DD and HH:MM."}
    if duration_minutes <= 0 or duration_minutes > 240:
        return {"schedule_loaded": False, "available": False, "error": "Invalid duration."}

    state = get_smiley_sync_status()
    synced_at_text = state.get("synced_at", "")
    try:
        synced_at = datetime.fromisoformat(synced_at_text)
        if synced_at.tzinfo is None:
            synced_at = synced_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        synced_at = None
    now_utc = datetime.now(timezone.utc)
    local_now = datetime.now(ZoneInfo(str(state.get("timezone") or "America/Los_Angeles")))
    if now_local is not None:
        candidate = now_local
        if candidate.tzinfo is None:
            candidate = candidate.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
        now_utc = candidate.astimezone(timezone.utc)
        local_now = candidate.astimezone(
            ZoneInfo(str(state.get("timezone") or "America/Los_Angeles"))
        )
    in_window = state.get("window_start", "") <= requested_date <= state.get("window_end", "")
    fresh = bool(synced_at and now_utc - synced_at <= timedelta(minutes=max_age_minutes))
    if not in_window or not fresh or not _daily_capacity_source_ready(requested_date):
        return {
            "schedule_loaded": False,
            "available": False,
            "requested_date": requested_date,
            "requested_time": requested_time,
            "duration_minutes": duration_minutes,
            "matches": [],
            "suggested_times": [],
            "source": "Smiley",
            "error": "Smiley availability is temporarily unavailable or out of date.",
        }

    requested_start = _minutes(requested_time)
    requested_end = requested_start + duration_minutes
    if requested_date < local_now.date().isoformat():
        return {
            "schedule_loaded": True,
            "available": False,
            "requested_date": requested_date,
            "requested_time": requested_time,
            "duration_minutes": duration_minutes,
            "matches": [],
            "suggested_times": [],
            "source": "Smiley",
            "error": "The requested date is in the past.",
        }
    therapist_choices, therapists_available, therapists_off, therapist_error = (
        _smiley_therapist_choices(
            requested_date, therapist_preference, requested_time
        )
    )
    if not therapist_choices:
        return {
            "schedule_loaded": True,
            "available": False,
            "requested_date": requested_date,
            "requested_time": requested_time,
            "duration_minutes": duration_minutes,
            "matches": [],
            "suggested_times": [],
            "therapists_available": therapists_available,
            "therapists_off": therapists_off,
            "source": "Smiley",
            "last_synced_at": synced_at_text,
            "error": therapist_error,
        }
    earliest_start = 0
    if requested_date == local_now.date().isoformat():
        earliest_start = local_now.hour * 60 + local_now.minute
        if local_now.second or local_now.microsecond:
            earliest_start += 1
    with connect() as connection:
        rows, local_requests = _smiley_rows_for_date(connection, requested_date)

    def conflicts(start: int, end: int, therapist: str) -> bool:
        return any(
            start < _minutes(item["requested_time"]) + int(item["duration_minutes"])
            and end > _minutes(item["requested_time"])
            and (
                not str(item["therapist"] or "").strip()
                or str(item["therapist"]).casefold() == therapist.casefold()
            )
            for item in local_requests
        )

    matches: list[dict[str, str]] = []
    suggestions: list[dict[str, str]] = []
    for row in rows:
        slot_index = int(row["capacity_slot"]) - 1
        if slot_index < 0 or slot_index >= MAX_DAILY_CAPACITY:
            continue
        therapist = _capacity_name(slot_index)
        if therapist not in therapist_choices:
            continue
        interval_start = _minutes(row["start_time"])
        interval_end = _minutes(row["end_time"])
        if (
            requested_start >= max(interval_start, earliest_start)
            and requested_end <= interval_end
            and not conflicts(requested_start, requested_end, therapist)
        ):
            matches.append({
                "therapist": therapist,
                "start_time": requested_time,
                "end_time": f"{requested_end // 60:02d}:{requested_end % 60:02d}",
            })
        slot = interval_start
        while slot + duration_minutes <= interval_end and len(suggestions) < 5:
            if (
                slot != requested_start
                and slot >= earliest_start
                and not conflicts(slot, slot + duration_minutes, therapist)
            ):
                suggestions.append({
                    "therapist": therapist,
                    "start_time": f"{slot // 60:02d}:{slot % 60:02d}",
                    "end_time": f"{(slot + duration_minutes) // 60:02d}:{(slot + duration_minutes) % 60:02d}",
                })
            slot += 30

    return {
        "schedule_loaded": True,
        "available": bool(matches),
        "requested_date": requested_date,
        "requested_time": requested_time,
        "duration_minutes": duration_minutes,
        "matches": matches[:5],
        "suggested_times": suggestions[:5],
        "therapists_available": therapists_available,
        "therapists_off": therapists_off,
        "source": "Smiley",
        "last_synced_at": synced_at_text,
    }


def list_smiley_openings(
    requested_date: str,
    duration_minutes: int | None = None,
    therapist_preference: str = "",
    *,
    now_local: datetime | None = None,
    max_age_minutes: int = 15,
) -> dict:
    """Return deterministic openings when a caller asks for options on a date."""
    if duration_minutes is None:
        result = list_smiley_openings(
            requested_date, 30, therapist_preference,
            now_local=now_local, max_age_minutes=max_age_minutes,
        )
        result.update(availability_scope="start_time", duration_minutes=None,
                      minimum_checked_minutes=30, requires_duration_recheck=True)
        for opening in result.get("openings", []):
            opening.pop("end_time", None)
        return result
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
    except ValueError:
        return {"schedule_loaded": False, "openings": [], "error": "Use YYYY-MM-DD."}
    if duration_minutes <= 0 or duration_minutes > 240:
        return {"schedule_loaded": False, "openings": [], "error": "Invalid duration."}

    state = get_smiley_sync_status()
    synced_at_text = state.get("synced_at", "")
    try:
        synced_at = datetime.fromisoformat(synced_at_text)
        if synced_at.tzinfo is None:
            synced_at = synced_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        synced_at = None
    local_zone = ZoneInfo(str(state.get("timezone") or "America/Los_Angeles"))
    local_now = now_local or datetime.now(local_zone)
    if local_now.tzinfo is None:
        local_now = local_now.replace(tzinfo=local_zone)
    local_now = local_now.astimezone(local_zone)
    fresh = bool(
        synced_at
        and local_now.astimezone(timezone.utc) - synced_at
        <= timedelta(minutes=max_age_minutes)
    )
    in_window = state.get("window_start", "") <= requested_date <= state.get("window_end", "")
    if not fresh or not in_window or not _daily_capacity_source_ready(requested_date):
        return {
            "schedule_loaded": False,
            "requested_date": requested_date,
            "duration_minutes": duration_minutes,
            "openings": [],
            "source": "Smiley",
            "error": "Smiley availability is temporarily unavailable or out of date.",
        }
    if requested_date < local_now.date().isoformat():
        return {
            "schedule_loaded": True,
            "requested_date": requested_date,
            "duration_minutes": duration_minutes,
            "openings": [],
            "source": "Smiley",
            "error": "The requested date is in the past.",
        }
    therapist_choices, therapists_available, therapists_off, therapist_error = (
        _smiley_therapist_choices(requested_date, therapist_preference)
    )
    if not therapist_choices:
        return {
            "schedule_loaded": True,
            "requested_date": requested_date,
            "duration_minutes": duration_minutes,
            "openings": [],
            "therapists_available": therapists_available,
            "therapists_off": therapists_off,
            "source": "Smiley",
            "last_synced_at": synced_at_text,
            "error": therapist_error,
        }

    earliest_start = 0
    if requested_date == local_now.date().isoformat():
        earliest_start = local_now.hour * 60 + local_now.minute
        if local_now.second or local_now.microsecond:
            earliest_start += 1
    with connect() as connection:
        rows, local_requests = _smiley_rows_for_date(connection, requested_date)

    def conflicts(start: int, end: int, therapist: str) -> bool:
        return any(
            start < _minutes(item["requested_time"]) + int(item["duration_minutes"])
            and end > _minutes(item["requested_time"])
            and (
                not str(item["therapist"] or "").strip()
                or str(item["therapist"]).casefold() == therapist.casefold()
            )
            for item in local_requests
        )

    openings: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        slot_index = int(row["capacity_slot"]) - 1
        if slot_index < 0 or slot_index >= MAX_DAILY_CAPACITY:
            continue
        therapist = _capacity_name(slot_index)
        if therapist not in therapist_choices:
            continue
        slot = max(_minutes(row["start_time"]), earliest_start)
        if slot % 15:
            slot += 15 - slot % 15
        interval_end = _minutes(row["end_time"])
        while slot + duration_minutes <= interval_end:
            start_time = f"{slot // 60:02d}:{slot % 60:02d}"
            key = (therapist, start_time)
            if key not in seen and not conflicts(slot, slot + duration_minutes, therapist):
                seen.add(key)
                openings.append({
                    "therapist": therapist,
                    "start_time": start_time,
                    "end_time": f"{(slot + duration_minutes) // 60:02d}:{(slot + duration_minutes) % 60:02d}",
                })
            slot += 30
    return {
        "schedule_loaded": True,
        "requested_date": requested_date,
        "duration_minutes": duration_minutes,
        "openings": list({
            start: next(item for item in openings if item["start_time"] == start)
            for start in sorted({item["start_time"] for item in openings})
        }.values())[:8],
        "therapists_available": therapists_available,
        "therapists_off": therapists_off,
        "source": "Smiley",
        "last_synced_at": synced_at_text,
    }


def check_availability(
    requested_date: str,
    requested_time: str,
    duration_minutes: int,
    therapist_preference: str = "",
    *,
    now_local: datetime | None = None,
    exclude_appointment_id: int | None = None,
) -> dict:
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
        datetime.strptime(requested_time, "%H:%M")
    except ValueError:
        return {"available": False, "error": "Use YYYY-MM-DD and HH:MM."}
    if duration_minutes <= 0 or duration_minutes > 240:
        return {"available": False, "error": "Invalid duration."}

    requested_start = _minutes(requested_time)
    requested_end = requested_start + duration_minutes
    current = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    current_date = current.date().isoformat()
    current_minute = current.hour * 60 + current.minute
    if current.second or current.microsecond:
        current_minute += 1
    if requested_date == current_date:
        reset_daily_schedule_if_due(now_local=current)
    with connect() as connection:
        rows = connection.execute(
            """
            SELECT a.therapist,
                   MAX(a.start_time, s.start_time) AS start_time,
                   MIN(a.end_time, s.end_time) AS end_time
            FROM availability a
            JOIN staff_shifts s
              ON s.date = a.date AND s.therapist = a.therapist
            WHERE a.date = ? AND a.status = 'available'
              AND MAX(a.start_time, s.start_time) < MIN(a.end_time, s.end_time)
            ORDER BY a.therapist, start_time
            """,
            (requested_date,),
        ).fetchall()
        appointments = connection.execute(
            """
            SELECT id, therapist, requested_time, duration_minutes
            FROM appointment_requests
            WHERE requested_date = ? AND status IN ('pending', 'confirmed')
            """,
            (requested_date,),
        ).fetchall()
        shifts = _staff_shifts(connection, requested_date)
        manual_staffing = _manual_staffing_exists(connection, requested_date)

    therapists_in_store = [
        row["therapist"]
        for row in shifts
        if _minutes(row["start_time"]) <= requested_start < _minutes(row["end_time"])
    ]
    present_names = {name.casefold() for name in therapists_in_store}
    therapists_not_in_store = [
        name for name in KNOWN_THERAPISTS if name.casefold() not in present_names
    ]
    schedule_loaded = bool(shifts) or bool(manual_staffing)

    if requested_date < current_date:
        return {
            "schedule_loaded": schedule_loaded,
            "available": False,
            "requested_date": requested_date,
            "requested_time": requested_time,
            "duration_minutes": duration_minutes,
            "matches": [],
            "suggested_times": [],
            "therapists_in_store": therapists_in_store,
            "therapists_not_in_store": therapists_not_in_store if schedule_loaded else [],
            "error": "The requested date is in the past.",
        }

    earliest_start = current_minute if requested_date == current_date else 0

    matches: list[dict[str, str]] = []
    suggested_slots: list[dict[str, str | int]] = []
    preference = therapist_preference.strip().casefold()

    def has_conflict(therapist: str, start: int, end: int) -> bool:
        for appointment in appointments:
            if exclude_appointment_id == appointment["id"]:
                continue
            assigned = (appointment["therapist"] or "").casefold()
            if assigned and assigned != therapist.casefold():
                continue
            existing_start = _minutes(appointment["requested_time"])
            existing_end = existing_start + int(appointment["duration_minutes"])
            if start < existing_end and end > existing_start:
                return True
        return False

    for row in rows:
        therapist = row["therapist"]
        if preference and therapist.casefold() != preference:
            continue
        interval_start = _minutes(row["start_time"])
        interval_end = _minutes(row["end_time"])
        if (
            requested_start >= interval_start
            and requested_start >= earliest_start
            and requested_start <= LATEST_APPOINTMENT_START_MINUTE
            and requested_end <= interval_end
            and not has_conflict(therapist, requested_start, requested_end)
        ):
            matches.append(
                {
                    "therapist": therapist,
                    "start_time": requested_time,
                    "end_time": f"{requested_end // 60:02d}:{requested_end % 60:02d}",
                }
            )

        # Generate real alternatives from the uploaded schedule at 30-minute
        # boundaries. The model must never invent an alternative appointment.
        slot_start = interval_start
        while slot_start + duration_minutes <= interval_end:
            slot_end = slot_start + duration_minutes
            if (
                slot_start != requested_start
                and slot_start >= earliest_start
                and slot_start <= LATEST_APPOINTMENT_START_MINUTE
                and not has_conflict(therapist, slot_start, slot_end)
            ):
                suggested_slots.append(
                    {
                        "therapist": therapist,
                        "start_time": f"{slot_start // 60:02d}:{slot_start % 60:02d}",
                        "end_time": f"{slot_end // 60:02d}:{slot_end % 60:02d}",
                        "distance_minutes": abs(slot_start - requested_start),
                    }
                )
            slot_start += 30

    suggested_slots.sort(
        key=lambda slot: (int(slot["distance_minutes"]), str(slot["start_time"]), str(slot["therapist"]))
    )
    suggestions: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for slot in suggested_slots:
        key = (str(slot["therapist"]), str(slot["start_time"]))
        if key in seen:
            continue
        seen.add(key)
        suggestions.append(
            {
                "therapist": str(slot["therapist"]),
                "start_time": str(slot["start_time"]),
                "end_time": str(slot["end_time"]),
            }
        )
        if len(suggestions) == 5:
            break

    return {
        "schedule_loaded": schedule_loaded,
        "available": bool(matches),
        "requested_date": requested_date,
        "requested_time": requested_time,
        "duration_minutes": duration_minutes,
        "matches": matches[:5],
        "suggested_times": suggestions,
        "therapists_in_store": therapists_in_store,
        "therapists_not_in_store": therapists_not_in_store if schedule_loaded else [],
    }


def get_therapists_in_store(requested_date: str, requested_time: str = "") -> dict:
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
        if requested_time:
            datetime.strptime(requested_time, "%H:%M")
    except ValueError:
        return {"schedule_loaded": False, "error": "Use YYYY-MM-DD and HH:MM."}
    current = datetime.now(ZoneInfo("America/Los_Angeles"))
    if requested_date == current.date().isoformat():
        reset_daily_schedule_if_due(now_local=current)
    with connect() as connection:
        rows = _staff_shifts(connection, requested_date)
        manual_staffing = _manual_staffing_exists(connection, requested_date)
    if requested_time:
        minute = _minutes(requested_time)
        in_store = [
            row["therapist"] for row in rows
            if _minutes(row["start_time"]) <= minute < _minutes(row["end_time"])
        ]
    else:
        in_store = [row["therapist"] for row in rows]
    present_names = {name.casefold() for name in in_store}
    not_in_store = [
        name for name in KNOWN_THERAPISTS if name.casefold() not in present_names
    ]
    return {
        "schedule_loaded": bool(rows) or bool(manual_staffing),
        "requested_date": requested_date,
        "requested_time": requested_time,
        "therapists_in_store": in_store,
        "therapists_not_in_store": not_in_store if rows or manual_staffing else [],
        "staff_shifts": [dict(row) for row in rows],
    }


def _insert_appointment(
    *,
    customer_name: str,
    customer_phone: str,
    requested_date: str,
    requested_time: str,
    service: str,
    duration_minutes: int,
    therapist: str,
    notes: str,
    status: str,
) -> int:
    with connect() as connection:
        return connection.execute(
            """
            INSERT INTO appointment_requests (
                created_at, customer_name, customer_phone, requested_date,
                requested_time, service, duration_minutes, therapist, notes, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(timezone.utc).isoformat(), customer_name.strip(),
                customer_phone.strip(), requested_date, requested_time, service.strip(),
                duration_minutes, therapist, notes.strip(), status,
            ),
        ).lastrowid


@_serialize_appointment_change
def save_appointment_request(
    customer_name: str,
    customer_phone: str,
    requested_date: str,
    requested_time: str,
    service: str,
    duration_minutes: int,
    therapist_preference: str = "",
    notes: str = "",
) -> dict:
    availability = check_availability(
        requested_date, requested_time, duration_minutes, therapist_preference
    )
    if not availability.get("available"):
        return {
            "saved": False,
            "reason": "That time is no longer available. Check availability again.",
        }

    therapist = availability["matches"][0]["therapist"]
    request_id = _insert_appointment(
        customer_name=customer_name, customer_phone=customer_phone,
        requested_date=requested_date, requested_time=requested_time, service=service,
        duration_minutes=duration_minutes, therapist=therapist, notes=notes,
        status="confirmed",
    )
    return {
        "saved": True,
        "request_id": request_id,
        "status": "confirmed",
        "therapist": therapist,
        "message": "A local reservation was saved; final Smiley booking is not yet confirmed.",
    }


@_serialize_appointment_change
def save_unverified_appointment_request(
    customer_name: str,
    customer_phone: str,
    requested_date: str,
    requested_time: str,
    service: str,
    duration_minutes: int,
    therapist_preference: str = "",
    notes: str = "",
) -> dict:
    """Save a callback request when no schedule exists for the requested date."""
    smiley_state = get_smiley_sync_status()
    if (
        smiley_state.get("window_start", "")
        <= requested_date
        <= smiley_state.get("window_end", "")
    ):
        smiley_availability = check_smiley_availability(
            requested_date,
            requested_time,
            duration_minutes,
            therapist_preference,
        )
        if not smiley_availability.get("available"):
            return {
                "saved": False,
                "reason": "That time is not currently available in Smiley. Check availability again.",
            }
        request_id = _insert_appointment(
            customer_name=customer_name,
            customer_phone=customer_phone,
            requested_date=requested_date,
            requested_time=requested_time,
            service=service,
            duration_minutes=duration_minutes,
            therapist=smiley_availability["matches"][0]["therapist"],
            notes=notes,
            status="confirmed",
        )
        return {
            "saved": True,
            "request_id": request_id,
            "status": "confirmed",
            "therapist": smiley_availability["matches"][0]["therapist"],
            "message": "A local reservation was saved; final Smiley booking is not yet confirmed.",
        }

    availability = check_availability(
        requested_date, requested_time, duration_minutes, therapist_preference
    )
    if availability.get("schedule_loaded"):
        return {
            "saved": False,
            "reason": "A schedule is loaded. Check availability and use the normal save tool.",
        }

    request_id = _insert_appointment(
        customer_name=customer_name, customer_phone=customer_phone,
        requested_date=requested_date, requested_time=requested_time, service=service,
        duration_minutes=duration_minutes, therapist=therapist_preference.strip(),
        notes=notes, status="needs_review",
    )
    return {
        "saved": True,
        "request_id": request_id,
        "status": "needs_review",
        "message": "The preferred time was saved for staff review, not confirmed.",
    }


def list_schedule() -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT a.id, a.date, a.therapist,
                   MAX(a.start_time, s.start_time) AS start_time,
                   MIN(a.end_time, s.end_time) AS end_time,
                   a.status, a.uploaded_at
            FROM availability a
            JOIN staff_shifts s
              ON s.date = a.date AND s.therapist = a.therapist
            WHERE MAX(a.start_time, s.start_time) < MIN(a.end_time, s.end_time)
            ORDER BY a.date, start_time, a.therapist
            """
        ).fetchall()


def list_appointments() -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM appointment_requests ORDER BY id DESC"
        ).fetchall()


def cancel_appointment(appointment_id: int) -> dict:
    """Soft-cancel an appointment so its time becomes available again."""
    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM appointment_requests WHERE id = ?",
            (appointment_id,),
        ).fetchone()
        if not row:
            return {"cancelled": False, "reason": "Appointment not found."}
        if row["status"] == "cancelled":
            return {"cancelled": True, "appointment": dict(row), "already_cancelled": True}
        connection.execute(
            """
            UPDATE appointment_requests
            SET status = 'cancelled', cancelled_at = ?
            WHERE id = ?
            """,
            (datetime.now(timezone.utc).isoformat(), appointment_id),
        )
        updated = dict(row)
        updated["status"] = "cancelled"
        return {"cancelled": True, "appointment": updated}


@_serialize_appointment_change
def edit_appointment(appointment_id: int, **changes: object) -> dict:
    """Edit a booking after validating the resulting slot against the schedule."""
    allowed = {
        "requested_date", "requested_time", "service",
        "duration_minutes", "therapist_preference",
    }
    if not changes or any(key not in allowed for key in changes):
        return {"updated": False, "reason": "Invalid or empty appointment changes."}

    with connect() as connection:
        row = connection.execute(
            "SELECT * FROM appointment_requests WHERE id = ?",
            (appointment_id,),
        ).fetchone()
    if not row:
        return {"updated": False, "reason": "Appointment not found."}
    if row["status"] in {"cancelled", "rejected"}:
        return {"updated": False, "reason": f"Appointment is {row['status']}."}

    requested_date = str(changes.get("requested_date", row["requested_date"])).strip()
    requested_time = str(changes.get("requested_time", row["requested_time"])).strip()
    service = str(changes.get("service", row["service"])).strip()
    duration_minutes = int(changes.get("duration_minutes", row["duration_minutes"]))
    therapist_preference = str(
        changes.get("therapist_preference", row["therapist"] or "")
    ).strip()
    availability = check_availability(
        requested_date,
        requested_time,
        duration_minutes,
        therapist_preference,
        exclude_appointment_id=appointment_id,
    )
    if not availability.get("available"):
        return {
            "updated": False,
            "reason": availability.get("error") or "That time is not available.",
            "availability": availability,
        }

    therapist = availability["matches"][0]["therapist"]
    with connect() as connection:
        connection.execute(
            """
            UPDATE appointment_requests
            SET requested_date = ?, requested_time = ?, service = ?,
                duration_minutes = ?, therapist = ?
            WHERE id = ?
            """,
            (
                requested_date, requested_time, service,
                duration_minutes, therapist, appointment_id,
            ),
        )
        updated = connection.execute(
            "SELECT * FROM appointment_requests WHERE id = ?",
            (appointment_id,),
        ).fetchone()
    return {"updated": True, "appointment": dict(updated)}


def update_appointment_status(appointment_id: int, status: str) -> None:
    if status not in {"needs_review", "pending", "confirmed", "rejected", "cancelled"}:
        raise ValueError("Invalid appointment status.")
    with connect() as connection:
        if status == "cancelled":
            connection.execute(
                """
                UPDATE appointment_requests
                SET status = ?, cancelled_at = ? WHERE id = ?
                """,
                (status, datetime.now(timezone.utc).isoformat(), appointment_id),
            )
        else:
            connection.execute(
                """
                UPDATE appointment_requests
                SET status = ?, cancelled_at = NULL WHERE id = ?
                """,
                (status, appointment_id),
            )


def save_unanswered_question(
    caller_phone: str,
    question: str,
    context: str = "",
) -> dict:
    cleaned_question = question.strip()
    if not cleaned_question:
        return {"saved": False, "reason": "The question is empty."}
    with connect() as connection:
        cursor = connection.execute(
            """
            INSERT INTO unanswered_questions
                (created_at, caller_phone, question, context, status)
            VALUES (?, ?, ?, ?, 'pending')
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                caller_phone.strip(),
                cleaned_question,
                context.strip(),
            ),
        )
        question_id = cursor.lastrowid
    return {"saved": True, "question_id": question_id, "status": "pending"}


def list_unanswered_questions(status: str = "pending") -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            """
            SELECT * FROM unanswered_questions
            WHERE status = ?
            ORDER BY id DESC
            """,
            (status,),
        ).fetchall()


def get_unanswered_question(question_id: int) -> sqlite3.Row | None:
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM unanswered_questions WHERE id = ?",
            (question_id,),
        ).fetchone()


def answer_unanswered_question(question_id: int, answer: str) -> bool:
    cleaned_answer = answer.strip()
    if not cleaned_answer:
        raise ValueError("Answer cannot be empty.")
    with connect() as connection:
        question = connection.execute(
            "SELECT question FROM unanswered_questions WHERE id = ?",
            (question_id,),
        ).fetchone()
        if not question:
            return False
        connection.execute(
            """
            UPDATE unanswered_questions
            SET status = 'answered', answer = ?, answered_at = ?
            WHERE id = ?
            """,
            (cleaned_answer, datetime.now(timezone.utc).isoformat(), question_id),
        )
        connection.execute(
            """
            INSERT INTO knowledge_entries
                (created_at, question, answer, source_question_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(source_question_id) DO UPDATE SET answer = excluded.answer
            """,
            (
                datetime.now(timezone.utc).isoformat(),
                question["question"],
                cleaned_answer,
                question_id,
            ),
        )
    return True


def ignore_unanswered_question(question_id: int) -> bool:
    with connect() as connection:
        cursor = connection.execute(
            """
            UPDATE unanswered_questions SET status = 'ignored'
            WHERE id = ? AND status = 'pending'
            """,
            (question_id,),
        )
    return cursor.rowcount > 0


def list_knowledge_entries() -> list[sqlite3.Row]:
    with connect() as connection:
        return connection.execute(
            "SELECT * FROM knowledge_entries ORDER BY id",
        ).fetchall()


def stage_schedule(user_id: str, rows: list[dict[str, str]]) -> int:
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO pending_schedule_uploads (user_id, created_at, rows_json)
            VALUES (?, ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                created_at = excluded.created_at,
                rows_json = excluded.rows_json
            """,
            (user_id, datetime.now(timezone.utc).isoformat(), json.dumps(rows)),
        )
    return len(rows)


def get_staged_schedule(user_id: str) -> list[dict[str, str]]:
    with connect() as connection:
        row = connection.execute(
            "SELECT rows_json FROM pending_schedule_uploads WHERE user_id = ?",
            (user_id,),
        ).fetchone()
    return json.loads(row["rows_json"]) if row else []


def confirm_staged_schedule(user_id: str) -> int:
    rows = get_staged_schedule(user_id)
    if not rows:
        return 0
    count = replace_schedule(rows)
    discard_staged_schedule(user_id)
    return count


def discard_staged_schedule(user_id: str) -> None:
    with connect() as connection:
        connection.execute(
            "DELETE FROM pending_schedule_uploads WHERE user_id = ?",
            (user_id,),
        )
