# Contributing

Contributions for different appointment businesses are welcome: useful profiles, setup fixes, translations, regression cases, accessibility improvements and tested scheduling adapters.

1. Fork and branch from `main`.
2. Use Python 3.11+, install `requirements-dev.txt`, and run `python -m pytest tests -q`.
3. Keep provider calls mocked in automated tests. Use synthetic names and reserved example phone numbers.
4. Run `python scripts/check_public_files.py`. Never attach live customer transcripts, photos, private staff rosters, credentials or database files to an issue/PR.
5. Describe the user-visible problem, change, tests and limitations. For a calendar adapter include failure, stale-read, double-booking and retry behavior.

Keep the shared core business-neutral. Store-specific policies belong in an explicit profile. Do not make the AI invent availability, named staff presence or successful bookings. Do not automatically turn a staff request into a public issue.

First-party contributions are licensed under the repository's MIT license. Preserve all third-party notices. The `reference/` snapshots are explanatory legacy code; changes to the supported app belong in `receptionist/`.
