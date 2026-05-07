# Add Texter Management — Design Spec
**Date:** 2026-04-16  
**Status:** Approved  

---

## Problem

The texter roster (real names like "Marwan Ehab Zaghloul Attia") is hardcoded in two places:
- `dashboard/app.py` — `_DEFAULT_ROSTER` list (Python)
- `dashboard/templates/index.html` — `const AGENT_ROSTER = [...]` (JavaScript)

There is no UI to add or remove texter real names. Any change requires editing source code manually.

---

## Goal

Add a **"Add Texter"** section to the Settings sidebar that lets the user:
1. Add a new texter real name to the roster
2. See all current texters in a list
3. Remove a texter — which also wipes all their historical data

---

## Scope

- Backend: 3 new API endpoints in `dashboard/app.py`
- Frontend: new sidebar item + view in `dashboard/templates/index.html`
- Remove hardcoded roster from JS; load dynamically from API
- No changes to `main.py`, `scraper/`, `ai/`, or database schema (tables already exist)

---

## Backend

### Storage
`config/agent_roster.json` — already exists, already used. Remains the single source of truth.

### New API Endpoints

#### `GET /api/roster`
Returns the full list of texter names.

```json
["Omar Abdellatif Hamed", "Marwan Ehab Zaghloul Attia", ...]
```

#### `POST /api/roster`
Appends a new texter name to the roster and saves the file.

Request body:
```json
{"name": "New Texter Full Name"}
```

Response:
```json
{"status": "ok", "roster": [...updated list...]}
```

No duplicate check — save whatever is typed.

#### `DELETE /api/roster/{name}`
- URL-encoded name in path
- Removes name from `agent_roster.json`
- Wipes `trend_snapshots` rows where `agent_name = name`
- Wipes `account_assignments` rows where `agent_name = name`

Response:
```json
{"status": "ok", "deleted_snapshots": 5, "deleted_assignments": 3}
```

### Existing `_load_agent_roster()` and `AGENT_ROSTER`
- Keep `_load_agent_roster()` as-is (used at startup for validation in `/api/assignments`)
- After any POST/DELETE to `/api/roster`, reload `AGENT_ROSTER` in memory so the assignments endpoint stays in sync

---

## Frontend

### Sidebar — new item
Add a 4th Settings sidebar entry after "Daily Assignments":

```
+ Add Account
  Edit Account
  Daily Assignments
  Add Texter          ← new
```

- Icon: person-plus SVG (matches existing sidebar icon style)
- Label: "Add Texter"
- Click: switches active view to `"add-texter"`

### Add Texter View

Two parts stacked vertically inside the same settings card pattern:

**Part 1 — Add form:**
```
Add Texter
Connect a new texter to the audit system

Full Name  [                              ]
           e.g. Marwan Ehab Zaghloul Attia

           [+ Add Texter]
```

- Single text input, placeholder text shown above
- Button triggers `POST /api/roster`
- On success: clear input, refresh list, show inline success message

**Part 2 — Current Texters list:**
```
Current Texters  (13)

  Omar Abdellatif Hamed                    [Remove]
  Abdellatif Omar Osama Mohamed Ahmed      [Remove]
  ...
```

- Count shown in header, updates after add/remove
- Each row: name left, red Remove button right
- Clicking Remove: inline confirmation replaces the row:
  ```
  "Are you sure? This will wipe all their history.  [Yes, Remove]  [Cancel]"
  ```
  No modal — inline under the row only
- On confirm: triggers `DELETE /api/roster/{name}`, row disappears, count updates

### Dynamic Roster Loading

Remove hardcoded `const AGENT_ROSTER = [...]` from `index.html`.

Replace with:
```js
let AGENT_ROSTER = [];

async function loadRoster() {
  const res = await fetch("/api/roster");
  AGENT_ROSTER = await res.json();
}
```

Call `loadRoster()` once on page load (alongside existing init calls). All existing consumers (`loadAssignments()` dropdown, Trends agent dropdown) already reference `AGENT_ROSTER` — they will automatically get the live data.

---

## Data Flow

```
User types name → clicks Add Texter
  → POST /api/roster
  → server appends to agent_roster.json, reloads AGENT_ROSTER in memory
  → frontend refreshes list

User clicks Remove → confirms
  → DELETE /api/roster/{name}
  → server removes from agent_roster.json
  → server deletes trend_snapshots WHERE agent_name = name
  → server deletes account_assignments WHERE agent_name = name
  → server reloads AGENT_ROSTER in memory
  → frontend removes row, updates count
```

---

## Files Changed

| File | Change |
|------|--------|
| `dashboard/app.py` | Add `GET/POST/DELETE /api/roster` endpoints; reload `AGENT_ROSTER` after mutations |
| `dashboard/templates/index.html` | New sidebar item + Add Texter view; replace hardcoded roster with `loadRoster()` |
| `config/agent_roster.json` | Modified at runtime by the new endpoints (no code change) |

---

## Out of Scope

- No duplicate name validation (save whatever is typed)
- No reordering of the roster list
- No editing an existing name (remove + re-add)
- No pagination (13 texters fits on one screen)
