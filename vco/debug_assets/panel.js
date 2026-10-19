// vco debug panel — talks to a headless Chromium through /ws.
//
// Interaction contract (this is the whole point of the panel):
//   plain click        → forwarded to the page: buttons fire, links navigate
//   ⌘/Ctrl/Alt + click → locks the element under the cursor; the right sidebar
//                        gains an image-first card: the element's screenshot,
//                        an annotation box, and 插入图片 / 插入HTML
//   …clicking more elements keeps ADDING cards (nothing is replaced)
//   ×                  → drops that one card
//
// The sidebar is deliberately thin: a hover readout, the current page URL, and
// the card stack. Reports, console logs, run timelines and diagnostics used to
// live here too; they were removed because the panel's product is the pick
// card, not a dashboard.
(function () {
  const $ = (id) => document.getElementById(id);
  const FRAME = $("frame");
  const STAGE = $("stage");
  const VIEWPORT = $("viewport");
  const MASK = $("mask");
  const OVERLAY = $("overlay");
  const URL_BAR = $("url");

  let picks = [];            // canonical selection, owned by the server
  // selector -> the user's note. The panel owns this text: the server copy only
  // ever arrives *after* a round trip, so a re-broadcast arriving mid-typing
  // must not wipe a note typed moments earlier.
  const annotations = {};
  const pendingAnnotations = {};
  let annotateTimer = null;
  let hotKey = null;         // card ↔ marker highlight link
  let pageContext = { url: "", title: "" };
  // Where the operator's pointer is, in page coordinates — the agent cursor
  // appears here when an action starts, since the action's own coordinates
  // only exist once it finishes.
  let lastPointerPage = null;
  const keyOf = (p) => (p && p.selector) || "";

  // --- websocket ---------------------------------------------------------
  // The panel is useless the moment the socket dies, and a dead socket used to
  // be silent: the last screenshot stayed on screen and every click vanished.
  // So: reconnect automatically and say so on screen.
  let ws = null;
  let connected = false;

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }

  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.binaryType = "arraybuffer";
    ws.addEventListener("open", () => {
      connected = true;
      $("link-banner").className = "";
      send({
        type: "client_info",
        userAgent: navigator.userAgent,
        platform: navigator.platform || "",
        pointerEvents: typeof window.PointerEvent !== "undefined",
        beforeInput: typeof InputEvent !== "undefined" && "inputType" in InputEvent.prototype,
        contentEditable: MASK.isContentEditable,
        viewport: [window.innerWidth, window.innerHeight],
      });
    });
    ws.addEventListener("close", () => {
      connected = false;
      $("link-banner").className = "on";
      setTimeout(connect, 1200);
    });
    ws.onmessage = onMessage;
  }

  function onMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "frame") {
      // CDP screencast frames are JPEG; the `png` field only ever arrives from
      // an older server. Accept both so either side can upgrade first.
      FRAME.src = msg.jpeg
        ? "data:image/jpeg;base64," + msg.jpeg
        : "data:image/png;base64," + msg.png;
      if (!stageFitted) FRAME.decode().then(fitStage).catch(() => {});
    } else if (msg.type === "navigated") {
      URL_BAR.value = msg.url || "";
      $("target-url").textContent = msg.url || "";
      pageContext.url = msg.url || "";
      pageContext.title = msg.title || "";
      // Selectors may not survive a navigation; re-measure so markers either
      // move with the element or go dashed instead of lying.
      setTimeout(() => send({ type: "measure_picks" }), 350);
    } else if (msg.type === "hover") {
      renderHover(msg.payload);
    } else if (msg.type === "selection") {
      copyFromPage(msg.text || "");
    } else if (msg.type === "pick") {
      // Server owns the selection: it accumulates one entry per locked
      // element and echoes the whole stack after every change — including a
      // re-lock of an already-picked element, which arrives with FRESH
      // shot/clip images and must simply re-render (the old detail block only
      // re-rendered when the key set changed, which is why a re-pick looked
      // like it kept the first screenshot forever).
      picks = Array.isArray(msg.picks) ? msg.picks : [];
      picks.forEach((p) => {
        const k = keyOf(p);
        if (annotations[k] !== undefined) p.annotation = annotations[k];
      });
      renderPicks();
      renderMarkers();
    } else if (msg.type === "agent_cursor") {
      showAgentCursor(msg);
    }
  }

  // Push a fresh frame immediately when the browser tab comes back, instead of
  // showing whatever was on screen when it was hidden.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && connected) send({ type: "measure_picks" });
  });

  // --- viewport geometry --------------------------------------------------
  // The controlled browser has a fixed viewport; the screenshot is always that
  // size. Size the stage from the frame itself so the mask covers exactly the
  // rendered page (no dead letterbox, no mis-scaled coordinates).
  function frameSize() {
    return { w: FRAME.naturalWidth || 1024, h: FRAME.naturalHeight || 768 };
  }
  function fitStage() {
    const { w, h } = frameSize();
    const avail = VIEWPORT.clientWidth - 16;
    const width = Math.max(240, Math.min(avail, w));
    STAGE.style.width = width + "px";
    STAGE.style.height = (width * h / w) + "px";
    stageFitted = true;
    renderMarkers();
  }
  window.addEventListener("resize", fitStage);

  function clientToPage(e) {
    const rect = MASK.getBoundingClientRect();
    const { w, h } = frameSize();
    if (!rect.width || !rect.height) return null;
    return {
      x: (e.clientX - rect.left) * (w / rect.width),
      y: (e.clientY - rect.top) * (h / rect.height),
    };
  }
  function pageRectToPanel(rect) {
    const box = MASK.getBoundingClientRect();
    const { w, h } = frameSize();
    const sx = box.width / w, sy = box.height / h;
    return {
      left: rect.x * sx,
      top: rect.y * sy,
      width: Math.max(2, rect.w * sx),
      height: Math.max(2, rect.h * sy),
    };
  }

  // --- markers ------------------------------------------------------------
  // Locked elements get a numbered box on the panel overlay, matching the card
  // numbers, so "which card is which element" never needs guessing.
  function renderMarkers() {
    OVERLAY.textContent = "";
    const box = MASK.getBoundingClientRect();
    if (!box.width) return;
    picks.forEach((p, i) => {
      if (!p.rect) return;
      const at = pageRectToPanel(p.rect);
      const marker = document.createElement("div");
      marker.className = "marker" + (p.missing ? " stale" : "")
        + (hotKey && hotKey === keyOf(p) ? " hot" : "");
      marker.style.left = at.left + "px";
      marker.style.top = at.top + "px";
      marker.style.width = at.width + "px";
      marker.style.height = at.height + "px";
      const tag = document.createElement("span");
      tag.className = "tag";
      tag.textContent = "#" + (i + 1) + " " + (p.tag || "") + (p.id ? "#" + p.id : "")
        + (p.missing ? " (已不在页面上)" : "");
      marker.appendChild(tag);
      OVERLAY.appendChild(marker);
    });
  }

  // --- agent cursor ---------------------------------------------------------
  // dom_* verbs are how a remote driver (PanelClient) acts on the page, so the
  // server brackets each one with agent_cursor start/end markers. The panel
  // turns that into a fake cursor: the human needs to see "this movement is
  // the agent's", the same question the desktop channel answers with its
  // floating badge. One element, reused across a burst of actions — a queue
  // of them would just smear.
  let agentCursorEl = null;
  let agentCursorTimer = null;

  function ensureAgentCursor() {
    if (agentCursorEl) return agentCursorEl;
    const el = document.createElement("div");
    // Sits on the stage (not the overlay: renderMarkers clears that), in the
    // same page→panel coordinate space the pick markers use.
    el.style.cssText = "position:absolute;pointer-events:none;z-index:60;"
      + "display:flex;align-items:flex-start;opacity:0;transition:opacity 350ms;";
    el.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" style="flex:none">'
      + '<path d="M4 2 L20 12 L12 14 L9 22 Z" fill="#f97316" stroke="#fff" stroke-width="1.5"/></svg>'
      + '<span style="background:#f97316;color:#fff;font:10px/1.5 ui-monospace,monospace;'
      + 'padding:0 4px;border-radius:3px;margin:14px 0 0 -4px"></span>';
    STAGE.appendChild(el);
    agentCursorEl = el;
    return el;
  }

  function moveAgentCursor(el, x, y) {
    const box = MASK.getBoundingClientRect();
    const { w, h } = frameSize();
    if (!box.width) return;
    el.style.left = (x * box.width / w) + "px";
    el.style.top = (y * box.height / h) + "px";
  }

  function showAgentCursor(msg) {
    if (agentCursorTimer) { clearTimeout(agentCursorTimer); agentCursorTimer = null; }
    const el = ensureAgentCursor();
    const label = el.querySelector("span");
    if (msg.phase === "start") {
      if (label) label.textContent = msg.action || "agent";
      const at = lastPointerPage || { x: frameSize().w / 2, y: frameSize().h / 2 };
      moveAgentCursor(el, at.x, at.y);
      el.style.opacity = "1";
      return;
    }
    // end: land the cursor on the clicked point (when the action had one),
    // linger a beat so the eye catches where it acted, then fade out.
    if (msg.x != null && msg.y != null) moveAgentCursor(el, msg.x, msg.y);
    el.style.opacity = "1";
    agentCursorTimer = setTimeout(() => {
      el.style.opacity = "0";
      agentCursorTimer = null;
    }, 800);
  }

  function renderHover(desc) {
    const el = $("hover-line");
    if (!el) return;
    if (!desc) { el.textContent = ""; return; }
    const cls = desc.className ? "." + desc.className.split(/\s+/).filter(Boolean).slice(0, 2).join(".") : "";
    el.textContent = "<" + (desc.tag || "?") + ">" + (desc.id ? "#" + desc.id : "") + cls
      + "  ·  " + Math.round(desc.rect.w) + "×" + Math.round(desc.rect.h)
      + (desc.text ? "  ·  “" + desc.text.slice(0, 32) + "”" : "");
  }

  // --- viewport event plumbing -------------------------------------------
  // Pointer events are the primary path, but Safari (and any engine that
  // decides not to deliver them for a contenteditable target) must not leave
  // the panel dead. Mouse events are wired as a fallback and deduplicated
  // against the pointer events that already handled the same gesture.
  let dragStart = null;
  let selecting = false;
  let stageFitted = false;
  let lastPointerAt = 0;
  const isLockModifier = (e) => !!(e.ctrlKey || e.metaKey || e.altKey);

  function onPress(x, y, button, lock) {
    selecting = lock;
    dragStart = null;
    // Focus the mask so subsequent keystrokes reach the controlled page. The
    // panel's own focus is unrelated to the page's focus, so this is safe.
    MASK.focus();
    if (lock) {
      // Lock on the way down: a modifier click is *ours*, and picking here
      // survives macOS turning ctrl+click into a context-menu gesture.
      send({ type: "pick_at", x: x, y: y });
    } else {
      dragStart = { x: x, y: y };
      send({ type: "mouse_down", x: x, y: y, button: button });
    }
  }

  function onRelease(x, y, button) {
    if (!selecting && dragStart) {
      // Plain click: a real press/release pair in the page, so buttons fire
      // and links navigate exactly as if you clicked the real browser.
      send({ type: "mouse_up", x: x, y: y, button: button });
    }
    selecting = false;
    dragStart = null;
  }

  MASK.addEventListener("pointermove", (e) => {
    const p = clientToPage(e);
    if (p) {
      lastPointerPage = p;
      send({ type: "mouse_move", x: p.x, y: p.y });
    }
  });
  MASK.addEventListener("pointerdown", (e) => {
    lastPointerAt = Date.now();
    const p = clientToPage(e);
    if (!p) return;
    onPress(p.x, p.y, ["left", "middle", "right"][e.button] || "left", isLockModifier(e));
    e.preventDefault();
  });
  MASK.addEventListener("pointerup", (e) => {
    const p = clientToPage(e);
    if (!p) { selecting = false; dragStart = null; return; }
    onRelease(p.x, p.y, ["left", "middle", "right"][e.button] || "left");
  });
  MASK.addEventListener("pointerleave", () => { selecting = false; dragStart = null; });
  // Fallback (only if pointer events did not already handle this gesture).
  MASK.addEventListener("mousedown", (e) => {
    if (Date.now() - lastPointerAt < 600) return;
    const p = clientToPage(e);
    if (!p) return;
    onPress(p.x, p.y, ["left", "middle", "right"][e.button] || "left", isLockModifier(e));
    e.preventDefault();
  });
  MASK.addEventListener("mouseup", (e) => {
    if (Date.now() - lastPointerAt < 600) return;
    const p = clientToPage(e);
    if (!p) return;
    onRelease(p.x, p.y, ["left", "middle", "right"][e.button] || "left");
  });
  // Right-click is a page gesture, not a browser-menu gesture.
  MASK.addEventListener("contextmenu", (e) => e.preventDefault());
  MASK.addEventListener("wheel", (e) => {
    e.preventDefault();
    send({ type: "wheel", dx: e.deltaX, dy: e.deltaY });
    setTimeout(() => send({ type: "measure_picks" }), 260);
  }, { passive: false });

  // --- keyboard -----------------------------------------------------------
  // The panel is the remote keyboard, so it has to reproduce a real one:
  //   * plain typing and IME go out as *text* (key events alone cannot carry
  //     a Chinese composition, and a burst of keydowns is fragile)
  //   * ⌘/Ctrl/Alt chords go out as real chords, so the page's shortcuts work
  //     (previously ⌘V was forwarded as a bare "v" and typed a letter)
  //   * paste/copy are handled here, because a synthetic key event does not
  //     make Chromium execute the paste edit command
  const MODIFIERS = { Control: "ctrlKey", Meta: "metaKey", Alt: "altKey", Shift: "shiftKey" };
  const NAMED_KEYS = new Set([
    "Enter", "Tab", "Backspace", "Delete", "Escape", "ArrowUp", "ArrowDown",
    "ArrowLeft", "ArrowRight", "Home", "End", "PageUp", "PageDown", "Insert",
  ]);

  // With Ctrl/Alt held, macOS rewrites e.key (Alt+a is "å"), so fall back to
  // the physical key for letters and digits.
  function baseKey(e) {
    if (/^Key[A-Z]$/.test(e.code)) return e.code.slice(3).toLowerCase();
    if (/^Digit[0-9]$/.test(e.code)) return e.code.slice(5);
    return e.key;
  }
  function modifiersOf(e) {
    const mods = [];
    if (e.ctrlKey) mods.push("Control");
    if (e.metaKey) mods.push("Meta");
    if (e.altKey) mods.push("Alt");
    // A printable key already carries its shift state in e.key ("A"), so
    // adding Shift as a modifier would double it.
    if (e.shiftKey && e.key.length !== 1) mods.push("Shift");
    return mods;
  }
  const isPasteKey = (e) => (e.metaKey || e.ctrlKey) && (e.key === "v" || e.key === "V");
  const isCopyKey = (e) => (e.metaKey || e.ctrlKey) && (e.key === "c" || e.key === "C");

  let composing = false;
  let lastPasteAt = 0;

  // One user action, one delivery.
  //
  // Browsers disagree about which events a single edit fires, and the extra
  // event always carries the *same text*: WebKit reports an IME commit as both
  // `compositionend` and a cancelable `insertText` beforeinput, and a paste as
  // both `paste` and `insertFromPaste` beforeinput. Each handler below used to
  // send on its own, so one commit landed in the page twice — the reported
  // symptom was typed words arriving doubled ("你好" → "你好你好").
  //
  // The guard keys on (text, path): the same text from a DIFFERENT path inside
  // the window is one action seen twice and is dropped; the same text from the
  // SAME path is the user typing it again ("aa", "哈哈") and always passes.
  // A fixed dedupe window would have to choose between breaking repeated
  // characters and leaking doubled IME commits; keying on the path needs
  // neither.
  const DEDUPE_MS = 350;
  let lastSend = { text: "", via: "", at: 0 };
  function sendText(text, via) {
    const value = String(text === undefined ? "" : text);
    if (!value) return false;
    const now = Date.now();
    if (value === lastSend.text && via !== lastSend.via && now - lastSend.at < DEDUPE_MS) {
      return false;
    }
    lastSend = { text: value, via: via, at: now };
    send({ type: "insert_text", text: value });
    return true;
  }

  MASK.addEventListener("compositionstart", () => { composing = true; });
  MASK.addEventListener("compositionend", (e) => {
    composing = false;
    const text = e.data || MASK.textContent || "";
    MASK.textContent = "";
    if (text) sendText(text, "ime");
  });
  // Text the browser actually inserted into the mask (typing, IME leftovers).
  MASK.addEventListener("beforeinput", (e) => {
    const type = e.inputType || "";
    if (type === "insertCompositionText") return;   // IME composes in the mask
    if (type.startsWith("insert")) {
      e.preventDefault();
      let text = e.data || "";
      if (!text && e.dataTransfer) text = e.dataTransfer.getData("text/plain") || "";
      if (text) sendText(text, "beforeinput");
    }
  });
  // Safety net: never let the mask keep text (it is only a keyboard sink).
  //
  // A browser that commits the composition into the mask *after*
  // compositionend lands here carrying the text just sent by the IME path; the
  // (text, path) guard in sendText drops it as the duplicate it is. An earlier
  // version used a blanket 400 ms silence after every IME commit instead, which
  // also swallowed real input typed in that window — one word committed and the
  // fast next character vanished.
  MASK.addEventListener("input", () => {
    if (composing) return;
    const text = MASK.textContent;
    if (!text) return;
    MASK.textContent = "";
    sendText(text, "input");
  });
  MASK.addEventListener("paste", (e) => {
    e.preventDefault();
    const text = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
    if (text) {
      lastPasteAt = Date.now();
      sendText(text, "paste");
    }
  });
  MASK.addEventListener("copy", (e) => {
    e.preventDefault();
    send({ type: "copy_selection" });
  });
  MASK.addEventListener("keydown", (e) => {
    if (document.activeElement && document.activeElement !== MASK) return;
    if (Object.prototype.hasOwnProperty.call(MODIFIERS, e.key)) return;
    if (isPasteKey(e)) {
      // Chromium fires the paste event when it handles the shortcut itself.
      // If it does not, ask for the clipboard explicitly — but never both, or
      // the text lands in the page twice.
      setTimeout(() => {
        if (Date.now() - lastPasteAt < 600) return;
        if (navigator.clipboard && navigator.clipboard.readText) {
          navigator.clipboard.readText().then((text) => {
            if (text) sendText(text, "clipboard");
          }).catch(() => {});
        }
      }, 0);
      return;
    }
    if (isCopyKey(e)) return;   // the copy event does the work
    const printable = e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey;
    if (printable) return;      // let beforeinput carry it as text
    e.preventDefault();
    send({ type: "key", key: NAMED_KEYS.has(e.key) ? e.key : baseKey(e),
           modifiers: modifiersOf(e) });
  });
  MASK.tabIndex = 0;

  // ⌘C in the viewport: the page's selection is not on this side of the wire,
  // so ask the server for it and put it on the real clipboard.
  function copyFromPage(text) {
    if (!text) return;
    const helper = $("clip-helper");
    helper.value = text;
    helper.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (!ok && navigator.clipboard) {
      navigator.clipboard.writeText(text).catch(() => {});
    }
  }

  $("btn-back").onclick = () => send({ type: "back" });
  $("btn-forward").onclick = () => send({ type: "forward" });
  $("btn-reload").onclick = () => send({ type: "reload" });
  $("btn-clear-picks").onclick = () => send({ type: "clear_picks" });
  URL_BAR.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      let v = URL_BAR.value.trim();
      // Only add a scheme when there is none at all: data:/file:/about: URLs
      // must survive untouched, and a bare host:port needs http:// (svc:80 is
      // not a scheme we can dispatch on).
      if (v && !/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(v) && !/^(data|file|about|blob):/i.test(v)) {
        v = "http://" + v;
      }
      send({ type: "goto", url: v });
      MASK.focus();
    }
  });

  // --- selection cards ----------------------------------------------------
  // One card per locked element, image first: the element's own screenshot is
  // the identity of the card (a selector is how the *machine* finds it again).
  // Cards accumulate; nothing here replaces a previous one — except re-locking
  // the same element, which refreshes its card in place (see the pick handler).

  // What a locked element has to answer when it is handed to a chat composer,
  // in the order a reader needs it: WHERE the page is, WHICH element it is,
  // and the pictures of it as files the reader (or a model on the other side)
  // can open. Selector alone left out the first and last, which is exactly
  // the pair that makes a report actionable.
  function detailBlock(pick, noteOverride) {
    const lines = [];
    // noteOverride lets a stack card build ITS OWN block (its own annotation).
    const note = (noteOverride !== undefined
      ? noteOverride
      : (pick.annotation || "")).trim();
    if (note) lines.push(note);

    // The page is the pick's OWN page, stamped when it was locked, so a pick
    // stays honest after you navigate away.
    const url = pick.pageUrl || pageContext.url || "";
    const title = pick.pageTitle || pageContext.title || "";
    if (url) lines.push("- 页面: " + (title ? title + " — " : "") + url);
    else lines.push("- 页面: (未知)");

    // The DOM tree position, which is what identifies the element: the unique
    // selector, and the short human path when it says something the selector
    // does not.
    const sel = pick.selector || "?";
    const path = pick.path && pick.path !== sel ? "  (" + pick.path + ")" : "";
    lines.push("- DOM: `" + sel + "`" + path);
    if (pick.visible === false || pick.missing) lines.push("- 状态: ⚠ 不可见/已不在页面上");
    if (pick.imgSrc) lines.push("- 图片: " + pick.imgSrc);

    // Absolute paths, last, so the block still reads as prose and hands the
    // machine its pointers at the end.
    if (pick.shotPath) lines.push("- 截图: " + pick.shotPath);
    if (pick.clipPath) lines.push("- 裁剪图: " + pick.clipPath);

    return lines.join("\n");
  }

  // Hand the payload to whoever embedded this panel (dsh's own page, a browser
  // tab: anyone listening on window.parent). The panel never assumes a parent
  // is there — in a bare tab the buttons simply copy, which is the fallback.
  function forwardToHost(kind, text, pick, noteOverride) {
    let delivered = false;
    // The annotation travels as its OWN field, not merely baked into `text`:
    // the embedder decides where the note lands, and a raw-HTML payload
    // carries no prose of its own to carry it in.
    const note = (noteOverride !== undefined
      ? noteOverride
      : ((pick && pick.annotation) || "")).trim();
    try {
      if (window.parent && window.parent !== window) {
        window.parent.postMessage({
          source: "vco-debug-panel",
          kind: kind,
          text: text,
          selector: pick ? (pick.selector || "") : "",
          annotation: note,
          insert: kind === "insert",
        }, "*");
        delivered = true;
      }
    } catch (e) { delivered = false; }
    return delivered;
  }

  function copyText(text) {
    if (!navigator.clipboard) return Promise.resolve(false);
    return navigator.clipboard.writeText(text).then(() => true, () => false);
  }

  // Sending annotations is debounced; nothing here re-renders on every
  // keystroke, so the debounce exists purely to keep the server's copy fresh
  // without one round trip per character.
  function queueAnnotation(sel, text) {
    pendingAnnotations[sel] = text;
    clearTimeout(annotateTimer);
    annotateTimer = setTimeout(flushAnnotations, 250);
  }

  function flushAnnotations() {
    clearTimeout(annotateTimer);
    Object.keys(pendingAnnotations).forEach((sel) => {
      send({ type: "annotate", selector: sel, text: pendingAnnotations[sel] });
      delete pendingAnnotations[sel];
    });
  }

  function renderPicks() {
    const stack = $("picks-stack");
    $("picks-count").textContent = String(picks.length);
    // Keep whatever the user is typing (and their caret) across re-renders.
    const typed = {};
    const focused = document.activeElement;
    const focusedSel = focused && focused.dataset && focused.dataset.selector;
    const caret = focused && focused.selectionStart;
    stack.querySelectorAll("textarea[data-selector]").forEach((ta) => {
      typed[ta.dataset.selector] = ta.value;
    });

    stack.textContent = "";
    if (!picks.length) {
      const empty = document.createElement("div");
      empty.className = "card";
      empty.id = "picks-empty";
      empty.textContent = "⌘/Ctrl + 点击视口里的元素即可锁定。";
      stack.appendChild(empty);
      return;
    }
    // Newest first: the card for the element you just clicked belongs at the
    // FRONT of the stack instead of under three older ones. The number badge
    // and the remove payload still use the pick's own index, so labels and
    // removal stay stable while the order flips.
    const order = picks.map((p, i) => i).reverse();
    order.forEach((i) => {
      const p = picks[i];
      const key = keyOf(p);
      const card = document.createElement("div");
      card.className = "pick-card";
      card.onmouseenter = () => { hotKey = key; renderMarkers(); };
      card.onmouseleave = () => { hotKey = null; renderMarkers(); };

      const close = document.createElement("button");
      close.className = "pick-close";
      close.textContent = "×";
      close.title = "移除这张卡片";
      close.onclick = (ev) => {
        ev.stopPropagation();
        send({ type: "remove_pick", selector: key });
      };

      // The picture leads: the element crop when there is one (it names the
      // element), else the viewport shot. Rendered from the pick's own data
      // on every broadcast, so a re-lock's fresh capture shows up by itself.
      const cardShot = p.clip || p.shot;
      let fig = null;
      if (cardShot) {
        fig = document.createElement("div");
        fig.className = "pick-shot";
        const img = document.createElement("img");
        img.src = "data:image/jpeg;base64," + cardShot;
        img.alt = p.clip ? "锁定元素的裁剪图" : "锁定时的页面截图";
        img.loading = "lazy";
        // Click to open full size: a card is a summary, and a full-page shot
        // is unreadable at summary scale.
        img.onclick = () => window.open(img.src, "_blank");
        const at = p.clipPath || p.shotPath;
        img.title = at ? "点击看大图（也存于 " + at + "）" : "点击看大图";
        fig.appendChild(img);
      }

      const sel = document.createElement("div");
      sel.className = "pick-sel";
      const num = document.createElement("span");
      num.className = "pick-num";
      num.textContent = String(i + 1);
      const selText = document.createElement("span");
      selText.textContent = (p.selector || "(no selector)")
        + (p.missing || p.visible === false ? "   ⚠ 已不在页面上" : "");
      sel.append(num, selText);

      const ta = document.createElement("textarea");
      if (annotations[key] === undefined && p.annotation) annotations[key] = p.annotation;
      ta.placeholder = "annotation：这里怎么了？（例：点了没反应 / 文字被截断）";
      ta.value = annotations[key] !== undefined ? annotations[key]
        : (typed[key] !== undefined ? typed[key] : (p.annotation || ""));
      ta.dataset.selector = key;
      ta.oninput = (ev) => {
        const value = ev.target.value;
        annotations[key] = value;
        const item = picks.find((q) => keyOf(q) === key);
        if (item) item.annotation = value;
        queueAnnotation(key, value);
      };
      ta.onkeydown = (ev) => ev.stopPropagation();

      // Two insert verbs and nothing else. Both go to the chat composer; the
      // annotation rides as its own field, so the embedder never has to parse
      // it back out of the prose. 插入图片 carries the detail block (note +
      // page + DOM + image paths); 插入HTML carries the element's markup.
      const acts = document.createElement("div");
      acts.className = "pick-acts";
      const putStatus = document.createElement("span");
      putStatus.className = "pick-status";
      const noteOf = () => annotations[key] !== undefined ? annotations[key] : (p.annotation || "");
      const deliver = (payload) => {
        if (!payload) return;
        if (forwardToHost("info", payload, p, noteOf())) {
          putStatus.textContent = "已送到对话输入框";
          return;
        }
        copyText(payload).then((ok) => {
          putStatus.textContent = ok ? "已复制（没有可插入的页面）" : "复制失败";
        });
      };
      const putImg = document.createElement("button");
      putImg.textContent = "插入图片";
      putImg.title = "把这张卡片的截图路径 + DOM 信息 + annotation 送进对话输入框";
      putImg.onclick = (ev) => {
        ev.stopPropagation();
        deliver(detailBlock(p, noteOf()));
      };
      const putHtml = document.createElement("button");
      putHtml.textContent = "插入HTML";
      putHtml.title = "把这个元素的 outerHTML 送进对话输入框";
      putHtml.onclick = (ev) => {
        ev.stopPropagation();
        deliver((p.outerHTML || "").trim());
      };
      acts.append(putImg, putHtml, putStatus);

      const parts = [close];
      if (fig) parts.push(fig);
      parts.push(sel, ta, acts);
      card.append.apply(card, parts);
      stack.appendChild(card);
      if (key === focusedSel && caret != null) ta.focus();
    });
    if (focusedSel && caret != null) {
      try { document.activeElement.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
    }
  }

  document.addEventListener("visibilitychange", () => { if (document.hidden) flushAnnotations(); });
  window.addEventListener("pagehide", flushAnnotations);

  fitStage();
  connect();
})();
