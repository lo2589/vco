"""Numbered DOM marks for the web debug path.

The model side of the web agent only ever saw an aria tree — no coordinates,
no stable way to say "that button". Marking every visible interactive element
with a short id (F1/N2) turns "guess a selector" into "address by id", and the
same ids give intercepted user clicks and user annotations something to attach
to. Everything lives on the page (window.__vcoMarks / __vcoClickLog) because
the interesting events happen between two agent steps, when nothing on this
side is awake — the same staging pattern as window.__vcoTrace in webagent.
"""

_SNAPSHOT_JS = r"""([maxNormal]) => {
  const INTERACTIVE = 'a[href],button,input,select,textarea,[onclick],'
    + '[role="button"],[role="link"],[role="checkbox"],[role="tab"],'
    + '[role="menuitem"],summary,label';

  // Pure observation, armed once: re-arming would drop clicks that landed
  // since the last snapshot.
  if (!window.__vcoClickLog) {
    window.__vcoClickLog = [];
    window.__vcoClickHandler = (event) => {
      const hit = event.target && event.target.closest
        ? event.target.closest('[data-vco-id]') : null;
      if (window.__vcoClickLog.length < 500) {
        window.__vcoClickLog.push({
          id: hit ? hit.getAttribute('data-vco-id') : null,
          x: event.clientX, y: event.clientY, ts: Date.now() / 1000,
        });
      }
    };
    document.addEventListener('click', window.__vcoClickHandler, true);
  }

  // Idempotent re-snapshot: ids are re-derived from the live page, so stale
  // badges and attributes from the previous round must go first.
  if (window.__vcoMarksLayer) window.__vcoMarksLayer.remove();
  for (const el of document.querySelectorAll('[data-vco-id]')) {
    el.removeAttribute('data-vco-id');
  }
  window.__vcoMarks = {};

  const visible = (el, rect) => {
    if (rect.width <= 0 || rect.height <= 0) return false;
    const style = getComputedStyle(el);
    return style.visibility !== 'hidden' && style.display !== 'none';
  };
  const isFixed = (el) => {
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      const position = getComputedStyle(node).position;
      if (position === 'fixed' || position === 'sticky') return true;
    }
    return false;
  };
  const cssPath = (el) => {
    if (el.id) return '#' + CSS.escape(el.id);
    const parts = [];
    for (let node = el; node && node.nodeType === 1; node = node.parentElement) {
      let part = node.tagName.toLowerCase();
      if (node.id) { parts.unshift('#' + CSS.escape(node.id)); break; }
      if (node.classList.length) {
        part += [...node.classList].slice(0, 2)
          .map((name) => '.' + CSS.escape(name)).join('');
      }
      const parent = node.parentElement;
      if (parent) {
        const siblings = [...parent.children]
          .filter((child) => child.tagName === node.tagName);
        if (siblings.length > 1) {
          part += ':nth-of-type(' + (siblings.indexOf(node) + 1) + ')';
        }
      }
      parts.unshift(part);
    }
    return parts.join(' > ');
  };
  const textOf = (el) => (
    el.innerText || el.value || el.getAttribute('aria-label')
    || el.getAttribute('placeholder') || el.getAttribute('title') || ''
  ).trim().slice(0, 80);

  const fixed = [];
  const normal = [];
  for (const el of document.querySelectorAll(INTERACTIVE)) {
    const rect = el.getBoundingClientRect();
    if (!visible(el, rect)) continue;
    if (isFixed(el)) {
      fixed.push({el, rect});
    } else if (rect.bottom > 0 && rect.right > 0
               && rect.top < innerHeight && rect.left < innerWidth) {
      normal.push({el, rect});
    }
  }
  // Fixed chrome (navbars, sticky headers) is where the actions a user
  // complains about usually live, so it is numbered in full; the in-flow
  // long tail is truncated by area to keep the prompt table small.
  normal.sort((a, b) => (b.rect.width * b.rect.height)
                        - (a.rect.width * a.rect.height));
  const picked = [
    ...fixed.map((item, index) => ({...item, id: 'F' + (index + 1), fixed: true})),
    ...normal.slice(0, maxNormal)
      .map((item, index) => ({...item, id: 'N' + (index + 1), fixed: false})),
  ];

  const layer = document.createElement('div');
  layer.style.cssText = 'position:fixed;left:0;top:0;width:0;height:0;'
    + 'pointer-events:none;z-index:2147483646;';
  document.body.appendChild(layer);
  window.__vcoMarksLayer = layer;

  const marks = [];
  for (const {el, rect, id, fixed: isPinned} of picked) {
    el.setAttribute('data-vco-id', id);
    let selector = cssPath(el);
    if (document.querySelectorAll(selector).length !== 1) {
      selector = '[data-vco-id="' + id + '"]';
    }
    window.__vcoMarks[id] = {
      tag: el.tagName.toLowerCase(), text: textOf(el),
      bbox: [rect.left, rect.top, rect.right, rect.bottom],
      selector, fixed: isPinned, annotations: [],
    };
    const badge = document.createElement('div');
    badge.textContent = id;
    badge.style.cssText = 'position:absolute;pointer-events:none;'
      + 'left:' + rect.left + 'px;top:' + rect.top + 'px;'
      + 'font:700 10px/1.2 monospace;color:#fff;padding:1px 3px;'
      + 'border-radius:2px;background:' + (isPinned ? '#e64500' : '#1668dc')
      + ';';
    layer.appendChild(badge);
    marks.push({id, ...window.__vcoMarks[id]});
  }

  const clicks = window.__vcoClickLog.splice(0);
  return {marks, clicks};
}"""

_ANNOTATE_JS = r"""([id, text]) => {
  const marks = window.__vcoMarks || {};
  const mark = marks[id];
  if (!mark) return false;
  mark.annotations.push(text);
  // A pin the user can see in the real browser, so writing an annotation
  // never feels like shouting into a log file.
  const el = document.querySelector('[data-vco-id="' + id + '"]');
  const layer = window.__vcoMarksLayer;
  if (el && layer) {
    const rect = el.getBoundingClientRect();
    const pin = document.createElement('div');
    pin.textContent = '⚠ ' + text;
    pin.style.cssText = 'position:absolute;pointer-events:none;'
      + 'left:' + rect.left + 'px;top:' + Math.max(0, rect.top - 18) + 'px;'
      + 'font:12px/1.4 system-ui;color:#7a2e00;background:#ffe1c2;'
      + 'border:1px solid #e64500;border-radius:3px;padding:0 4px;'
      + 'max-width:320px;';
    layer.appendChild(pin);
  }
  return true;
}"""

_CLEAR_JS = r"""() => {
  if (window.__vcoMarksLayer) window.__vcoMarksLayer.remove();
  window.__vcoMarksLayer = null;
  if (window.__vcoClickHandler) {
    document.removeEventListener('click', window.__vcoClickHandler, true);
    window.__vcoClickHandler = null;
  }
  window.__vcoClickLog = null;
  window.__vcoMarks = {};
  for (const el of document.querySelectorAll('[data-vco-id]')) {
    el.removeAttribute('data-vco-id');
  }
}"""


def snapshot_marks(page, *, max_normal: int = 60) -> dict:
    """Number the page's interactive elements and harvest intercepted clicks.

    Fixed/sticky chrome is numbered in full (F1…); in-flow elements are
    limited to the viewport and truncated to ``max_normal`` by area (N1…).
    Each call rebuilds the badge layer, so repeated calls never stack badges.
    """
    return page.evaluate(_SNAPSHOT_JS, [max_normal])


def annotate(page, mark_id: str, text: str) -> bool:
    """Attach a user note to a mark and pin it next to the element on-page."""
    return bool(page.evaluate(_ANNOTATE_JS, [mark_id, text]))


def clear_marks(page) -> None:
    """Undo everything snapshot/annotate ever touched, listener included."""
    page.evaluate(_CLEAR_JS)


def marks_prompt_table(marks: list) -> str:
    """Render marks as the compact table injected into the model prompt."""
    lines = []
    for mark in marks:
        x0, y0, x1, y1 = (round(v) for v in mark["bbox"])
        text = str(mark.get("text", "")).replace('"', "'")
        line = f'#{mark["id"]} [{mark["tag"]}] "{text}" bbox=({x0},{y0},{x1},{y1})'
        if mark.get("fixed"):
            line += " fixed"
        for note in mark.get("annotations") or []:
            line += f' ⚠ "{str(note).replace(chr(34), chr(39))}"'
        lines.append(line)
    return "\n".join(lines)
