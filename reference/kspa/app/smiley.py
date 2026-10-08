from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Any, Iterable
from urllib.parse import quote
from zoneinfo import ZoneInfo

import httpx

from app.kspa_business import KSPA_TEMPORARY_CAPACITY_OVERRIDES


DEFAULT_API_BASE = "https://api.joinsmiley.com"
logger = logging.getLogger("ai-receptionist")


class SmileyVerificationError(RuntimeError):
    def __init__(self, remote_id: str):
        super().__init__("Smiley create returned an ID but readback was not verified.")
        self.remote_id = remote_id


@dataclass(frozen=True)
class SmileyConfig:
    email: str
    password: str
    entity: str
    api_base: str = DEFAULT_API_BASE
    timezone_name: str = "America/Los_Angeles"
    poll_seconds: int = 300
    days_ahead: int = 14
    write_enabled: bool = False
    write_fields: tuple[str, ...] = ("service", "time")

    @property
    def configured(self) -> bool:
        return bool(self.email and self.password and self.entity)

    @classmethod
    def from_env(cls) -> "SmileyConfig":
        try:
            poll_seconds = max(60, int(os.getenv("SMILEY_POLL_SECONDS", "300")))
        except ValueError:
            poll_seconds = 300
        try:
            days_ahead = max(1, min(60, int(os.getenv("SMILEY_DAYS_AHEAD", "14"))))
        except ValueError:
            days_ahead = 14
        return cls(
            email=os.getenv("SMILEY_EMAIL", "").strip(),
            password=os.getenv("SMILEY_PASSWORD", ""),
            entity=os.getenv("SMILEY_ENTITY", "").strip(),
            api_base=os.getenv("SMILEY_API_BASE", DEFAULT_API_BASE).rstrip("/"),
            timezone_name=os.getenv("SMILEY_TIMEZONE", "America/Los_Angeles"),
            poll_seconds=poll_seconds,
            days_ahead=days_ahead,
            # The owner explicitly resumed automatic Smiley booking. Keep the
            # production switch reversible through the environment variable.
            write_enabled=os.getenv("SMILEY_WRITE_ENABLED", "false").strip().casefold()
            in {"1", "true", "yes", "on"},
            write_fields=tuple(
                field.strip().casefold()
                for field in os.getenv("SMILEY_WRITE_FIELDS", "service,time").split(",")
                if field.strip().casefold() in {"service", "time", "phone"}
            ) or ("service", "time"),
        )


def build_appointment_write_preview(
    service: str,
    requested_date: str,
    requested_time: str,
    duration_minutes: int,
    *,
    timezone_name: str = "America/Los_Angeles",
) -> dict[str, Any]:
    """Build the approved Smiley fields without sending or persisting anything."""
    if not service.strip():
        raise ValueError("Service is required.")
    if duration_minutes <= 0 or duration_minutes > 240:
        raise ValueError("Invalid duration.")
    local_zone = ZoneInfo(timezone_name)
    try:
        start_at = datetime.strptime(
            f"{requested_date} {requested_time}", "%Y-%m-%d %H:%M"
        ).replace(tzinfo=local_zone)
    except ValueError as exc:
        raise ValueError("Use YYYY-MM-DD and HH:MM.") from exc
    end_at = start_at + timedelta(minutes=duration_minutes)
    return {
        "service": service.strip(),
        "time": {
            "start_at": start_at.isoformat(),
            "end_at": end_at.isoformat(),
        },
    }


def _service_words(value: str) -> set[str]:
    words = set(re.findall(r"[a-z]+", value.casefold()))
    return words - {"massage", "min", "mins", "minute", "minutes", "session"}


def _service_duration(attributes: dict[str, Any]) -> int | None:
    value = _value(attributes, "duration", "duration-minutes", "duration_minutes")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _find_service_id(
    records: Iterable[dict[str, Any]], service: str, duration_minutes: int
) -> str:
    requested_words = _service_words(service)
    if not requested_words:
        raise RuntimeError("The requested service name is too vague for Smiley.")

    ranked: list[tuple[int, str]] = []
    for record in records:
        attributes = _attributes(record)
        if _value(attributes, "deletedAt", "deleted-at", "deleted_at"):
            continue
        name = str(_value(attributes, "name") or "")
        candidate_words = _service_words(name)
        if not requested_words.issubset(candidate_words):
            continue
        configured_duration = _service_duration(attributes)
        duration_in_name = {
            int(value)
            for value in re.findall(r"\b(\d{2,3})\s*(?:min|mins|minute|minutes)\b", name.casefold())
        }
        if configured_duration is not None and configured_duration != duration_minutes:
            continue
        if duration_in_name and duration_minutes not in duration_in_name:
            continue
        score = 100 - len(candidate_words - requested_words)
        if configured_duration == duration_minutes or duration_minutes in duration_in_name:
            score += 25
        record_id = str(record.get("id") or "").strip()
        if record_id:
            ranked.append((score, record_id))

    if not ranked:
        raise RuntimeError("The requested service and duration were not found in Smiley.")
    ranked.sort(reverse=True)
    top_score = ranked[0][0]
    top_ids = {record_id for score, record_id in ranked if score == top_score}
    if len(top_ids) != 1:
        raise RuntimeError("The requested service matches more than one Smiley item.")
    return next(iter(top_ids))


def _attributes(item: dict[str, Any]) -> dict[str, Any]:
    attributes = item.get("attributes")
    return attributes if isinstance(attributes, dict) else item


def _value(mapping: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _parse_datetime(value: Any, timezone_name: str) -> datetime | None:
    if not value:
        return None
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    local_zone = ZoneInfo(timezone_name)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=local_zone)
    return parsed.astimezone(local_zone)


def _parse_clock(value: Any) -> time | None:
    if not value:
        return None
    text = str(value).strip().replace(" ", "")
    for pattern in ("%I:%M%p", "%I%p", "%H:%M"):
        try:
            return datetime.strptime(text.upper(), pattern).time()
        except ValueError:
            continue
    return None


def _day_name(target: date) -> str:
    return target.strftime("%a")


def merge_local_bookings(remote_records, local_bookings, timezone_name):
    """Count remote/local copies once; merge all bookings before assigning lanes."""
    busy = list(remote_records)
    by_id = {str(row.get("id")): _attributes(row) for row in remote_records}
    represented = {}
    for row in local_bookings:
        preview = build_appointment_write_preview(
            row["service"], row["requested_date"], row["requested_time"],
            row["duration_minutes"], timezone_name=timezone_name,
        )
        starts = _parse_datetime(preview["time"]["start_at"], timezone_name)
        ends = _parse_datetime(preview["time"]["end_at"], timezone_name)
        remote = by_id.get(str(row.get("smiley_remote_id") or ""), {})
        same_interval = (
            not _value(remote, "canceled-at", "canceledAt")
            and _parse_datetime(_value(remote, "start-at", "startAt"), timezone_name) == starts
            and _parse_datetime(_value(remote, "end-at", "endAt"), timezone_name) == ends
        )
        if not same_interval:
            # A missing, moved or cancelled remote copy cannot erase a local
            # confirmation. Keep its original interval blocked for staff review.
            busy.append({"type": "appointment", "attributes": {
                "start-at": starts.isoformat(), "end-at": ends.isoformat(),
                "num-of-people": 1,
            }})
        represented[str(row["id"])] = [
            row["requested_date"], row["requested_time"], row["duration_minutes"],
        ]
    return busy, represented


def derive_free_intervals(
    business_hours: Iterable[dict[str, Any]],
    busy_items: Iterable[dict[str, Any]],
    start_date: date,
    end_date: date,
    timezone_name: str,
    capacity: int = 3,
    base_capacity: int = 2,
    temporary_capacity_overrides: dict | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, str]]]:
    """Build anonymous lanes, including explicitly dated temporary capacity."""
    if temporary_capacity_overrides is None:
        temporary_capacity_overrides = KSPA_TEMPORARY_CAPACITY_OVERRIDES
    local_zone = ZoneInfo(timezone_name)
    hours_by_day: dict[str, list[tuple[time, time]]] = {}
    for raw in business_hours:
        day = str(_value(raw, "dayOfWeek", "day-of-week", "day_of_week") or "")[:3].title()
        starts = _parse_clock(_value(raw, "startTime", "start-time", "start_time"))
        ends = _parse_clock(_value(raw, "endTime", "end-time", "end_time"))
        if day and starts and ends:
            hours_by_day.setdefault(day, []).append((starts, ends))

    appointments: list[tuple[datetime, datetime]] = []
    blockers: list[tuple[datetime, datetime]] = []
    for item in busy_items:
        attrs = _attributes(item)
        if _value(attrs, "canceledAt", "canceled-at", "canceled_at"):
            continue
        starts = _parse_datetime(
            _value(attrs, "startAt", "start-at", "start_at"), timezone_name
        )
        ends = _parse_datetime(
            _value(attrs, "endAt", "end-at", "end_at"), timezone_name
        )
        if starts and ends and starts < ends:
            target_intervals = (
                blockers
                if "blocker" in str(item.get("type") or "").casefold()
                else appointments
            )
            count = 1 if target_intervals is blockers else max(
                1, int(_value(attrs, "num-of-people", "numOfPeople") or 1)
            )
            target_intervals.extend([(starts, ends)] * min(count, 20))

    capacity = max(1, capacity)
    lane_busy: list[list[tuple[datetime, datetime]]] = [
        list(blockers) for _ in range(capacity)
    ]
    for starts, ends in sorted(appointments, key=lambda interval: interval[0]):
        available_lane = next(
            (
                lane for lane in lane_busy
                if not any(
                    starts < busy_end and ends > busy_start
                    for busy_start, busy_end in lane
                )
            ),
            None,
        )
        if available_lane is None:
            # Existing calendar data already exceeds configured capacity. Block
            # every lane for this interval rather than offer an unsafe opening.
            for lane in lane_busy:
                lane.append((starts, ends))
        else:
            available_lane.append((starts, ends))

    free_rows: list[dict[str, Any]] = []
    shift_rows: list[dict[str, str]] = []
    target = start_date
    while target <= end_date:
        for opens, closes in hours_by_day.get(_day_name(target), []):
            open_at = datetime.combine(target, opens, local_zone)
            close_at = datetime.combine(target, closes, local_zone)
            if close_at <= open_at:
                close_at += timedelta(days=1)
            shift_rows.append({
                "date": target.isoformat(),
                "start_time": open_at.strftime("%H:%M"),
                "end_time": close_at.strftime("%H:%M"),
            })
            capacity_override = temporary_capacity_overrides.get(target.isoformat())
            day_capacity = (
                min(capacity, int(capacity_override["capacity"]))
                if capacity_override
                else base_capacity
            )
            for capacity_slot, busy in enumerate(lane_busy[:day_capacity], start=1):
                lane_open_at = open_at
                if capacity_slot > base_capacity:
                    extra_capacity_start = time.fromisoformat(
                        str(capacity_override["start_time"])
                    )
                    lane_open_at = max(
                        lane_open_at,
                        datetime.combine(target, extra_capacity_start, local_zone),
                    )
                if lane_open_at >= close_at:
                    continue
                overlapping = sorted(
                    (
                        (max(lane_open_at, busy_start), min(close_at, busy_end))
                        for busy_start, busy_end in busy
                        if busy_start < close_at and busy_end > lane_open_at
                    ),
                    key=lambda interval: interval[0],
                )
                cursor = lane_open_at
                for busy_start, busy_end in overlapping:
                    if busy_start > cursor:
                        free_rows.append({
                            "date": target.isoformat(),
                            "start_time": cursor.strftime("%H:%M"),
                            "end_time": busy_start.strftime("%H:%M"),
                            "capacity_slot": capacity_slot,
                        })
                    cursor = max(cursor, busy_end)
                if cursor < close_at:
                    free_rows.append({
                        "date": target.isoformat(),
                        "start_time": cursor.strftime("%H:%M"),
                        "end_time": close_at.strftime("%H:%M"),
                        "capacity_slot": capacity_slot,
                    })
        target += timedelta(days=1)
    return free_rows, shift_rows


def _token_from_login(payload: dict[str, Any]) -> str:
    candidates = [payload, payload.get("user", {}), payload.get("data", {})]
    data = payload.get("data")
    if isinstance(data, dict):
        candidates.append(data.get("attributes", {}))
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        token = _value(
            candidate,
            "token",
            "authentication-token",
            "authentication_token",
            "authenticationToken",
        )
        if token:
            return str(token)
    raise RuntimeError("Smiley login succeeded but returned no API token.")


class SmileyClient:
    def __init__(self, config: SmileyConfig, transport: httpx.AsyncBaseTransport | None = None):
        self.config = config
        self.transport = transport

    async def _json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        token: str = "",
        params: dict[str, str] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/vnd.api+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
            if json_body is not None:
                headers["Content-Type"] = "application/vnd.api+json"
        response = await client.request(
            method, path, headers=headers, params=params, json=json_body
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError:
            # Error bodies may echo caller information; log status only.
            logger.error(
                "Smiley API request failed: method=%s path=%s status=%s",
                method,
                path,
                response.status_code,
            )
            raise
        payload = response.json()
        if not isinstance(payload, dict):
            raise RuntimeError("Smiley returned an unexpected response.")
        return payload

    async def fetch_free_intervals(
        self, *, now_local: datetime | None = None,
        local_bookings: Iterable[dict[str, Any]] = (),
    ) -> dict[str, Any]:
        if not self.config.configured:
            raise RuntimeError("Smiley credentials are not configured.")
        local_zone = ZoneInfo(self.config.timezone_name)
        current = now_local or datetime.now(local_zone)
        if current.tzinfo is None:
            current = current.replace(tzinfo=local_zone)
        start_date = current.astimezone(local_zone).date()
        end_date = start_date + timedelta(days=self.config.days_ahead)
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(
            base_url=self.config.api_base,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            login = await self._json(
                client,
                "POST",
                "/sessions",
                json_body={
                    "user": {
                        "email": self.config.email,
                        "password": self.config.password,
                    }
                },
            )
            token = _token_from_login(login)
            entity_payload = await self._json(
                client, "GET", f"/entities/{self.config.entity}", token=token
            )
            entity_data = entity_payload.get("data", {})
            if not isinstance(entity_data, dict):
                raise RuntimeError("Smiley entity response is invalid.")
            entity_id = str(entity_data.get("id") or self.config.entity)
            entity_attrs = _attributes(entity_data)
            timezone_name = str(
                _value(entity_attrs, "timezone", "time-zone", "time_zone")
                or self.config.timezone_name
            )
            business_hours = _value(
                entity_attrs, "businessHours", "business-hours", "business_hours"
            ) or []
            range_params = {
                "filter[entity]": entity_id,
                "filter[canceled]": "false",
                "range[start]": (start_date - timedelta(days=1)).isoformat(),
                "range[end]": end_date.isoformat(),
                "fields[appointment]": "start-at,end-at,canceled-at,num-of-people",
            }
            blocker_params = {
                "filter[entity]": entity_id,
                "range[start]": (start_date - timedelta(days=1)).isoformat(),
                "range[end]": end_date.isoformat(),
                "fields[slot-blocker]": "start-at,end-at",
            }
            appointments = await self._json(
                client, "GET", "/appointments", token=token, params=range_params
            )
            blockers = await self._json(
                client, "GET", "/slot-blockers", token=token, params=blocker_params
            )
        busy_items, represented = merge_local_bookings(
            list(appointments.get("data") or []), local_bookings, timezone_name
        )
        busy_items += list(blockers.get("data") or [])
        free_rows, shift_rows = derive_free_intervals(
            business_hours, busy_items, start_date, end_date, timezone_name
        )
        return {
            "free_rows": free_rows,
            "shift_rows": shift_rows,
            "window_start": start_date.isoformat(),
            "window_end": end_date.isoformat(),
            "timezone": timezone_name,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "represented_local_bookings": represented,
            "capacity_source": {
                "business_hours": business_hours,
                "appointments": list(appointments.get("data") or []),
                "blockers": list(blockers.get("data") or []),
                "timezone": timezone_name,
            },
        }

    async def create_appointment(
        self,
        service: str,
        requested_date: str,
        requested_time: str,
        duration_minutes: int,
        *,
        customer_phone: str = "",
    ) -> dict[str, Any]:
        """Create one entry with the explicitly approved service, time and phone."""
        if not self.config.configured:
            raise RuntimeError("Smiley credentials are not configured.")
        if not self.config.write_enabled:
            raise RuntimeError("Smiley appointment writing is disabled.")
        if set(self.config.write_fields) != {"service", "time", "phone"}:
            raise RuntimeError("Smiley writing requires approval for service, time and phone.")
        phone = re.sub(r"[\s().-]", "", customer_phone)
        if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
            raise ValueError("A valid international caller phone number is required for Smiley.")

        preview = build_appointment_write_preview(
            service,
            requested_date,
            requested_time,
            duration_minutes,
            timezone_name=self.config.timezone_name,
        )
        timeout = httpx.Timeout(20.0)
        async with httpx.AsyncClient(
            base_url=self.config.api_base,
            timeout=timeout,
            transport=self.transport,
        ) as client:
            login = await self._json(
                client,
                "POST",
                "/sessions",
                json_body={
                    "user": {
                        "email": self.config.email,
                        "password": self.config.password,
                    }
                },
            )
            token = _token_from_login(login)
            entity_payload = await self._json(
                client, "GET", f"/entities/{self.config.entity}", token=token
            )
            entity_data = entity_payload.get("data", {})
            if not isinstance(entity_data, dict):
                raise RuntimeError("Smiley entity response is invalid.")
            entity_id = str(entity_data.get("id") or self.config.entity)
            services_payload = await self._json(
                client,
                "GET",
                "/entity-services",
                token=token,
                params={
                    "filter[entity]": entity_id,
                    "fields[entity-service]": "name,duration,deleted-at",
                },
            )
            services = services_payload.get("data") or []
            if not isinstance(services, list):
                raise RuntimeError("Smiley service response is invalid.")
            service_id = _find_service_id(services, service, duration_minutes)
            start_at = datetime.fromisoformat(preview["time"]["start_at"])
            end_at = datetime.fromisoformat(preview["time"]["end_at"])
            payload = {
                "data": {
                    "type": "appointments",
                    "attributes": {
                        "start-at": start_at.astimezone(timezone.utc).isoformat(
                            timespec="milliseconds"
                        ).replace("+00:00", "Z"),
                        "end-at": end_at.astimezone(timezone.utc).isoformat(
                            timespec="milliseconds"
                        ).replace("+00:00", "Z"),
                        # Smiley's dashboard always serializes these neutral
                        # appointment defaults on create. Only the approved
                        # caller phone is populated; SMS is explicitly disabled.
                        "num-of-people": 1,
                        "notes": None,
                        "phone-number": phone,
                        "internal-notes": None,
                        "services": [service_id],
                        "assignees": [],
                        "disable-text": True,
                        "canceled-at": None,
                        "completed-at": None,
                        "confirmed-at": None,
                        "first-name": None,
                        "last-name": None,
                        "rooms": [],
                        "repeat": None,
                    },
                    "relationships": {
                        "entity": {
                            "data": {"type": "entities", "id": entity_id}
                        },
                        # Smiley returns 404 for explicit null relationship
                        # linkage. Omit absent relationships entirely.
                    },
                }
            }
            created = await self._json(
                client,
                "POST",
                "/appointments",
                token=token,
                json_body=payload,
            )
            created_data = created.get("data", {})
            remote_id = str(created_data.get("id") or "") if isinstance(created_data, dict) else ""
            if not remote_id:
                raise RuntimeError("Smiley did not return the created appointment ID.")
            try:
                readback = await self._json(
                    client, "GET", f"/appointments/{quote(remote_id, safe='')}", token=token,
                )
                data = readback.get("data") or {}
                attrs = _attributes(data)
                if (str(data.get("id")) != remote_id
                        or _parse_datetime(_value(attrs, "start-at", "startAt"), self.config.timezone_name) != start_at
                        or _parse_datetime(_value(attrs, "end-at", "endAt"), self.config.timezone_name) != end_at
                        or [str(value) for value in attrs.get("services", [])] != [service_id]
                        or _value(attrs, "canceled-at", "canceledAt")):
                    raise RuntimeError("Readback did not match requested appointment.")
            except Exception as exc:
                raise SmileyVerificationError(remote_id) from exc
        return {
            "written": True,
            "verified": True,
            "remote_id": remote_id,
            "fields": ["service", "time", "phone"],
        }
