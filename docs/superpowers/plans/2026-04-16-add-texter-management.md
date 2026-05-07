# Add Texter Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add GET/POST/DELETE `/api/roster` endpoints and an "Add Texter" sidebar UI so texter names can be managed without editing source code.

**Architecture:** Backend adds three endpoints that read/write `config/agent_roster.json` and reload the in-memory `AGENT_ROSTER` global. Frontend adds a sidebar nav item and view with an add form + live list with inline remove confirmation. The hardcoded `const AGENT_ROSTER` in JS is replaced with an async `loadRoster()` call.

**Tech Stack:** FastAPI (app.py), Pydantic BaseModel, aiosqlite, vanilla JS fetch, Jinja2 HTML template

---

### Task 1: Backend — GET/POST/DELETE /api/roster endpoints

**Files:**
- Modify: `dashboard/app.py:408-439` (roster globals), `dashboard/app.py:1140-1172` (add new endpoints before entry point)

- [ ] **Step 1: Add `AddTexterRequest` Pydantic model after existing request models**

  Find the block around line 440 in `dashboard/app.py` (just after `AGENT_ROSTER = _load_agent_roster()`). Add this model after the `AssignmentRequest` model (which is defined somewhere above line 1033). Search for `class AssignmentRequest` to locate it, then add immediately after:

  ```python
  class AddTexterRequest(BaseModel):
      name: str
  ```

- [ ] **Step 2: Add `GET /api/roster` endpoint**

  Add immediately before the `# ── Entry point` comment at line 1174:

  ```python
  @app.get("/api/roster")
  async def api_get_roster():
      """Return the current texter roster list."""
      return AGENT_ROSTER
  ```

- [ ] **Step 3: Add `POST /api/roster` endpoint**

  Add after the `GET /api/roster` endpoint:

  ```python
  @app.post("/api/roster")
  async def api_post_roster(body: AddTexterRequest):
      """Append a new texter name to the roster and save to disk."""
      global AGENT_ROSTER
      name = body.name.strip()
      if not name:
          raise HTTPException(status_code=400, detail="name is required")
      AGENT_ROSTER.append(name)
      _AGENT_ROSTER_FILE.write_text(
          json.dumps(AGENT_ROSTER, indent=2, ensure_ascii=False), encoding="utf-8"
      )
      logger.info(f"Roster: added '{name}' ({len(AGENT_ROSTER)} total)")
      return {"status": "ok", "roster": AGENT_ROSTER}
  ```

- [ ] **Step 4: Add `DELETE /api/roster/{name}` endpoint**

  Add after the `POST /api/roster` endpoint:

  ```python
  @app.delete("/api/roster/{name:path}")
  async def api_delete_roster(name: str):
      """Remove a texter from the roster and wipe all their historical data."""
      global AGENT_ROSTER
      name = name.strip()
      if name not in AGENT_ROSTER:
          raise HTTPException(status_code=404, detail=f"'{name}' not found in roster")
      AGENT_ROSTER.remove(name)
      _AGENT_ROSTER_FILE.write_text(
          json.dumps(AGENT_ROSTER, indent=2, ensure_ascii=False), encoding="utf-8"
      )
      try:
          async with aiosqlite.connect(DB_PATH) as db:
              cur1 = await db.execute(
                  "DELETE FROM trend_snapshots WHERE agent_name = ?", (name,)
              )
              deleted_snapshots = cur1.rowcount
              cur2 = await db.execute(
                  "DELETE FROM account_assignments WHERE agent_name = ?", (name,)
              )
              deleted_assignments = cur2.rowcount
              await db.commit()
          logger.info(
              f"Roster: removed '{name}', wiped {deleted_snapshots} snapshots, "
              f"{deleted_assignments} assignments"
          )
          return {
              "status": "ok",
              "deleted_snapshots": deleted_snapshots,
              "deleted_assignments": deleted_assignments,
          }
      except Exception as exc:
          logger.exception(f"Error wiping data for '{name}'")
          raise HTTPException(status_code=500, detail=str(exc))
  ```

- [ ] **Step 5: Start the dashboard and smoke-test the endpoints**

  ```bash
  cd "c:\Users\vos\Desktop\TEXTING AUDIT AUTOMATION"
  python -m uvicorn dashboard.app:app --port 5000 --reload
  ```

  In another terminal:
  ```bash
  # List roster
  curl http://localhost:5000/api/roster
  # Expected: ["Omar Abdellatif Hamed", ...]

  # Add a test texter
  curl -X POST http://localhost:5000/api/roster -H "Content-Type: application/json" -d "{\"name\": \"Test Texter Delete Me\"}"
  # Expected: {"status": "ok", "roster": [..., "Test Texter Delete Me"]}

  # Delete the test texter
  curl -X DELETE "http://localhost:5000/api/roster/Test%20Texter%20Delete%20Me"
  # Expected: {"status": "ok", "deleted_snapshots": 0, "deleted_assignments": 0}
  ```

- [ ] **Step 6: Commit**

  ```bash
  git add dashboard/app.py
  git commit -m "feat: add GET/POST/DELETE /api/roster endpoints"
  ```

---

### Task 2: Frontend — sidebar nav item + Add Texter view

**Files:**
- Modify: `dashboard/templates/index.html` — sidebar nav (around line 596), settings views section (after the Daily Assignments view), JS section

- [ ] **Step 1: Add the "Add Texter" sidebar nav item**

  Find the closing `</div>` after the Daily Assignments `snav-item` (around line 596). The block ends with:

  ```html
          <span class="snav-label">Daily Assignments</span>
        </div>
      </div>
  ```

  Replace that closing `</div>` (the one that closes the snav group, not the item) — actually, insert a new `snav-item` BEFORE the closing `</div>` of the snav group. The current structure is:

  ```html
        <div class="snav-item" id="snav-assign" onclick="showSettingsSection('assign')">
          <div class="snav-icon" style="background:rgba(16,185,129,.12); color:#10b981;">
            <svg ...calendar svg...</svg>
          </div>
          <span class="snav-label">Daily Assignments</span>
        </div>
      </div>
  ```

  Change to:

  ```html
        <div class="snav-item" id="snav-assign" onclick="showSettingsSection('assign')">
          <div class="snav-icon" style="background:rgba(16,185,129,.12); color:#10b981;">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="18" rx="2"/><line x1="16" y1="2" x2="16" y2="6"/><line x1="8" y1="2" x2="8" y2="6"/><line x1="3" y1="10" x2="21" y2="10"/></svg>
          </div>
          <span class="snav-label">Daily Assignments</span>
        </div>
        <div class="snav-item" id="snav-add-texter" onclick="showSettingsSection('add-texter')">
          <div class="snav-icon" style="background:rgba(234,88,12,.12); color:#ea580c;">
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.3" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>
          </div>
          <span class="snav-label">Add Texter</span>
        </div>
      </div>
  ```

- [ ] **Step 2: Wire "Add Texter" into `showSettingsSection()`**

  Search `index.html` for `showSettingsSection` function definition and find where it handles `'assign'`. It will set `display` on `vw-settings-assign`. Add `vw-settings-add-texter` to the same hide-all list and the show branch.

  Find the function — it will look something like:
  ```js
  function showSettingsSection(section) {
    // hide all
    ['vw-settings-add', 'vw-settings-edit', 'vw-settings-assign'].forEach(id => {
      const el = document.getElementById(id);
      if (el) el.style.display = 'none';
    });
    // deactivate all snav items
    ...
    // show selected
    if (section === 'add') { ... }
    else if (section === 'edit') { ... }
    else if (section === 'assign') { ... }
  }
  ```

  Add `'vw-settings-add-texter'` to the hide-all array and add this branch:
  ```js
  else if (section === 'add-texter') {
    const el = document.getElementById('vw-settings-add-texter');
    if (el) { el.style.display = 'flex'; loadTexterList(); }
    const snav = document.getElementById('snav-add-texter');
    if (snav) snav.classList.add('active');
  }
  ```

- [ ] **Step 3: Add the Add Texter view HTML**

  Locate the Daily Assignments view (`id="vw-settings-assign"`) in the HTML, find its closing `</div>` (the one that closes the view container), and insert the new view directly after it.

  The new view (insert after `</div>` that closes `vw-settings-assign`):

  ```html
  <!-- ── View: Settings — Add Texter ── -->
  <div id="vw-settings-add-texter" style="display:none; flex:1; flex-direction:column; overflow:hidden; background:var(--surface);">
    <div style="padding:20px 40px 16px; border-bottom:1px solid var(--surface-border); flex-shrink:0; display:flex; align-items:center; gap:10px;">
      <div style="width:30px; height:30px; border-radius:8px; display:flex; align-items:center; justify-content:center; background:rgba(234,88,12,.12);">
        <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="#ea580c" stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round"><path d="M16 21v-2a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v2"/><circle cx="9" cy="7" r="4"/><line x1="19" y1="8" x2="19" y2="14"/><line x1="22" y1="11" x2="16" y2="11"/></svg>
      </div>
      <div>
        <div style="font-size:1.05rem; font-weight:700; color:var(--text-primary); letter-spacing:-.02em;">Add Texter</div>
        <div style="font-size:.76rem; color:var(--text-muted); margin-top:1px;">Connect a new texter to the audit system</div>
      </div>
    </div>
    <div style="flex:1; overflow-y:auto; padding:24px 40px;">

      <!-- Add form -->
      <div style="background:var(--surface); border:1px solid var(--surface-border); border-radius:var(--radius-md); padding:20px; margin-bottom:24px; box-shadow:var(--shadow-xs);">
        <div style="font-size:.85rem; font-weight:600; color:var(--text-primary); margin-bottom:14px;">Full Name</div>
        <div style="font-size:.75rem; color:var(--text-muted); margin-bottom:8px;">e.g. Marwan Ehab Zaghloul Attia</div>
        <div style="display:flex; gap:10px; align-items:flex-start;">
          <input id="add-texter-input" type="text" placeholder="Full texter name"
            style="flex:1; padding:9px 12px; border:1px solid var(--surface-border); border-radius:var(--radius-sm); background:var(--input-bg); color:var(--text-primary); font-size:.84rem; outline:none;"
            onkeydown="if(event.key==='Enter') addTexter()" />
          <button onclick="addTexter()"
            style="padding:9px 18px; background:var(--brand); color:#fff; border:none; border-radius:var(--radius-sm); font-size:.84rem; font-weight:600; cursor:pointer; white-space:nowrap;">
            + Add Texter
          </button>
        </div>
        <div id="add-texter-msg" style="margin-top:8px; font-size:.78rem; display:none;"></div>
      </div>

      <!-- Current texters list -->
      <div style="background:var(--surface); border:1px solid var(--surface-border); border-radius:var(--radius-md); padding:20px; box-shadow:var(--shadow-xs);">
        <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
          <div style="font-size:.85rem; font-weight:600; color:var(--text-primary);">
            Current Texters
            <span id="texter-count" style="margin-left:6px; font-size:.75rem; font-weight:500; color:var(--text-muted); background:var(--surface-subtle); border:1px solid var(--surface-border); border-radius:var(--radius-full); padding:1px 8px;"></span>
          </div>
        </div>
        <div id="texter-list" style="display:flex; flex-direction:column; gap:2px;">
          <div style="font-size:.78rem; color:var(--text-muted); padding:8px 0;">Loading...</div>
        </div>
      </div>

    </div>
  </div>
  ```

- [ ] **Step 4: Verify view renders (start dashboard, navigate to Settings → Add Texter)**

  Open `http://localhost:5000`, click Settings, click "Add Texter". Confirm the card layout appears with the form and "Loading..." in the list area.

---

### Task 3: Frontend JS — loadRoster, addTexter, removeTexter

**Files:**
- Modify: `dashboard/templates/index.html` — JS section (around line 2326)

- [ ] **Step 1: Replace hardcoded `const AGENT_ROSTER` with dynamic `let AGENT_ROSTER`**

  Find (around line 2327):
  ```js
  const AGENT_ROSTER = [
    "Omar Abdellatif Hamed",
    ...
    "Khaled Mahmoud Abdelbaky Ali Mosa",
  ];
  ```

  Replace the entire const declaration with:
  ```js
  let AGENT_ROSTER = [];

  async function loadRoster() {
    const res = await fetch("/api/roster");
    AGENT_ROSTER = await res.json();
  }
  ```

- [ ] **Step 2: Call `loadRoster()` on page init**

  Find the page initialization block — look for the function that calls `loadAssignments()`, `updateStats()`, `renderSidebar()` etc. on DOMContentLoaded. It will look like:

  ```js
  document.addEventListener("DOMContentLoaded", async () => {
    await loadAgents();
    updateStats();
    renderSidebar();
    // ...
  });
  ```

  Add `await loadRoster();` as the FIRST await call in this block, before `loadAgents()`:

  ```js
  document.addEventListener("DOMContentLoaded", async () => {
    await loadRoster();
    await loadAgents();
    updateStats();
    renderSidebar();
    // ...
  });
  ```

- [ ] **Step 3: Add `loadTexterList()`, `addTexter()`, and `removeTexter()` functions**

  Add these three functions after `loadRoster()` in the JS section:

  ```js
  async function loadTexterList() {
    const listEl = document.getElementById("texter-list");
    const countEl = document.getElementById("texter-count");
    if (!listEl) return;
    listEl.innerHTML = `<div style="font-size:.78rem;color:var(--text-muted);padding:8px 0;">Loading...</div>`;
    try {
      const res = await fetch("/api/roster");
      AGENT_ROSTER = await res.json();
      countEl.textContent = AGENT_ROSTER.length;
      if (!AGENT_ROSTER.length) {
        listEl.innerHTML = `<div style="font-size:.78rem;color:var(--text-muted);padding:8px 0;">No texters yet.</div>`;
        return;
      }
      listEl.innerHTML = AGENT_ROSTER.map((name, i) => `
        <div id="texter-row-${i}" style="display:flex;align-items:center;justify-content:space-between;padding:9px 12px;border-radius:var(--radius-sm);border-bottom:1px solid var(--surface-border);">
          <span style="font-size:.84rem;color:var(--text-primary);">${esc(name)}</span>
          <button onclick="confirmRemoveTexter(${i}, '${esc(name).replace(/'/g, "\\'")}')"
            style="padding:4px 12px;background:transparent;border:1px solid var(--danger);color:var(--danger);border-radius:var(--radius-sm);font-size:.78rem;font-weight:600;cursor:pointer;">
            Remove
          </button>
        </div>
        <div id="texter-confirm-${i}" style="display:none;padding:8px 12px;background:var(--surface-subtle);border-radius:var(--radius-sm);font-size:.78rem;color:var(--text-secondary);border:1px solid var(--surface-border);">
          Are you sure? This will wipe all their history.
          <button onclick="removeTexter(${i}, '${esc(name).replace(/'/g, "\\'")}')"
            style="margin-left:10px;padding:4px 10px;background:var(--danger);color:#fff;border:none;border-radius:var(--radius-sm);font-size:.78rem;font-weight:600;cursor:pointer;">
            Yes, Remove
          </button>
          <button onclick="document.getElementById('texter-row-${i}').style.display='flex';document.getElementById('texter-confirm-${i}').style.display='none';"
            style="margin-left:6px;padding:4px 10px;background:transparent;border:1px solid var(--surface-border);color:var(--text-secondary);border-radius:var(--radius-sm);font-size:.78rem;cursor:pointer;">
            Cancel
          </button>
        </div>
      `).join("");
    } catch (err) {
      listEl.innerHTML = `<div style="font-size:.78rem;color:var(--danger);padding:8px 0;">Error loading texters.</div>`;
    }
  }

  function confirmRemoveTexter(i, name) {
    document.getElementById(`texter-row-${i}`).style.display = "none";
    document.getElementById(`texter-confirm-${i}`).style.display = "block";
  }

  async function addTexter() {
    const input = document.getElementById("add-texter-input");
    const msgEl = document.getElementById("add-texter-msg");
    const name = (input.value || "").trim();
    if (!name) return;
    msgEl.style.display = "none";
    try {
      const res = await fetch("/api/roster", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      input.value = "";
      msgEl.style.display = "block";
      msgEl.style.color = "var(--success)";
      msgEl.textContent = `"${name}" added successfully.`;
      await loadTexterList();
    } catch (err) {
      msgEl.style.display = "block";
      msgEl.style.color = "var(--danger)";
      msgEl.textContent = "Error: " + err.message;
    }
  }

  async function removeTexter(i, name) {
    try {
      const res = await fetch(`/api/roster/${encodeURIComponent(name)}`, { method: "DELETE" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Failed");
      await loadTexterList();
    } catch (err) {
      alert("Error removing texter: " + err.message);
    }
  }
  ```

- [ ] **Step 4: Verify full flow in browser**

  1. Open `http://localhost:5000`
  2. Confirm Settings → Daily Assignments still works (AGENT_ROSTER loads dynamically now — dropdown should still show all 13 texters)
  3. Confirm Trends agent dropdown also shows all texters
  4. Go to Settings → Add Texter — confirm list shows all 13 texters with count
  5. Type "Test Delete Me" and click "+ Add Texter" — confirm success message, list updates to 14, count shows 14
  6. Click Remove on "Test Delete Me" — confirm inline confirmation appears
  7. Click "Yes, Remove" — confirm row disappears, count back to 13
  8. Refresh page — confirm "Test Delete Me" is gone (persisted to agent_roster.json)

- [ ] **Step 5: Commit**

  ```bash
  git add dashboard/templates/index.html
  git commit -m "feat: add Add Texter sidebar view with dynamic roster management"
  ```

---

## Self-Review

**Spec coverage check:**

| Spec requirement | Task |
|---|---|
| GET /api/roster | Task 1 Step 2 |
| POST /api/roster — append, save, reload | Task 1 Step 3 |
| DELETE /api/roster/{name} — remove, wipe snapshots, wipe assignments, reload | Task 1 Step 4 |
| Sidebar item "Add Texter" with person-plus icon | Task 2 Step 1 |
| showSettingsSection('add-texter') wiring | Task 2 Step 2 |
| Add form with input + button, success message | Task 3 Step 3 (addTexter) |
| Current Texters list with count | Task 3 Step 3 (loadTexterList) |
| Inline confirmation (no modal) | Task 3 Step 3 (confirmRemoveTexter) |
| Replace const AGENT_ROSTER with let + loadRoster() | Task 3 Step 1 |
| loadRoster() called on page init | Task 3 Step 2 |
| Existing consumers (Assignments dropdown, Trends dropdown) auto-get live data | Task 3 Step 2 — AGENT_ROSTER is now live from API |

**No placeholders found.**

**Type consistency:** `AGENT_ROSTER` is `string[]` throughout. `loadRoster()` is async and sets the global. `loadTexterList()` also refreshes the global as a side-effect to keep it in sync after add/remove.
