# Proposal

## Why

Fixes #9. The `read-only-and-egress` spec already allows `CAPABILITY` (`imaplib` sends it on
connect), but the guarantee is weaker than it looks: `src/pv_icloud/mail/imap.py`'s module
docstring omits `CAPABILITY` from its list of allowed commands, and `tests/fakeimap.py`'s
`FakeConn` never records a `CAPABILITY` command. Because the fake's `commands` log is what
`test_reads_never_change_flags` (and similar assertions) check against the spec's allowed-command
list, an incomplete log means those tests cannot actually catch a forbidden command slipping in
on connect — the check passes whether or not the real client ever sends something it shouldn't.

## What Changes

- `tests/fakeimap.py`: `FakeConn` records a `CAPABILITY` command when a connection is made
  (mirroring `imaplib`'s real connect-time behaviour), so the fake's command log reflects every
  command a real server would see.
- `tests/test_mail.py`: `test_reads_never_change_flags` (and any other place that filters
  `server.commands` against an allow-list) is tightened to assert the recorded commands are
  exactly the set the `read-only-and-egress` spec allows, including `CAPABILITY`.
- `src/pv_icloud/mail/imap.py`: the module docstring's list of allowed commands gains
  `CAPABILITY`, matching what the spec already states and what `imaplib` actually sends.
- No change to server behaviour: the IMAP client already only ever issues the commands the spec
  allows. This closes a test-fidelity gap and a documentation gap, not a behavioural one.

## Capabilities

### New Capabilities

(none)

### Modified Capabilities

(none — the `read-only-and-egress` spec's requirement text already allows `CAPABILITY`; no
requirement changes)

## Impact

- `tests/fakeimap.py`, `tests/test_mail.py`: test fixture and assertions only.
- `src/pv_icloud/mail/imap.py`: docstring comment only, no code path changes.
- Stays fully read-only; no new IMAP command or DAV method is introduced.
