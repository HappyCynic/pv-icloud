# Tasks

## 1. Fake IMAP connection records CAPABILITY

- [x] 1.1 In `tests/fakeimap.py`, update `FakeConn` so that making a connection (i.e.
      `FakeServer.connect()` / `FakeConn.__init__`) logs a `CAPABILITY` command via `self._log(...)`,
      mirroring `imaplib`'s real connect-time behaviour. Verify with a new unit test asserting
      `server.commands[0] == ("CAPABILITY",)` right after `server.connect()`.

## 2. Tighten the read-only command assertion

- [x] 2.1 In `tests/test_mail.py`, update `test_reads_never_change_flags` so the allowed-command
      set includes `CAPABILITY` and the assertion checks the recorded commands are a subset of
      the set the `read-only-and-egress` spec's "Read-only IMAP commands" requirement
      allows (`CAPABILITY`, `LOGIN`, `ENABLE`, `LIST`, `STATUS`, `EXAMINE`, `UID`, `NOOP`, `IDLE`,
      `LOGOUT`), rather than only subtracting a hard-coded allow-list. Verify by running
      `uv run pytest tests/test_mail.py -k test_reads_never_change_flags` and confirming it fails
      if `FakeConn` is temporarily made to log an extra command outside that set (check locally,
      do not leave the regression in place).
- [x] 2.2 Check any other place in `tests/` that filters or asserts on `server.commands` (for
      example the sync-worker tests) still passes with `CAPABILITY` now present in the log;
      update any assertion that assumed an incomplete log. Verify with `uv run pytest tests/`.

## 3. Documentation

- [x] 3.1 Add `CAPABILITY` to the list of allowed commands in the module docstring of
      `src/pv_icloud/mail/imap.py`, matching the `read-only-and-egress` spec and the fixed fake.
      Verify by re-reading the docstring against the spec's requirement text. This change is
      test- and docstring-only, so no user would notice it and it gets no `CHANGELOG.md` entry.

## 4. Final checks

- [x] 4.1 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .` and
      `uv run mypy`; fix anything they report.
- [x] 4.2 Run `openspec validate --all --strict` and fix anything it reports.
