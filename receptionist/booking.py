"""Transactional local calendar; model output never determines availability."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import re
import secrets
import sqlite3
from zoneinfo import ZoneInfo

from .config import Business


class Calendar:
    def __init__(self, path: str | Path, business: Business):
        self.path, self.business = Path(path), business
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript('''
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS bookings (
                    id TEXT PRIMARY KEY, call_id TEXT NOT NULL, service TEXT NOT NULL,
                    name TEXT NOT NULL, phone TEXT NOT NULL, start INTEGER NOT NULL,
                    end INTEGER NOT NULL, units INTEGER NOT NULL, status TEXT NOT NULL,
                    details TEXT NOT NULL, created INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS booking_range ON bookings(status, start, end);
                CREATE TABLE IF NOT EXISTS proposals (
                    token TEXT PRIMARY KEY, call_id TEXT NOT NULL, details TEXT NOT NULL,
                    expires INTEGER NOT NULL, booking_id TEXT);
                CREATE TABLE IF NOT EXISTS callbacks (
                    id TEXT PRIMARY KEY, call_id TEXT UNIQUE NOT NULL, phone TEXT NOT NULL,
                    reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS overrides (day TEXT PRIMARY KEY, capacity INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS outbox (
                    id TEXT PRIMARY KEY, body TEXT NOT NULL, sent INTEGER NOT NULL DEFAULT 0,
                    attempts INTEGER NOT NULL DEFAULT 0, created INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS streams (
                    token TEXT PRIMARY KEY, call_id TEXT NOT NULL, phone TEXT NOT NULL, expires INTEGER NOT NULL);
            ''')

    @contextmanager
    def connect(self, write=False):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def now():
        return datetime.now(timezone.utc)

    def local_time(self, day, at):
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day) or not re.fullmatch(r"\d{2}:\d{2}", at):
            raise ValueError("Use YYYY-MM-DD and HH:MM in the business timezone")
        naive = datetime.fromisoformat(f"{day}T{at}")
        zone = ZoneInfo(self.business.timezone)
        dt = naive.replace(tzinfo=zone)
        if dt.astimezone(timezone.utc).astimezone(zone).replace(tzinfo=None) != naive:
            raise ValueError("That local time does not exist due to daylight saving time")
        if dt.utcoffset() != naive.replace(tzinfo=zone, fold=1).utcoffset():
            raise ValueError("That local time is ambiguous due to daylight saving time; choose another time")
        return dt

    def _slot(self, db, day, at, duration=None, service_id=None, party_size=1):
        if type(party_size) is not int or not 1 <= party_size <= 20:
            raise ValueError("Party size must be 1–20")
        service = self.business.service(service_id) if service_id else None
        if duration is not None and (type(duration) is not int or not 5 <= duration <= 480):
            raise ValueError("Duration must be 5–480 minutes")
        if service and duration is not None and duration not in service.prices:
            raise ValueError("This service does not support that duration")
        start = self.local_time(day, at)
        now = self.now().astimezone(start.tzinfo)
        if start <= now:
            return dict(available=False, reason="past")
        if (start.date() - now.date()).days > self.business.max_days_ahead:
            return dict(available=False, reason="outside_booking_window")
        minutes = duration if duration is not None else 1
        buffer = service.buffer_minutes if service and duration is not None else 0
        end = start + timedelta(minutes=minutes + buffer)
        if end.date() != start.date() or not any(
            self.local_time(day, a) <= start and end <= self.local_time(day, b)
            for a, b in self.business.periods(day)
        ):
            return dict(available=False, reason="outside_hours")
        override = db.execute("SELECT capacity FROM overrides WHERE day=?", (day,)).fetchone()
        capacity = override[0] if override else self.business.capacity
        units = party_size * (service.capacity_units if service else 1)
        if units > capacity:
            return dict(available=False, reason="insufficient_staff")
        a, b = int(start.timestamp()), int(end.timestamp())
        rows = db.execute("SELECT start,end,units FROM bookings WHERE status='confirmed' AND start<? AND end>?", (b, a)).fetchall()
        # Peak concurrent occupancy, not the sum of all overlapping bookings.
        events = [(max(a, r['start']), r['units']) for r in rows] + [(min(b, r['end']), -r['units']) for r in rows]
        occupied = 0
        for _, change in sorted(events):
            occupied += change
            if occupied + units > capacity:
                return dict(available=False, reason="already_booked")
        return dict(available=True, reason="available", start=a, end=b, units=units,
                    scope="start_only" if duration is None else "full_session")

    def availability(self, day, time, duration=None, service_id=None, party_size=1):
        with self.connect() as db:
            result = self._slot(db, day, time, duration, service_id, party_size)
        return {k: v for k, v in result.items() if k not in {'start', 'end', 'units'}}

    def openings(self, day, duration=None, service_id=None, party_size=1):
        date.fromisoformat(day)
        times = []
        for a, b in self.business.periods(day):
            at = datetime.fromisoformat(f"{day}T{a}")
            while at.strftime('%H:%M') < b and len(times) < 12:
                value = at.strftime('%H:%M')
                try:
                    if self.availability(day, value, duration, service_id, party_size)['available']:
                        times.append(value)
                except ValueError as exc:
                    if 'daylight saving' not in str(exc):
                        raise
                at += timedelta(minutes=self.business.slot_minutes)
        return dict(day=day, times=times, scope='start_only' if duration is None else 'full_session')

    def prepare(self, call_id, phone, *, day, time, service_id, duration, name, party_size=1):
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= 120:
            raise ValueError("Ask for the customer's name")
        if not isinstance(phone, str) or not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            raise ValueError("Ask for a callback phone number including country code")
        check = self.availability(day, time, duration, service_id, party_size)
        if not check['available']:
            return check
        service = self.business.service(service_id)
        details = dict(day=day, time=time, service_id=service_id, duration=duration,
                       name=name.strip(), phone=phone, party_size=party_size,
                       price=service.prices[duration]*party_size, currency=self.business.currency)
        token = secrets.token_urlsafe(24)
        with self.connect(write=True) as db:
            db.execute("INSERT INTO proposals VALUES(?,?,?,?,NULL)", (token, call_id, json.dumps(details), int(self.now().timestamp()) + 300))
        return dict(confirmation_token=token, details=details, confirmed=False,
                    instruction="Read back name, date, time, service, duration and party size. Ask for confirmation. This does not reserve a slot.")

    def confirm(self, call_id, token, confirmed):
        if confirmed is not True:
            raise ValueError("Explicit customer confirmation is required")
        with self.connect(write=True) as db:
            proposal = db.execute("SELECT * FROM proposals WHERE token=? AND call_id=?", (token, call_id)).fetchone()
            if not proposal:
                raise ValueError("No proposal for this session")
            if proposal['booking_id']:
                return dict(confirmed=True, booking_id=proposal['booking_id'], repeated=True)
            if proposal['expires'] < self.now().timestamp():
                raise ValueError("Proposal expired; prepare and confirm again")
            d = json.loads(proposal['details'])
            slot = self._slot(db, d['day'], d['time'], d['duration'], d['service_id'], d['party_size'])
            if not slot['available']:
                return dict(confirmed=False, **slot)
            uid = secrets.token_hex(8)
            db.execute("INSERT INTO bookings VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                uid, call_id, d['service_id'], d['name'], d['phone'], slot['start'], slot['end'], slot['units'],
                'confirmed', json.dumps(d), int(self.now().timestamp())))
            db.execute("UPDATE proposals SET booking_id=? WHERE token=?", (uid, token))
            self._enqueue(db, uid, 'New appointment', d)
        return dict(confirmed=True, booking_id=uid, calendar='local')

    def callback(self, call_id, phone, reason):
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone or ''):
            raise ValueError("Ask for a callback phone number including country code")
        if not isinstance(reason, str) or not 1 <= len(reason.strip()) <= 1000:
            raise ValueError("Provide a short callback reason")
        with self.connect(write=True) as db:
            existing = db.execute('SELECT id FROM callbacks WHERE call_id=?', (call_id,)).fetchone()
            if existing:
                return dict(saved=True, callback_id=existing['id'])
            uid = secrets.token_hex(8)
            db.execute("INSERT INTO callbacks(id,call_id,phone,reason,created) VALUES(?,?,?,?,?)",
                       (uid, call_id, phone, reason.strip(), int(self.now().timestamp())))
            self._enqueue(db, uid, 'Staff callback requested', dict(phone=phone, reason=reason.strip()))
        return dict(saved=True, callback_id=uid)

    def _enqueue(self, db, uid, kind, details):
        db.execute('INSERT INTO outbox(id,body,created) VALUES(?,?,?)',
                   (uid, json.dumps(dict(id=uid, type=kind, business=self.business.name, details=details)), int(self.now().timestamp())))

    def set_capacity(self, day, capacity):
        date.fromisoformat(day)
        if type(capacity) is not int or not 0 <= capacity <= 100:
            raise ValueError("Capacity must be 0–100")
        with self.connect(write=True) as db:
            db.execute('INSERT OR REPLACE INTO overrides VALUES(?,?)', (day, capacity))
        return dict(day=day, capacity=capacity)

    def setting(self, key, default, value=None):
        with self.connect(write=value is not None) as db:
            if value is not None:
                db.execute('INSERT OR REPLACE INTO settings VALUES(?,?)', (key, json.dumps(value)))
                return value
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row[0]) if row else default

    def cancel(self, booking_id):
        with self.connect(write=True) as db:
            cur = db.execute("UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'", (booking_id,))
            return dict(cancelled=cur.rowcount == 1)

    def issue_stream(self, call_id, phone):
        token = secrets.token_urlsafe(32)
        with self.connect(write=True) as db:
            db.execute('DELETE FROM streams WHERE expires<?', (int(self.now().timestamp()),))
            db.execute('INSERT INTO streams VALUES(?,?,?,?)', (token, call_id, phone, int(self.now().timestamp())+180))
        return token

    def consume_stream(self, token, call_id):
        with self.connect(write=True) as db:
            row = db.execute('SELECT * FROM streams WHERE token=? AND call_id=? AND expires>=?', (token, call_id, int(self.now().timestamp()))).fetchone()
            if row:
                db.execute('DELETE FROM streams WHERE token=?', (token,))
                return dict(row)
        return None
