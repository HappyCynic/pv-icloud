## What and why

<!-- One change per pull request. Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated; `uv run pytest`, `uv run ruff check .`, `uv run mypy` pass
- [ ] Still read-only: no new IMAP write commands, no DAV methods beyond `PROPFIND`/`REPORT`
- [ ] Nothing private is logged (message content, credentials, feed URLs)
- [ ] Tool descriptions state any new limits and how to page
- [ ] `CHANGELOG.md` updated (`Unreleased`) if users will notice the change
- [ ] New files carry the SPDX header
