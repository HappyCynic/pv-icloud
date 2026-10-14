## What and why

<!-- One change per pull request. Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated; `uv run pytest`, `uv run ruff check .`, `uv run mypy` pass
- [ ] Read path still read-only; the DAV base sends only `PROPFIND`/`REPORT`; any mail write
      stays in `mail/imap_write.py` / `mail/smtp.py` (ADR-0004), any calendar write in
      `cal/caldav_write.py` (ADR-0005) and any contact write in `contacts/carddav_write.py`
      (ADR-0006), with only the commands those ADRs allow
- [ ] Nothing private is logged (message content, credentials, feed URLs)
- [ ] Tool descriptions state any new limits and how to page
- [ ] `CHANGELOG.md` updated (`Unreleased`) if users will notice the change
- [ ] New files carry the SPDX header
