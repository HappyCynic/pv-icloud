# ADR-0002: iCloud feasibility — probe results and path decisions

- **Status:** Accepted (Mail, Calendar; Reminders deferred)
- **Date:** 2026-10-08
- **Evidence:** a read-only probe script (stdlib) run three times against a real iCloud account from a laptop

## Results

| Area | Result | Detail |
|---|---|---|
| IMAP login (app-specific password) | ✅ | `imap.mail.me.com:993` |
| IMAP capabilities | ✅ | `CONDSTORE`, `QRESYNC`, `IDLE`, `ESEARCH`, `SORT`, `LIST-STATUS`, `UIDPLUS`; **no** `MULTISEARCH` |
| Folder layout | ~40 folders, small INBOX | Mail filed by topic → cross-folder search is the normal case |
| Single-folder TEXT search (30 d) | 0.19–0.28 s | |
| Cross-folder TEXT search (39 folders, 30 d) | **13.4 s and 20.1 s** (per-folder p50 0.28–0.34 s, max 1.40 s) | Fails the 3 s target even with 2 parallel connections |
| CalDAV discovery | ✅ | Principal → home on the account's `pNN-caldav.icloud.com` host |
| Calendars (VEVENT) | ✅ events returned | Server-side recurrence expansion (`<C:expand>`) works and keeps `TZID` (2026-10-09). Subscribed (webcal) calendars appear as collections but return **no events** over CalDAV (checked 2026-10-09); their `cs:source` URL is exposed, so the server fetches the feeds directly ([design](../design.md#calendar)) |
| Reminders (VTODO) | ❌ (effectively) | Only one VTODO collection: **`Reminders ⚠️`**, with a few items. That is Apple's legacy placeholder left behind after the upgrade to CloudKit Reminders, not the live lists |

## Decisions

1. **Mail over IMAP, with a local index for search.** Sync is incremental via `CONDSTORE`; `mail_get` and `mail_thread` read live. Cross-folder IMAP search is not viable.
2. **Calendar over CalDAV.** No Mac needed. *(Amended 2026-10-09: contacts over CardDAV were added the same way.)*
3. **Reminders and Apple Notes are out of scope.** CalDAV does not expose upgraded Reminders, and Notes has no supported protocol. Reading them would need a Mac running Apple Events or EventKit tooling, which conflicts with running on a small always-on server. Revisit if Apple exposes them over a supported protocol.

## Consequences
- `pv-icloud` ships Mail, Calendar and Contacts; no reminders or notes tools.
- Mail bodies are stored in the index on the server host → put it on an encrypted volume; the index honours the folder scope.
- `calendar_list`/`calendar_events` only return VEVENT collections, so the `Reminders ⚠️` VTODO placeholder is never surfaced.
