"""DOM-level check of the vco debug panel's slim sidebar and image-first cards.

Runs a real Chromium against a running `vco debug` server, replaces the page's
WebSocket with a stub that answers the handshake, then feeds one synthetic
"pick" message and asserts what the sidebar actually renders:

  * the sidebar is just the target-url card and the pick stack (the detail
    block, console, runs, export and diagnostics sections are gone),
  * a locked element produces an image-first card: screenshot on top, selector,
    annotation box, and exactly two insert verbs,
  * the insert verbs postMessage to the embedding page (the dsh insert
    channel).

Usage:  python tests/verify_panel_dom.py [--url http://127.0.0.1:8919/]
"""

from __future__ import annotations

import argparse
import json
import sys

from playwright.sync_api import sync_playwright

STUB = """
window.__sent = [];
window.__posted = [];
(function () {
  const RealWS = window.WebSocket;
  window.WebSocket = function (url) {
    const ws = this;
    // The native prototype exposes url/readyState/OPEN/binaryType/onmessage as
    // getters, and a plain assignment to any of them is an illegal invocation.
    // The panel both reads readyState/OPEN and writes binaryType/onmessage, so
    // every one of them has to be a plain own data property here.
    let binary = "";
    let handler = null;
    const own = (name, value) => Object.defineProperty(this, name, {
      get() { return value; }, set(v) { value = v; },
      enumerable: true, configurable: true,
    });
    own("url", url);
    own("readyState", 1);
    own("CONNECTING", 0);
    own("OPEN", 1);
    own("CLOSING", 2);
    own("CLOSED", 3);
    own("binaryType", "");
    own("onmessage", null);
    this.send = function (payload) {
      try {
        if (ws.readyState !== ws.OPEN) { return; }
      } catch (e) { window.__sendErr = String(e) + " readyState=" + typeof ws.readyState; return; }
      window.__sent.push(String(payload));
    };
    this.close = function () { ws.readyState = 3; };
    this.setBinaryType = function () {};
    this.addEventListener = function (kind, fn) {
      if (kind === "open") setTimeout(function () { fn({}); }, 0);
    };
    // Hand the panel's handler a synthetic frame the way a real socket would.
    window.__deliver = function (msg) {
      const fn = ws.onmessage;
      if (typeof fn === "function") fn({ data: JSON.stringify(msg) });
    };
  };
  window.WebSocket.OPEN = 1;
  window.WebSocket.prototype = RealWS.prototype;
})();
"""

# The panel is normally embedded, so the check runs it inside a real iframe of
# this host page: that is what makes window.parent.postMessage - the dsh insert
# channel - genuinely exercisable instead of stubbed.
HOST = """<!doctype html><html><body style="margin:0">
<div id="host-status">host</div>
<iframe id="panel" src="%s" style="width:1400px;height:860px;border:0"></iframe>
<script>
  window.__posted = [];
  window.addEventListener("message", (e) => { window.__posted.push(e.data); });
</script>
</body></html>"""

# Two distinct 1x1 PNGs, so the check can prove the card prefers the element
# crop over the page shot. Browsers sniff the magic bytes, so the PNGs render
# even under the panel's jpeg data-URI prefix.
SHOT_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")
CLIP_1PX = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNk"
            "+M9QDwADhgGAWjR9awAAAABJRU5ErkJggg==")

PICK = {
    "type": "pick",
    "picks": [{
        "selector": "button#submit.primary",
        "tag": "button",
        "id": "submit",
        "className": "primary btn",
        "role": "button",
        "text": "提交订单",
        "rect": {"x": 40, "y": 60, "w": 120, "h": 36},
        "childCount": 1,
        "path": "body > form > button#submit",
        "attrs": {"type": "submit", "class": "primary btn"},
        "outerHTML": '<button id="submit" class="primary btn" type="submit">提交订单</button>',
        "owner": {"tag": "form", "id": "order-form", "cls": "checkout", "role": "form", "hops": 1},
        # Stamped by the server at lock time, so a pick keeps naming the page it
        # came from even after the operator navigates the controlled browser.
        "pageUrl": "http://127.0.0.1:8931/vco-target.html",
        "pageTitle": "VCO 测试页",
        # The server attaches the lock-time captures; the paths are what a
        # report points at, the base64 is what the card renders.
        "shotPath": "/abs/.vco-runtime/shots/pick-example.jpg",
        "shot": SHOT_1PX,
        "clipPath": "/abs/.vco-runtime/shots/pick-example-clip.jpg",
        "clip": CLIP_1PX,
        "lockedAt": 1700000000000,
    }],
}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8919/")
    ap.add_argument("--shot", default="/tmp/vco-panel-dom.png")
    args = ap.parse_args()

    failures: list[str] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        print(("PASS  " if ok else "FAIL  ") + name + (("  — " + detail) if detail else ""))
        if not ok:
            failures.append(name)

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_page(viewport={"width": 1420, "height": 900})
        page.add_init_script(STUB)
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        page.set_content(HOST % args.url, wait_until="load")
        page.wait_for_timeout(500)

        # --- the sidebar is the slim one -------------------------------------
        layout = page.frames[1].evaluate("""() => {
          const right = document.getElementById('right');
          return {
            width: Math.round(right.getBoundingClientRect().width),
            hasHoverLine: !!document.getElementById('hover-line'),
            hasTargetUrl: !!document.getElementById('target-url'),
            gone: ['detail-host', 'detail-card', 'type-status', 'errors', 'runs',
                   'btn-copy-report', 'diag'].filter(id => !!document.getElementById(id)),
          };
        }""")
        check("sidebar is slim (<= 400px)", layout["width"] <= 400, f"{layout['width']}px")
        check("hover line survived the cut", layout["hasHoverLine"])
        check("target-url card survived the cut", layout["hasTargetUrl"])
        check("old dashboard sections are gone", layout["gone"] == [], str(layout["gone"]))

        # Delivering a pick goes through the frame's own ws.onmessage handler, so
        # this exercises the real render path rather than calling a renderer.
        page.frames[1].evaluate("""(msg) => { window.__deliver(msg); }""", PICK)
        page.wait_for_timeout(300)

        check("panel has no page errors", not errors, "; ".join(errors[:2]))

        # --- the card is image-first ------------------------------------------
        card = page.frames[1].evaluate("""() => {
          const c = document.querySelector('#picks-stack .pick-card');
          if (!c) return null;
          const img = c.querySelector('.pick-shot img');
          return {
            sel: (c.querySelector('.pick-sel') || {}).textContent || '',
            hasAnno: !!c.querySelector('textarea[data-selector]'),
            buttons: Array.from(c.querySelectorAll('.pick-acts button'))
                          .map(b => b.textContent),
            close: !!c.querySelector('.pick-close'),
            imgSrc: img ? img.src : '',
            imgOk: !!img && img.complete && img.naturalWidth > 0,
            imgFirst: !!img && img.getBoundingClientRect().top
                      < c.querySelector('textarea').getBoundingClientRect().top,
          };
        }""")
        check("pick renders a card", card is not None)
        if card is None:
            browser.close()
            return 1
        check("card shows the selector", "button#submit.primary" in card["sel"], card["sel"])
        check("card carries the annotation box", card["hasAnno"])
        check("card offers exactly the two insert verbs",
              card["buttons"] == ["插入图片", "插入HTML"], str(card["buttons"]))
        check("card has the close button", card["close"])
        check("the screenshot leads the card", card["imgFirst"])
        check("the card renders that screenshot", card["imgOk"])
        check("the element crop wins over the page shot",
              card["imgSrc"].endswith(PICK["picks"][0]["clip"]),
              card["imgSrc"][:60])

        # --- the insert channel into the host page ---------------------------
        page.frames[1].evaluate("""() => {
          const b = Array.from(document.querySelectorAll('#picks-stack .pick-acts button'))
            .find(x => x.textContent === '插入图片');
          b.click();
        }""")
        page.wait_for_timeout(200)
        posted = page.evaluate("() => window.__posted")
        block = posted[-1].get("text", "") if posted else ""
        check("插入图片 posts to the host page",
              bool(posted) and posted[-1].get("kind") == "info",
              json.dumps(posted[-1], ensure_ascii=False)[:200] if posted else "nothing posted")
        check("payload names the page the pick came from",
              "VCO 测试页" in block and "vco-target.html" in block,
              block.splitlines()[1] if block else "nothing posted")
        check("payload carries the DOM tree position",
              "- DOM:" in block and "button#submit.primary" in block)
        check("payload ends with the image files",
              "- 截图: /abs/.vco-runtime/shots/pick-example.jpg" in block
              and block.rstrip().endswith("- 裁剪图: /abs/.vco-runtime/shots/pick-example-clip.jpg"),
              block.splitlines()[-1] if block else "")

        page.frames[1].evaluate("""() => {
          const b = Array.from(document.querySelectorAll('#picks-stack .pick-acts button'))
            .find(x => x.textContent === '插入HTML');
          b.click();
        }""")
        page.wait_for_timeout(200)
        posted = page.evaluate("() => window.__posted")
        check("插入HTML posts the element markup",
              len(posted) >= 2 and posted[-1].get("text") == PICK["picks"][0]["outerHTML"],
              (posted[-1].get("text", "")[:120] if posted else "nothing posted"))

        # --- annotation still syncs to the server ----------------------------
        page.frames[1].locator("#picks-stack textarea[data-selector]").fill("点了没反应")
        page.wait_for_timeout(700)
        check("annotation is sent back to the server",
              page.frames[1].evaluate(
                  "() => window.__sent.some(s => s.includes('annotate') && s.includes('点了没反应'))"))

        page.screenshot(path=args.shot, full_page=False)
        print(f"\nscreenshot: {args.shot}")
        browser.close()

    if failures:
        print("\nFAILED: " + ", ".join(failures))
        return 1
    print("\nall checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
