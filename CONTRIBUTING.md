# Contributing to Private iCloud MCP

Thanks for helping. Bug reports, fixes and focused features are welcome; for anything larger
than a small fix, open an issue first so we can agree on the approach.

**Security issues:** do not open a public issue. Report them privately through GitHub's
private vulnerability reporting (the repository's **Security** tab); see
[SECURITY.md](SECURITY.md).

Everyone taking part is expected to follow the [code of conduct](CODE_OF_CONDUCT.md).

The design is described in [docs/design.md](docs/design.md), with decisions in
[docs/adr/](docs/adr/).

## Development setup

You need Python 3.14 and [uv](https://docs.astral.sh/uv/), or just Docker and an editor that
supports dev containers (VS Code, or the `devcontainer` CLI).

### Dev container (recommended)

Open the repository in the dev container. On first start it creates three gitignored files
from their templates:

| File | Purpose |
|---|---|
| `.devcontainer/devcontainer.env` | iCloud credentials, only for live runs (tests don't need them) |
| `.devcontainer/compose.local.yaml` | Your own additions to the container: extra mounts, a host SSH agent |
| `config/pv-icloud.yaml` | Local server config |

SSH and git work through the agent your editor forwards. To use the host's own agent
instead (for example a 1Password agent with OrbStack or Docker Desktop on macOS), uncomment
the example in `compose.local.yaml` and rebuild the container; `post-create.sh` then points
ssh and git commit signing at it.

### Without the dev container

```sh
uv sync
```

## Checks

CI runs these on every pull request; run them before you push:

```sh
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
```

The tests need no iCloud account and no network: `tests/fakeimap.py` and
`tests/fakecaldav.py` and `tests/fakecarddav.py` are in-process fakes that copy the iCloud
behaviour the server depends on (CONDSTORE, CalDAV expansion, subscribed-calendar feeds,
CardDAV sync tokens). When you find iCloud behaving
differently from a fake, fix the fake as well as the server, and add a test.

### Live runs (optional)

To try changes against a real account, put an Apple ID and an
[app-specific password](https://support.apple.com/102654) in
`.devcontainer/devcontainer.env`, set a token hash in `config/pv-icloud.yaml`
(`uv run pv-icloud hash-token`), and run `uv run pv-icloud`. Use an account you own; the
mail index is written to `/data` inside the container, never to the workspace.

## Ground rules for changes

- **Read-only by default.** The IMAP read path only uses `EXAMINE` and `BODY.PEEK`; the
  CalDAV and CardDAV read clients can only send `PROPFIND` and `REPORT` (`dav.py`). Mail writes are
  opt-in ([ADR-0004](docs/adr/0004-opt-in-mail-write.md)): only in `mail/imap_write.py` and
  `mail/smtp.py`, only behind `PV_ICLOUD_MAIL_WRITE=enabled` and a per-token grant, and only
  with the commands `CLAUDE.md` lists. Calendar event writes are opt-in too
  ([ADR-0005](docs/adr/0005-opt-in-calendar-write.md)): only in `cal/caldav_write.py`, only
  behind `PV_ICLOUD_CALENDAR_WRITE=enabled` and `calendar_write: true`, only conditional
  `PUT`/`DELETE` of one event in a calendar the account may write, and never with attendees.
  Contact writes are opt-in as well ([ADR-0006](docs/adr/0006-opt-in-contact-write.md)): only
  in `contacts/carddav_write.py`, only behind `PV_ICLOUD_CONTACTS_WRITE=enabled` and a
  per-token grant, conditional and lossless (only the properties asked for change). Any other
  write needs a new ADR first.
- **Content is untrusted.** Mail, calendar and contact text can contain prompt-injection
  attempts.
  Return it as data with `content_trust: "untrusted"`; never let it change server behaviour.
- **Nothing private in logs.** Audit lines record tool, account, counts and timings; search
  text and ids are redacted to their length. Never log message content, credentials or
  subscribed-calendar feed URLs (they often contain private tokens).
- **Tool descriptions state their limits.** If a tool caps results, its description says what
  the cap is and how to page (`limit` / `offset` / `next_offset`).
- **Dependencies** must have permissive licences compatible with Apache-2.0 (MIT, BSD,
  Apache, PSF, MPL-2.0 unmodified). No GPL/LGPL.
- New source files start with the SPDX header:

  ```python
  # SPDX-FileCopyrightText: 2026 Happy Cynic Software LLC
  # SPDX-License-Identifier: Apache-2.0
  ```

## Pull requests

- Keep each pull request to one change, with tests.
- Add a line to the `Unreleased` section of [`CHANGELOG.md`](CHANGELOG.md) for anything a
  user would notice.
- Commits to `main` must be signed (SSH or GPG signing both work).
- By contributing you agree that your contribution is licensed under the
  [Apache License 2.0](LICENSE), as described in its section 5.
