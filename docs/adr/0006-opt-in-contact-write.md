# ADR-0006: Opt-in contact write access

- **Status:** Accepted
- **Date:** 2026-10-14
- **Context:** Contacts have been read-only, cached in memory and refreshed with
  `sync-collection` (docs/design.md). Contact writes are expected next, after calendar writes
  (ADR-0005), and are decided ahead of their issue so both follow the same rules. Live checks on
  iCloud (2026-10-14, read-only): one address book (`card`) under the CardDAV home, with
  `bind` and `write`; `PUT` and `DELETE` are allowed; every one of 1,581 entries has an ETag;
  groups are vCards in the same address book (`X-ADDRESSBOOKSERVER-KIND:group`, members by
  `urn:uuid`). Unlike calendars, contact writes email nobody. The risks are data loss and
  silent damage: the reader drops what it does not show (photos, Apple-internal properties),
  iCloud keeps no per-contact trash, and every change syncs to all the user's devices.

## Decision

1. **Off unless the deployment turns it on.** Contact writes exist only when
   `PV_ICLOUD_CONTACTS_WRITE=enabled` (independent of the mail and calendar switches). Unset,
   empty or `disabled` leaves them off; any other value is a config error. While off, the
   contact write module is not imported and a token with a contact write grant is a config
   error.
2. **Granted per token, per action.** `contacts_write: [create, update, delete]`, each
   granted alone and all off by default, because delete is permanent. Tools a token was not
   granted are absent from its `tools/list` and refused as unknown.
3. **Three tools, one contact per call.** `contacts_create`, `contacts_update` (including
   adding the contact to or removing it from existing groups) and `contacts_delete`. Creating,
   renaming or deleting groups, merging contacts, photos and the me-card's deletion are out of
   scope.
4. **Updates are lossless.** An update edits the stored vCard text in place: it replaces only
   the properties the call changes and keeps every other line (photos, item groups with
   `X-ABLabel`, `X-APPLE-…` and unknown properties, their order) byte for byte. It never
   rebuilds a card from the parsed model. If a requested change cannot be applied to the stored
   text unambiguously, the call is refused and nothing is written.
5. **Delete is permanent and fenced.** `contacts_delete` needs its own `delete` grant, never
   deletes the account's me-card or a group card, and its description says it cannot be undone.
6. **Excluded groups stay invisible.** Contacts in `contacts.exclude_groups` cannot be read,
   so they cannot be written either (same `not found` error); no call may add a contact to an
   excluded group or create one inside it.
7. **Writes are confined to one module.** `PUT` and `DELETE` of one vCard in the account's
   address book are sent only from `contacts/carddav_write.py`; the DAV base keeps
   `PROPFIND` and `REPORT` only. Never `MKCOL` (or extended `MKCOL`), `PROPPATCH`, `MOVE`,
   `POST`, `COPY`, or a write to the address book collection itself.
8. **Never overwrite someone else's change.** Creating uses `If-None-Match: *` with a new UID
   as the file name. Every update or delete first re-reads that card and sends `If-Match` with
   its ETag; a 412 is reported as "the contact changed; read it again" and never retried
   automatically. After a write, the in-memory cache refreshes with `sync-collection`.
9. **One write budget, no content in logs.** Contact writes count against the token's
   `max_writes_per_hour` shared with mail and calendar writes; iCloud's throttle back-off
   applies. Audit lines get `write: true` and no names, addresses, numbers or notes.

## Consequences

- The project stays "read-only by default"; contacts are a third, separate opt-in.
- These are vulnerabilities: a contact write without the switch and the matching grant; any
  change to a property the call did not ask to change; any write to a contact in an excluded
  group; deleting the me-card or a group; any DAV method other than those listed here.
- Prompt injection could now edit or delete contacts. The defences match ADR-0004 and
  ADR-0005, with delete as a separate grant because it cannot be undone.
- Changing the allowed methods, managing groups, or editing photos needs a new ADR.
