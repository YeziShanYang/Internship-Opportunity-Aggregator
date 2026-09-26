"""Render a page in headless Chromium, for the pages a plain GET cannot read.

Two callers. `enrich.postings.fetch_many` uses `render_many` as the fallback for a
posting page that came back as a JavaScript shell, and `gather.page` uses
`render_page` for a watched `page_text` row marked `render_js=true` -- a Wix or React
site whose text only exists after scripts run (Duke's trading competition measured 942
characters at ratio 0.001 on every page of its site, 2026-09-26). It lives in `gather`
because it is network I/O; `enrich` may import down to it.

For postings it is only ever the fallback: most
posting pages answer a plain GET with their full text, and a browser costs a second or
two a page where httpx costs milliseconds. Measured 2026-09-26 on five zshah-2027
postings from that morning's digest:

    host                  plain GET    rendered
    Workday               0 chars      7,457 chars  2.1s
    Ashby                 134 chars    3,808 chars  1.3s
    SmartRecruiters       2,926        (not needed)
    Rippling              6,378        (not needed)
    Greenhouse            11,017       (not needed)

Before this, every zshah row reached the model with a title and no requirements, and
all 14 of them printed "low confidence, kept deliberately" with no deadline or year.

Two properties this module must keep:

* **Honest, and anonymous.** The browser presents `paths.USER_AGENT` -- an automated
  tracker, never a Chrome string -- and nothing that identifies the owner: a fresh
  profile with no cookies, UTC rather than the laptop's timezone, a generic locale. The
  rule against defeating a block still holds; a site that refuses this client stays a
  fetch failure.
* **Optional.** No playwright, no Chromium, a browser that will not launch: every URL
  comes back as an error, which degrades to "judged on the row alone" exactly as a
  failed plain fetch does. A missing dependency must never become a relevance decision.
"""
from __future__ import annotations

import time

from core import paths, text as coretext

PAGE_TIMEOUT_MS = 30_000
# Workday keeps a socket open long after the posting has rendered, so waiting for true
# network idle would spend the whole timeout on every page. Wait a bounded while, then
# read whatever has rendered.
SETTLE_TIMEOUT_MS = 8_000
# Nothing a relevance judgment reads is in an image, and skipping them roughly halves
# the bytes per page.
_SKIPPED_RESOURCES = {"image", "media", "font"}


def render_many(urls: list[str], deadline: float) -> dict[str, tuple[str, str]]:
    """{url: (text, error)}, one at a time, until `deadline` (a `time.monotonic()`).

    Sequential on purpose. Playwright's sync API may not be shared across threads, and
    the set this receives is small -- the JavaScript shells left over after the plain
    fetch, about half of one morning's zshah rows.
    """
    if not urls:
        return {}
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return {u: ("", "JavaScript page, and playwright is not installed") for u in urls}

    results: dict[str, tuple[str, str]] = {}
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = _context(browser)
                for url in urls:
                    if time.monotonic() > deadline:
                        results[url] = ("", "skipped: the run's total fetch budget was exhausted")
                        continue
                    results[url] = _render(context, url)
            finally:
                browser.close()
    except Exception as exc:  # a browser that will not launch is an error per URL
        for url in urls:
            results.setdefault(url, ("", f"browser unavailable: {type(exc).__name__}: {exc}"))
    return results


def render_page(url: str) -> tuple[str, str, int, str]:
    """(rendered_html, final_url, status, error) for one watched page. Never raises.

    Same honest, anonymous context as `render_many`. HTML rather than text, because the
    page pipeline normalises and diffs the markup itself, and `final_url` because where
    a fetch landed is part of whether it worked (`process.redirect`).
    """
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return "", url, 0, "render_js=true, but playwright is not installed"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            try:
                context = _context(browser)
                page = context.new_page()
                response = page.goto(url, wait_until="domcontentloaded",
                                     timeout=PAGE_TIMEOUT_MS)
                status = response.status if response is not None else 0
                if status >= 400:
                    return "", page.url, status, f"browser: HTTP {status}"
                try:
                    page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
                except Exception:
                    pass
                return page.content(), page.url, status or 200, ""
            finally:
                browser.close()
    except Exception as exc:
        return "", url, 0, f"browser: {type(exc).__name__}: {exc}"


def _context(browser):
    context = browser.new_context(
        user_agent=paths.USER_AGENT, locale="en-US", timezone_id="UTC")
    context.route("**/*", lambda route: route.abort()
                  if route.request.resource_type in _SKIPPED_RESOURCES
                  else route.continue_())
    return context


def _render(context, url: str) -> tuple[str, str]:
    page = context.new_page()
    try:
        response = page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
        if response is not None and response.status >= 400:
            return "", f"browser: HTTP {response.status}"
        try:
            page.wait_for_load_state("networkidle", timeout=SETTLE_TIMEOUT_MS)
        except Exception:
            pass  # still loading something; read what has rendered
        return coretext.extract_text(page.content()), ""
    except Exception as exc:
        return "", f"browser: {type(exc).__name__}: {exc}"
    finally:
        page.close()
