"""Chrome CDP connection helper.

Connects to an already-running Chrome instance on the debug port so we can
reuse the user's existing Google / TikTok login sessions.
"""

import logging
from playwright.sync_api import sync_playwright, Browser, BrowserContext, Page

log = logging.getLogger(__name__)

_playwright = None
_browser: Browser | None = None


def get_browser(cdp_url: str = "http://localhost:9222") -> Browser:
    """Return a (cached) Playwright browser connected via CDP."""
    global _playwright, _browser
    if _browser is None:
        _playwright = sync_playwright().start()
        _browser = _playwright.chromium.connect_over_cdp(cdp_url)
        log.info("Connected to Chrome at %s", cdp_url)
    return _browser


def get_context(cdp_url: str = "http://localhost:9222") -> BrowserContext:
    """Return the first browser context (the user's normal profile)."""
    browser = get_browser(cdp_url)
    contexts = browser.contexts
    if contexts:
        return contexts[0]
    return browser.new_context()


def new_page(cdp_url: str = "http://localhost:9222") -> Page:
    """Open a new tab in the connected Chrome session."""
    ctx = get_context(cdp_url)
    page = ctx.new_page()
    return page


def close_all():
    """Tear down Playwright (called on app exit)."""
    global _playwright, _browser
    if _browser:
        try:
            _browser.close()
        except Exception:
            pass
        _browser = None
    if _playwright:
        try:
            _playwright.stop()
        except Exception:
            pass
        _playwright = None
