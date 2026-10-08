import sqlite3
import json
import os
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from zoneinfo import ZoneInfo


DATA_DIR = Path(
    os.getenv("DATA_DIR")
    or os.getenv("RAILWAY_VOLUME_MOUNT_PATH")
    or (Path(__file__).resolve().parent.parent / "data")
)
DATABASE_PATH = DATA_DIR / "receptionist.db"
_DATABASE_PATH_OVERRIDE: ContextVar[Path | None] = ContextVar(
    "database_path_override", default=None
)
LATEST_APPOINTMENT_START_MINUTE = 21 * 60 + 30
KNOWN_THERAPISTS = ("Staff A", "Staff B", "Staff C")
INACTIVE_THERAPISTS = {"former staff"}
DEFAULT_STAFF_SHIFTS = {
    "Staff B": ("10:00", "22:00"),
    "Staff C": ("10:00", "22:00"),
    "Staff A": ("19:00", "22:00"),
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


def is_active_therapist(name: str) -> bool:
    return name.strip().casefold() not in INACTIVE_THERAPISTS


@contextmanager
def connect() -> Iterator[sqlite3.Connection]:
    database_path = _DATABASE_PATH_OVERRIDE.get() or DATABASE_PATH
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(database_path)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


@contextmanager
def use_database(database_path: Path) -> Iterator[None]:
    """Temporarily route storage calls in this context to another SQLite file."""
    token = _DATABASE_PATH_OVERRIDE.set(database_path)
    try:
        yield
    finally:
        _DATABASE_PATH_OVERRIDE.reset(token)


def clone_database(destination: Path) -> None:
    """Create a transactionally consistent SQLite copy for an isolated test."""
    source_path = _DATABASE_PATH_OVERRIDE.get() or DATABASE_PATH
    destination.parent.mkdir(parents=True, exist_ok=True)
    source = sqlite3.connect(source_path)
    target = sqlite3.connect(destination)
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()


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


def _staff_shifts(
    connection: sqlite3.Connection, schedule_date: str
) -> list[sqlite3.Row]:
    rows = connection.execute(
        """
        SELECT therapist, start_time, end_time
        FROM staff_shifts WHERE date = ? ORDER BY therapist
        """,
        (schedule_date,),
    ).fetchall()
    return [row for row in rows if is_active_therapist(row["therapist"])]


def _staffing_overrides(
    connection: sqlite3.Connection, schedule_date: str
) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT therapist, status, note, start_time, end_time, updated_at
        FROM staffing_overrides WHERE date = ? ORDER BY therapist
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

            CREATE TABLE IF NOT EXISTS staffing_overrides (
                date TEXT NOT NULL,
                therapist TEXT NOT NULL,
                status TEXT NOT NULL,
                note TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                updated_at TEXT NOT NULL,
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
        # Backfill shifts for schedules saved before staff_shifts was introduced.
        existing_dates = [
            row["date"]
            for row in connection.execute(
                "SELECT DISTINCT date FROM availability"
            ).fetchall()
        ]
        prior_staff = {(row["date"], row["therapist"]) for row in connection.execute(
            "SELECT date, therapist FROM staff_shifts"
        )}
        connection.executemany(
            """
            INSERT OR IGNORE INTO staff_shifts
                (date, therapist, start_time, end_time)
            VALUES (?, ?, ?, ?)
            """,
            [
                (date, therapist, shift[0], shift[1])
                for date in existing_dates
                if not _manual_staffing_exists(connection, date)
                for therapist, shift in DEFAULT_STAFF_SHIFTS.items()
            ],
        )
        # Apply the new default roster once to existing non-manual current/future
        # dates. Preserve uploaded gaps, explicit staff edits, and all bookings.
        if _get_system_state(connection, "default_staffing_version") != "2026-09-23":
            today = datetime.now(ZoneInfo("America/Los_Angeles")).date().isoformat()
            for date in existing_dates:
                if date < today or _manual_staffing_exists(connection, date):
                    continue
                for name, (start, end) in DEFAULT_STAFF_SHIFTS.items():
                    connection.execute(
                        "UPDATE staff_shifts SET start_time = ?, end_time = ? WHERE date = ? AND therapist = ?",
                        (start, end, date, name),
                    )
                    if name != "Staff C" or (date, name) in prior_staff:
                        continue
                    connection.execute(
                        "INSERT INTO availability (date, therapist, start_time, end_time, status, uploaded_at) "
                        "SELECT ?, ?, ?, ?, 'available', ? WHERE NOT EXISTS "
                        "(SELECT 1 FROM availability WHERE date = ? AND therapist = ?)",
                        (date, name, start, end, datetime.now(timezone.utc).isoformat(), date, name),
                    )
            _set_system_state(connection, "default_staffing_version", "2026-09-23")


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


def ensure_default_schedule(schedule_date: str) -> int:
    """Initialize only an untouched date; never erase bookings or staff edits."""
    rows = default_schedule_rows(schedule_date)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if (
            _manual_staffing_exists(connection, schedule_date)
            or _staff_shifts(connection, schedule_date)
            or connection.execute(
                "SELECT 1 FROM availability WHERE date = ? LIMIT 1", (schedule_date,)
            ).fetchone()
            or _staffing_overrides(connection, schedule_date)
        ):
            return 0
        connection.executemany(
            "INSERT INTO availability (date, therapist, start_time, end_time, status, uploaded_at) "
            "VALUES (:date, :therapist, :start_time, :end_time, :status, :uploaded_at)",
            [{**row, "uploaded_at": datetime.now(timezone.utc).isoformat()} for row in rows],
        )
        connection.executemany(
            "INSERT INTO staff_shifts (date, therapist, start_time, end_time) VALUES (?, ?, ?, ?)",
            [(schedule_date, name, shift[0], shift[1]) for name, shift in
             TEMPORARY_STAFF_OVERRIDES.get(schedule_date, DEFAULT_STAFF_SHIFTS).items()],
        )
    return len(rows)


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


def set_daily_staffing(
    schedule_date: str,
    shifts: list[dict[str, str]],
    *,
    preserve_staffing_overrides: bool = False,
) -> int:
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
        previous_shifts = {row["therapist"]: (row["start_time"], row["end_time"])
                           for row in _staff_shifts(connection, schedule_date)}
        for therapist, (start, end) in normalized.items():
            previous = previous_shifts.get(therapist)
            if not previous:
                continue
            windows = connection.execute(
                "SELECT id, start_time, end_time, status FROM availability WHERE date = ? AND therapist = ?",
                (schedule_date, therapist),
            ).fetchall()
            # Default whole-shift availability can move with a manual shift.
            # Do not overwrite photo-derived gaps or occupied intervals.
            if (len(windows) == 1 and windows[0]["status"] == "available"
                    and (windows[0]["start_time"], windows[0]["end_time"]) == previous):
                connection.execute("UPDATE availability SET start_time = ?, end_time = ? WHERE id = ?",
                                   (start, end, windows[0]["id"]))
        if not preserve_staffing_overrides:
            connection.execute(
                "DELETE FROM staffing_overrides WHERE date = ?", (schedule_date,)
            )
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


def set_therapist_staffing_override(
    schedule_date: str,
    therapist: str,
    status: str,
    *,
    start_time: str = "",
    end_time: str = "",
) -> dict:
    """Apply one LINE staffing change while preserving everyone else that day."""
    datetime.strptime(schedule_date, "%Y-%m-%d")
    canonical = next(
        (name for name in KNOWN_THERAPISTS if name.casefold() == therapist.casefold()),
        "",
    )
    if not canonical:
        raise ValueError(f"Unknown therapist: {therapist}")
    if status not in {"off", "working"}:
        raise ValueError("Staffing status must be off or working.")

    with connect() as connection:
        rows = _staff_shifts(connection, schedule_date)
        manual_staffing = _manual_staffing_exists(connection, schedule_date)
    if rows or manual_staffing:
        shifts = {
            row["therapist"]: (row["start_time"], row["end_time"])
            for row in rows
        }
    else:
        shifts = dict(
            TEMPORARY_STAFF_OVERRIDES.get(schedule_date, DEFAULT_STAFF_SHIFTS)
        )

    if status == "off":
        shifts.pop(canonical, None)
        saved_start = ""
        saved_end = ""
        note = "请假"
    else:
        current_start, current_end = shifts.get(
            canonical, DEFAULT_STAFF_SHIFTS.get(canonical, ("", ""))
        )
        saved_start = start_time or current_start
        saved_end = end_time or current_end
        if not saved_start or not saved_end:
            raise ValueError(f"{canonical} has no default shift; provide start and end times.")
        if _minutes(saved_start) >= _minutes(saved_end):
            raise ValueError("End time must be after start time.")
        shifts[canonical] = (saved_start, saved_end)
        note = "临时上班"

    set_daily_staffing(
        schedule_date,
        [
            {
                "therapist": name,
                "start_time": shift[0],
                "end_time": shift[1],
            }
            for name, shift in shifts.items()
        ],
        preserve_staffing_overrides=True,
    )
    updated_at = datetime.now(timezone.utc).isoformat()
    with connect() as connection:
        connection.execute(
            """
            INSERT INTO staffing_overrides
                (date, therapist, status, note, start_time, end_time, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date, therapist) DO UPDATE SET
                status = excluded.status,
                note = excluded.note,
                start_time = excluded.start_time,
                end_time = excluded.end_time,
                updated_at = excluded.updated_at
            """,
            (
                schedule_date, canonical, status, note,
                saved_start or None, saved_end or None, updated_at,
            ),
        )
    return {
        "date": schedule_date,
        "therapist": canonical,
        "status": status,
        "note": note,
        "start_time": saved_start,
        "end_time": saved_end,
    }


def reset_daily_schedule_if_due(
    *,
    now_local: datetime | None = None,
) -> dict:
    current = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    schedule_date = current.date().isoformat()
    # The local calendar date changes at midnight; do not delay until opening hours.

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

    count = ensure_default_schedule(schedule_date)
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
            "staffing_overrides": (
                "DELETE FROM staffing_overrides WHERE date < ?",
                local_cutoff.date().isoformat(),
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
    if not stream_sid or not cleaned or speaker not in {"customer", "karin"}:
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
        # LINE group IDs start with C and multi-person room IDs start with R.
        # Never persist a U-prefixed private user ID as a proactive target.
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


def check_availability(
    requested_date: str,
    requested_time: str,
    duration_minutes: int | None = None,
    therapist_preference: str = "",
    party_size: int = 1,
    *,
    now_local: datetime | None = None,
    exclude_appointment_id: int | None = None,
) -> dict:
    try:
        datetime.strptime(requested_date, "%Y-%m-%d")
        datetime.strptime(requested_time, "%H:%M")
    except ValueError:
        return {"available": False, "error": "Use YYYY-MM-DD and HH:MM."}
    preliminary = duration_minutes is None
    # A start-time inquiry checks the shortest offered session, not a final booking.
    # The caller's actual duration must be checked again before saving.
    if preliminary:
        duration_minutes = 30
    if duration_minutes <= 0 or duration_minutes > 240:
        return {"available": False, "error": "Invalid duration."}
    if party_size not in {1, 2}:
        return {"available": False, "error": "Party size must be one or two."}

    requested_start = _minutes(requested_time)
    requested_end = requested_start + duration_minutes
    current = now_local or datetime.now(ZoneInfo("America/Los_Angeles"))
    current_date = current.date().isoformat()
    current_minute = current.hour * 60 + current.minute
    if current.second or current.microsecond:
        current_minute += 1
    if requested_date == current_date:
        reset_daily_schedule_if_due(now_local=current)
    elif requested_date > current_date:
        ensure_default_schedule(requested_date)
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
        staffing_overrides = _staffing_overrides(connection, requested_date)

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
    matched_therapists: set[str] = set()
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
        if not is_active_therapist(therapist):
            continue
        if party_size == 1 and preference and therapist.casefold() != preference:
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
            therapist_key = therapist.casefold()
            if therapist_key not in matched_therapists:
                matched_therapists.add(therapist_key)
                matches.append(
                    {
                        "therapist": therapist,
                        "start_time": requested_time,
                        "end_time": f"{requested_end // 60:02d}:{requested_end % 60:02d}",
                    }
                )

        # Find the next real opening, including non-half-hour booking end times.
        slot_start = max(interval_start, requested_start + 1, earliest_start)
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
            slot_start += 1

    if preference and party_size > 1:
        matches.sort(key=lambda match: match["therapist"].casefold() != preference)
    preferred_match_found = not preference or any(
        match["therapist"].casefold() == preference for match in matches
    )

    suggested_slots.sort(
        key=lambda slot: (int(slot["distance_minutes"]), str(slot["start_time"]), str(slot["therapist"]))
    )
    suggestions: list[dict[str, str]] = []
    if party_size == 1:
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
    else:
        slots_by_time: dict[str, list[dict[str, str | int]]] = {}
        for slot in suggested_slots:
            slots_by_time.setdefault(str(slot["start_time"]), []).append(slot)
        for start_time, slots in slots_by_time.items():
            unique_by_therapist = {
                str(slot["therapist"]).casefold(): slot for slot in slots
            }
            if len(unique_by_therapist) < party_size:
                continue
            if preference and preference not in unique_by_therapist:
                continue
            selected = list(unique_by_therapist.values())[:party_size]
            suggestions.append({
                "therapist": str(selected[0]["therapist"]),
                "start_time": start_time,
                "end_time": str(selected[0]["end_time"]),
            })
            if len(suggestions) == 5:
                break

    return {
        "schedule_loaded": schedule_loaded,
        "available": len(matches) >= party_size and preferred_match_found,
        "requested_date": requested_date,
        "requested_time": requested_time,
        "duration_minutes": duration_minutes,
        "preliminary": preliminary,
        "current_local_datetime": current.isoformat(timespec="seconds"),
        "party_size": party_size,
        "matches": matches[:5],
        "suggested_times": suggestions,
        "therapists_in_store": therapists_in_store,
        "therapists_not_in_store": therapists_not_in_store if schedule_loaded else [],
        "staffing_overrides": [dict(row) for row in staffing_overrides],
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
    elif requested_date > current.date().isoformat():
        ensure_default_schedule(requested_date)
    with connect() as connection:
        rows = _staff_shifts(connection, requested_date)
        manual_staffing = _manual_staffing_exists(connection, requested_date)
        staffing_overrides = _staffing_overrides(connection, requested_date)
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
        "staffing_overrides": [dict(row) for row in staffing_overrides],
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
        return _insert_appointment_with_connection(
            connection,
            customer_name=customer_name,
            customer_phone=customer_phone,
            requested_date=requested_date,
            requested_time=requested_time,
            service=service,
            duration_minutes=duration_minutes,
            therapist=therapist,
            notes=notes,
            status=status,
        )


def _insert_appointment_with_connection(
    connection: sqlite3.Connection,
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


def _therapist_slot_is_still_open(
    connection: sqlite3.Connection,
    *,
    therapist: str,
    requested_date: str,
    requested_time: str,
    duration_minutes: int,
) -> bool:
    if not is_active_therapist(therapist):
        return False
    requested_start = _minutes(requested_time)
    requested_end = requested_start + duration_minutes
    availability_rows = connection.execute(
        """
        SELECT MAX(a.start_time, s.start_time) AS start_time,
               MIN(a.end_time, s.end_time) AS end_time
        FROM availability a
        JOIN staff_shifts s
          ON s.date = a.date AND s.therapist = a.therapist
        WHERE a.date = ? AND a.therapist = ? AND a.status = 'available'
          AND MAX(a.start_time, s.start_time) < MIN(a.end_time, s.end_time)
        """,
        (requested_date, therapist),
    ).fetchall()
    if not any(
        requested_start >= _minutes(row["start_time"])
        and requested_end <= _minutes(row["end_time"])
        for row in availability_rows
    ):
        return False
    appointments = connection.execute(
        """
        SELECT requested_time, duration_minutes
        FROM appointment_requests
        WHERE requested_date = ?
          AND (therapist = ? OR therapist IS NULL OR therapist = '')
          AND status IN ('pending', 'confirmed')
        """,
        (requested_date, therapist),
    ).fetchall()
    return not any(
        requested_start < _minutes(row["requested_time"]) + int(row["duration_minutes"])
        and requested_end > _minutes(row["requested_time"])
        for row in appointments
    )


def save_appointment_request(
    customer_name: str,
    customer_phone: str,
    requested_date: str,
    requested_time: str,
    service: str,
    duration_minutes: int,
    therapist_preference: str = "",
    notes: str = "",
    party_size: int = 1,
) -> dict:
    availability = check_availability(
        requested_date, requested_time, duration_minutes, therapist_preference,
        party_size,
    )
    if not availability.get("available"):
        return {
            "saved": False,
            "reason": (
                "There are not enough therapists available for everyone at that time."
                if party_size == 2
                else "That time is no longer available. Check availability again."
            ),
        }

    therapists = [
        str(match["therapist"])
        for match in availability["matches"][:party_size]
    ]
    party_note = (
        f"Party of {party_size}; simultaneous booking." if party_size > 1 else ""
    )
    combined_notes = " ".join(part for part in (notes.strip(), party_note) if part)
    with connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if not all(
            _therapist_slot_is_still_open(
                connection,
                therapist=therapist,
                requested_date=requested_date,
                requested_time=requested_time,
                duration_minutes=duration_minutes,
            )
            for therapist in therapists
        ):
            return {
                "saved": False,
                "reason": "That time changed while booking. Check availability again.",
            }
        request_ids = [
            _insert_appointment_with_connection(
                connection,
                customer_name=customer_name,
                customer_phone=customer_phone,
                requested_date=requested_date,
                requested_time=requested_time,
                service=service,
                duration_minutes=duration_minutes,
                therapist=therapist,
                notes=combined_notes,
                status="confirmed",
            )
            for therapist in therapists
        ]
    return {
        "saved": True,
        "request_id": request_ids[0],
        "request_ids": request_ids,
        "status": "confirmed",
        "therapist": therapists[0],
        "therapists": therapists,
        "party_size": party_size,
        "message": (
            "Both appointments were booked successfully."
            if party_size == 2
            else "The appointment was booked successfully."
        ),
    }


def save_unverified_appointment_request(
    customer_name: str,
    customer_phone: str,
    requested_date: str,
    requested_time: str,
    service: str,
    duration_minutes: int,
    therapist_preference: str = "",
    notes: str = "",
    party_size: int = 1,
) -> dict:
    """Save a callback request when no schedule exists for the requested date."""
    if party_size not in {1, 2}:
        return {"saved": False, "reason": "Party size must be one or two."}
    availability = check_availability(
        requested_date, requested_time, duration_minutes, therapist_preference,
        party_size,
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
        notes=" ".join(part for part in (
            notes.strip(),
            f"Party of {party_size}; simultaneous request." if party_size > 1 else "",
        ) if part),
        status="needs_review",
    )
    return {
        "saved": True,
        "request_id": request_id,
        "status": "needs_review",
        "party_size": party_size,
        "message": "The preferred time was saved for staff review, not confirmed.",
    }


def list_schedule() -> list[sqlite3.Row]:
    with connect() as connection:
        rows = connection.execute(
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
    return [row for row in rows if is_active_therapist(row["therapist"])]


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
