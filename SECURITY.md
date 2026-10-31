# Security & Privacy

This repository is a sanitized portfolio snapshot of a multi-source real-estate collection system.

The public version intentionally excludes:

- production `.env` values
- Aqar / Haraj account credentials
- Telegram bot tokens and chat IDs
- saved Playwright/Chromium session state
- production SQLite databases and backups
- advertiser phone numbers and contact records
- production listing URLs/data exports
- Google Maps diagnostic screenshots, galleries, and run logs
- generated recovery/debug artifacts

Credentials must be supplied through local environment variables and authenticated-session files must remain local.

The collectors are written to detect access restrictions and human-verification pages and to stop/retry rather than implement CAPTCHA bypass logic.

If any credential from the original development environment was shared or committed elsewhere, rotate/revoke it before reuse.
