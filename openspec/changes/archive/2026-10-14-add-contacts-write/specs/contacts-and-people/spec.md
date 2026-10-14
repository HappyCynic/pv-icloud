## MODIFIED Requirements

### Requirement: In-memory address book
Each account's address book SHALL be held in memory only, never written to disk. It SHALL be
loaded in the background at server startup and on first use, then refreshed at most once every
60 seconds when a contacts tool or `find_person` needs it, using CardDAV `sync-collection` so
only changed cards are fetched (with `addressbook-multiget`, 100 cards per request). When iCloud
rejects the sync token (HTTP 403, 409 or 412), the whole book SHALL be loaded again. A card that
cannot be parsed SHALL be skipped. Group cards SHALL never be returned as contacts. After a
contact write call that wrote anything, the address book SHALL be refreshed at once, whatever
the time since the last refresh, as the `contacts-write-tools` capability requires. The
address book SHALL be refreshed only by reading; a refresh SHALL never write.

#### Scenario: Only changes are fetched
- **WHEN** the book was loaded, then one card is added, one changed and one deleted, and a
  caller searches after 60 seconds
- **THEN** one sync request and one multiget for the two changed cards are sent, and the results
  reflect all three changes

#### Scenario: Nothing changed
- **WHEN** a caller searches again after 60 seconds and nothing changed
- **THEN** one small sync request is sent and no cards are fetched

#### Scenario: Within the refresh interval
- **WHEN** a caller searches twice within 60 seconds
- **THEN** the second search sends no request

#### Scenario: Right after a write
- **WHEN** a caller searched 10 seconds ago and then updates a contact
- **THEN** the update call ends with a sync request that fetches the changed card, and a
  search right after it shows the change without another request
