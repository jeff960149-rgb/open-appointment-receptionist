# Business configuration

Set `BUSINESS_CONFIG` to a JSON file. Changes take effect after restart. Invalid profiles fail at startup rather than producing guessed availability.

| Field | Meaning |
| --- | --- |
| `name`, `assistant_name` | Business and receptionist names |
| `timezone` | IANA timezone, e.g. `Asia/Taipei`, `America/Los_Angeles` |
| `currency` | Three-letter currency code |
| `address`, `phone` | Public business contact details only |
| `capacity` | Simultaneous interchangeable resource units; 0 means no availability |
| `hours` | Weekday keys `0` Monday through `6` Sunday; missing/empty means closed |
| `date_overrides` | `YYYY-MM-DD` → periods for that date; `[]` closes it |
| `slot_minutes` | Interval between suggested starts, default 30 |
| `max_days_ahead` | Booking horizon, default 60 days |
| `services` | Stable ID, name, `prices` mapping duration minutes to total service price |
| `capacity_units` | Resource units consumed per booking participant, default 1 |
| `buffer_minutes` | Additional occupied time after the service, default 0 |
| `policies` | Short, owner-approved business answers; do not put secrets here |

Example split hours: `"0": [["09:00", "12:00"], ["13:00", "17:00"]]`.
Periods must be sorted, non-overlapping and within one day; overnight hours are not supported. A full service plus buffer must fit in one period. DST times that do not exist or are ambiguous are rejected for clarification.

Prices are per service participant. `party_size=2` multiplies both the price and required resources. A four-hands service can use `capacity_units=2`: a single participant consumes two resources. Do not use this simplified model for mixed worker skills or services needing separately constrained equipment; implement a resource adapter first.

Set a date's capacity with the admin API or Telegram. Reducing capacity does not cancel or move existing appointments; it can make additional bookings unavailable. Overrides take precedence over the default capacity.

The shared calendar is the source of truth for the generic app. If staff also book in another calendar, integrate that system before enabling AI confirmations. Source material for Smiley is in `reference/kspa/app/smiley.py`; it is not a generic adapter yet.
