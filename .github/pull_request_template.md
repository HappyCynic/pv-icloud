## What and why

<!-- One change per pull request. Link the issue if there is one. -->

## Checklist

- [ ] Tests added or updated; `uv run pytest`, `uv run ruff check .`, `uv run mypy` pass
- [ ] Read path still read-only; DAV methods only `PROPFIND`/`REPORT`; any mail write stays in
      `mail/imap_write.py` / `mail/smtp.py` with only the commands ADR-0004 allows
- [ ] Nothing private is logged (message content, credentials, feed URLs)
- [ ] Tool descriptions state any new limits and how to page
- [ ] `CHANGELOG.md` updated (`Unreleased`) if users will notice the change
- [ ] New files carry the SPDX header
