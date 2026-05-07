# SmarterContact Audit Automation — Claude Context

## SESSION START — Load Second Brain
At the start of every session, before anything else:
1. Read `C:\Users\vos\Desktop\obsidian_brain\index.md`
2. Read `C:\Users\vos\Desktop\obsidian_brain\01-projects\TEXTING AUDIT AUTOMATION.md`
3. Read `C:\Users\vos\Desktop\obsidian_brain\03-decisions\Known Gotchas.md`

This is your memory. Use it to understand what was done before, what's in progress, and what gotchas to avoid.

---

## Project
Playwright-based automation that logs into SmarterContact (a React/Chakra UI SPA),
extracts conversation threads, and scores agent performance using AI.

Entry point: `python main.py --single "AgentName"`
Core scraper: `scraper/browser_bot.py`
Config: `config/settings.py`, `config/agents.json`, `.env`

---

## Login Fix (April 2026)

### Root cause
SmarterContact is a React SPA. After clicking Login, there is NO full page navigation —
the login happens via an AJAX call. Using `wait_for_load_state("load")` or
`wait_for_load_state("networkidle")` both fail:
- `"load"` returns immediately (already fired), so `_is_logged_in()` runs before the API responds
- `"networkidle"` times out because SPAs keep background websocket/polling connections open forever

### Fix in `scraper/browser_bot.py` → `login()` method
After clicking the Login button, poll every second for up to 20s waiting for the URL
to leave `/login`. As soon as the URL is no longer `/login`, login succeeded.
Do NOT check `_is_logged_in()` at this point — the page shows a blank white screen
while React re-renders. The URL change is sufficient proof.

```python
for _ in range(20):
    await asyncio.sleep(1)
    if "/login" not in self.page.url.lower():
        logger.info(f"[Worker-{self.worker_id}] ✓ Login successful for {self.agent_name}")
        return True
```

### Fix in `extract_conversations()` — navigate after login
After login, the page is on `app.smartercontact.com/` with a blank React loading screen.
Do NOT wait for selectors on this page. Navigate directly to the messenger inbox and wait
for the conversation list to appear:

```python
inbox_all_url = SMARTERCONTACT_MESSENGER_URL.rstrip("/") + "/inbox/all"
await self.page.goto(inbox_all_url, wait_until="load", timeout=30000)
await self.page.wait_for_selector(
    "div.ReactVirtualized__Grid__innerScrollContainer, [data-test-class='messenger_nav_inbox_all_messages_row']",
    timeout=20000,
)
```

---

## Known Selectors (verified April 2026)

| Element | Selector |
|---|---|
| Email input on login | `input#email` |
| Password input | `input#password` |
| TOS checkbox | `label.chakra-checkbox` (fallback: `span.chakra-checkbox__control`) |
| Login button | `button:has-text("Log in")` |
| Chat panel | `div[data-test-id="messenger_nav_inbox_all_contact-panel_messages"]` |
| Messages inside panel | `p.chakra-text` inside the panel |
| Conversation rows | `[data-test-class='messenger_nav_inbox_all_messages_row']` |
| Virtualized list | `div.ReactVirtualized__Grid__innerScrollContainer` |

---

## Do NOT do these things

- Do NOT use `wait_until="networkidle"` anywhere — SPAs keep connections open, it always times out
- Do NOT use `wait_for_load_state("networkidle")` after login button click
- Do NOT check `_is_logged_in()` immediately after clicking Login — React takes 2-3s to render
- Do NOT wait on selectors after login before navigating to messenger — page is blank

---

## Knowledge Base (Obsidian)

**Vault:** `C:\Users\vos\Desktop\obsidian_brain`
**Project doc:** `01-projects/TEXTING AUDIT AUTOMATION.md`

When significant changes happen, update the vault:
- New selector verified → update the Verified Selectors table in project doc
- SmarterContact UI changes → update selectors + note the date verified
- Bug or gotcha discovered → add to `03-decisions/Known Gotchas.md`
- New runbook needed → create in `04-how-to/`
- Session log → append to the **Session Log** section in the project doc

---

## AI Key Pool Model (April 2026)

**Two stores in `ai.analyzer.KeyPoolManager`:**

1. **Groq shared pool** — `config/groq_keys.json` (flat list of key strings).
   Every agent NOT listed in `agent_keys.json` as a NIM entry uses this pool.
   Selection is LRU; rate-limited keys rotate automatically; quota-exhausted
   keys are permanently removed from rotation.

2. **NIM dedicated keys** — `config/agent_keys.json` (only `provider: "nim"` entries).
   Each NIM agent has its own key. Non-NIM entries here are ignored (logged warning).

**Guarantee:** No conversation is skipped due to rate limits. The Groq pool
cycles up to 10 times (≈140 key attempts) before giving up. A skip only
happens when the model returns malformed JSON — that is a data issue, not
a key issue.

**Do NOT:**
- Put Groq keys in `agent_keys.json` — they'll be logged as warnings and ignored
- Use `networkidle` waits anywhere (existing rule — SPA login)
- Add Gemini support — removed April 2026

