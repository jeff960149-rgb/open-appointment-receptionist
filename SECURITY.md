# Security and private data

Report suspected credential exposure or exploitable issues through GitHub private vulnerability reporting when available. Do not post credentials, private recordings, customer information or exploit data in public issues. If private reporting is unavailable, open a minimal issue asking for a private channel without sensitive details.

Supported scope for this early release: the `receptionist/` core. Sanitized `reference/` source is historical material, not an independently hardened deployment product.

The core rejects unauthenticated admin operations, validates Twilio webhook signatures and one-time media tokens, and validates the Telegram secret plus staff chat ID. It starts with AI fallback disabled and has a maximum call duration. Public deployments should add HTTPS, rate limits, monitoring, persistent backups, access controls and their own retention policy.

Customer data belongs on your deployment's private disk. Do not commit `.env`, SQLite databases or sidecars, exports, handoff notes, provider IDs, SSH keys, customer recordings, trained personal voice models, browser session tokens or debug logs. `.gitignore`, CI and the public-file scanner are defense in depth, not proof that arbitrary data is safe to publish.

The speech provider receives live audio. The agent must never be allowed to change its own authentication settings or execute arbitrary commands. The tool set intentionally excludes arbitrary HTTP, SQL, shell execution and access to other customers' records.
