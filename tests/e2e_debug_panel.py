#!/usr/bin/env python
"""End-to-end check of the debug panel's gesture contract.

Not collected by pytest (no ``test_`` prefix) — it needs a live panel and a real
Chromium, so it is run on demand:

    # terminal 1
    vco debug http://127.0.0.1:3080/ --port 8788 --profile /tmp/vco-e2e
    # terminal 2
    .venv/bin/python tests/e2e_debug_panel.py            # PANEL_URL env overrides

It drives the panel through a browser exactly like a user: navigates the
controlled Chromium, ⌘/Ctrl+clicks elements, types annotations, and then
plain-clicks a button on a fixture page to prove the page's *own* handler ran.
Screenshots land in the directory given by OUT_DIR (default: .tmp-e2e).
"""

import asyncio
import json
import os
import pathlib
import shutil
import sys

from playwright.async_api import async_playwright

PANEL = os.environ.get("PANEL_URL", "http://127.0.0.1:8788/")
OUT = os.environ.get("OUT_DIR", "cache/e2e-debug")
TARGET = os.environ.get("TARGET_URL", "http://127.0.0.1:3080/")
# ENGINE=webkit is Safari's engine. Chromium tolerates things Safari rejects
# (an HTTP/1.0 "101 Switching Protocols", for one), so a Chromium-only green run
# is not evidence that a Safari user can use the panel at all.
ENGINE = os.environ.get("ENGINE", "chromium")
FIXTURE = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "e2e_fixtures", "clicktest.html"))
TYPING = "file://" + os.path.abspath(
    os.path.join(os.path.dirname(__file__), "e2e_fixtures", "typing.html"))

# Fixture elements are absolutely positioned, so these fractions are stable:
# the button occupies x 20..420 / y 20..160 of the 1024x768 viewport, the link
# x 20..420 / y 220..280.
BUTTON = (0.215, 0.117)
LINK = (0.215, 0.325)
# typing.html: input x 20..620 / y 60..120, echo line y ~130..150, flag y ~170.
TFIELD = (0.3125, 0.117)
TECHO = (0.41, 0.182)
TFLAG = (0.41, 0.234)

results = []


def check(name, ok, detail=""):
    results.append((name, bool(ok), detail))
    print(("PASS  " if ok else "FAIL  ") + name + ("   " + detail if detail else ""))


async def main():
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as pw:
        browser = await getattr(pw, ENGINE).launch(headless=True)
        ctx = await browser.new_context(viewport={"width": 1400, "height": 950})
        panel = await ctx.new_page()
        page_errors = []
        panel.on("pageerror", lambda e: page_errors.append(str(e)))
        await panel.goto(PANEL, wait_until="load")
        await panel.wait_for_timeout(2500)

        # First thing to check on any engine: is the panel even connected? A
        # rejected WebSocket upgrade leaves it blank and silent.
        diag = await panel.inner_text("#diag")
        check("panel websocket is connected", "socket: connected" in diag,
              diag.replace("\n", " ")[:120])
        check("panel receives frames",
              await panel.evaluate("() => document.getElementById('frame').src.length") > 1000)

        async def cards():
            return await panel.evaluate("""() => Array.from(document.querySelectorAll('.pick-card')).map(c => ({
                num: c.querySelector('.pick-num').textContent,
                head: c.querySelector('.pick-head').innerText,
                text: Array.from(c.querySelectorAll('.pick-meta')).map(e => e.innerText).join(' | '),
                html: c.querySelector('.pick-html').textContent,
                annotation: c.querySelector('textarea').value,
            }))""")

        async def goto(url, wait=3500):
            await panel.fill("#url", url)
            await panel.press("#url", "Enter")
            await panel.wait_for_timeout(wait)

        async def clear_picks():
            await panel.click("#btn-clear-picks")
            await panel.wait_for_timeout(500)

        async def _coords(fx, fy):
            box = await panel.locator("#mask").bounding_box()
            return box["x"] + box["width"] * fx, box["y"] + box["height"] * fy

        async def ctrl_click(fx, fy):
            x, y = await _coords(fx, fy)
            await panel.keyboard.down("Control")
            await panel.mouse.click(x, y)
            await panel.keyboard.up("Control")
            await panel.wait_for_timeout(1000)

        async def plain_click(fx, fy):
            x, y = await _coords(fx, fy)
            await panel.mouse.click(x, y)
            await panel.wait_for_timeout(1300)

        # 1. the harness GUI must render, not its 401 page
        await goto(TARGET, 4500)
        errors = await panel.inner_text("#errors")
        check("target renders (no auth wall)", "401" not in errors and "authentication required" not in errors,
              repr(errors[:120]))

        # 2. ⌘/Ctrl+click accumulates one card per element
        await clear_picks()
        await ctrl_click(0.06, 0.05)
        first = await cards()
        await ctrl_click(0.06, 0.28)          # a session row in the sidebar
        second = await cards()
        await ctrl_click(0.35, 0.50)          # the composer
        third = await cards()
        check("ctrl+click locks an element", len(first) == 1, "cards=%d" % len(first))
        check("ctrl+click on another element adds a card", len(second) == 2, "cards=%d" % len(second))
        check("cards keep accumulating", len(third) >= 3, "cards=%d" % len(third))
        selectors = [c["head"].split("\n")[-1] for c in third]
        check("each card names a distinct element", len(set(selectors)) == len(selectors))
        check("cards carry DOM detail", all(c["html"] for c in third))
        await ctrl_click(0.06, 0.05)
        check("re-locking the same element does not duplicate",
              len(await cards()) == len(third), "cards=%d" % len(await cards()))

        # 3. annotations belong to their card and survive re-renders
        await panel.fill(".pick-card:nth-of-type(1) textarea", "e2e annotation")
        await panel.wait_for_timeout(500)
        await ctrl_click(0.7, 0.7)
        check("annotation survives a re-render",
              (await cards())[0]["annotation"] == "e2e annotation")

        # The export is the whole product: where it broke and who owns it.
        report = await panel.inner_text("#report-preview")
        check("report names the page", TARGET.rstrip("/") in report, report[:60])
        check("report carries the annotation", "e2e annotation" in report)
        check("report says WHERE (selector + rect + path)",
              "选择器:" in report and "位置:" in report and "路径:" in report)
        who = [l for l in report.split("\n") if l.startswith("- 是谁")]
        check("report says WHO (owning container)", "是谁:" in report, " | ".join(who)[:120])

        # A note typed a moment ago must be in the report the user exports —
        # the server round trip is debounced, so exporting straight after
        # typing used to hand over a report with the note missing.
        await panel.fill(".pick-card:nth-of-type(1) textarea", "刚写完就导出")
        await panel.click("#btn-copy-report")
        await panel.wait_for_timeout(300)
        hot = await panel.inner_text("#report-preview")
        hot_heads = " | ".join(l for l in hot.split("\n") if l.startswith("## "))
        check("a just-typed note is in the exported report", "刚写完就导出" in hot, hot_heads[:120])

        # …and a note is not thrown away by clearing and re-locking the element
        await panel.click("#btn-clear-picks")
        await panel.wait_for_timeout(500)
        await ctrl_click(0.06, 0.05)
        await panel.wait_for_timeout(900)
        redo = await panel.inner_text("#report-preview")
        redo_heads = " | ".join(l for l in redo.split("\n") if l.startswith("## "))
        check("note survives clear + re-lock of the same element",
              "刚写完就导出" in redo, redo_heads[:120])
        await panel.screenshot(path=os.path.join(OUT, "e2e-target-cards.png"))

        # 4. plain click is a real click and never locks anything
        await clear_picks()
        await goto(FIXTURE, 2500)
        check("fixture page loaded", "clicktest.html" in await panel.inner_text("#target-url"))
        await ctrl_click(*BUTTON)
        check("fixture button locked", any("CLICK ME" in c["text"] for c in await cards()))
        before = len(await cards())
        await plain_click(*BUTTON)
        check("plain click locks nothing", len(await cards()) == before)
        await ctrl_click(*BUTTON)
        check("plain click ran the page's own handler",
              any("clicked 1" in c["text"] or "clicked 1" in c["html"] for c in await cards()))
        await plain_click(*LINK)
        await panel.wait_for_timeout(2800)
        landed = await panel.inner_text("#target-url")
        check("plain click on a link navigates the controlled browser",
              "clicktest" not in landed, landed)
        # …and the landing page must be real content, not its 401 page: with a
        # SameSite=Strict cookie a cross-origin navigation arrives unauthenticated.
        if "3080" in landed:
            await clear_picks()
            await ctrl_click(0.35, 0.60)
            text = json.dumps(await cards(), ensure_ascii=False)
            check("landing page is authenticated content, not a 401 wall",
                  "authentication required" not in text, text[:160])
        await panel.screenshot(path=os.path.join(OUT, "e2e-clickthrough.png"))

        # 5. the panel is the keyboard too: typing, IME text, paste, shortcuts
        await clear_picks()
        await goto(TYPING, 2500)

        async def peek(spot):
            """Ctrl+click a spot and return that element's text: line."""
            await ctrl_click(*spot)
            got = await cards()
            await clear_picks()
            for card in got:
                for part in card["text"].split(" | "):
                    if part.startswith("text:"):
                        return part
            return " | ".join(c["text"] for c in got)[:200]

        async def frame_hash():
            import hashlib
            src = await panel.evaluate("() => document.getElementById('frame').src")
            return hashlib.sha256(src.encode()).hexdigest()[:16]

        await plain_click(*TFIELD)
        await panel.keyboard.type("hello world", delay=35)
        await panel.wait_for_timeout(900)
        echo = await peek(TECHO)
        check("typing reaches the page", "input: hello world" in echo, echo[:140])

        # …and the panel must SHOW it: with the mouse held still, only a real
        # page change can alter the frame. This is the "打了字/点了按钮要有反馈"
        # requirement, which a DOM-only assertion cannot prove.
        await plain_click(*TFIELD)
        await panel.keyboard.press("Meta+a")
        await panel.keyboard.press("Backspace")
        await panel.wait_for_timeout(1500)
        idle_hash = await frame_hash()
        await panel.keyboard.type("feedback", delay=45)
        await panel.wait_for_timeout(1600)
        typed_hash = await frame_hash()
        check("typing is visible in the panel viewport", idle_hash != typed_hash,
              "%s -> %s" % (idle_hash, typed_hash))

        # the peek above left focus on the panel's clear button, so go back to
        # the field, put the caret at the end, then delete
        await plain_click(*TFIELD)
        await panel.keyboard.press("End")
        await panel.keyboard.press("Backspace")
        await panel.wait_for_timeout(700)
        echo = await peek(TECHO)
        check("Backspace reaches the page", "feedback" not in echo
              and "input:" in echo, echo[:140])

        await plain_click(*TFIELD)
        await panel.keyboard.press("Control+b")
        await panel.wait_for_timeout(800)
        flag = await peek(TFLAG)
        check("modifier chord reaches the page (Ctrl+B)", "ctrl+b x1" in flag, flag[:140])

        # paste must insert the clipboard text exactly once (it used to type "v")
        try:
            await ctx.grant_permissions(["clipboard-read", "clipboard-write"])
        except Exception as exc:  # noqa: BLE001  (WebKit has no such API)
            print("      (clipboard permission unsupported on %s: %s)" % (ENGINE, str(exc)[:60]))
        await panel.evaluate("() => navigator.clipboard.writeText('粘贴的中文')")
        await plain_click(*TFIELD)
        await panel.keyboard.press("Meta+v")
        await panel.wait_for_timeout(900)
        echo = await peek(TECHO)
        check("paste inserts the clipboard text exactly once",
              echo.count("粘贴的中文") == 1, echo[:160])

        # the explicit text box: the guaranteed path for IME / long text
        await plain_click(*TFIELD)
        await panel.fill("#typebox", "中文输入测试")
        await panel.press("#typebox", "Enter")
        await panel.wait_for_timeout(900)
        echo = await peek(TECHO)
        check("text box delivers Chinese into the page", "中文输入测试" in echo, echo[:160])
        await panel.screenshot(path=os.path.join(OUT, "e2e-keyboard.png"))

        # 6. the run timeline: an automated `vco webclick` must be visible here
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from vco.events import emit
        run_dir = pathlib.Path(OUT) / "e2e-run"
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "events.jsonl").write_text("", encoding="utf-8")
        emit(run_dir, "observation", summary="打开 " + TARGET, image="before.png")
        emit(run_dir, "action_result", step=1, summary="点击 「New Session」")
        emit(run_dir, "page_error", step=2, summary="requestfailed: /api/x → ERR_ABORTED")
        await panel.click("#btn-runs-refresh")
        await panel.wait_for_timeout(1500)
        listed = await panel.evaluate(
            "() => Array.from(document.querySelectorAll('#runs option')).map(o => o.value)")
        check("a new run appears in the run list", any("e2e-run" in v for v in listed),
              str(listed)[:120])
        await panel.evaluate("""() => {
            const sel = document.getElementById('runs');
            sel.value = Array.from(sel.options).find(o => o.value.indexOf('e2e-run') >= 0).value;
            sel.dispatchEvent(new Event('change'));
        }""")
        await panel.wait_for_timeout(1500)
        timeline = await panel.inner_text("#run-events")
        check("run timeline renders its steps", "点击 「New Session」" in timeline, timeline[:120])
        check("run timeline flags the error step", "ERR_ABORTED" in timeline)
        check("run timeline shows step thumbnails",
              await panel.evaluate("() => document.querySelectorAll('#run-events img.thumb').length") >= 1)

        # artifacts are served from the runs root only — no traversal
        escaped = await panel.evaluate("""async () => {
            const r = await fetch('/runs/../../../../etc/passwd');
            return r.status;
        }""")
        check("run artifact route refuses path traversal", escaped == 404, "status=" + str(escaped))

        # 7. `vco webclick --panel`: the automation drives THIS browser, so a
        #    person watching the panel sees it happen.
        import subprocess
        repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        run_dir = pathlib.Path(OUT) / "e2e-panel-click"

        def run_cli(*extra):
            return subprocess.run(
                [sys.executable, "-m", "vco.cli", "webclick", FIXTURE,
                 "--panel", PANEL, "--dir", str(run_dir), *extra],
                capture_output=True, text=True, cwd=repo, timeout=180,
            )

        shutil.rmtree(run_dir, ignore_errors=True)
        done = run_cli("--target", "CLICK ME")
        try:
            payload = json.loads(done.stdout)
        except json.JSONDecodeError:
            payload = {}
        check("webclick --panel reports the click", done.returncode == 0
              and payload.get("clicked") is True,
              (done.stdout[-120:] or done.stderr[-120:]).replace("\n", " "))
        check("webclick --panel writes run events",
              (run_dir / "events.jsonl").is_file()
              and "action_result" in (run_dir / "events.jsonl").read_text(encoding="utf-8"))
        check("webclick --panel leaves before/after screenshots",
              len(list(run_dir.glob("*.before.png"))) == 1
              and len(list(run_dir.glob("*.after.png"))) == 1)

        # the click really happened in the panel's browser, not somewhere else
        from vco.panel_client import PanelClient
        with PanelClient(PANEL) as pc:
            marks = pc.call("dom_marks", max_normal=40).get("marks", [])
        texts = [(m.get("id"), m.get("text") or "") for m in marks]
        check("the panel's own page shows the click's effect",
              any("clicked 1" in t for _i, t in texts), str(texts)[:140])

        # and it refuses an ambiguous/absent target exactly like the standalone path
        refused = run_cli("--target", "NO SUCH TEXT ANYWHERE")
        try:
            refused_payload = json.loads(refused.stdout)
        except json.JSONDecodeError:
            refused_payload = {}
        check("webclick --panel refuses a target that is not there",
              refused.returncode == 2 and refused_payload.get("error") == "no match",
              str(refused_payload.get("error")))

        await panel.click("#btn-runs-refresh")
        await panel.wait_for_timeout(1500)
        listed = await panel.evaluate(
            "() => Array.from(document.querySelectorAll('#runs option')).map(o => o.value)")
        check("the panel-mode run shows up in 运行记录",
              any("e2e-panel-click" in v for v in listed), str(listed)[:140])

        check("no panel JS errors", not page_errors, str(page_errors)[:200])
        await browser.close()

    failed = [r for r in results if not r[1]]
    print("\n%d/%d checks passed" % (len(results) - len(failed), len(results)))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
