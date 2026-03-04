"""Chrome CDP connection helper.

CRITICAL rules (from architecture doc):
- Always connect to 127.0.0.1:9222 -- NOT localhost (Windows resolves
  localhost to IPv6 ::1 but Chrome binds on IPv4).
- Never call browser.close() -- we are attached to the user's running Chrome;
  closing it would kill their login sessions.
- Never use launch_persistent_context -- opens a new window, breaks sessions.
- Retry CDP connect up to 10 times (3s apart) to handle Chrome startup lag.
"""

import logging
import time

from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

log = logging.getLogger(__name__)

CDP_URL = "http://127.0.0.1:9222"
_RETRY_ATTEMPTS = 10
_RETRY_DELAY = 3

_playwright = None
_browser: Browser | None = None


def _connect_cdp_with_retry(cdp_url: str = CDP_URL) -> Browser:
    global _playwright
    if _playwright is None:
        _playwright = sync_playwright().start()

    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            browser = _playwright.chromium.connect_over_cdp(cdp_url)
            log.info("Connected to Chrome at %s (attempt %d)", cdp_url, attempt)
            return browser
        except Exception as exc:
            if attempt < _RETRY_ATTEMPTS:
                log.warning(
                    "CDP connect attempt %d/%d failed (%s) — retrying in %ds ...",
                    attempt, _RETRY_ATTEMPTS, exc, _RETRY_DELAY,
                )
                time.sleep(_RETRY_DELAY)
            else:
                raise RuntimeError(
                    f"Could not connect to Chrome on {cdp_url} after {_RETRY_ATTEMPTS} attempts.\n"
                    "Make sure Chrome is running with --remote-debugging-port=9222.\n"
                    f"Last error: {exc}"
                ) from exc


def get_browser(cdp_url: str = CDP_URL) -> Browser:
    """Return a (cached) Playwright browser connected via CDP.

    If the previous connection went stale (Chrome restarted, etc.)
    we transparently reconnect instead of failing forever.
    """
    global _browser
    if _browser is not None:
        try:
            # Quick liveness check — will throw if CDP link is dead
            _browser.contexts
        except Exception:
            log.warning("CDP connection stale — reconnecting ...")
            _browser = None
    if _browser is None:
        _browser = _connect_cdp_with_retry(cdp_url)
    return _browser


def get_context(cdp_url: str = CDP_URL) -> BrowserContext:
    """Return the first browser context (the user's normal logged-in profile)."""
    browser = get_browser(cdp_url)
    contexts = browser.contexts
    if contexts:
        return contexts[0]
    return browser.new_context()


def new_page(cdp_url: str = CDP_URL) -> Page:
    """Open a new tab in the connected Chrome session."""
    return get_context(cdp_url).new_page()


def close_all():
    """Stop Playwright on app exit. Does NOT close Chrome itself."""
    global _playwright, _browser
    # Do NOT call _browser.close() -- that would kill the user's Chrome session
    _browser = None
    if _playwright:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None
