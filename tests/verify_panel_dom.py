"""DOM-level check of the vco debug panel's slim sidebar + floating detail block.

Runs a real Chromium against a running `vco debug` server, replaces the page's
WebSocket with a stub that answers the handshake, then feeds one synthetic
"pick" message and asserts what the sidebar actually renders:

  * the sidebar is slim (no verbose hint blocks),
  * a locked element produces the floating detail block at the top,
  * the block offers selector / HTML / text / info, and
  * its buttons postMessage to the embedding page (the dsh insert channel).

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
        frame = page.frame_locator("#panel")
        page.wait_for_timeout(500)

        # Delivering a pick goes through the frame's own ws.onmessage handler, so
        # this exercises the real render path rather than calling a renderer.
        page.frames[1].evaluate("""(msg) => { window.__deliver(msg); }""", PICK)
        page.wait_for_timeout(300)

        check("panel has no page errors", not errors, "; ".join(errors[:2]))

        # --- layout: slim sidebar, no leftover verbose blocks ---------------
        blocks = page.frames[1].evaluate("""() => {
          const right = document.getElementById('right');
          const rect = right.getBoundingClientRect();
          return {
            width: Math.round(rect.width),
            kids: right.children.length,
            hasCard: !!document.querySelector('#detail-card .fcard'),
          };
        }""")
        check("sidebar is slim (<= 400px)", blocks["width"] <= 400, f"{blocks['width']}px")
        check("detail block rendered after pick", blocks["hasCard"])

        # --- the floating block sits at the top and sticks -------------------
        geom = page.frames[1].evaluate("""() => {
          const right = document.getElementById('right');
          const host = document.getElementById('detail-host');
          const card = document.querySelector('#detail-card .fcard');
          const rightTop = right.getBoundingClientRect().top;
          const before = card.getBoundingClientRect().top - rightTop;
          right.scrollTop = 260;
          const after = card.getBoundingClientRect().top - rightTop;
          return { before: Math.round(before), after: Math.round(after),
                   sticky: getComputedStyle(host).position };
        }""")
        check("detail block starts at the top of the sidebar",
              geom["before"] < 60, f"offset {geom['before']}px")
        check("detail block stays put while the sidebar scrolls",
              geom["after"] == geom["before"], f"{geom['before']} -> {geom['after']}")
        check("detail host uses sticky positioning", geom["sticky"] == "sticky", geom["sticky"])

        # --- content of the block -------------------------------------------
        content = page.frames[1].evaluate("""() => {
          const card = document.querySelector('#detail-card .fcard');
          return {
            head: card.querySelector('.grow').textContent,
            sel: card.querySelector('.fcard-sel').textContent,
            meta: card.querySelector('.fcard-meta').textContent,
            buttons: Array.from(card.querySelectorAll('.fcard-acts button'))
                           .map(b => b.textContent),
            anno: !!card.querySelector('input.anno'),
          };
        }""")
        check("block names the locked element", content["head"] == "<button>#submit.primary.btn",
              content["head"])
        check("block shows the selector", content["sel"] == "button#submit.primary", content["sel"])
        check("block shows size/role", "120×36" in content["meta"] and "role button" in content["meta"],
              content["meta"])
        check("block carries the annotation box", content["anno"])
        check("block offers 4 actions", content["buttons"] == ["选择器", "HTML", "文字", "全部信息"],
              str(content["buttons"]))

        # --- the insert channel into the host page --------------------------
        page.frames[1].evaluate("""() => {
          document.querySelector('#detail-card .fcard-acts button:nth-child(1)').click();
        }""")
        page.wait_for_timeout(200)
        posted = page.evaluate("() => window.__posted")
        check("selector button posts to the host page",
              bool(posted) and posted[0].get("kind") == "insert-text",
              json.dumps(posted[0], ensure_ascii=False) if posted else "nothing posted")
        check("posted payload is the selector",
              bool(posted) and posted[0].get("text") == "button#submit.primary",
              posted[0].get("text") if posted else "")

        page.frames[1].evaluate("""() => {
          document.querySelector('#detail-card .fcard-acts button.wide').click();
        }""")
        page.wait_for_timeout(200)
        info = page.evaluate("() => window.__posted")
        check("info button posts the whole block",
              len(info) >= 2 and "button#submit.primary" in info[1].get("text", "")
              and "提交订单" in info[1].get("text", ""),
              (info[1].get("text", "").replace("\n", " | ") if len(info) >= 2 else "nothing posted"))

        # --- annotation reaches the report ----------------------------------
        frame.locator("#detail-card input.anno").fill("点了没反应")
        page.wait_for_timeout(700)
        # textContent, not innerText: the report lives inside a closed <details>.
        check("annotation lands in the report",
              "点了没反应" in page.frames[1].evaluate(
                  "() => document.getElementById('report-preview').textContent"))
        check("the stack still lists the locked element",
              page.frames[1].evaluate(
                  "() => document.querySelectorAll('#picks-stack .pick-card').length") == 1)

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
