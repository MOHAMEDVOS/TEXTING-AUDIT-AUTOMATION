"""
SmarterContact Browser Bot - Core Playwright automation.
Handles login, navigation, and data extraction from SmarterContact.
"""
import asyncio
import logging
import re
from datetime import datetime
from pathlib import Path
import random
from playwright.async_api import async_playwright, Page, BrowserContext

from database.db import Database
from ai.transcript_parser import parse_transcript

from config.settings import (
    SMARTERCONTACT_LOGIN_URL,
    SMARTERCONTACT_REPORTING_URL,
    SMARTERCONTACT_MESSENGER_URL,
    HEADLESS_MODE,
    SCREENSHOT_ON_ERROR,
    MAX_RETRIES,
    LOG_DIR,
    DATE_FILTER,
    DEFAULT_SAMPLE_SIZE,
    get_now
)
from scraper.anti_detect import (
    human_delay,
    human_type,
    human_click,
    random_mouse_movement,
    get_stealth_context_options,
)

logger = logging.getLogger(__name__)


async def _read_labels(row, registry: list[str] = None) -> list[str]:
    """
    Read label text directly from the DOM label elements in a row.
    Returns all labels found in the row, mapped to full names if a registry is provided.
    Filters out initials (e.g., 2-letter uppercase codes that aren't real labels).
    """
    try:
        # 1. Primary selector (current SC UI): Chakra tag labels in row
        label_els = await row.query_selector_all('span.chakra-tag__label')

        # 2. ARIA label container (older SC UI)
        if not label_els:
            label_els = await row.query_selector_all('div[aria-label="Label container"] span span')

        # 3. Specialized checkbox label part
        if not label_els:
            label_els = await row.query_selector_all('span[data-part="label"] span')

        # 4. Fallback: Data test class
        if not label_els:
            label_els = await row.query_selector_all("[data-test-class='messenger_nav_inbox_all_item_messages_label'] span")
            
        # Note: We removed the "Last resort" broad CSS selector because it was 
        # accidentally picking up lead initials from the avatar.

        labels = []
        for el in label_els:
            text = (await el.inner_text()).strip()
            
            # Clean up text (SmarterContact sometimes adds icons or chevrons inside the span)
            if text and len(text) > 1:
                # ── Initials Filter ──
                # If text is 2 characters and all uppercase (e.g., "MF", "JD"), 
                # it's likely an avatar. We only keep it if it's explicitly in our registry.
                if len(text) == 2 and text.isupper():
                    if not registry or not any(text.lower() == r.lower() for r in registry):
                        continue

                clean_text = text.rstrip(".").rstrip("…")
                
                # ── Fuzzy Matching ─────────────────────────────────────────────
                # Map truncated text (e.g., "Not int...") back to full registry names.
                if registry and clean_text:
                    match = next(
                        (full for full in registry if full.lower().startswith(clean_text.lower())),
                        None
                    )
                    if match:
                        labels.append(match)
                        continue
                
                labels.append(text)
        
        if not labels:
            html_dump = await row.inner_html()
            logger.warning(f"[DEBUG] Empty labels. Row HTML dump: {html_dump[:500]} ...")

        return labels
    except Exception as e:
        logger.warning(f"[DEBUG] _read_labels Exception: {e}")
        return []


# Matches the SmarterContact inbox-row date, e.g. "05/15/2026".
_SC_DATE_RE = re.compile(r"\b(\d{1,2}/\d{1,2}/\d{4})\b")


def _extract_row_date(row_text: str) -> str:
    """
    Pull the SmarterContact inbox-row date (MM/DD/YYYY) out of a row's text.
    The date column renders as plain text alongside the time and labels, so a
    regex is more resilient than a class selector. Returns "" if not found.
    """
    if not row_text:
        return ""
    m = _SC_DATE_RE.search(row_text)
    return m.group(1) if m else ""


class SmarterContactBot:
    """
    Automates login, navigation, and data extraction from a single
    SmarterContact account using Playwright.
    """

    def __init__(self, agent_name: str, email: str, password: str, worker_id: int = 0,
                 date_filter: str = None, limit: int = None,
                 date_start: str = None, date_end: str = None):
        self.agent_name = agent_name
        self.email = email
        self.password = password
        self.worker_id = worker_id
        self.date_filter = date_filter or DATE_FILTER
        self.date_start = date_start  # "YYYY-MM-DD" for custom range
        self.date_end = date_end      # "YYYY-MM-DD" for custom range
        self.limit = limit or DEFAULT_SAMPLE_SIZE
        self.browser = None
        self.context: BrowserContext = None
        self.page: Page = None
        self.extracted_data = {}
        self.label_registry: list[str] = []  # Full names from sidebar
        self._screenshot_dir = LOG_DIR / "screenshots"
        self._screenshot_dir.mkdir(parents=True, exist_ok=True)

    async def start_browser(self, playwright):
        """Launch browser with stealth settings."""
        self.browser = await playwright.chromium.launch(
            headless=HEADLESS_MODE,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
                "--disable-accelerated-2d-canvas",
                "--disable-gpu",
            ],
        )

        # Each worker gets unique stealth context
        context_options = get_stealth_context_options()
        # IMPORTANT: Ensure viewport is wide enough for chat panel to appear
        # (SmarterContact requires ~1920px width to show side panel with messages)
        if context_options.get("viewport", {}).get("width", 0) < 1400:
            context_options["viewport"] = {"width": 1920, "height": 1080}
        self.context = await self.browser.new_context(**context_options)

        # Remove navigator.webdriver flag (bot detection bypass)
        await self.context.add_init_script("""
            Object.defineProperty(navigator, 'webdriver', {
                get: () => undefined
            });
            Object.defineProperty(navigator, 'plugins', {
                get: () => [1, 2, 3, 4, 5]
            });
            Object.defineProperty(navigator, 'languages', {
                get: () => ['en-US', 'en']
            });
            window.chrome = { runtime: {} };
        """)

        self.page = await self.context.new_page()
        logger.info(f"[Worker-{self.worker_id}] Browser started for {self.agent_name}")

    async def login(self) -> bool:
        """
        Log in to SmarterContact with the agent's credentials.
        SmarterContact uses Chakra UI (React) - no <form> tag.
        Login flow: fill email → fill password → check TOS → click Log in
        Returns True if login successful, False otherwise.
        """
        # EARLY ESCAPE: Check if we are already logged in from a previous session
        if await self._is_logged_in():
            logger.info(f"[Worker-{self.worker_id}] Already logged in for {self.agent_name}")
            return True

        logger.info(f"[Worker-{self.worker_id}] ── {self.agent_name} — logging in ({self.email})")
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(
                    f"[Worker-{self.worker_id}] Login attempt {attempt}/{MAX_RETRIES} "
                    f"for {self.agent_name}"
                )

                # Navigate to login page
                await self.page.goto(SMARTERCONTACT_LOGIN_URL, wait_until="load", timeout=30000)
                await human_delay(0.5, 1)
                await random_mouse_movement(self.page)

                # Wait for email input to appear (Chakra UI: input#email)
                await self.page.wait_for_selector("input#email", timeout=15000)

                # Fill email field
                await human_type(self.page, "input#email", self.email)
                await human_delay(0.3, 0.5)

                # Fill password field
                await human_type(self.page, "input#password", self.password)
                await human_delay(0.3, 0.6)

                # CRITICAL: Check the "Terms of Service" checkbox
                # The Log in button is DISABLED until this is checked
                # SmarterContact uses Ark UI checkbox (custom component with data-scope="checkbox")
                # Try multiple selectors in order of preference
                tos_checkbox = None
                for selector in [
                    'div[data-scope="checkbox"]',  # Primary: Ark UI checkbox
                    'div[data-part="control"][data-state="unchecked"]',  # Control div
                    'input[type="checkbox"]',   # Standard checkbox input
                    'input[name*="agree" i]',  # Name contains 'agree'
                    "label.chakra-checkbox",    # Legacy: old Chakra structure
                    "span.chakra-checkbox__control",  # Legacy: old Chakra span
                ]:
                    tos_checkbox = await self.page.query_selector(selector)
                    if tos_checkbox:
                        logger.debug(f"[Worker-{self.worker_id}] Found TOS checkbox with selector: {selector}")
                        break

                if tos_checkbox:
                    await tos_checkbox.click()
                    logger.info(f"[Worker-{self.worker_id}] ✓ Checked Terms of Service")
                    await human_delay(0.3, 0.5)
                else:
                    logger.warning(f"[Worker-{self.worker_id}] Could not find TOS checkbox — login will likely fail")

                # Wait for Login button to become enabled
                await human_delay(0.3, 0.5)

                # Click "Log in" button
                login_btn = self.page.locator('button:has-text("Log in")').first
                try:
                    await login_btn.click(timeout=5000)
                except Exception:
                    # Fallback: try pressing Enter
                    await self.page.keyboard.press("Enter")

                # Wait up to 20s for the page to navigate away from /login.
                # We do NOT use networkidle — SPAs keep background connections open indefinitely.
                # Navigating away from /login is sufficient proof of success (React loads after).
                for _ in range(20):
                    await asyncio.sleep(1)
                    if "/login" not in self.page.url.lower():
                        logger.info(f"[Worker-{self.worker_id}] ✓ Login successful for {self.agent_name}")
                        return True

                current_url = self.page.url
                logger.warning(
                    f"[Worker-{self.worker_id}] ✗ Login failed for {self.agent_name} "
                    f"(URL: {current_url}) — attempt {attempt}"
                )
                if SCREENSHOT_ON_ERROR:
                    await self._take_screenshot(f"login_fail_{attempt}")

            except Exception as e:
                logger.error(
                    f"[Worker-{self.worker_id}] Login error for {self.agent_name}: {e}"
                )
                if SCREENSHOT_ON_ERROR:
                    await self._take_screenshot(f"login_error_{attempt}")

            if attempt < MAX_RETRIES:
                wait_time = attempt * 5  # Progressive backoff: 5, 10, 15 sec
                logger.info(f"[Worker-{self.worker_id}] Retrying in {wait_time}s...")
                await asyncio.sleep(wait_time)

        return False

    async def extract_reporting_data(self) -> dict:
        """
        Navigate to reporting page and extract key metrics.
        SmarterContact reporting uses Chakra UI tabs with stat cards.
        """
        try:
            logger.info(f"[Worker-{self.worker_id}] Extracting reporting data for {self.agent_name}")

            await self.page.goto(SMARTERCONTACT_REPORTING_URL, wait_until="load", timeout=30000)
            await human_delay(1, 2)
            await random_mouse_movement(self.page)

            # Wait for the statistics panel to load
            await self.page.wait_for_load_state("load", timeout=20000)
            await human_delay(0.5, 1)

            reporting_data = {
                "agent_name": self.agent_name,
                "extracted_at": get_now().isoformat(),
                "metrics": {},
            }

            # Extract specific metrics by their labels
            # SmarterContact stat cards have label text + numeric values
            metric_labels = [
                "SMS sent",
                "SMS segments sent",
                "Carrier block rate",
                "Replies received",
                "Delivery rate",
                "Opt-out rate",
                "AI filtering rate",
                "Reply rate",
                "Median response time",
                "Leads",
                "Contacts",
                "SMS to lead conversion rate",
                "Contact to lead conversion rate",
            ]

            for label in metric_labels:
                value = await self._extract_metric_by_label(label)
                if value:
                    reporting_data["metrics"][label] = value

            # Capture full page text for AI analysis fallback
            reporting_data["page_text"] = await self.page.inner_text("body")

            self.extracted_data["reporting"] = reporting_data

            logger.info(
                f"[Worker-{self.worker_id}] ✓ Reporting data extracted for {self.agent_name} "
                f"({len(reporting_data['metrics'])} metrics)"
            )
            return reporting_data

        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Error extracting reporting: {e}")
            if SCREENSHOT_ON_ERROR:
                await self._take_screenshot("reporting_error")
            return {"error": str(e), "agent_name": self.agent_name}

    async def _open_date_popover(self) -> bool:
        """
        Open the SmarterContact date filter popover.
        Returns True if successfully opened, False otherwise.
        """
        open_selectors = [
            '[data-test-id="messenger_nav_inbox_all_date-filter"]',
            '[data-test-id*="sort-by-dates"]',
            'button:has-text("Today"), button:has-text("Last Week"), '
            'button:has-text("This Month"), button:has-text("All Time"), '
            '[data-test-id*="date"]',
        ]
        for attempt, open_sel in enumerate(open_selectors, 1):
            try:
                date_locator = self.page.locator(open_sel).first
                await date_locator.wait_for(state="visible", timeout=6000)
                await date_locator.click()
                logger.info(f"[Worker-{self.worker_id}] Opened date filter popover (attempt {attempt})")
                return True
            except Exception as e:
                logger.warning(f"[Worker-{self.worker_id}] Date filter open attempt {attempt} failed: {e}")
                try:
                    await self.page.keyboard.press("Escape")
                except Exception as _e:
                    logger.debug("swallowed: %r", _e)
                await asyncio.sleep(0.5)

        logger.warning(
            f"[Worker-{self.worker_id}] Could not open date filter — "
            f"proceeding without filter (all conversations visible)"
        )
        return False

    async def _apply_date_filter(self, date_filter: str = "today") -> None:
        """
        Apply the date filter in the SmarterContact inbox.
        Tries multiple selector strategies to ensure the filter is applied.

        date_filter options:
            "today" | "last_week" | "this_month" | "last_month" |
            "last_30_days" | "last_year" | "all_time" | "custom"

        When date_filter="custom", uses self.date_start / self.date_end
        (YYYY-MM-DD strings) to click specific dates on the calendar.
        """
        # ── Custom date range ────────────────────────────────────────────
        if date_filter == "custom" and self.date_start and self.date_end:
            await self._apply_custom_date_range(self.date_start, self.date_end)
            return

        # ── Preset filter ────────────────────────────────────────────────
        label_map = {
            "today":        "Today",
            "last_week":    "Last Week",
            "this_month":   "This Month",
            "last_month":   "Last Month",
            "last_30_days": "Last 30 days",
            "last_year":    "Last Year",
            "all_time":     "Clear",
        }
        option_text = label_map.get(date_filter, "Today")

        if not await self._open_date_popover():
            return

        try:
            # Wait for the popover to render, then click the option by exact text
            option_locator = self.page.get_by_text(option_text, exact=True)
            await option_locator.first.wait_for(state="visible", timeout=5000)
            await option_locator.first.click()
            logger.info(f"[Worker-{self.worker_id}] ✓ Date filter set to: {option_text}")
            # Wait for the conversation list to refresh
            await human_delay(0.8, 1.5)
        except Exception as e:
            logger.warning(f"[Worker-{self.worker_id}] Date filter option click failed: {e}")
            try:
                await self.page.keyboard.press("Escape")
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

    async def _apply_custom_date_range(self, start_date: str, end_date: str) -> None:
        """
        Select a custom date range on SmarterContact's react-date-range calendar.

        Args:
            start_date: "YYYY-MM-DD" format
            end_date:   "YYYY-MM-DD" format

        Flow:
            1. Open the date popover
            2. Click "Clear" to reset any existing range
            3. Navigate to the start month and click the start day
            4. Navigate to the end month and click the end day
        """
        from datetime import datetime as _dt

        try:
            start = _dt.strptime(start_date, "%Y-%m-%d")
            end = _dt.strptime(end_date, "%Y-%m-%d")
        except ValueError as e:
            logger.error(f"[Worker-{self.worker_id}] Invalid date format: {e}")
            return

        if start > end:
            start, end = end, start
            logger.info(f"[Worker-{self.worker_id}] Swapped start/end dates (start was after end)")

        logger.info(
            f"[Worker-{self.worker_id}] Setting custom date range: "
            f"{start.strftime('%m/%d/%Y')} → {end.strftime('%m/%d/%Y')}"
        )

        # 1. Open the popover
        if not await self._open_date_popover():
            return
        await human_delay(0.5, 0.8)

        # 2. Click "Clear" first to reset any existing selection
        try:
            clear_btn = self.page.locator(
                '[data-test-id*="clear"], button.rdrClearButton'
            ).first
            await clear_btn.wait_for(state="visible", timeout=3000)
            await clear_btn.click()
            logger.info(f"[Worker-{self.worker_id}] Cleared existing date selection")
            await human_delay(0.5, 0.8)
        except Exception:
            logger.debug(f"[Worker-{self.worker_id}] No clear button found or not needed")

        # Re-open popover if clearing closed it
        try:
            calendar_visible = await self.page.locator(".rdrDateRangePickerWrapper, .rdrCalendarWrapper").first.is_visible()
        except Exception:
            calendar_visible = False

        if not calendar_visible:
            if not await self._open_date_popover():
                return
            await human_delay(0.5, 0.8)

        # 3. Navigate to start month and click start day
        await self._navigate_calendar_to_month(start.year, start.month)
        await human_delay(0.3, 0.5)
        await self._click_calendar_day(start.day, start.month, start.year)
        await human_delay(0.3, 0.5)

        # 4. Navigate to end month and click end day
        if end.year != start.year or end.month != start.month:
            await self._navigate_calendar_to_month(end.year, end.month)
            await human_delay(0.3, 0.5)
        await self._click_calendar_day(end.day, end.month, end.year)

        logger.info(
            f"[Worker-{self.worker_id}] ✓ Custom date range set: "
            f"{start.strftime('%m/%d/%Y')} → {end.strftime('%m/%d/%Y')}"
        )
        await human_delay(0.8, 1.5)

        # Close popover by pressing Escape or clicking outside
        try:
            await self.page.keyboard.press("Escape")
        except Exception as _e:
            logger.debug("swallowed: %r", _e)

    async def _navigate_calendar_to_month(self, target_year: int, target_month: int) -> None:
        """
        Navigate SmarterContact's custom react-date-range calendar so the target
        month/year is the FIRST visible month.

        Verified DOM (May 2026):
          <div class="rdrCustomHeader">
            <div class="rdrHeaderArrows">
              <button data-test-id="..._arrow-prev-year">    (« double — year)
              <button data-test-id="..._arrow-prev-month">   (‹ single — month)
            </div>
            <span class="rdrHeaderLabel">May 2026 — Jun 2026</span>
            <div class="rdrHeaderArrows">
              <button data-test-id="..._arrow-next-month">   (› single — month)
              <button data-test-id="..._arrow-next-year">    (» double — year)
            </div>
          </div>

        Sequence: fix the YEAR first (double arrows), THEN the MONTH (single
        arrows). This avoids clicking the single month arrow 10+ times to cross
        a year boundary.
        """
        import re

        MONTH_NAMES = [
            "", "January", "February", "March", "April", "May", "June",
            "July", "August", "September", "October", "November", "December"
        ]
        PFX = "messenger_nav_inbox_all_sort-by-dates"
        SEL_PREV_YEAR  = f'[data-test-id="{PFX}_arrow-prev-year"], .rdrPrevYear'
        SEL_NEXT_YEAR  = f'[data-test-id="{PFX}_arrow-next-year"], .rdrNextYear'
        SEL_PREV_MONTH = f'[data-test-id="{PFX}_arrow-prev-month"], .rdrPrevMonth'
        SEL_NEXT_MONTH = f'[data-test-id="{PFX}_arrow-next-month"], .rdrNextMonth'

        async def _read_current() -> tuple[int | None, int | None]:
            """Return (year, month) of the FIRST visible calendar month."""
            try:
                label = await self.page.locator(
                    ".rdrHeaderLabel"
                ).first.inner_text(timeout=3000)
            except Exception:
                return None, None
            # Label e.g. "May 2026 — Jun 2026" — parse only the part before the dash
            first = re.split(r"[—–-]", label)[0].strip()
            month = None
            for i, name in enumerate(MONTH_NAMES):
                if name and (name in first or name[:3] in first):
                    month = i
                    break
            ym = re.search(r"\b(20\d{2})\b", first)
            year = int(ym.group(1)) if ym else None
            return year, month

        # ── Phase 1: YEAR — double arrows ──────────────────────────────────
        for _ in range(20):
            year, _month = await _read_current()
            if year is None:
                logger.warning(
                    f"[Worker-{self.worker_id}] Cannot read calendar header (year phase)"
                )
                return
            if year == target_year:
                break
            sel = SEL_NEXT_YEAR if year < target_year else SEL_PREV_YEAR
            try:
                await self.page.locator(sel).first.click()
                await human_delay(0.2, 0.35)
            except Exception as e:
                logger.warning(f"[Worker-{self.worker_id}] Year arrow click failed: {e}")
                return

        # ── Phase 2: MONTH — single arrows ─────────────────────────────────
        for _ in range(14):
            _year, month = await _read_current()
            if month is None:
                logger.warning(
                    f"[Worker-{self.worker_id}] Cannot read calendar header (month phase)"
                )
                return
            if month == target_month:
                break
            sel = SEL_NEXT_MONTH if month < target_month else SEL_PREV_MONTH
            try:
                await self.page.locator(sel).first.click()
                await human_delay(0.2, 0.35)
            except Exception as e:
                logger.warning(f"[Worker-{self.worker_id}] Month arrow click failed: {e}")
                return

        logger.info(
            f"[Worker-{self.worker_id}] ✓ Calendar at "
            f"{MONTH_NAMES[target_month]} {target_year}"
        )

    async def _click_calendar_day(self, day: int, month: int, year: int) -> None:
        """
        Click a specific day number on the currently visible calendar.
        Uses the rdrDay buttons and verifies the day isn't passive (belongs to adjacent month).
        """
        try:
            # react-date-range uses .rdrDay buttons with .rdrDayNumber span inside
            # Passive days (from adjacent months) have .rdrDayPassive class
            day_str = str(day)

            # Strategy 1: Use aria-label which contains the full date
            # e.g., aria-label="May 13, 2026"
            MONTH_NAMES = [
                "", "January", "February", "March", "April", "May", "June",
                "July", "August", "September", "October", "November", "December"
            ]
            full_label = f"{MONTH_NAMES[month]} {day}, {year}"
            aria_locator = self.page.locator(f'button[aria-label="{full_label}"]')
            try:
                count = await aria_locator.count()
                if count > 0:
                    await aria_locator.first.click()
                    logger.debug(f"[Worker-{self.worker_id}] Clicked day via aria-label: {full_label}")
                    return
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

            # Strategy 2: Find day number spans and click the non-passive one within the correct month container
            target_month_container = None
            try:
                months = await self.page.query_selector_all(".rdrMonth")
                for m in months:
                    name_el = await m.query_selector(".rdrMonthName")
                    if name_el:
                        name_text = await name_el.inner_text()
                        target_month_name = MONTH_NAMES[month]
                        # Check full name or 3-letter abbreviation
                        if (target_month_name in name_text or target_month_name[:3] in name_text) and str(year) in name_text:
                            target_month_container = m
                            break
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

            if target_month_container:
                day_buttons = await target_month_container.query_selector_all("button.rdrDay:not(.rdrDayPassive)")
            else:
                day_buttons = await self.page.query_selector_all("button.rdrDay:not(.rdrDayPassive)")

            for btn in day_buttons:
                num_span = await btn.query_selector(".rdrDayNumber span")
                if num_span:
                    text = (await num_span.inner_text()).strip()
                    if text == day_str:
                        await btn.click()
                        logger.debug(f"[Worker-{self.worker_id}] Clicked day via DOM scan: {day_str}")
                        return

            # Strategy 3: Broadest fallback — text match
            if target_month_container:
                # Need to use javascript evaluation since we can't easily build a locator from an ElementHandle for text
                for btn in day_buttons:
                    inner_text = await btn.inner_text()
                    if inner_text.strip() == day_str:
                        await btn.click()
                        logger.debug(f"[Worker-{self.worker_id}] Clicked day via ElementHandle innerText: {day_str}")
                        return

            day_locator = self.page.locator(
                f"button.rdrDay:not(.rdrDayPassive) .rdrDayNumber span:text-is('{day_str}')"
            )
            await day_locator.first.click(timeout=3000)
            logger.debug(f"[Worker-{self.worker_id}] Clicked day via text-is: {day_str}")
        except Exception as e:
            logger.warning(
                f"[Worker-{self.worker_id}] Failed to click day {day}/{month}/{year}: {e}"
            )

    async def extract_conversations(self, limit: int = None) -> list:
        """
        Navigate to messenger, click into recent conversations, and extract FULL chat histories.
        SmarterContact uses ReactVirtualized for the list. Full chat loads on the right pane.
        URL: /messenger/inbox/all
        """
        # Resolve limit: explicit arg > self.limit > fallback 10
        limit = limit or self.limit or 10
        try:
            # Navigate directly to messenger inbox — avoids waiting on a blank post-login page
            inbox_all_url = SMARTERCONTACT_MESSENGER_URL.rstrip("/") + "/inbox/all"
            await self.page.goto(inbox_all_url, wait_until="load", timeout=30000)
            await self.page.wait_for_selector(
                "div.ReactVirtualized__Grid__innerScrollContainer, [data-test-class='messenger_nav_inbox_all_messages_row']",
                timeout=20000,
            )
            await human_delay(0.5, 1)

            logger.info(f"[Worker-{self.worker_id}] ── {self.agent_name} — starting conversation extraction")

            conversations = []
            self.extracted_data["unread_conversations"] = []
            self.extracted_data["unread_count"] = 0
            db = Database()
            await db.initialize()
            skip_unread = 0
            skip_unprocessed = 0
            skip_audited = 0

            # ── Get true unread count by visiting the Unread inbox ──────────
            # Navigate to /unread, count rows without opening any, then come back.
            inbox_all_url   = SMARTERCONTACT_MESSENGER_URL.rstrip("/") + "/inbox/all"
            inbox_unread_url = SMARTERCONTACT_MESSENGER_URL.rstrip("/") + "/inbox/unread"
            unread_count = 0
            try:
                await self.page.goto(inbox_unread_url, wait_until="load", timeout=20000)
                await human_delay(0.5, 1)

                # Wait for rows to appear (or accept 0 if inbox is empty)
                try:
                    await self.page.wait_for_selector(
                        "div.ReactVirtualized__Grid__innerScrollContainer > div",
                        timeout=6000,
                    )
                except Exception as _e:
                    logger.debug("swallowed: %r", _e)

                unread_rows = await self.page.query_selector_all(
                    "div.ReactVirtualized__Grid__innerScrollContainer > div"
                )
                unread_count = len(unread_rows)
                logger.info(
                    f"[Worker-{self.worker_id}] Unread count for {self.agent_name}: {unread_count}"
                )
            except Exception as e:
                logger.warning(f"[Worker-{self.worker_id}] Could not get unread count: {e}")

            # Always navigate to inbox/all before processing rows
            await self.page.goto(inbox_all_url, wait_until="load", timeout=20000)
            await human_delay(1, 1.5)
            self.extracted_data["unread_count"] = unread_count

            # Close support widget if it's open (it blocks clicks sometimes)
            try:
                close_btn = await self.page.query_selector("button[aria-label='Close Message']")
                if close_btn:
                    await close_btn.click()
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

            # Apply date filter before reading rows
            await self._apply_date_filter(self.date_filter)

            # Build Label Registry once per session
            await self._scrape_label_registry()

            # ── Wait for rows to appear after date filter ─────────────────────
            # The inbox re-renders after filter — wait up to 15s for at least
            # one row. If none appear within that window, log and return empty.
            row_selector = "[data-test-class='messenger_nav_inbox_all_item_messages_row']"
            fallback_row_selector = "div.ReactVirtualized__Grid__innerScrollContainer > div"

            logger.info(f"[Worker-{self.worker_id}] Waiting for conversation rows to appear...")
            try:
                await self.page.wait_for_function(
                    """() =>
                        document.querySelectorAll('[data-test-class="messenger_nav_inbox_all_messages_row"]').length > 0
                        || document.querySelectorAll('div.ReactVirtualized__Grid__innerScrollContainer > div').length > 0
                    """,
                    timeout=30000  # 30s — fail safely instead of hanging forever
                )
            except Exception as e:
                logger.error(
                    f"[Worker-{self.worker_id}] No conversation rows appeared within 30s "
                    f"for {self.agent_name} — inbox may be empty or page stuck. Error: {e}"
                )
                if SCREENSHOT_ON_ERROR:
                    await self._take_screenshot("no_rows_timeout")
                return []

            # Check which selector actually has rows
            probe = await self.page.query_selector_all(row_selector)
            if not probe:
                row_selector = fallback_row_selector

            # Labels are read directly from the DOM — no hardcoded list needed

            # ── Pass 1: Scroll & collect all unique contacts ───────────────
            # ReactVirtualized only renders ~9-20 rows at a time in the DOM.
            # We must harvest contact info DURING scrolling, not after.
            scroll_container_selector = "div.ReactVirtualized__Grid"
            scroll_container = await self.page.query_selector(scroll_container_selector)

            collected_contacts: dict[str, dict] = {}  # name → {labels, preview, is_unread, order, row_text}
            # ── Early-stop strategy ─────────────────────────────────────────
            # Stop scrolling as soon as we have collected enough TOTAL unique
            # contacts (limit * 5 buffer) OR enough plausible candidates
            # (limit * 3).  The total-contacts cap prevents runaway scrolling
            # when the account has many unprocessed/unread conversations.
            total_stop_target = limit * 10    # hard cap on contacts collected
            plausible_stop_target = limit * 6  # stop if enough "good" candidates
            plausible_count = 0

            if scroll_container:
                logger.info(
                    f"[Worker-{self.worker_id}] Scanning conversation list "
                    f"(need {limit}, collecting up to {total_stop_target} total / "
                    f"{plausible_stop_target} plausible)..."
                )
                scroll_step = 800  # larger jumps = fewer iterations
                current_scroll = 0
                stall_rounds = 0
                prev_collected_count = 0

                while stall_rounds < 3:  # 3 stalls is enough to confirm end-of-list
                    await self.page.evaluate(
                        "({ sel, top }) => { const el = document.querySelector(sel); if (el) el.scrollTop = top; }",
                        {"sel": scroll_container_selector, "top": current_scroll}
                    )
                    await asyncio.sleep(0.2)  # shorter wait — UI is fast enough

                    visible_rows = await self.page.query_selector_all(row_selector)

                    for vrow in visible_rows:
                        try:
                            p_tags = await vrow.query_selector_all("p")
                            name = (await p_tags[0].inner_text()).strip() if p_tags else ""
                            preview = (await p_tags[1].inner_text()).strip() if len(p_tags) > 1 else ""
                            if not name or name in collected_contacts:
                                continue

                            labels = await _read_labels(vrow, self.label_registry)

                            unread_badge = await vrow.query_selector(
                                'div[data-cy="messenger-avatar-icon"] > div:nth-child(2)'
                            )

                            # SmarterContact inbox-row date (MM/DD/YYYY) — shown
                            # on the conversation card alongside the audit date.
                            convo_date = _extract_row_date(await vrow.inner_text())

                            collected_contacts[name] = {
                                "labels": labels,
                                "preview": preview,
                                "is_unread": bool(unread_badge),
                                "order": len(collected_contacts),
                                "row_text": "",
                                "convo_date": convo_date,
                                "scroll_pos": current_scroll,  # remember where we found it
                            }
                            # Track plausible candidates (not unread + has real labels)
                            if not unread_badge and labels and not any(l.lower() == "extra" for l in labels) and not all(l.lower() == "new lead" for l in labels):
                                plausible_count += 1
                        except Exception:
                            continue

                    new_collected_count = len(collected_contacts)
                    if new_collected_count == prev_collected_count:
                        stall_rounds += 1
                    else:
                        stall_rounds = 0
                        logger.info(f"[Worker-{self.worker_id}] Collected {new_collected_count} unique contacts so far...")

                    prev_collected_count = new_collected_count
                    current_scroll += scroll_step

                    # Hard cap: stop scrolling once we have enough total contacts
                    if len(collected_contacts) >= total_stop_target:
                        logger.info(
                            f"[Worker-{self.worker_id}] Hard-stop: reached {len(collected_contacts)} total "
                            f"contacts collected (cap={total_stop_target}) — done scrolling"
                        )
                        break

                    # Soft cap: stop early if enough plausible candidates already found
                    if plausible_count >= plausible_stop_target:
                        logger.info(
                            f"[Worker-{self.worker_id}] Plausible-stop: {plausible_count} plausible candidates "
                            f"found (need {limit}, buffered x3) — skipping remaining scroll"
                        )
                        break

                # Scroll back to top
                await self.page.evaluate(
                    "(sel) => { const el = document.querySelector(sel); if (el) el.scrollTop = 0; }",
                    scroll_container_selector
                )
                await asyncio.sleep(0.5)
            else:
                # No scroll container — collect from visible rows only
                visible_rows = await self.page.query_selector_all(row_selector)
                for vrow in visible_rows:
                    try:
                        p_tags = await vrow.query_selector_all("p")
                        name = (await p_tags[0].inner_text()).strip() if p_tags else ""
                        preview = (await p_tags[1].inner_text()).strip() if len(p_tags) > 1 else ""
                        if not name or name in collected_contacts:
                            continue
                        labels = await _read_labels(vrow, self.label_registry)
                        unread_badge = await vrow.query_selector(
                            'div[data-cy="messenger-avatar-icon"] > div:nth-child(2)'
                        )
                        convo_date = _extract_row_date(await vrow.inner_text())
                        collected_contacts[name] = {
                            "labels": labels, "preview": preview,
                            "is_unread": bool(unread_badge),
                            "order": len(collected_contacts), "row_text": "",
                            "convo_date": convo_date,
                            "scroll_pos": 0,
                        }
                    except Exception:
                        continue

            if not collected_contacts:
                logger.warning(f"[Worker-{self.worker_id}] No conversation rows found")
                return []

            logger.info(f"[Worker-{self.worker_id}] Found {len(collected_contacts)} unique contacts — applying filters...")

            # ── Pass 2: Filter & process each contact ──────────────────────
            contacts_to_process = []
            for contact_name, info in sorted(collected_contacts.items(), key=lambda x: x[1]["order"]):
                # 1. UNREAD — never open
                if info["is_unread"]:
                    lines = [l.strip() for l in info["row_text"].strip().split("\n") if l.strip()]
                    unread_data = {
                        "contact_name": contact_name,
                        "message_preview": info["preview"],
                        "date": "", "time": "",
                        "extracted_at": get_now().isoformat(),
                    }
                    for line in lines:
                        if "/" in line and "202" in line: unread_data["date"] = line
                        if "AM" in line or "PM" in line: unread_data["time"] = line
                    self.extracted_data["unread_conversations"].append(unread_data)
                    skip_unread += 1
                    logger.info(f"[Worker-{self.worker_id}] SKIP unread: {contact_name}")
                    continue

                # 2. UNPROCESSED — no label, only "New Lead", or has "Extra" label
                assigned_labels = info["labels"]
                _skip_any_labels = {"extra"}
                _skip_only_labels = {"new lead"}
                has_skip_any = any(l.lower() in _skip_any_labels for l in assigned_labels)
                is_processed = (
                    assigned_labels
                    and not has_skip_any
                    and not all(l.lower() in _skip_only_labels for l in assigned_labels)
                )
                if not is_processed:
                    skip_unprocessed += 1
                    logger.info(f"[Worker-{self.worker_id}] SKIP unprocessed (label={assigned_labels or 'none'}): {contact_name}")
                    continue

                # 3. Already AUDITED
                if await db.is_chat_audited(self.email, contact_name, info["preview"]):
                    skip_audited += 1
                    logger.info(f"[Worker-{self.worker_id}] SKIP already audited: {contact_name}")
                    continue

                contacts_to_process.append((contact_name, info))

            actual_count = min(len(contacts_to_process), limit)
            logger.info(
                f"[Worker-{self.worker_id}] {actual_count} of {len(contacts_to_process)} contacts to extract "
                f"(limit={limit}, skip_unread={skip_unread}, skip_unprocessed={skip_unprocessed}, skip_audited={skip_audited})"
            )

            for idx, (contact_name, info) in enumerate(contacts_to_process):
                if idx >= limit:
                    break
                try:
                    assigned_labels = info["labels"]
                    message_preview = info["preview"]

                    # Scroll the virtualized list to find this contact's row.
                    # Start near where we saw it during collection to avoid re-scrolling the whole list.
                    row = await self._scroll_to_contact(
                        scroll_container_selector, row_selector, contact_name,
                        hint_scroll=info.get("scroll_pos", 0),
                    )
                    if not row:
                        logger.warning(f"[Worker-{self.worker_id}] Could not find {contact_name} in list — skipping")
                        continue

                    logger.info(
                        f"[Worker-{self.worker_id}] Opening thread {idx+1}/{actual_count}: "
                        f"{contact_name} (labels: {assigned_labels})"
                    )

                    await row.scroll_into_view_if_needed()
                    try:
                        await row.click(timeout=5000)
                    except Exception:
                        await row.click(timeout=5000, force=True)
                    await human_delay(0.5, 1)

                    # Close any open calendar/popover that might be blocking the chat panel
                    try:
                        await self.page.keyboard.press("Escape")
                        await human_delay(0.3, 0.5)
                    except Exception as _e:
                        logger.debug("swallowed: %r", _e)

                    # Extract Chat Transcript
                    try:
                        full_thread_data = {
                            "contact_name": contact_name,
                            "assigned_labels": assigned_labels,
                            "index": idx,
                            "extracted_at": get_now().isoformat(),
                            "convo_date": info.get("convo_date", ""),
                            "full_transcript": ""
                        }

                        panel_selector = 'div[data-test-id="messenger_nav_inbox_all_contact-panel_messages"]'

                        try:
                            chat_panel = await self.page.wait_for_selector(panel_selector, timeout=15000)
                        except Exception as e:
                            logger.warning(f"[Worker-{self.worker_id}] Primary panel selector timed out (15s): {e}")
                            # Take screenshot to debug what's on the page
                            if SCREENSHOT_ON_ERROR:
                                await self._take_screenshot(f"chat_panel_timeout_{contact_name[:10]}")
                            try:
                                chat_panel = await self.page.wait_for_selector(
                                    '[data-test-id*="contact-panel"]', timeout=8000
                                )
                            except Exception:
                                logger.warning(f"[Worker-{self.worker_id}] Fallback selector also failed for {contact_name}")
                                chat_panel = None

                        # Note: Message elements may be in various HTML structures
                        # We extract ALL text via inner_text() below, so selector check is optional
                        # This is just for debug logging; actual extraction happens on line 722+
                        msg_selector = f'{panel_selector} p'  # Try to find paragraphs, but don't fail if not found
                        try:
                            await self.page.wait_for_selector(msg_selector, timeout=5000)
                            logger.debug(f"[Worker-{self.worker_id}] Found message paragraphs for {contact_name}")
                        except Exception:
                            logger.debug(f"[Worker-{self.worker_id}] No <p> selector match for {contact_name} (using inner_text extraction)")

                        if chat_panel:
                            full_text = await chat_panel.inner_text()
                            cleaned_lines = [line.strip() for line in full_text.split('\n') if line.strip()]
                            full_thread_data["full_transcript"] = "\n".join(cleaned_lines)
                        else:
                            logger.warning(
                                f"[Worker-{self.worker_id}] No chat panel for {contact_name} — "
                                f"skipping to avoid UI-text contamination"
                            )
                            continue

                        side_map: dict[str, bool] = {}
                        try:
                            side_map = await self.page.evaluate(
                                """(sel) => {
                                    const panel = document.querySelector(sel);
                                    if (!panel) return {};
                                    const pr = panel.getBoundingClientRect();
                                    const cx = pr.left + pr.width / 2;
                                    const timeRe = /^\\d{1,2}:\\d{2}\\s*(AM|PM)$/i;
                                    const dateRe = /^(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),/i;
                                    const campRe = /^Sent from campaign:/i;
                                    const out = {};

                                    // Helper: walk up the DOM tree (max 8 levels) to find the
                                    // message bubble container and determine if it's agent-side.
                                    function isAgentBubble(el) {
                                        let node = el;
                                        for (let i = 0; i < 8 && node && node !== panel; i++) {
                                            const cs = getComputedStyle(node);
                                            const bg = cs.backgroundColor || '';

                                            // Signal 1: Background color.
                                            // SmarterContact uses blue (#2563EB-ish) for campaign
                                            // messages and green (#22C55E-ish) for agent replies.
                                            // Contact bubbles are gray/white/transparent.
                                            if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
                                                // Parse RGB values
                                                const m = bg.match(/\\d+/g);
                                                if (m && m.length >= 3) {
                                                    const [r, g, b] = m.map(Number);
                                                    // Blue-ish (agent campaign): high blue, low red
                                                    if (b > 180 && r < 100 && g < 150) return true;
                                                    // Green-ish (agent reply): high green
                                                    if (g > 160 && r < 100 && b < 100) return true;
                                                    // Teal/cyan agent variants
                                                    if (b > 150 && g > 150 && r < 80) return true;
                                                    // Gray/white = contact (r≈g≈b, all high)
                                                    if (r > 200 && g > 200 && b > 200) return false;
                                                    // Medium gray = contact
                                                    if (Math.abs(r-g) < 20 && Math.abs(g-b) < 20 && r > 150) return false;
                                                }
                                            }

                                            // Signal 2: CSS alignment (flex).
                                            // Agent messages use margin-left:auto or align-self:flex-end
                                            if (cs.marginLeft === 'auto' || cs.alignSelf === 'flex-end') return true;
                                            if (cs.marginRight === 'auto' || cs.alignSelf === 'flex-start') return false;

                                            // Signal 3: text-align right on wrapper
                                            if (cs.textAlign === 'right') return true;

                                            node = node.parentElement;
                                        }

                                        // Signal 4: Check for checkmark SVG near the element
                                        // (SmarterContact shows ✓ on sent messages)
                                        let sibling = el.parentElement;
                                        if (sibling) {
                                            const svgs = sibling.querySelectorAll('svg');
                                            for (const svg of svgs) {
                                                // Check marks are typically small SVGs near timestamps
                                                const sr = svg.getBoundingClientRect();
                                                if (sr.width > 0 && sr.width < 20) return true;
                                            }
                                        }

                                        // Signal 5: Fallback — position-based. 
                                        // Agent bubbles are right-aligned, so their right edge is very close 
                                        // to the panel's right edge (typically past 80% of panel width).
                                        // Contact bubbles are left-aligned.
                                        const r = el.getBoundingClientRect();
                                        if (r.width > 0) {
                                            // Check distance from right edge
                                            const distFromRight = pr.right - r.right;
                                            const distFromLeft = r.left - pr.left;
                                            
                                            // If it's much closer to the right edge than the left, it's agent.
                                            // Especially useful for wide messages.
                                            if (distFromRight < distFromLeft && distFromRight < pr.width * 0.2) return true;
                                            // If it's much closer to the left edge, it's contact.
                                            if (distFromLeft < distFromRight && distFromLeft < pr.width * 0.2) return false;
                                            
                                            // Hard threshold for left edge if not clearly snapped to a side
                                            if (r.width < pr.width * 0.8) {
                                                if (r.left > (pr.left + pr.width * 0.4)) return true;
                                                return false;
                                            }
                                        }

                                        return null;  // unknown
                                    }

                                    // Walk leaf text nodes. Text can be directly in a div, p, or span.
                                    // To avoid grabbing the entire panel's text at once, we only look at elements
                                    // that don't contain block-level children (div, p, ul, li).
                                    const candidates = panel.querySelectorAll('p, span, div');
                                    for (const el of candidates) {
                                        // Skip containers that hold other potential message wrappers
                                        if (el.querySelector('p, div, ul')) continue;

                                        const t = (el.innerText || '').trim();
                                        if (!t || t.length < 1) continue;
                                        if (timeRe.test(t) || dateRe.test(t) || campRe.test(t)) continue;

                                        const r = el.getBoundingClientRect();
                                        if (r.width === 0 && r.height === 0) continue;

                                        const isRight = isAgentBubble(el);
                                        if (isRight === null) continue;

                                        const lines = t.split('\\n');
                                        for (const line of lines) {
                                            const lt = line.trim();
                                            if (lt && lt.length >= 1 && !timeRe.test(lt) && !(lt in out)) {
                                                out[lt] = isRight;
                                            }
                                        }
                                    }
                                    return out;
                                }""",
                                panel_selector,
                            )
                            # Log side_map stats for debugging
                            if side_map:
                                agent_count = sum(1 for v in side_map.values() if v)
                                contact_count = sum(1 for v in side_map.values() if not v)
                                logger.info(
                                    f"[Worker-{self.worker_id}] side_map for {contact_name}: "
                                    f"{len(side_map)} entries (agent={agent_count}, contact={contact_count})"
                                )
                                # Dump all entries for debugging
                                for text, is_agent in side_map.items():
                                    label = "AGENT" if is_agent else "CONTACT"
                                    logger.info(f"  side_map [{label}]: {text[:80]}")
                                if agent_count > 0 and contact_count == 0 and len(side_map) > 2:
                                    logger.warning(
                                        f"[Worker-{self.worker_id}] side_map ALL agent for {contact_name} — "
                                        f"likely bad DOM detection, clearing side_map"
                                    )
                                    side_map = {}
                            else:
                                logger.info(f"[Worker-{self.worker_id}] side_map empty for {contact_name}")


                        except Exception as e:
                            logger.debug(f"[Worker-{self.worker_id}] DOM side_map failed: {e}")


                        full_thread_data["parsed_messages"] = parse_transcript(
                            full_thread_data["full_transcript"],
                            agent_name=self.agent_name,
                            side_map=side_map or None,
                        )

                        conversations.append(full_thread_data)

                        try:
                            await db.mark_chat_audited(self.email, contact_name, message_preview)
                        except Exception as db_err:
                            logger.error(f"[Worker-{self.worker_id}] DB save failed: {db_err}")

                    except Exception as e:
                        logger.error(f"[Worker-{self.worker_id}] Chat panel extraction failed: {e}")

                    await human_delay(0.5, 1)

                except Exception as e:
                    logger.error(f"[Worker-{self.worker_id}] Error processing {contact_name}: {e}")
                    continue

            self.extracted_data["conversations"] = conversations
            await db.close()

            logger.info(
                f"[Worker-{self.worker_id}] ── {self.agent_name} DONE | "
                f"grabbed={len(conversations)} | "
                f"skip_unread={skip_unread} | "
                f"skip_unprocessed={skip_unprocessed} | "
                f"skip_audited={skip_audited} | "
                f"unread_inbox={len(self.extracted_data['unread_conversations'])}"
            )
            return conversations

        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Error extracting conversations: {e}")
            if SCREENSHOT_ON_ERROR:
                await self._take_screenshot("conversations_error")
            return []

    async def _scroll_to_contact(
        self, container_sel: str, row_sel: str, target_name: str,
        max_scrolls: int = 30, hint_scroll: int = 0,
    ):
        """
        Scroll the virtualized list to find and return a row matching target_name.
        hint_scroll: the scrollTop we recorded when we first saw this contact —
        jump there first for speed, then fall back to a full scan if the list
        has reordered since Pass 1 (e.g. new messages arrived).
        """
        async def _scan_from(start_pos: int, steps: int, sleep_s: float = 0.2):
            await self.page.evaluate(
                "({ sel, top }) => { const el = document.querySelector(sel); if (el) el.scrollTop = top; }",
                {"sel": container_sel, "top": start_pos}
            )
            await asyncio.sleep(sleep_s)
            for step in range(steps):
                rows = await self.page.query_selector_all(row_sel)
                for row in rows:
                    try:
                        p_tags = await row.query_selector_all("p")
                        if p_tags:
                            name = (await p_tags[0].inner_text()).strip()
                            if name == target_name:
                                return row
                    except Exception:
                        continue
                await self.page.evaluate(
                    "({ sel, top }) => { const el = document.querySelector(sel); if (el) el.scrollTop = top; }",
                    {"sel": container_sel, "top": start_pos + (step + 1) * 800}
                )
                await asyncio.sleep(sleep_s)
            return None

        # Fast path: jump near the recorded hint position
        row = await _scan_from(max(0, hint_scroll - 800), max_scrolls)
        if row:
            return row

        # Fallback: full scan from top — handles list reorder since Pass 1
        # 100 steps × 800px = 80,000px, covers any realistic inbox size
        logger.debug(
            f"[Worker-{self.worker_id}] Hint miss for {target_name} "
            f"(hint={hint_scroll}) — falling back to full list scan"
        )
        return await _scan_from(0, 100, sleep_s=0.15)

    async def extract_all(self) -> dict:
        """
        Run the full extraction pipeline:
        1. Login
        2. Extract conversations (skips reporting page — not used for scoring)
        3. Logout

        Returns all extracted data.
        """
        result = {
            "agent_name": self.agent_name,
            "email": self.email,
            "worker_id": self.worker_id,
            "started_at": get_now().isoformat(),
            "status": "pending",
            "reporting": {},
            "conversations": [],
            "unread_conversations": [],
            "errors": [],
        }

        try:
            # Step 1: Login
            login_ok = await self.login()
            if not login_ok:
                result["status"] = "login_failed"
                result["errors"].append("Failed to login after maximum retries")
                return result

            # Step 2: Extract conversations (which also fills unread_conversations + unread_count)
            logger.info(f"[Worker-{self.worker_id}] Extraction params: date_filter={self.date_filter}, limit={self.limit}")
            result["conversations"] = await self.extract_conversations(limit=self.limit)
            result["unread_conversations"] = self.extracted_data.get("unread_conversations", [])
            result["unread_count"] = self.extracted_data.get("unread_count", 0)

            # Step 4: Logout cleanly
            await self._logout()

            result["status"] = "success"
            result["completed_at"] = get_now().isoformat()

        except Exception as e:
            result["status"] = "error"
            result["errors"].append(str(e))
            logger.error(f"[Worker-{self.worker_id}] Pipeline error for {self.agent_name}: {e}")

        return result

    async def close(self):
        """Clean up browser resources."""
        try:
            if self.context:
                await self.context.close()
            if self.browser:
                await self.browser.close()
            logger.info(f"[Worker-{self.worker_id}] Browser closed for {self.agent_name}")
        except Exception as e:
            logger.error(f"[Worker-{self.worker_id}] Error closing browser: {e}")

    # ─── Private Helpers ────────────────────────────────────

    async def _is_logged_in(self) -> bool:
        """Check if we are successfully logged in using robust application markers."""
        try:
            # Indicator 1: Top Navigation Bar elements (highly specific to SC shell)
            robust_selectors = [
                "button.notifications-button",      # The bell icon
                ".chakra-avatar__initials",         # User's circular avatar
                "button[aria-label='Settings']",    # Settings gear
                "a[href='/messenger']",             # Messenger link in top nav
                "button.user-dropdown-button",      # User profile dropdown
            ]
            
            for sel in robust_selectors:
                try:
                    elem = await self.page.query_selector(sel)
                    if elem and await elem.is_visible():
                        return True
                except Exception:
                    continue

            # Indicator 2: Explicit headers
            main_header = await self.page.query_selector("h1, h2, h3")
            if main_header:
                header_text = (await main_header.inner_text()).lower()
                if "inbox" in header_text or "messenger" in header_text:
                    return True

            # Factor 3: Sidebar presence + URL
            if "/messenger" in self.page.url:
                side_nav = await self.page.query_selector('nav, aside, [role="navigation"]')
                if side_nav:
                    return True
                
            return False
        except Exception:
            return False


    # Known SmarterContact label names — inbox rows only show truncated versions
    # like "Not Int..." but the real labels are always one of these. Used to resolve
    # truncation via prefix match in _read_labels.
    KNOWN_LABELS = [
        # Drip campaigns
        "AP drip",
        "WL drip",
        "HL drip",
        # Follow-up stages
        "FU1",
        "FU2",
        "FU3",
        "No Reply Follow up",
        "Follow Up",
        "Call back",
        # Lead status
        "New Lead",
        "Lead",
        "Lead, Pushed",
        "Pushed to client",
        "Potential",
        # Interest / Response
        "Not interested",
        "Not Interested",
        "Hung up",
        "Left voicemail",
        "No answer",
        "Agent untouched yet",
        "Stop Responding",
        "Stopped Responding",
        "Missed Call",
        # Decision
        "Decision Maker",
        "Hot",
        "Sold",
        "Listed",
        "Deal closed",
        "Disqualified",
        # Other statuses
        "Wrong Number",
        "Wrong Message",
        "Do Not Call",
        "DO NOT CALL",
        "Bluffer",
        "Abv MV",
        "Verified",
        "Duplicate",
        "Undefined",
        "Investor",
        "Maybe later",
    ]

    async def _scrape_label_registry(self):
        """
        Build the Label Registry used to resolve truncated inbox labels
        (e.g. "Not Int..." → "Not Interested").

        SmarterContact's inbox rows show labels as truncated Chakra tags with no
        hover/title attribute. The full names aren't rendered anywhere in the DOM
        on the inbox page — so we combine:
          1. A hardcoded list of known labels (KNOWN_LABELS)
          2. Any full-text labels found in the inbox buttons (non-truncated ones)
        """
        logger.info(f"[Worker-{self.worker_id}] Building Label Registry...")
        try:
            registry = list(self.KNOWN_LABELS)

            # Pick up any non-truncated labels visible on the page (e.g., "Verified", "FU1")
            try:
                tag_texts = await self.page.evaluate("""() => {
                    const tags = document.querySelectorAll('span.chakra-tag__label');
                    const out = new Set();
                    tags.forEach(t => {
                        const s = (t.innerText || '').trim();
                        if (s && !s.endsWith('...') && !s.endsWith('…')) out.add(s);
                    });
                    return Array.from(out);
                }""")
                for name in tag_texts:
                    if name and name not in registry:
                        registry.append(name)
            except Exception as _e:
                logger.debug("swallowed: %r", _e)

            # Deduplicate while preserving order (longer entries first for prefix match)
            seen = set()
            self.label_registry = sorted(
                [x for x in registry if not (x in seen or seen.add(x))],
                key=lambda s: -len(s),
            )
            logger.info(
                f"[Worker-{self.worker_id}] Registry built: {len(self.label_registry)} full labels "
                f"({', '.join(self.label_registry[:5])}...)"
            )
        except Exception as e:
            logger.warning(f"[Worker-{self.worker_id}] Could not build Label Registry: {e}")
            self.label_registry = list(self.KNOWN_LABELS)


    async def _logout(self):
        """Attempt to log out cleanly."""
        try:
            logout_selectors = [
                'a:has-text("Logout")',
                'a:has-text("Log Out")',
                'button:has-text("Logout")',
                'a:has-text("Sign Out")',
                '[href*="logout"]',
                '.logout-btn',
            ]
            for sel in logout_selectors:
                try:
                    elem = await self.page.query_selector(sel)
                    if elem and await elem.is_visible():
                        await human_click(self.page, sel)
                        await human_delay(1, 2)
                        logger.info(f"[Worker-{self.worker_id}] Logged out {self.agent_name}")
                        return
                except Exception:
                    continue

            # If no logout button found, just clear cookies
            await self.context.clear_cookies()
            logger.info(f"[Worker-{self.worker_id}] Cleared cookies for {self.agent_name}")

        except Exception as e:
            logger.warning(f"[Worker-{self.worker_id}] Logout issue: {e}")

    async def _safe_extract_text(self, selector: str) -> str:
        """Safely extract text from an element, return empty string if not found."""
        try:
            for sel in selector.split(","):
                sel = sel.strip()
                elem = await self.page.query_selector(sel)
                if elem:
                    text = await elem.inner_text()
                    return text.strip()
        except Exception as _e:
            logger.debug("swallowed: %r", _e)
        return ""

    async def _extract_metric_by_label(self, label: str) -> str:
        """
        Extract a metric value by its label text from the reporting page.
        SmarterContact displays metrics as label + value in stat card divs.
        """
        try:
            # Strategy 1: Find element containing the label, get its parent's text
            locator = self.page.locator(f"text='{label}'").first
            parent = locator.locator("..")
            text = await parent.inner_text(timeout=3000)

            # Parse: the value is typically on a separate line from the label
            lines = [l.strip() for l in text.split("\n") if l.strip()]
            for line in lines:
                if line != label and line:
                    return line

        except Exception as _e:
            logger.debug("swallowed: %r", _e)

        try:
            # Strategy 2: Use XPath to find sibling/child with numeric content
            elements = await self.page.query_selector_all("p, span, div")
            found_label = False
            for elem in elements:
                text = await elem.inner_text()
                if label in text:
                    found_label = True
                    continue
                if found_label and text.strip():
                    return text.strip()
        except Exception as _e:
            logger.debug("swallowed: %r", _e)

        return ""

    async def _take_screenshot(self, name: str):
        """Take a screenshot for debugging."""
        try:
            timestamp = get_now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.agent_name}_{name}_{timestamp}.png"
            filepath = self._screenshot_dir / filename
            await self.page.screenshot(path=str(filepath), full_page=True)
            logger.info(f"[Worker-{self.worker_id}] Screenshot saved: {filepath}")
        except Exception as e:
            logger.warning(f"[Worker-{self.worker_id}] Screenshot failed: {e}")
