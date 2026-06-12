"""
Logging utilities for the SmarterContact Audit Automation system.

Houses the console log filter that condenses verbose worker/scorer log
records into a small set of human-readable status lines for the dashboard
log tail. Extracted from ``main.py`` to keep the entry point slim.
"""
import logging


class SimplifiedConsoleFilter(logging.Filter):
    def __init__(self):
        super().__init__()
        self.worker_to_agent = {}
        self.agent_targets = {}

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        agent_name = self._resolve_agent_name(msg)

        # Ordered dispatch — first matching category wins, preserving the
        # original if/elif precedence. A handler returns True (keep, rewritten),
        # False (drop), or None (not its message → try the next category).
        for handler in (
            self._handle_login,
            self._handle_collection,
            self._handle_progress,
            self._handle_scoring,
            self._handle_result,
        ):
            outcome = handler(msg, agent_name, record)
            if outcome is not None:
                return outcome

        return False

    def _resolve_agent_name(self, msg: str) -> str:
        """Derive the display agent name from a raw log message.

        Mirrors the original precedence: worker association → scorer →
        single-run → fallback → [STEP]/[GQL] tag override.
        """
        # Determine worker ID if present
        worker_id = None
        if "[Worker-" in msg:
            try:
                worker_id = int(msg.split("[Worker-")[1].split("]")[0])
            except Exception:
                pass

        # Try to parse agent name and associate with worker
        agent_name = None
        if worker_id is not None:
            if "──" in msg and "—" in msg:
                try:
                    agent_name = msg.split("──")[1].split("—")[0].strip()
                    self.worker_to_agent[worker_id] = agent_name
                except Exception:
                    pass
            agent_name = agent_name or self.worker_to_agent.get(worker_id)

        # Check for scorer agent name
        if not agent_name and "[Scorer]" in msg:
            if "──" in msg and "—" in msg:
                try:
                    agent_name = msg.split("──")[1].split("—")[0].strip()
                except Exception:
                    pass
            else:
                try:
                    agent_name = msg.split("[Scorer]")[1].strip().split()[0]
                except Exception:
                    pass

        # Check for main.py single agent runs
        if not agent_name and "single extraction for:" in msg:
            try:
                agent_name = msg.split("single extraction for:")[1].strip()
            except Exception:
                pass

        # Fallback to general name if not found
        agent_name = agent_name or "Agent"

        # ── Extract agent name from [STEP] / [GQL] tags ──────────────────────────
        if "[STEP]" in msg or "[GQL]" in msg:
            try:
                tag = "[STEP]" if "[STEP]" in msg else "[GQL]"
                after = msg.split(tag, 1)[1].strip()
                if after.startswith("["):
                    agent_name = after[1:after.index("]")]
            except Exception:
                pass

        return agent_name

    def _handle_login(self, msg, agent_name, record):
        """Login / audit-start lifecycle messages."""
        # 1. Login/Audit Started
        if "single extraction for:" in msg or "Running single extraction for" in msg:
            record.msg = f"[LOGIN] [{agent_name}] Starting audit..."
            record.args = ()
            return True
        elif "logging in" in msg:
            record.msg = f"[LOGIN] [{agent_name}] Logging in to SmarterContact..."
            record.args = ()
            return True

        # 2. Login Successful (browser bot legacy + API bot)
        elif "Login successful for" in msg or "Already logged in for" in msg:
            record.msg = f"[LOGIN] [{agent_name}] Login successful."
            record.args = ()
            return True
        elif "[STEP]" in msg and "Firebase auth OK" in msg:
            record.msg = f"[LOGIN] [{agent_name}] Firebase auth successful."
            record.args = ()
            return True
        elif "[STEP]" in msg and "Firebase auth FAILED" in msg:
            reason = msg.split("FAILED:", 1)[-1].strip() if "FAILED:" in msg else "check password"
            record.msg = f"[FAILED] [{agent_name}] Login failed: {reason}"
            record.args = ()
            return True

        return None

    def _handle_collection(self, msg, agent_name, record):
        """Conversation collection / fetch-status messages."""
        # 3. Collection Started / Fetch status
        if "starting conversation extraction" in msg:
            record.msg = f"[COLLECT] [{agent_name}] Starting conversation extraction..."
            record.args = ()
            return True
        elif "[STEP]" in msg and "Fetching conversations:" in msg:
            try:
                detail = msg.split("Fetching conversations:", 1)[1].strip()
            except Exception:
                detail = ""
            record.msg = f"[COLLECT] [{agent_name}] Fetching conversations... {detail}"
            record.args = ()
            return True
        elif "[STEP]" in msg and "Found" in msg and "conversations to process" in msg:
            try:
                count = msg.split("Found")[1].split("conversations")[0].strip()
            except Exception:
                count = "?"
            record.msg = f"[COLLECT] [{agent_name}] Found {count} conversations."
            record.args = ()
            return True
        elif "[STEP]" in msg and "0 eligible conversations" in msg:
            try:
                detail = msg.split("0 eligible conversations in range", 1)[-1].strip()
            except Exception:
                detail = ""
            record.msg = f"[COLLECT] [{agent_name}] 0 eligible conversations {detail}"
            record.args = ()
            return True
        elif "[STEP]" in msg and "Conversation fetch FAILED" in msg:
            reason = msg.split("FAILED:", 1)[-1].strip() if "FAILED:" in msg else ""
            record.msg = f"[FAILED] [{agent_name}] Conversation fetch failed: {reason}"
            record.args = ()
            return True
        elif "[GQL]" in msg and "find_conversations done" in msg:
            try:
                stats = msg.split("find_conversations done:", 1)[1].strip()
            except Exception:
                stats = msg
            record.msg = f"[COLLECT] [{agent_name}] Inbox scan: {stats}"
            record.args = ()
            return True
        elif "[GQL]" in msg and ("date boundary" in msg or "inbox empty" in msg):
            record.msg = f"[COLLECT] [{agent_name}] {msg.split('[GQL]', 1)[-1].strip()}"
            record.args = ()
            return True
        elif "[STEP]" in msg and "Unread count:" in msg:
            try:
                count = msg.split("Unread count:", 1)[1].strip()
            except Exception:
                count = "?"
            record.msg = f"[COLLECT] [{agent_name}] Unread messages in inbox: {count}"
            record.args = ()
            return True
        elif "contacts to extract" in msg and "limit=" in msg:
            try:
                parts = msg.split("contacts to extract")
                first_part = parts[0].strip()
                actual_count = int(first_part.split("of")[0].strip().split()[-1])
            except Exception:
                actual_count = 0
            if actual_count > 0:
                self.agent_targets[agent_name] = actual_count
            record.msg = f"[COLLECT] [{agent_name}] Target: {actual_count} samples"
            record.args = ()
            return True

        return None

    def _handle_progress(self, msg, agent_name, record):
        """Per-thread progress and collection-complete messages."""
        # 4. Progress Updates (e.g. "Opening thread X/Y")
        if "Opening thread" in msg:
            try:
                thread_part = msg.split("Opening thread")[1].split(":")[0].strip()
                if "/" in thread_part:
                    curr_str, tgt_str = thread_part.split("/", 1)
                    curr_val = int(curr_str.strip())
                    if curr_val % 25 == 0:
                        progress_str = f"Progress: {thread_part}"
                    else:
                        return False
                else:
                    progress_str = f"Progress: {thread_part}"
            except Exception:
                progress_str = "Progress: extracting..."
            record.msg = f"[COLLECT] [{agent_name}] {progress_str}"
            record.args = ()
            return True

        # 5. Collection Done
        elif "DONE | grabbed=" in msg:
            try:
                grabbed = int(msg.split("grabbed=")[1].split("|")[0].strip())
                target = self.agent_targets.get(agent_name, grabbed)
                done_str = f"Progress: {grabbed}/{target} (Done)"
            except Exception:
                done_str = "Progress: completed collection"
            record.msg = f"[COLLECT] [{agent_name}] {done_str}"
            record.args = ()
            return True

        return None

    def _handle_scoring(self, msg, agent_name, record):
        """Scoring start / completion messages."""
        # 6. Scoring Start
        if "scoring" in msg and "conversation" in msg and "parallel" in msg:
            try:
                count = msg.split("scoring")[1].split("conversation")[0].strip()
                scoring_str = f"Scoring {count} conversations..."
            except Exception:
                scoring_str = "Scoring conversations..."
            record.msg = f"[SCORE] [{agent_name}] {scoring_str}"
            record.args = ()
            return True

        # 7. Scoring Done
        elif "overall=" in msg and "adherence=" in msg:
            try:
                overall = msg.split("overall=")[1].split("|")[0].strip()
                done_str = f"Completed scoring (Score: {overall})."
            except Exception:
                done_str = "Completed scoring."
            record.msg = f"[SCORE] [{agent_name}] {done_str}"
            record.args = ()
            return True

        return None

    def _handle_result(self, msg, agent_name, record):
        """Final audit success / failure messages."""
        # 8. Audit Success
        if "Extraction complete for" in msg:
            record.msg = f"[SUCCESS] [{agent_name}] Audit run completed successfully."
            record.args = ()
            return True

        # 9. Audit Failed
        elif "Extraction failed for" in msg or "Audit failed for" in msg or "Fatal error for" in msg or "Fatal login error" in msg:
            reason = msg.split(":")[-1].strip() if ":" in msg else "unknown error"
            record.msg = f"[FAILED] [{agent_name}] Run failed: {reason}"
            record.args = ()
            return True

        return None
