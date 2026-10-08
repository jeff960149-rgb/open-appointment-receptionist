# Sanitized source history

These two independent source snapshots explain the project's origins and preserve integrations useful to other developers. They are **not imported by the supported generic application**.

| Directory | Useful material |
| --- | --- |
| `kspa/app/` | Smiley read/write with verification, human-first Twilio routing, SIP, callback escalation, Telegram/LINE, browser voice and phone tests |
| `footprint/app/` | Local staffing calendar, party bookings, walk-ins, Telegram schedule administration, isolated browser conversation tests |

These are adapted snapshots, not byte-for-byte production backups. Contacts and personal staff names were replaced with examples; the Smiley entity default is empty and automatic writes default off. Original databases, credentials, handoffs, transcripts, recordings, deployment identifiers and store-specific test fixtures are intentionally absent. Historical policies/defaults may be stale; do not treat them as current business information.

Source layout and imports were retained to make the integration code readable. These folders still contain hardcoded legacy assumptions and are not covered by the new core's passing tests. Root installation, Docker and CI use only the generic `receptionist/` application. Start there; port a specific integration with its own authentication, scheduling contract and tests instead of exposing these legacy apps unchanged.

First-party code is MIT licensed under the root license. The vendored Twilio browser SDK retains the notice alongside it.
