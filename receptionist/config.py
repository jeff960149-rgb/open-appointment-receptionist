from __future__ import annotations

import json
import re
from datetime import date, time
from pathlib import Path
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Service(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9_-]{1,60}$")
    name: str = Field(min_length=1, max_length=120)
    prices: dict[int, float]
    capacity_units: int = Field(default=1, ge=1, le=100)
    buffer_minutes: int = Field(default=0, ge=0, le=120)

    @field_validator("prices")
    @classmethod
    def check_prices(cls, value):
        if not value or any(d < 5 or d > 480 or p < 0 or p != p or p == float('inf') for d, p in value.items()):
            raise ValueError("Use durations of 5–480 minutes with finite nonnegative prices")
        return value


class Business(StrictModel):
    name: str = Field(min_length=1, max_length=120)
    assistant_name: str = "Alex"
    timezone: str = "America/Los_Angeles"
    currency: str = Field(default="USD", pattern=r"^[A-Z]{3}$")
    address: str = ""
    phone: str = ""
    capacity: int = Field(default=1, ge=0, le=100)
    hours: dict[str, list[tuple[str, str]]]
    services: list[Service] = Field(min_length=1)
    policies: list[str] = Field(default_factory=list)
    date_overrides: dict[str, list[tuple[str, str]]] = Field(default_factory=dict)
    slot_minutes: int = Field(default=30, ge=5, le=60)
    max_days_ahead: int = Field(default=60, ge=1, le=365)

    @field_validator("timezone")
    @classmethod
    def check_timezone(cls, value):
        ZoneInfo(value)
        return value

    @model_validator(mode="after")
    def validate_calendar(self):
        if len({s.id for s in self.services}) != len(self.services):
            raise ValueError("Service IDs must be unique")
        for key, periods in list(self.hours.items()) + list(self.date_overrides.items()):
            if key not in {str(i) for i in range(7)}:
                date.fromisoformat(key)
            if key in self.hours and key not in {str(i) for i in range(7)}:
                raise ValueError("Weekday keys are 0 (Monday) through 6 (Sunday)")
            previous = "00:00"
            for start, end in periods:
                if not all(re.fullmatch(r"\d{2}:\d{2}", v) for v in (start, end)):
                    raise ValueError("Hours must be HH:MM")
                time.fromisoformat(start)
                time.fromisoformat(end)
                if start >= end or start < previous:
                    raise ValueError("Hours must be sorted, non-overlapping, within the same day")
                previous = end
        return self

    def service(self, service_id: str) -> Service:
        for service in self.services:
            if service.id == service_id:
                return service
        raise ValueError("Unknown service; ask the caller which listed service they want")

    def periods(self, day: str):
        return self.date_overrides.get(day, self.hours.get(str(date.fromisoformat(day).weekday()), []))


def load_business(path: str | Path) -> Business:
    return Business.model_validate(json.loads(Path(path).read_text()))
