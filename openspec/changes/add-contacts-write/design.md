# Design

## Context

See proposal.md for motivation, and ADR-0006 for the rules this change implements; CLAUDE.md
carries them as hard rules. Contacts are read-only by construction today: `dav.py`'s
`DavClient._request` refuses every method but `PROPFIND` and `REPORT`; `contacts/carddav.py`
discovers the home, reads the `cs:me-card`, and syncs the one address book (`card/`) with
`sync-collection` plus `addressbook-multiget`; `contacts/service.py` keeps the parsed cards in
memory, rebuilds a `Snapshot` (visible contacts, groups, excluded-group members removed) and
refreshes at most every 60 seconds; `contacts/vcard.py` parses vCard 3.0, dropping what it
does not show (photos, Apple-internal properties). A contact id is the card's file name
without `.vcf` and is never used to build a URL; group cards are vCards in the same book with
`X-ADDRESSBOOKSERVER-KIND:group` and one `X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<UID>` line per
member.

What ADR-0006's live checks found (2026-10-14, read-only): one address book under the home
with `bind` and `write`; `PUT` and `DELETE` allowed; every card has an ETag; groups are
vCards in the same book. Contact writes email nobody. The risks are data loss and silent
damage: the reader drops what it does not show, iCloud keeps no per-contact trash, and every
change syncs to all the user's devices.

Mail and calendar writes set the pattern: a deployment switch, a per-token grant, write code
confined to one module and kept out of the process when the switch is off, a per-token
`tools/list` and instructions, a per-token hourly write limit (`ratelimit.py`), conditional
requests after a fresh read, and audit lines with `write: true` and no content.

Modules touched: `config`, `auth`, `server`, `contacts/service`, `contacts/carddav`
(read-only addition), new `contacts/carddav_write` and new `contacts/compose`. `dav`,
`contacts/vcard` (reused as is for reading), `cal/`, `mail/`, `people` and `ratelimit`
(reused as is) are not changed. No new limit needs paging: every write tool returns a single
result.

## Goals / Non-Goals

**Goals:**

- A token without `contacts_write` gets byte-for-byte today's `tools/list`, instructions and
  CardDAV request stream.
- Contact writes cannot be turned on by accident: deployment switch, then a per-action grant;
  delete is its own grant.
- `PUT` and `DELETE` for contacts exist in exactly one module, aimed only at one `.vcf`
  resource directly inside the account's address book, and always conditional.
- An update never changes a line the call did not ask to change; anything ambiguous is
  refused.
- A write never overwrites a change made elsewhere since the card was read.
- A change is visible to every read tool as soon as the write call returns.

**Non-Goals:**

- Creating, renaming or deleting groups; photos; merging contacts (ADR-0006).
- Deleting the me-card or any group card.
- Contacts in excluded groups, in any way.
- Batch tools: one contact per call limits what a prompt-injected client can do per call.
- Rebuilding a card from the parsed model, ever.

## Decisions

### 1. Deployment switch and per-token, per-action grant

`PV_ICLOUD_CONTACTS_WRITE` is read at startup like the mail and calendar switches: `enabled`
turns contact writes on; unset, empty or `disabled` leave them off; anything else is a config
error naming the variable. The three switches are independent.

```yaml
tokens:
  - { label: laptop, sha256: "<hex>", contacts_write: [create, update, delete] }
  - { label: phone,  sha256: "<hex>" }                       # read-only, as today
  - { label: agent,  sha256: "<hex>", contacts_write: [update], calendar_write: true,
      max_writes_per_hour: 10 }
```

`contacts_write` is a list of actions from `create`, `update` and `delete` (default empty).
`TokenEntry` gains `contacts_write: frozenset[str]`, and `AccountContext` carries it. A value
that is not a list or an unknown action is a config error naming account, token
label and key, and listing the allowed actions. A non-empty `contacts_write` while the switch
is off is a config error naming account, token label, `contacts_write` and
`PV_ICLOUD_CONTACTS_WRITE`. A non-empty `contacts_write` on an account with
`contacts.enabled: false` is a config error naming account, token label, `contacts_write` and
`contacts.enabled`, since those tools could never work. While the switch is off, `server.py`
never imports `contacts/carddav_write.py` or `contacts/compose.py` and registers no contact
write tool.

*Why per action, unlike `calendar_write: true`:* #25 asks for one switch, but ADR-0006
(decision 2) and CLAUDE.md require separate actions because delete is permanent and iCloud has
no per-contact trash. A token that should have everything lists all three.

No per-account contact write settings are added (no "protected groups"): excluded groups
already hide contacts completely, and a token that must not delete simply lacks `delete`.

### 2. Tools

- `contacts_create(given_name?, family_name?, middle_name?, prefix?, suffix?, nickname?,
  maiden_name?, organization?, department?, title?, emails?, phones?, addresses?, urls?,
  social_profiles?, instant_messaging?, birthday?, dates?, related_names?, note?, groups?)`
- `contacts_update(id, <any scalar field above>?, clear?, add?, remove?, add_groups?,
  remove_groups?)`
- `contacts_delete(id)`

Field names and value shapes are those of `contacts_get`, so a caller can copy what it read:

| Field | Shape (input) |
| --- | --- |
| `given_name`, `family_name`, `middle_name`, `prefix`, `suffix`, `nickname`, `maiden_name`, `organization`, `department`, `title` | string |
| `birthday` | `YYYY-MM-DD` or `--MM-DD` (no year) |
| `note` | string |
| `emails`, `phones` | `{label?, value, preferred?}` |
| `addresses` | `{label?, street?, city?, region?, postal_code?, country?, po_box?, extended?}` |
| `urls`, `related_names` | `{label?, value}` |
| `dates` | `{label?, value}` with `value` as for `birthday` |
| `social_profiles` | `{service, user?, url?}` (at least one of `user`, `url`) |
| `instant_messaging` | `{service, handle}` |

`name` (the display name), `formatted` addresses, `groups` and `is_me` are derived and not
accepted as input. Create needs at least one of a name part, `organization`, an email or a
phone.

On update:

- A scalar field given replaces the stored value; `clear` lists scalar fields to remove
  (`null` and omitted cannot be told apart reliably across MCP clients). A field both given
  and cleared is a tool error.
- `add` is an object with the list fields above (`emails`, `phones`, …), each a list of new
  entries appended to the card.
- `remove` is an object with the same keys, each a list of entries **as `contacts_get`
  returned them**. An entry matches a stored value when every field `contacts_get` shows for
  it is equal (`preferred` and `formatted` are ignored). No match → `not found on this contact:
  <field>`; more than one match → refused as ambiguous. Changing a value is `remove` plus
  `add` in one call.
- `add_groups` and `remove_groups` are lists of group names (case-insensitive, as
  `contacts_search`'s `group`). Adding to a group the contact is already in, or removing it
  from one it is not in, is a no-op for that group.

A call that changes nothing is a tool error (`nothing to change`), and counts against the
write limit like any call.

Results: create and update return `id`, `name`, `groups` and `contacts_as_of`, in
`contacts_get`'s formats; the `id` works with `contacts_get` at once. Delete returns
`deleted: true` and `id`.

### 3. Labels written so they read back the same

`contacts_get` derives labels from Apple item-group labels (`X-ABLabel`) or `TYPE`s. New
values are written exactly as Apple Contacts writes them, so that reading a new value back
gives the label that was given and Apple's apps see the same card they would have written.
Each type is its own `TYPE=` parameter, in the order below, never a comma list
(`TEL;TYPE=CELL;TYPE=VOICE`, not `TEL;TYPE=CELL,VOICE` or `TEL;TYPE=CELL`). These are the
parameter orders of cards Apple's apps write, checked by the maintainer on the live address
book (2026-10-14):

| Label given (case-insensitive) | Written as |
| --- | --- |
| `mobile` (phones) | `TEL;TYPE=CELL;TYPE=VOICE` |
| `iPhone` (phones) | `TEL;TYPE=IPHONE;TYPE=CELL;TYPE=VOICE` |
| `home`, `work` (phones) | `TEL;TYPE=HOME;TYPE=VOICE` / `TEL;TYPE=WORK;TYPE=VOICE` |
| `main` (phones) | `TEL;TYPE=MAIN` |
| `home fax`, `work fax` (phones) | `TEL;TYPE=HOME;TYPE=FAX` / `TEL;TYPE=WORK;TYPE=FAX` |
| `pager` (phones) | `TEL;TYPE=PAGER` |
| `home`, `work` (emails) | `EMAIL;TYPE=INTERNET;TYPE=HOME` / `EMAIL;TYPE=INTERNET;TYPE=WORK` |
| `home`, `work` (addresses, URLs) | `ADR;TYPE=HOME`, `URL;TYPE=WORK`, … |
| Apple's other built-in labels (`other`, `school`, `homepage`, `anniversary`, `mother`, `father`, `parent`, `brother`, `sister`, `child`, `friend`, `spouse`, `partner`, `assistant`, `manager`) | item group with `X-ABLabel:_$!<Other>!$_` style |
| any other label | item group with `X-ABLabel:<label>` |
| none | no label |

`TYPE=VOICE` is written only in the `mobile`, `iPhone`, `home` and `work` phone rows. An
unlabelled phone is plain `TEL`, and a phone in an item group (any `X-ABLabel`) is plain
`itemN.TEL`, both without `TYPE=VOICE`. Every email gets `TYPE=INTERNET` first: an unlabelled
one is `EMAIL;TYPE=INTERNET`, and one in an item group is `itemN.EMAIL;TYPE=INTERNET`, with no
`HOME` or `WORK`. `preferred: true` adds `TYPE=PREF` as the last parameter, on phones and
emails alike (`TEL;TYPE=CELL;TYPE=VOICE;TYPE=PREF`, `TEL;TYPE=PREF`, `itemN.TEL;TYPE=PREF`,
`EMAIL;TYPE=INTERNET;TYPE=HOME;TYPE=PREF`, `itemN.EMAIL;TYPE=INTERNET;TYPE=PREF`). Parameter
names and type values are written in upper case. The fake CardDAV server's fixture cards use these exact forms, and a test pins
both the written bytes of every row and the label `contacts_get` reads back. Item group names
are `item<N>` with `N` one more than the highest in the card. Dates are `X-ABDATE` and related
names `X-ABRELATEDNAMES` in item groups; a birthday without a year is written as Apple does,
`BDAY;X-APPLE-OMIT-YEAR=1604:1604-MM-DD`. Social profiles are `X-SOCIALPROFILE;TYPE=<service>`
with `X-USER=`, instant messaging `IMPP;X-SERVICE-TYPE=<service>:<scheme>:<handle>`. A test
pins, for every row, that `contacts_get` returns the label that was given.

### 4. Lossless in-place editing (`contacts/compose.py`)

The stored text is split into logical lines, each keeping its original physical bytes
(folding and line endings included). An update:

1. finds the logical lines each requested change touches;
2. rewrites only those lines (and, for a removed value, the other lines of its item group);
3. emits every other line exactly as stored, in the stored order; new lines go just before
   `END:VCARD`;
4. folds rewritten and new lines at 75 octets with CRLF and escapes values per vCard 3.0.

Per property:

- **Structured properties** (`N`: family;given;middle;prefix;suffix; `ORG`: organization;
  department): only the component asked for is replaced; the line's parameters and the other
  components are kept as stored (escaped as stored). Clearing the last non-empty component of
  `ORG` removes the line.
- **Display name (`FN`)** follows the name only when it was derived from it. The live
  address book has `FN` equal to "prefix given middle family suffix" on 1,526 of 1,544
  person cards; the other 18 carry a custom display name, which must survive. So when a name
  part changes, `FN` is rewritten from the new prefix, given, middle, family and suffix only
  if the card has exactly one `FN` line and its unescaped value equals what the *old* name
  parts give (non-empty parts joined by single spaces, compared exactly). For a card that
  shows as a company (`X-ABShowAs:COMPANY`), the same rule applies to `organization`: `FN` is
  rewritten to the new organization only if it equalled the old one, and name-part changes
  never touch it. In every other case `FN` is left as stored.
- **Revision (`REV`)** is bumped, as Apple's apps do (1,561 of 1,580 live cards carry `REV`,
  and other sync clients use it): every card an update writes, contact card or group card,
  gets its `REV` line replaced by the current UTC time as `REV:YYYY-MM-DDTHH:MM:SSZ`, or one
  added before `END:VCARD` when it has none. A card with more than one `REV` line is refused.
  `FN` and `REV` are the only properties written that the call did not name, and the tool
  description says so.
- **Single-valued properties** (`NICKNAME`, `X-MAIDENNAME`, `TITLE`, `NOTE`, `BDAY`): the one
  line is replaced or removed, or added when missing.
- **List values:** a removed value's line is deleted together with the other lines of its
  item group (`X-ABLabel`, `X-ABADR`, …) when the group holds nothing else; added values are
  new lines (decision 3).
- **Unknown, photo, `X-APPLE-…`, `PRODID` and every other line** are never touched.

The change is **refused** (`cannot change this contact safely: <field>`) with nothing written
when: a single-valued property the call changes appears more than once; a value to remove
shares its item group with another property that is not label metadata; the card's
`VERSION` is not `3.0`; or the stored text cannot be split into lines unambiguously (for
example, a property line without a colon). The parsed `Card` model is used only to find
values and check matches, never to write.

Tests pin a round trip on real-shaped cards (photo, item groups, Apple labels, folded
parameters, unknown `X-` properties, a custom display name): only the lines a change names,
`REV`, and `FN` when the rule above rewrites it, differ, byte for byte.

New cards are vCard 3.0 with `PRODID:-//pv-icloud//EN`, `N`, `FN`, `UID`, `REV`, the given
fields in a fixed order, and `X-ABShowAs:COMPANY` when the card has an organization and no
name part (so iCloud and the reader show the company, as Apple Contacts does). The file name
and `UID` are one new random UUID (`<UUID>.vcf`).

### 5. Group membership

Membership is stored in the group card, so `add_groups`, `remove_groups` and create's `groups`
edit group cards, losslessly, with the same rules: adding appends one
`X-ADDRESSBOOKSERVER-MEMBER:urn:uuid:<contact UID>` line before `END:VCARD`; removing deletes
exactly the lines whose value is that member; `REV` is bumped (decision 4). Every other line
of the group card, including members hidden from this token, is kept byte for byte. A group card is never renamed or
deleted, and its other properties are never changed.

A group named in `groups`, `add_groups` or `remove_groups` must be a visible group of the
account: a missing or excluded group fails with `unknown group: <name>` (the same error for
both) before anything is written. At most 10 group names per call in total.

Order of writes in one call:

1. One `addressbook-multiget` reads the contact card (update) and every group card the call
   changes, with their ETags.
2. Everything is validated against what was read: the contact is still visible, the groups
   still exist and are not excluded, every change applies unambiguously. Any failure ends the
   call with nothing written.
3. The contact card is written (`PUT`, or none if only groups change).
4. Each group card is written, in name order, with `If-Match` from step 1.

Writes 3 and 4 are separate requests, so a call can stop part way (a 412 on a group card
because it was changed in Contacts meanwhile, throttling, a network error). The call then
fails with a tool error that says what was saved and what was not, for example `the contact
was saved and added to 1 of 2 groups; group <name> changed in Contacts; read the contact
again`. Nothing is rolled back and nothing is retried. Group names in that message come from
the caller's own arguments and are visible groups, so nothing about excluded groups leaks.

### 6. Write path confined to `contacts/carddav_write.py`

`dav.py` is not changed. The new module holds a `CardDavWriter` built on the account's
`CardDavClient` (same httpx client, credentials, `Throttle` and discovered home). Its single
`_write` method:

- allows only `PUT` and `DELETE`;
- requires `If-None-Match: *` (create) or `If-Match: <etag>` (update, delete), and refuses to
  send without one;
- targets only `<address book>/<name>.vcf`, where the address book is the one listed under
  the account's home (re-checked from `home_info`, never derived from the id), and `<name>`
  is a single path segment: for an existing card, the href it was read from; for a new card,
  `<UUID>.vcf`;
- sends `Content-Type: text/vcard; charset=utf-8`;
- checks the account's `Throttle` first and pauses it on 429/503, exactly as reads do; a
  write is never retried automatically;
- logs failures with account, tool and HTTP status only, never the path, UID or body.

`contacts/carddav.py` gains one read-only method: an `addressbook-multiget` `REPORT` for a
few hrefs returning, for each, the stored text and its ETag (`getetag` is already asked for;
it is now kept). Ids are resolved to hrefs only through the in-memory snapshot (a card that
is not a visible individual contact is `not found`), never by building a URL from the id.

A static test in `tests/test_repo_policy.py` checks that `"PUT"` and `"DELETE"` appear as
HTTP methods only in `cal/caldav_write.py` and `contacts/carddav_write.py`, that
`dav.READ_METHODS` is still exactly `PROPFIND` and `REPORT`, that no source names `MKCOL`,
`PROPPATCH`, `MOVE`, `POST`, `COPY`, `ACL`, `LOCK` or `MKCALENDAR` as a DAV method, that
`contacts/` writes no `PHOTO` line, and that `contacts/carddav_write.py` is listed in
CODEOWNERS (it already is). The fake CardDAV server rejects unconditional `PUT` and `DELETE`
and records any write to the collection, a non-`.vcf` path or a path outside the address
book, so the tests also fail at run time.

### 7. Fresh read before every update and delete; the me-card and group cards

Update and delete run, for one id:

1. Look the id up in the current snapshot. Not a visible individual contact (unknown,
   stale, a group card, a member of an excluded group) → `contact not found (deleted, hidden,
   or a stale id)`, the same error `contacts_get` gives.
2. Re-read the card (and, for update, the group cards it changes) with its ETag
   (decision 5, step 1). Gone → `not found`. Re-check from the fresh text that it is still an
   individual card and is not in an excluded group (its membership is read from the group
   cards in the snapshot; a group card read in step 1 wins).
3. Delete only: refuse the me-card (`this is the account owner's own card; it cannot be
   deleted`).
4. Apply the change (`contacts/compose.py`) and write with `If-Match` set to the ETag just
   read, or `DELETE` with `If-Match`.

The ETag is never cached across calls.

Errors:

- HTTP 412: `the contact changed in Contacts; read it again`. Not retried.
- HTTP 404: `not found`.
- HTTP 403 or any other 4xx: `iCloud refused the change`, with nothing else.
- A timeout or broken connection after the request was sent: `iCloud did not confirm the
  change; read the contact again before retrying`.

Deleting a contact does not edit group cards: a member entry for a deleted UID is ignored by
the reader and is not counted by `contacts_groups`. Apple's own clients leave such entries
too: 23 of the 4,515 member entries in the live address book already point to no card.
Task 10.1 records whether iCloud cleans them up itself; either way pv-icloud leaves them.

### 8. Refreshing the address book after a write

The issue requires changes to show in `contacts_search` and `find_person` right away, and
ADR-0006 says the cache refreshes with `sync-collection`. After the last write of a call that
wrote anything (including a call that stopped part way), the service runs a refresh at once,
ignoring the 60-second interval, under the same lock as other refreshes. If that refresh
fails, the service applies what it wrote to the in-memory copy directly (the new card text
for a `PUT`, removal for a `DELETE`), marks the copy stale as today, and the call still
reports success: the write itself was confirmed by iCloud.

### 9. Field limits

- Name parts, `nickname`, `maiden_name`, `organization`, `department`, `title`: at most 255
  characters, no line breaks.
- `note`: at most 20,000 characters (the size `contacts_get` returns before truncating).
- Each list value (`value`, address components, `user`, `url`, `handle`): at most 1000
  characters, no line breaks; labels and services at most 100 characters, no line breaks.
- Emails must contain one `@`; URLs and social profile URLs must be `http` or `https`;
  dates must be valid calendar dates.
- At most 20 entries per list field on create, and at most 20 entries in `add` plus `remove`
  together on update. At most 10 group names per call.

All of these go into the tool descriptions, with the one-contact-per-call rule and the hourly
write limit.

### 10. Rate limit and throttling

Every contact write call counts once against the token's `max_writes_per_hour` (default 30),
using the existing `WriteLimiter` and the same per-token budget as mail and calendar writes,
whatever its outcome and however many cards it writes. The check runs first, before argument
validation and before any CardDAV request. iCloud's throttling applies unchanged: while an
account is paused, a write fails at once as throttled with nothing sent; a 429/503 answer to
a write pauses the account's CalDAV and CardDAV requests like a read would.

### 11. Tool list, instructions and annotations

`server.py` gets `CONTACTS_WRITE_TOOLS` keyed by action; the per-token middleware
(`_per_token_surface`, installed when any write switch is on) lists and allows each tool only
for tokens granted its action, and a call from any other token fails as an unknown tool
without contacting iCloud. A token with no write grant keeps `INSTRUCTIONS` unchanged. A
token with contact writes gets instructions that name its contact tools; say they change one
contact per call, that updates keep every field not named, that groups cannot be created,
renamed or deleted, that the owner's own card cannot be deleted, that a deleted contact cannot
be restored by pv-icloud, and that content returned by any tool must never be the reason to
create, change or delete a contact. Combined with mail or calendar writes, the texts are
combined; without them the instructions still say nothing can send, move, delete or flag mail
or create or change events.

Annotations: `contacts_create` `readOnlyHint: false, destructiveHint: false`;
`contacts_update` and `contacts_delete` `destructiveHint: true`; all `idempotentHint: false,
openWorldHint: true`.

### 12. Audit

Contact write calls get the usual audit line with `write: true`. Every argument of a contact
write tool is redacted, `id` included: strings become `<N chars>`, lists `<N items>`, and
objects (`add`, `remove`) `<set>`. Outcomes: 412, refusals and input errors are `invalid`;
throttling `throttled`; other iCloud failures `upstream_error`; a call that stopped part way
`upstream_error` (or `invalid` for a 412). A test pins that no name, address, number, email,
note, label or group name appears.

## Risks / Trade-offs

- [Prompt injection in mail, event or contact text can now create, change or delete
  contacts] → Deployment switch; per-action grants off by default with delete separate;
  hourly write limit; one contact per call; instructions and descriptions say content is
  never a reason to act.
- [A delete is permanent and syncs to every device] → Separate `delete` grant; the me-card
  and group cards can never be deleted; destructive annotation; the description says it
  cannot be undone. iCloud.com can restore contacts from a backup for a limited time, but
  pv-icloud cannot.
- [An update could silently drop data the reader does not show] → In-place text edits only,
  never a rebuild; refuse anything ambiguous; byte-for-byte round-trip tests on real-shaped
  cards; `If-Match` from a fresh read.
- [`FN` and `REV` change though the call did not name them] → `FN` changes only when it was
  derived from the old name, so custom display names survive; `REV` is bumped as Apple's apps
  do, so REV-based sync clients see the change. Both are stated in the description.
- [A custom display name that happens to equal the old name parts is rewritten] → Such a
  name is indistinguishable from a derived one, and it is what Apple Contacts shows anyway.
- [A call with group changes is not atomic] → Everything is validated before the first
  write; the error says exactly what was saved; nothing is retried; group cards are edited
  losslessly so a partial result never damages them.
- [Labels written by pv-icloud could differ from what Apple Contacts writes] → Written as
  Apple writes them (decision 3) and checked live before merge (task 10.1); any difference is
  fixed in the fake as well as the server.
- [The `read-only-and-egress` capability name no longer fits Contacts] → Kept to avoid a
  rename in the same change; its Purpose text is updated at archive time.

## Migration Plan

None: the new switch and key default to off or empty, so existing configs behave exactly as
before. Operators opt in with `PV_ICLOUD_CONTACTS_WRITE=enabled` and `contacts_write` on a
token. Rollback is unsetting the switch, removing the key and restarting.

## Resolved Questions

Answered by the maintainer on #25 (2026-10-14), with a read-only check of a live address book
(counts and property shapes only):

1. **Per-action grants** instead of #25's single switch: confirmed; ADR-0006 wins
   (decision 1).
2. **Member entries of deleted contacts are left** in group cards (decision 7); Apple's
   clients leave them too.
3. **`FN` is rewritten only when it was derived from the old name** (or, for company cards,
   the old organization); custom display names are kept (decision 4).
4. **`REV` is bumped** on every card an update writes (decision 4).
5. **A grant on an account with `contacts.enabled: false` is a config error** (decision 1).
6. **Labels are written in Apple's exact forms** (decision 3): separate `TYPE=` parameters,
   `TYPE=IPHONE` before `TYPE=CELL`, `TYPE=VOICE` only on phones with a built-in voice type,
   `TYPE=INTERNET` first on emails, `TYPE=PREF` always last; unlabelled and item-group
   phones without `TYPE=VOICE`, item-group emails without `HOME` or `WORK`. Pinned in the
   fake's fixture cards and the label tests.
7. **Group cards get a `REV` bump too**: every card an update writes, group cards included
   (30 of 36 live group cards carry `REV`).

## Open Questions

None. The maintainer live-tests on a test contact before merge (task 10.1).
