# Tasks

Implements #25 under ADR-0006. Do not start until the maintainer has approved the proposal.
Each group ends with its tests passing.

## 1. Policy tests

- [x] 1.1 Write failing tests in `tests/test_repo_policy.py`: `"PUT"` and `"DELETE"` are used
  as HTTP methods only in `src/pv_icloud/cal/caldav_write.py` and
  `src/pv_icloud/contacts/carddav_write.py`; `dav.READ_METHODS` is still exactly `PROPFIND`
  and `REPORT`; no source uses `MKCOL`, `PROPPATCH`, `MOVE`, `POST`, `COPY`, `ACL`, `LOCK` or
  `MKCALENDAR` as a DAV method; no source under `src/pv_icloud/contacts/` writes a `PHOTO`
  line; `contacts/carddav_write.py` does not import from `cal/` and `cal/caldav_write.py` does
  not import from `contacts/`; CODEOWNERS lists `/src/pv_icloud/contacts/carddav_write.py`.
  Verify they fail only for the missing module.

## 2. Deployment switch, configuration and auth

- [x] 2.1 Write tests in `tests/test_units.py` for `PV_ICLOUD_CONTACTS_WRITE`: unset, empty
  and `disabled` are off; `enabled` is on; other values are a config error naming the
  variable; a non-empty `contacts_write` while off is a config error naming account, label,
  `contacts_write` and the variable; a grant on an account with `contacts.enabled: false` is
  a config error naming account, label, `contacts_write` and `contacts.enabled`; the mail,
  calendar and contacts switches are independent.
- [x] 2.2 Write tests for the token key `contacts_write`: default empty; `[create]`,
  `[update, delete]` and all three load; repeated actions load once; a non-list (`true`,
  `"update"`) or an unknown action (`merge`) is rejected naming account, label, key and, for
  an unknown action, the allowed actions; `max_writes_per_hour` still defaults to 30.
- [x] 2.3 Implement in `src/pv_icloud/config.py` (`TokenEntry.contacts_write`,
  `Config.contacts_write`, the switch) and verify 2.1–2.2 pass.
- [x] 2.4 Write a test that two tokens of one account resolve to `AccountContext`s with their
  own `contacts_write`; extend `src/pv_icloud/auth.py` and verify it passes.
- [x] 2.5 Document the switch and `contacts_write` (off) in `config/pv-icloud.example.yaml`
  and verify the example still loads in the config tests.

## 3. Fake CardDAV server

- [x] 3.1 Extend `tests/fakecarddav.py`: an ETag per card that changes on every change;
  `getetag` in `addressbook-multiget` answers; `sync-collection` that reports cards written
  through `PUT` and removed through `DELETE`; `PUT` with `If-None-Match: *` (412 when the name
  exists) or `If-Match` (412 on mismatch) and `DELETE` with `If-Match`, rejecting either
  without a precondition; storing `PUT` bodies byte for byte; a hook to change a card between
  a read and a write; 503 with `Retry-After` on demand for writes; real-shaped fixture cards
  (photo, item groups with Apple and custom `X-ABLabel`s, folded parameters,
  `X-APPLE-OMIT-YEAR`, `X-ABShowAs:COMPANY`, unknown `X-` properties, a custom display name
  that differs from the name parts, a card with `REV` and one without, a card with two `NOTE`
  lines, a card with two `REV` lines, a vCard 4.0 card), with phones and emails in Apple's
  exact label forms from design decision 3 (separate `TYPE=` parameters, `TYPE=IPHONE`
  before `TYPE=CELL`, `TYPE=VOICE` only on built-in voice types, `TYPE=INTERNET` first on
  emails, `TYPE=PREF` last), and group cards with members hidden by an excluded group. Writes to
  the address book collection, the home, a non-`.vcf` or nested path, any CalDAV URL, a
  `DELETE` of a group card or the me-card, and any other method are recorded as forbidden
  write attempts. Verify the existing contacts and `find_person` tests still pass.

## 4. Read-side addition

- [x] 4.1 Write tests for the new read in `contacts/carddav.py`: one `addressbook-multiget`
  for a few hrefs returns each card's stored text and ETag; a missing href is reported as
  missing; only `REPORT` is sent.
- [x] 4.2 Implement it in `src/pv_icloud/contacts/carddav.py`; verify 4.1 passes and the read
  tools' results are unchanged.

## 5. vCard composition and lossless editing

- [ ] 5.1 Write tests in a new `tests/test_contacts_compose.py` for
  `src/pv_icloud/contacts/compose.py`: new cards (vCard 3.0, `PRODID`, `N`, `FN`, `UID`,
  `REV`, `X-ABShowAs:COMPANY` for an organization-only card, CRLF, folding at 75 octets,
  escaping of `,`, `;`, `\` and line breaks); for every row of the label table in design
  decision 3, the written line's bytes are exactly the form in the table (separate `TYPE=`
  parameters in that order, `TYPE=VOICE` and `TYPE=INTERNET` where listed, `TYPE=PREF` for
  preferred) and `parse_vcard` of it returns the given label; birthdays and dates with and
  without a year; in-place edits of each scalar field (only that line and `REV` differ byte
  for byte); `FN` rewritten on a name-part change only when it equalled the old name parts,
  and kept byte for byte when it is a custom display name or there is more than one `FN`
  line; a company card's `FN` following `organization` only when it equalled the old one,
  and untouched by name-part changes; `REV` replaced with the current UTC time
  (`YYYY-MM-DDTHH:MM:SSZ`, using an injected clock), added when missing, and the change
  refused for two `REV` lines; group card edits bump `REV` too; `N` and `ORG`
  component edits keeping parameters and other components as stored; `clear`; `add` and
  `remove` of every list field, removing a value's whole item group and nothing else; new
  item group numbers; `remove` matching on every shown field, refused with the field named
  when nothing matches, and refused as ambiguous for two matches; refusals with `cannot change
  this contact safely` for duplicated single-valued properties, a shared item group, vCard
  4.0 and unsplittable text; group card member lines added and removed with every other line
  (hidden members included) unchanged; no `PHOTO` ever written; every input limit in the
  `contacts-write-tools` spec refused with the field named.
- [ ] 5.2 Implement `src/pv_icloud/contacts/compose.py` (SPDX header); verify 5.1 passes.

## 6. Write path

- [ ] 6.1 Write tests for `src/pv_icloud/contacts/carddav_write.py` against the fake: only
  `PUT` and `DELETE`; refuses to send without `If-Match` or `If-None-Match: *`; refuses any
  URL that is not a direct `.vcf` child of the account's address book as listed under its
  home; sends `Content-Type: text/vcard; charset=utf-8`; 412 mapped to `the contact changed
  in Contacts; read it again` and not retried; 404 to `not found`; 403 and other 4xx to
  `iCloud refused the change`; 429/503 pauses the account's shared `Throttle` and is not
  retried; a paused account sends nothing; a timeout after sending reports an unconfirmed
  change; logs carry no path, UID or body.
- [ ] 6.2 Implement `src/pv_icloud/contacts/carddav_write.py` (SPDX header); verify 6.1 and
  1.1 pass.

## 7. Create, update and delete in the contacts service

- [ ] 7.1 Write tests in a new `tests/test_contacts_write.py`: create with every field and
  labels, readable at once by `contacts_get`, `contacts_search` and `find_person`; create in a
  group; update of one field keeps every other line byte for byte (photo included); fixing a
  phone with `remove` plus `add`; name change rewrites `N`, `FN` and `REV` only, and keeps a
  custom display name; group add and remove edit only the group cards' member and `REV`
  lines and do not write the contact card; delete removes the contact from every read tool
  and `contacts_groups` counts, writes no group card, and leaves its member entries in place; every update and delete re-reads the cards and
  sends those ETags (a stale ETag from an earlier call is never used); 412 on the contact
  leaves the other version and sends one `PUT` only; 412 on the second of two group cards
  reports what was saved, retries nothing, and the cache reflects what was written; contact
  gone (`not found`, no write); unknown, stale, group-card, other-account and
  excluded-group-member ids all fail with the same `not found`; missing and excluded groups
  fail with the same `unknown group` before any write; the me-card can be updated but not
  deleted; nothing to change is refused; the cache refreshes right after a write even within
  the 60-second interval, and when that refresh fails the written change is applied to the
  in-memory copy and marked stale; no forbidden write attempt is recorded in any test.
- [ ] 7.2 Implement create, update and delete in `src/pv_icloud/contacts/service.py`; verify
  7.1 passes.

## 8. Tools, permissions and audit

- [ ] 8.1 Write tests in `tests/test_server.py`: with the switch off,
  `contacts.carddav_write` and `contacts.compose` are not imported and no contact write tool
  is registered; a token without `contacts_write` gets the same `tools/list`, annotations and
  instructions as before (snapshot); a token sees exactly the contact tools of its granted
  actions with the annotations in the spec; a call to an ungranted contact tool fails as
  unknown with no CardDAV request; mail, calendar and contact grants do not imply each other;
  the shared hourly write limit counts mail, calendar and contact calls together, counts a
  contact call with group changes once, and refuses with `outcome` `throttled` before any
  request; instructions for contacts-only, contacts-plus-calendar and contacts-plus-mail
  tokens match the spec; descriptions state the limits, opt-in, one contact per call, that
  fields not named are kept and `FN` follows the name, that groups cannot be created,
  renamed or deleted, the me-card rule, that delete cannot be undone, and the
  untrusted-content warning, and that `REV` is updated and a custom display name is kept;
  audit lines have `write: true` and no id, name, number,
  address, email, note, label or group name, including for refused and part-way calls.
- [ ] 8.2 Implement the three tools, conditional registration behind the switch, per-token
  `tools/list` filtering and call-time checks by action, instructions, and contact-write
  audit redaction in `src/pv_icloud/server.py`; verify 8.1 passes.

## 9. Docs and checks

- [ ] 9.1 Update README.md (switch, tools, `contacts_write` actions, lossless updates, groups,
  delete is permanent), docs/design.md (Goals, Tools table, the in-memory contacts cache
  section, a "Contact writes" section on lossless edits, group membership, fresh reads and
  conditional writes), CONTRIBUTING.md and SECURITY.md wherever they still say Contacts are
  read-only, and docs/egress.md if it describes CardDAV methods; add `Unreleased` entries to
  CHANGELOG.md for the switch, `contacts_write` and the three tools.
- [ ] 9.2 Run `uv run pytest`, `uv run ruff check .`, `uv run ruff format --check .`,
  `uv run mypy` and `openspec validate --all --strict`; all pass.

## 10. Live check (maintainer, before merge)

- [ ] 10.1 With a test token on a test contact: create a contact with labelled values (one per
  row of the label table, plus an unlabelled, a custom-labelled and a preferred phone) and
  confirm Apple Contacts shows the same labels and that a card Apple Contacts writes with
  the same labels has the same `TYPE` parameters (design decision 3); update one field
  on a card that has a photo and confirm the photo and every other field survive, the
  stored bytes differ only in the changed line and `REV` (line endings included), and `REV`
  is bumped; change a name part on a card with a custom display name and confirm it is kept;
  add the contact to and remove it from a test group; delete it and record whether iCloud
  leaves its member entry in the group card. Any difference from the fake is fixed in the
  fake as well as the server.
