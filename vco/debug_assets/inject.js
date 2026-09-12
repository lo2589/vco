// vco debug inspector: installed via CDP add_init_script on every page.
// Exposes window.__vcoDebug for the server (and the panel) to query the DOM,
// apply constrained patches, and read recent console / network errors.
(function () {
  if (window.__vcoDebug) return;

  // Box that follows the mouse so the user sees what they're about to click.
  let hoverBox = null;
  let hoverLabel = null;
  function ensureHover() {
    if (hoverBox && document.documentElement.contains(hoverBox)) return;
    hoverBox = document.createElement("div");
    hoverBox.style.cssText =
      "position:fixed;pointer-events:none;z-index:2147483646;" +
      "border:2px solid #f97316;background:rgba(249,115,22,0.08);" +
      "box-sizing:border-box;transition:all 60ms ease;";
    hoverLabel = document.createElement("div");
    hoverLabel.style.cssText =
      "position:fixed;pointer-events:none;z-index:2147483647;" +
      "background:#f97316;color:#fff;font:11px/1.4 ui-monospace,monospace;" +
      "padding:1px 6px;border-radius:3px;max-width:480px;";
    document.documentElement.appendChild(hoverBox);
    document.documentElement.appendChild(hoverLabel);
  }
  function updateHover(el) {
    if (!el || el === hoverBox || el === hoverLabel) return;
    ensureHover();
    const r = el.getBoundingClientRect();
    hoverBox.style.left = r.left + "px";
    hoverBox.style.top = r.top + "px";
    hoverBox.style.width = r.width + "px";
    hoverBox.style.height = r.height + "px";
    hoverBox.style.display = "block";
    hoverLabel.textContent = el.tagName.toLowerCase() +
      (el.id ? "#" + el.id : "") +
      (el.className && typeof el.className === "string" && el.className
        ? "." + el.className.split(/\s+/).slice(0, 2).join(".")
        : "");
    hoverLabel.style.left = Math.max(0, r.left) + "px";
    hoverLabel.style.top = Math.max(0, r.top - 18) + "px";
    hoverLabel.style.display = "block";
  }
  function clearHover() {
    if (hoverBox) hoverBox.style.display = "none";
    if (hoverLabel) hoverLabel.style.display = "none";
  }
  document.addEventListener("mousemove", (e) => {
    const el = document.elementFromPoint(e.clientX, e.clientY);
    if (el) updateHover(el);
  }, true);
  document.addEventListener("mouseleave", clearHover, true);
  document.addEventListener("mouseout", (e) => {
    if (!e.relatedTarget) clearHover();
  }, true);

  function selectorOf(el) {
    if (!el || el.nodeType !== 1) return "";
    if (el.id) return "#" + CSS.escape(el.id);
    const parts = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && parts.length < 5) {
      let part = cur.tagName.toLowerCase();
      if (cur.classList && cur.classList.length) {
        part += "." + Array.from(cur.classList).slice(0, 2)
          .map((c) => CSS.escape(c)).join(".");
      }
      const parent = cur.parentElement;
      if (parent) {
        const same = Array.from(parent.children).filter(
          (s) => s.tagName === cur.tagName
        );
        if (same.length > 1) {
          part += ":nth-of-type(" + (same.indexOf(cur) + 1) + ")";
        }
      }
      parts.unshift(part);
      cur = parent;
    }
    return parts.join(">");
  }

  // "谁错了": the picked node is often an anonymous <div>, so also report the
  // nearest ancestor that actually identifies a region of the UI (id, role,
  // aria-label, data-* hook). That is what a maintainer can grep for, and it
  // turns "a div is broken" into "the composer is broken".
  function ownerOf(el) {
    let cur = el;
    let hops = 0;
    while (cur && cur.nodeType === 1 && hops < 8) {
      if (cur !== el) {
        const id = cur.id || "";
        const role = cur.getAttribute("role") || "";
        const label = cur.getAttribute("aria-label") || "";
        let hook = "";
        for (const a of cur.attributes || []) {
          if (a.name.indexOf("data-") === 0 && a.value && a.value.length <= 48) {
            hook = a.name + '="' + a.value + '"';
            break;
          }
        }
        if (id || role || label || hook) {
          return {
            tag: cur.tagName.toLowerCase(),
            id: id,
            cls: (cur.className && typeof cur.className === "string" ? cur.className : "")
              .split(/\s+/).filter(Boolean).slice(0, 2).join("."),
            role: role,
            label: label,
            hook: hook,
            hops: hops,
          };
        }
      }
      cur = cur.parentElement;
      hops++;
    }
    return null;
  }

  function describe(el) {
    if (!el || el.nodeType !== 1) return null;
    const r = el.getBoundingClientRect();
    const attrs = {};
    for (const a of el.attributes || []) attrs[a.name] = a.value;
    const cls = el.className && typeof el.className === "string" ? el.className : "";
    let outer = "";
    try { outer = el.outerHTML || ""; } catch (_) { outer = ""; }
    return {
      selector: selectorOf(el),
      tag: el.tagName.toLowerCase(),
      id: el.id || "",
      className: cls,
      attrs,
      text: (el.innerText || el.textContent || "").trim().slice(0, 200),
      role: el.getAttribute("role") || el.tagName.toLowerCase(),
      rect: { x: r.x, y: r.y, w: r.width, h: r.height },
      visible: r.width > 0 && r.height > 0,
      outerHTML: outer.slice(0, 600),
      htmlLen: outer.length,
      childCount: el.children ? el.children.length : 0,
      depth: (function () {
        let d = 0, c = el;
        while (c && c.parentElement) { d++; c = c.parentElement; }
        return d;
      })(),
      path: pathOf(el),
      owner: ownerOf(el),
    };
  }

  // A short human path ("body > div:nth-of-type(2) > button") for the card
  // header; the pick's `selector` stays unique so it can be re-queried.
  function pathOf(el) {
    const parts = [];
    let c = el;
    while (c && c.nodeType === 1 && parts.length < 6) {
      const parent = c.parentElement;
      let part = c.tagName.toLowerCase();
      if (parent) {
        const same = Array.from(parent.children).filter(
          (s) => s.tagName === c.tagName);
        if (same.length > 1) part += ":nth-of-type(" + (same.indexOf(c) + 1) + ")";
      }
      parts.unshift(part);
      c = parent;
    }
    return parts.join(" > ");
  }

  function describeAt(x, y) {
    const el = document.elementFromPoint(x, y);
    return el ? describe(el) : null;
  }

  function describeSelector(sel) {
    let el = null;
    try { el = document.querySelector(sel); } catch (_) { el = null; }
    return el ? describe(el) : null;
  }

  function measure(selectors) {
    return (selectors || []).map((sel) => {
      let el = null;
      try { el = document.querySelector(sel); } catch (_) { el = null; }
      if (!el) return null;
      const r = el.getBoundingClientRect();
      return {
        x: r.x, y: r.y, w: r.width, h: r.height,
        visible: r.width > 0 && r.height > 0,
      };
    });
  }

  function context() {
    return {
      url: location.href,
      title: document.title,
      consoleErrors: window.__vcoDebug._consoleErrors || [],
      failedRequests: window.__vcoDebug._failedRequests || [],
    };
  }

  window.__vcoDebug = {
    pick: (sel) => {
      const el = document.querySelector(sel);
      return el ? describe(el) : null;
    },
    pickAt: (x, y) => describeAt(x, y),
    describeAt,
    describeSelector,
    measure,
    describe,
    context,
    _consoleErrors: [],
    _failedRequests: [],
  };

  const origError = console.error && console.error.bind(console);
  if (origError) {
    console.error = function () {
      try {
        const first = arguments[0];
        const msg = (first && first.message) || (first && first.toString && first.toString()) || String(first || "");
        window.__vcoDebug._consoleErrors.push(msg);
        if (window.__vcoDebug._consoleErrors.length > 50) {
          window.__vcoDebug._consoleErrors.shift();
        }
      } catch (_) {}
      return origError.apply(console, arguments);
    };
  }
  window.addEventListener("error", (e) => {
    window.__vcoDebug._consoleErrors.push(
      (e.message || "") + " @ " + (e.filename || "") + ":" + (e.lineno || 0)
    );
  });

  const origFetch = window.fetch && window.fetch.bind(window);
  if (origFetch) {
    window.fetch = function () {
      const p = origFetch.apply(this, arguments);
      const url = (arguments[0] && (arguments[0].url || arguments[0])) || "";
      p.then((r) => {
        if (!r.ok) {
          window.__vcoDebug._failedRequests.push({
            url, status: r.status, statusText: r.statusText,
          });
        }
      }, (e) => {
        window.__vcoDebug._failedRequests.push({ url, error: String(e) });
      });
      return p;
    };
  }
})();
