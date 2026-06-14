# Session Collections (Bookmarks) — Design

Date: 2026-06-15
Status: Approved

## Problem

There is no way to save or group sessions for later. Users want to bookmark
sessions and organize them into named lists ("collections") that are reachable
across tools and projects, independent of search.

## Core constraint

The index DB at `~/.cache/ai-session-browser/index.db` reassigns each session's
integer `sessions.id` on every re-parse: `index()` calls `_delete_session_rows()`
then re-`INSERT`s, taking a fresh `lastrowid`. `--reindex` re-parses everything.
The only stable, unique session identity across reindex is `sessions.path`
(the `UNIQUE` column; `<file path>` for file sources, `<db>#<session_id>` for
multi-session stores).

Therefore: collection membership keys on `session_path`, and the tables live in
`SCHEMA` (created idempotently) and are never touched by `_delete_session_rows()`
or the reindex path.

## Data model

Two new tables, appended to `SCHEMA`:

```sql
CREATE TABLE IF NOT EXISTS collections (
    id INTEGER PRIMARY KEY,
    name TEXT UNIQUE NOT NULL,
    sort_order INTEGER DEFAULT 0,
    created TEXT
);
CREATE TABLE IF NOT EXISTS collection_items (
    collection_id INTEGER NOT NULL,
    session_path  TEXT NOT NULL,   -- stable sessions.path, NOT sessions.id
    added TEXT,
    PRIMARY KEY (collection_id, session_path)
);
```

- Membership keys on `session_path`.
- A session may belong to many collections; a collection holds many sessions.
- Deleting a session's file leaves a dangling membership row: harmless (it just
  stops joining to any session) and self-healing if the file returns. No orphan
  cleanup (YAGNI).
- Deleting a collection also deletes its `collection_items` rows.

## API

Reads (GET):

- `GET /api/collections` → `{collections: [{id, name, n}]}` with member counts.
- `GET /api/sessions?collection=ID` → existing endpoint; new filter clause
  `path IN (SELECT session_path FROM collection_items WHERE collection_id=?)`.
- `GET /api/session-collections?path=…` → `{ids: [collection_id, ...]}` the
  collections a given session belongs to (powers the checkbox popover, fetched
  lazily when the popover opens).

Writes (POST, JSON body, guarded by the existing `Handler.lock`). This adds the
first `do_POST` to `Handler`:

- `POST /api/collections` `{name}` → create; returns `{id, name}`. Trims name;
  rejects empty; unique-name enforced (409-ish error JSON on conflict).
- `POST /api/collections/rename` `{id, name}`
- `POST /api/collections/delete` `{id}` → deletes collection + its items.
- `POST /api/collection_items` `{collection_id, path, op:"add"|"remove"}` →
  idempotent (add twice = one row; remove missing = no-op).

Payload additions:

- `/api/sessions` rows and `/api/search` session rows gain `path` so the UI can
  toggle membership without an extra lookup.

## UI (app.html)

- **Sidebar "Collections" section** above Projects: each collection clickable
  like a project (sets `state.collection`, loads `?collection=ID`), shows
  `name (count)`. Inline `+ New collection`. Hover reveals rename/delete.
- **Add-to-collection popover**: a `⊕` control on each session row and in the
  open-transcript header opens one shared popover — checkboxes for existing
  collections + a "new collection" field. Toggling fires `add`/`remove` POSTs.
- **State**: add `state.collection`; selecting a collection clears
  `state.project` and vice versa (mutually exclusive, mirrors project filtering).

## Testing (`tests/test_core.py`, stdlib unittest)

1. **Membership survives reindex** (headline invariant, mirrors the existing
   "no-leak on re-parse"): add a session to a collection, force a full re-parse
   so `sessions.id` is reassigned, assert it still appears under the collection
   filter.
2. `?collection=ID` returns only members.
3. create / rename / delete (delete cascades items) / unique-name constraint.
4. add/remove item is idempotent.
5. orphaned membership (deleted file) does not break listing.

## Out of scope (YAGNI)

Drag-and-drop reordering, nested collections, export/share.
