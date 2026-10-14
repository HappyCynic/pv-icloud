# Proposal

## Why

Implements #25. Account owners can find a contact through pv-icloud but cannot fix it: a stale
phone number or address found with an agent has to be corrected by hand in Apple's apps. As
with mail (ADR-0004) and calendar writes (ADR-0005), different clients deserve different
trust, so contact writes have to be granted per bearer token and be off unless an operator
turns them on. The rules were decided ahead of this issue in ADR-0006, after live checks on
iCloud; CLAUDE.md, `openspec/config.yaml` and `.github/CODEOWNERS` already carry them.

## What Changes

- **This change ends the read-only guarantee for Contacts,** within ADR-0006. CardDAV gains
  two write methods, `PUT` and `DELETE` of one vCard in the account's address book, sent only
  from a new write module, `contacts/carddav_write.py`, and only for a granted contact write
  tool call. The DAV base (`dav.py`) keeps refusing every method but `PROPFIND` and
  `REPORT`, so every contacts read, `find_person` and every calendar read stay exactly as they
  are. No new outbound host is added.
- **Deployment switch:** `PV_ICLOUD_CONTACTS_WRITE=enabled` (default off), parsed like the
  mail and calendar switches and independent of them. Without it the write module is never
  imported, no contact write tool is registered, and a token granted contact writes is a
  config error.
- **Per-token, per-action grant:** `contacts_write: [create, update, delete]`, each action
  granted alone, all off by default. **This differs from #25**, which asks for one on/off
  switch: ADR-0006 (decision 2) and CLAUDE.md require separate actions because a delete is
  permanent, and they take precedence. A token without `contacts_write` sees exactly today's
  tools, annotations and instructions.
- Three new tools, each listed only to tokens granted its action:
  - `contacts_create`: one new contact with any field `contacts_get` returns (names,
    organization, department, title, emails, phones, addresses, URLs, social profiles,
    instant messaging, birthday, dates, related names, note), with labels, and optionally in
    existing groups.
  - `contacts_update`: change, add or remove individual fields of one contact, and add it to
    or remove it from existing groups. **Lossless:** the stored vCard text is edited in place;
    only the properties the call changes are rewritten, plus `REV` (bumped, as Apple's apps
    do) and `FN` when it was derived from the old name (custom display names are kept); every
    other line (photo, item groups with `X-ABLabel`, `X-APPLE-…` and unknown properties,
    their order) is kept byte for byte. A change that cannot be applied unambiguously is
    refused with nothing written. New values are written in Apple Contacts' exact label
    forms (`TEL;TYPE=CELL;TYPE=VOICE`, `EMAIL;TYPE=INTERNET;TYPE=HOME`, …).
  - `contacts_delete`: delete one contact, permanently. Never the account owner's own card
    (the me-card) or a group card.
- **Groups:** membership lives in the group's vCard (`X-ADDRESSBOOKSERVER-MEMBER`), so adding
  a contact to or removing it from a group is a lossless edit of that group card, one
  conditional `PUT` per group. Groups are never created, renamed or deleted.
- **Excluded groups stay invisible:** a contact in `contacts.exclude_groups` cannot be read,
  so it cannot be written (same `not found` error); an excluded group cannot be named (same
  `unknown group` error as a missing one).
- **Safe concurrent edits.** Create uses a new UUID as file name and `UID`, with
  `If-None-Match: *`. Update and delete first re-read the card (and any group card they
  change) and send `If-Match` with that ETag; if the card changed meanwhile, iCloud answers
  412 and the call fails with "the contact changed in Contacts; read it again". Never
  retried.
- **Changes show at once:** after a write, the in-memory address book is refreshed with
  `sync-collection` before the call returns, so `contacts_search`, `contacts_get`,
  `contacts_groups` and `find_person` reflect it immediately.
- **Limits:** one contact per call; at most 10 group changes per call; field length and count
  caps; the token's existing hourly write limit (`max_writes_per_hour`, one budget shared with
  mail and calendar writes); iCloud's throttling back-off applies to writes, and a throttled
  write is never retried.
- **Audit:** every write is audit-logged with `write: true` and no contact content: names,
  addresses, numbers, notes, labels, group names and ids are all redacted.
- Docs: README.md, docs/design.md (Goals, Tools table, the in-memory contacts cache section,
  a "Contact writes" section), CONTRIBUTING.md, SECURITY.md and
  `config/pv-icloud.example.yaml` are updated with the implementation. CLAUDE.md,
  `.github/CODEOWNERS`, `openspec/config.yaml` and ADR-0006 already carry the rules and are
  not changed. ADR-0003, which #25 lists, does not call contacts read-only and needs no
  amendment.

Out of scope (from #25 and ADR-0006): creating, renaming or deleting groups; merging
duplicate contacts; editing photos; deleting the me-card; contacts in excluded groups; batch
writes. Also not in this change: switching a card between showing as a person and as a
company, and cleaning up group membership entries of deleted contacts (Apple's own clients
leave them too; see design, resolved questions).

## Capabilities

### New Capabilities

- `contacts-write-tools`: `contacts_create`, `contacts_update` and `contacts_delete`; who may
  use them; their arguments, results and limits; lossless updates; group membership;
  the me-card, group cards and excluded groups; conditional requests after a fresh read;
  refreshing the address book after a write; and the shared write rate limit.

### Modified Capabilities

- `read-only-and-egress`: "No tool changes iCloud data" allows the contact write tools to
  change contacts; "Read-only DAV methods" now points at both write method lists; "Contacts
  stay read-only" is replaced by "Contact write DAV methods", which lists exactly what may be
  sent; "Calendar write DAV methods" no longer claims to be the only writer.
- `accounts-and-auth`: the `PV_ICLOUD_CONTACTS_WRITE` switch; the per-token `contacts_write`
  key and its validation; `max_writes_per_hour` also covers contact writes; permissions reach
  every call.
- `tool-conventions`: the tool set adds contact write tools for granted tokens; their
  annotations; instructions for a token with contact writes; audit lines and redaction for
  contact writes.
- `contacts-and-people`: the in-memory address book is refreshed right after a contact write.

## Impact

- Code: `src/pv_icloud/config.py`, `auth.py`, `server.py`, `contacts/service.py` (write
  orchestration and cache refresh), `contacts/carddav.py` (read-only addition: one or more
  cards with their ETags in one `addressbook-multiget`), new `contacts/carddav_write.py` (the
  only module that sends contact `PUT` or `DELETE`) and new `contacts/compose.py` (builds new
  vCards and edits stored vCard text in place). `dav.py`, `cal/`, `mail/` and `people.py` are
  not changed.
- Tests: `tests/fakecarddav.py` gains `PUT`, `DELETE`, ETags per card, `If-Match` and
  `If-None-Match` with HTTP 412, a hook to change a card between read and write, sync tokens
  that see writes, and a record of forbidden write attempts; `tests/test_repo_policy.py`
  allows `PUT`/`DELETE` in `contacts/carddav_write.py` as well as `cal/caldav_write.py` and
  nowhere else; new `tests/test_contacts_write.py` and `tests/test_contacts_compose.py`.
- Config: `config/pv-icloud.example.yaml` documents the switch and `contacts_write` (off).
- Dependencies: none new.
- Egress: unchanged hosts. CardDAV requests may now be `PUT` and `DELETE` to the account's own
  CardDAV host.
- Security posture: a token with contact writes can now create, change and delete the
  account's contacts, and every change syncs to all the user's devices. iCloud keeps no
  per-contact trash, so a delete is permanent from pv-icloud's side. Prompt injection from
  untrusted mail, event or contact text can now cause such changes; see `design.md` for
  mitigations.
- Compatibility: no breaking change. Existing configs keep working and behave exactly as
  today.
