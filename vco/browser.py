"""Headless page rendering and DOM interaction via Playwright."""

from __future__ import annotations

from pathlib import Path


class _PageLog:
    """Collect console errors, uncaught exceptions, and failed requests."""

    def __init__(self, limit: int = 20):
        self.console_errors: list[str] = []
        self.page_errors: list[str] = []
        self.failed_requests: list[str] = []
        self._limit = limit

    def attach(self, page) -> None:
        def on_console(message):
            if message.type == "error" and len(self.console_errors) < self._limit:
                self.console_errors.append(message.text)

        def on_page_error(error):
            if len(self.page_errors) < self._limit:
                self.page_errors.append(str(error))

        def on_request_failed(request):
            if len(self.failed_requests) < self._limit:
                failure = request.failure or ""
                self.failed_requests.append(f"{request.method} {request.url} {failure}")

        page.on("console", on_console)
        page.on("pageerror", on_page_error)
        page.on("requestfailed", on_request_failed)

    def as_dict(self) -> dict:
        return {
            "console_errors": self.console_errors,
            "page_errors": self.page_errors,
            "failed_requests": self.failed_requests,
        }


def _open(pw, url, *, width, height, timeout, profile=None, log=None, headless=True,
          record_dir=None):
    """Launch (persistent when ``profile`` is set) and open ``url``."""
    video = {}
    if record_dir:
        video["record_video_dir"] = str(record_dir)
        video["record_video_size"] = {"width": width, "height": height}
    if profile:
        context = pw.chromium.launch_persistent_context(
            profile,
            headless=headless,
            viewport={"width": width, "height": height},
            **video,
        )
        browser = None
    else:
        browser = pw.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": width, "height": height}, **video
        )
    page = context.pages[0] if context.pages else context.new_page()
    if log is not None:
        log.attach(page)
    page.goto(url, timeout=timeout * 1000)
    page.wait_for_load_state("load", timeout=timeout * 1000)
    return browser, context, page


def _close(browser, context, page=None, video_path: str | None = None) -> str | None:
    """Close and, when recording, move the video to ``video_path``."""
    context.close()
    saved = None
    if page is not None and video_path is not None and page.video is not None:
        original = page.video.path()
        page.video.save_as(video_path)
        Path(original).unlink(missing_ok=True)
        saved = video_path
    if browser is not None:
        browser.close()
    return saved


def _load_pw():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError("requires: pip install playwright") from exc
    return sync_playwright


def screenshot(
    url: str,
    output: str,
    *,
    full_page: bool = False,
    width: int = 1280,
    height: int = 800,
    timeout: float = 15.0,
    settle: float = 0.0,
    profile: str | None = None,
    headless: bool = True,
    hold: float = 0.0,
    record: str | None = None,
) -> dict:
    """Render ``url`` in Chromium and save a screenshot."""
    sync_playwright = _load_pw()
    log = _PageLog()
    with sync_playwright() as pw:
        record_dir = None if record is None else str(Path(record).parent)
        browser, context, page = _open(
            pw, url, width=width, height=height, timeout=timeout, profile=profile,
            log=log, headless=headless, record_dir=record_dir,
        )
        try:
            if settle > 0:
                page.wait_for_timeout(int(settle * 1000))
            page.screenshot(path=output, full_page=full_page)
            result = {
                "path": output,
                "url": page.url,
                "title": page.title(),
                "viewport": {"width": width, "height": height},
                "full_page": full_page,
                "headless": headless,
            }
            result.update(log.as_dict())
            if hold > 0:
                page.wait_for_timeout(int(hold * 1000))
            return result
        finally:
            video = _close(browser, context, page=page, video_path=record)
            if video:
                result["video"] = video


def _apply_fills(page, fills: list[str], visible: bool = False) -> list[str]:
    """Fill inputs as ``placeholder=text`` pairs; returns error strings.

    With ``visible=True`` types character by character so a human watching a
    headed browser can see the text appear.
    """
    errors = []
    for item in fills:
        if "=" not in item:
            errors.append(f"invalid --fill {item!r}; expected placeholder=text")
            continue
        placeholder, text = item.split("=", 1)
        locator = page.get_by_placeholder(placeholder, exact=True)
        if locator.count() == 0:
            locator = page.get_by_placeholder(placeholder)
        if locator.count() != 1:
            errors.append(
                f"--fill {placeholder!r}: expected 1 input, got {locator.count()}"
            )
            continue
        if visible:
            locator.first.click()
            locator.first.press_sequentially(text, delay=90)
        else:
            locator.first.fill(text)
    return errors


def _flash_ring(page, cx: float, cy: float, radius: float) -> None:
    """Draw a temporary orange halo at (cx, cy): transparent core, dense rim."""
    page.evaluate(
        """([x, y, r]) => {
            const el = document.createElement('div');
            el.style.cssText = 'position:fixed;pointer-events:none;z-index:2147483647;'
                + 'left:' + (x - r) + 'px;top:' + (y - r) + 'px;'
                + 'width:' + (2 * r) + 'px;height:' + (2 * r) + 'px;'
                + 'border-radius:50%;'
                + 'background:radial-gradient(circle,'
                + ' rgba(255,140,0,0) 30%, rgba(255,140,0,0.85) 55%,'
                + ' rgba(255,140,0,0.30) 75%, rgba(255,140,0,0) 100%);';
            document.body.appendChild(el);
            setTimeout(() => el.remove(), 1400);
        }""",
        [cx, cy, radius],
    )


def click(
    url: str,
    target: str | None,
    *,
    selector: str | None = None,
    contains: bool = False,
    fills: list[str] | None = None,
    width: int = 1280,
    height: int = 800,
    timeout: float = 15.0,
    settle: float = 0.5,
    profile: str | None = None,
    headless: bool = True,
    hold: float = 0.0,
    expect: str | None = None,
    expect_timeout: float = 10.0,
    record: str | None = None,
    before_path: str | None = None,
    after_path: str | None = None,
) -> dict:
    """Open ``url``, optionally fill inputs, DOM-click a unique ``target``."""
    sync_playwright = _load_pw()
    log = _PageLog()
    with sync_playwright() as pw:
        record_dir = None if record is None else str(Path(record).parent)
        browser, context, page = _open(
            pw, url, width=width, height=height, timeout=timeout, profile=profile,
            log=log, headless=headless, record_dir=record_dir,
        )
        try:
            if before_path:
                page.screenshot(path=before_path, full_page=False)

            demo = not headless or record is not None
            fill_errors = _apply_fills(page, fills or [], visible=demo)

            if selector:
                locator = page.locator(selector)
            else:
                locator = page.get_by_text(target, exact=True)
            count = locator.count()
            if count == 0 and not selector and contains:
                locator = page.get_by_text(target)
                count = locator.count()
            candidates = []
            for i in range(min(count, 8)):
                item = locator.nth(i)
                box = item.bounding_box()
                candidates.append(
                    {
                        "text": (item.text_content() or "").strip(),
                        "bbox": (
                            None
                            if box is None
                            else [
                                box["x"],
                                box["y"],
                                box["x"] + box["width"],
                                box["y"] + box["height"],
                            ]
                        ),
                    }
                )
            result = {
                "clicked": False,
                "url": page.url,
                "target": selector or target,
                "candidate_count": count,
                "candidates": candidates,
                "fill_errors": fill_errors,
                "before": before_path,
                "after": None,
                "x": None,
                "y": None,
            }
            if count != 1:
                result["error"] = "no match" if count == 0 else f"ambiguous: {count} matches"
                result.update(log.as_dict())
                return result
            box = locator.first.bounding_box()
            if demo and box is not None:
                page.wait_for_timeout(400)
                _flash_ring(
                    page,
                    box["x"] + box["width"] / 2,
                    box["y"] + box["height"] / 2,
                    max(14.0, min(32.0, float(min(box["width"], box["height"])))),
                )
                page.wait_for_timeout(900)
            locator.first.click()
            page.wait_for_timeout(int(settle * 1000))
            verified = None
            if expect:
                try:
                    page.get_by_text(expect).first.wait_for(
                        state="visible", timeout=int(expect_timeout * 1000)
                    )
                    verified = True
                except Exception:
                    verified = False
            if after_path:
                page.screenshot(path=after_path, full_page=False)
            result.update(
                {
                    "clicked": True,
                    "x": None if box is None else round(box["x"] + box["width"] / 2),
                    "y": None if box is None else round(box["y"] + box["height"] / 2),
                    "after": after_path,
                    "expect": expect,
                    "verified": verified,
                }
            )
            result.update(log.as_dict())
            if hold > 0:
                page.wait_for_timeout(int(hold * 1000))
            return result
        finally:
            video = _close(browser, context, page=page, video_path=record)
            if video:
                result["video"] = video


def snapshot(
    url: str,
    *,
    width: int = 1280,
    height: int = 800,
    timeout: float = 15.0,
    settle: float = 0.0,
    profile: str | None = None,
    headless: bool = True,
    hold: float = 0.0,
) -> dict:
    """Return the page's accessibility tree as text (no vision model needed)."""
    sync_playwright = _load_pw()
    log = _PageLog()
    with sync_playwright() as pw:
        browser, context, page = _open(
            pw, url, width=width, height=height, timeout=timeout, profile=profile,
            log=log, headless=headless,
        )
        try:
            if settle > 0:
                page.wait_for_timeout(int(settle * 1000))
            tree = page.locator("body").aria_snapshot()
            result = {
                "url": page.url,
                "title": page.title(),
                "snapshot": tree,
                "headless": headless,
            }
            result.update(log.as_dict())
            if hold > 0:
                page.wait_for_timeout(int(hold * 1000))
            return result
        finally:
            _close(browser, context)
