"""Shared browser automation utilities for zendriver-based brokers."""

import os
import asyncio
import logging

from zendriver import Browser

logger = logging.getLogger(__name__)

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/129.0.0.0 Safari/537.36"
)


async def create_browser(headless=None, user_agent=DEFAULT_USER_AGENT, browser_args=None):
    """Create a zendriver browser instance with standard configuration.

    Args:
        headless: Run in headless mode. If None, reads HEADLESS env var (default true).
        user_agent: User agent string. Pass None to use browser default.
        browser_args: Additional browser arguments to pass through.

    Returns:
        Browser instance.
    """
    if headless is None:
        headless = os.getenv("HEADLESS", "true").lower() == "true"

    args = list(browser_args or [])
    if user_agent:
        args.append(f"--user-agent={user_agent}")

    browser_path = os.getenv("BROWSER_PATH", "/usr/bin/helium-browser")
    return await Browser.create(
        browser_args=args,
        headless=headless,
        browser_executable_path=browser_path,
    )


async def stop_browser(browser, log=None):
    """Stop a browser instance with proper cleanup.

    Handles CancelledError and other exceptions during shutdown.

    Args:
        browser: Browser instance to stop. No-op if None.
        log: Optional logger for debug messages on errors.
    """
    if not browser:
        return
    try:
        await asyncio.shield(browser.stop())
    except BaseException as e:
        if not isinstance(e, asyncio.CancelledError) and log:
            log.debug("Error stopping browser: %s", e, exc_info=e)


async def get_page_url(page) -> str:
    """Get current page URL via JS evaluation with property fallback.

    Uses evaluate("window.location.href") for real-time accuracy since
    page.url can return stale cached target info.
    """
    try:
        result = await page.evaluate("window.location.href")
        return result if isinstance(result, str) else (getattr(page, "url", None) or "")
    except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
        return getattr(page, "url", None) or ""


async def get_page_title(page) -> str:
    """Get current page title via JS evaluation with empty-string fallback."""
    try:
        result = await page.evaluate("document.title")
        return result if isinstance(result, str) else ""
    except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError):
        return ""


async def wait_for_ready_state(page, state="complete", timeout=10):
    """Wait for document.readyState to reach the target state.

    Args:
        page: Page to check.
        state: Target readyState ("loading", "interactive", "complete").
        timeout: Max seconds to wait.
    """
    states = ["loading", "interactive", "complete"]
    target = states.index(state)

    for _ in range(timeout * 4):  # Poll every 0.25s
        try:
            current = await page.evaluate("document.readyState")
            if current and states.index(current) >= target:
                return
        except (asyncio.TimeoutError, AttributeError, RuntimeError, TypeError, ValueError):
            pass
        await asyncio.sleep(0.25)


async def navigate_and_wait(page, url, timeout=10):
    """Navigate to a URL and wait for the page to fully load.

    Replaces the common pattern: page.get(url) + asyncio.sleep(2-3).
    Use only for standard page loads, NOT SPA hash-route navigations.

    Args:
        page: Page to navigate.
        url: Target URL.
        timeout: Max seconds to wait for readyState "complete".
    """
    await page.get(url)
    await wait_for_ready_state(page, timeout=timeout)


async def poll_for_condition(check_fn, timeout=120, interval=2):
    """Poll until an async condition is met or timeout expires.

    Args:
        check_fn: Async callable returning True when done.
        timeout: Max seconds to poll.
        interval: Seconds between checks.

    Returns:
        True if condition was met, False on timeout.
    """
    iterations = int(timeout / interval)
    for _ in range(iterations):
        try:
            if await check_fn():
                return True
        except Exception:
            pass
        await asyncio.sleep(interval)
    return False
