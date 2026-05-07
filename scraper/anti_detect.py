"""
Anti-detection module for browser automation.
Implements human-like behavior to avoid bot detection on SmarterContact.
"""
import asyncio
import random
from config.settings import MIN_DELAY, MAX_DELAY, VIEWPORTS, USER_AGENTS


async def human_delay(min_sec: float = None, max_sec: float = None):
    """Wait a random amount of time to mimic human behavior."""
    low = min_sec or MIN_DELAY
    high = max_sec or MAX_DELAY
    delay = random.uniform(low, high)
    await asyncio.sleep(delay)


async def human_type(page, selector: str, text: str, clear_first: bool = True):
    """
    Type text character-by-character with random delays between keystrokes.
    Mimics natural human typing speed.
    """
    if clear_first:
        await page.click(selector, click_count=3)  # Select all
        await page.keyboard.press("Backspace")
        await human_delay(0.3, 0.6)

    for char in text:
        await page.type(selector, char, delay=random.randint(50, 150))

    await human_delay(0.5, 1.0)


async def human_click(page, selector: str):
    """Click with slight random offset and delay to mimic human clicks."""
    element = await page.query_selector(selector)
    if element:
        box = await element.bounding_box()
        if box:
            # Click with small random offset from center
            x = box["x"] + box["width"] / 2 + random.uniform(-3, 3)
            y = box["y"] + box["height"] / 2 + random.uniform(-3, 3)
            await page.mouse.move(x, y, steps=random.randint(5, 15))
            await human_delay(0.2, 0.5)
            await page.mouse.click(x, y)
        else:
            await page.click(selector)
    else:
        await page.click(selector)

    await human_delay(0.5, 1.5)


async def random_mouse_movement(page):
    """Perform random mouse movements to look more human."""
    for _ in range(random.randint(2, 5)):
        x = random.randint(100, 1200)
        y = random.randint(100, 700)
        await page.mouse.move(x, y, steps=random.randint(10, 25))
        await asyncio.sleep(random.uniform(0.1, 0.3))


async def random_scroll(page):
    """Perform random scrolling to mimic human browsing."""
    scroll_amount = random.randint(100, 400)
    direction = random.choice([1, -1])
    await page.mouse.wheel(0, scroll_amount * direction)
    await human_delay(0.5, 1.5)


def get_random_viewport() -> dict:
    """Return a random viewport size to mimic different devices."""
    return random.choice(VIEWPORTS)


def get_random_user_agent() -> str:
    """Return a random user agent string."""
    return random.choice(USER_AGENTS)


def get_stealth_context_options() -> dict:
    """
    Return browser context options that help avoid bot detection.
    Each worker gets a unique combination of viewport + user agent.
    """
    viewport = get_random_viewport()
    user_agent = get_random_user_agent()

    return {
        "viewport": viewport,
        "user_agent": user_agent,
        "locale": "en-US",
        "timezone_id": "America/New_York",
        "color_scheme": "light",
        "java_script_enabled": True,
        "bypass_csp": False,
        "ignore_https_errors": False,
        "extra_http_headers": {
            "Accept-Language": "en-US,en;q=0.9",
            "Accept-Encoding": "gzip, deflate, br",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "sec-ch-ua": '"Chromium";v="131", "Not:A-Brand";v="24"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
        },
    }
