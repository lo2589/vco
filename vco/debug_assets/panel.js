// vco debug panel — talks to a headless Chromium through /ws.
//
// Interaction contract (this is the whole point of the panel):
//   plain click        → forwarded to the page: buttons fire, links navigate
//   ⌘/Ctrl/Alt + click → locks the element under the cursor; the right sidebar
//                        gains a card holding its DOM + an annotation box
//   …clicking more elements keeps ADDING cards (nothing is replaced)
//   ×                  → drops that one card
(function () {
  const $ = (id) => document.getElementById(id);
  const FRAME = $("frame");
  const STAGE = $("stage");
  const VIEWPORT = $("viewport");
  const MASK = $("mask");
  const OVERLAY = $("overlay");
  const URL_BAR = $("url");
  const ERRORS = $("errors");

  let picks = [];            // canonical selection, owned by the server
  const liveErrors = [];      // console/pageerror lines, newest first
  // selector -> the user's note. The panel owns this text: the server copy only
  // ever arrives *after* a round trip, so building the report from `picks`
  // alone silently dropped a note typed moments earlier (and any re-broadcast
  // arriving mid-typing wiped it from the report).
  const annotations = {};
  const pendingAnnotations = {};
  let annotateTimer = null;
  let hotKey = null;         // card ↔ marker highlight link
  let pageContext = { url: "", title: "", consoleErrors: [], failedRequests: [] };
  const keyOf = (p) => (p && p.selector) || "";

  // --- websocket ---------------------------------------------------------
  // The panel is useless the moment the socket dies, and a dead socket used to
  // be silent: the last screenshot stayed on screen and every click vanished.
  // So: reconnect automatically, say so on screen, and keep counters of what
  // this browser actually delivered to us (that is how a "nothing happens"
  // report gets diagnosed without guessing).
  let ws = null;
  let connected = false;
  const counters = { pointerdown: 0, mousedown: 0, keydown: 0, beforeinput: 0,
                     pasted: 0, moved: 0, sent: 0, dropped: 0 };
  const lastMsg = { kind: "-", at: "-" };

  function send(obj) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify(obj));
      counters.sent++;
      lastMsg.kind = obj.type;
      lastMsg.at = new Date().toLocaleTimeString();
      renderDiag();
      return true;
    }
    counters.dropped++;
    renderDiag();
    return false;
  }

  function renderDiag() {
    const el = $("diag");
    if (!el) return;
    const editable = MASK.isContentEditable !== undefined
      ? String(MASK.isContentEditable) : "?";
    el.textContent = [
      "socket: " + (connected ? "connected" : "DISCONNECTED"),
      "editable: " + editable,
      "pointerdown: " + counters.pointerdown,
      "mousedown: " + counters.mousedown,
      "keydown: " + counters.keydown,
      "beforeinput: " + counters.beforeinput,
      "paste: " + counters.pasted,
      "sent: " + counters.sent,
      "dropped: " + counters.dropped,
      "last: " + lastMsg.kind + " @ " + lastMsg.at,
      "ua: " + navigator.userAgent,
    ].join("\n");
  }

  function connect() {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/ws");
    ws.binaryType = "arraybuffer";
    ws.addEventListener("open", () => {
      connected = true;
      $("link-banner").className = "";
      renderDiag();
      send({ type: "runs_list" });
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
      renderDiag();
      setTimeout(connect, 1200);
    });
    ws.addEventListener("error", () => { renderDiag(); });
    ws.onmessage = onMessage;
  }

  // Periodic pulse: the server records these, so after a "点了没反应" report we
  // can tell "this browser never delivered the event" from "the server never
  // acted on it".
  setInterval(() => {
    if (connected) {
      send({ type: "client_pulse", counters: Object.assign({}, counters),
             last: lastMsg.kind });
    }
  }, 5000);

  function onMessage(ev) {
    let msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "frame") {
      FRAME.src = "data:image/png;base64," + msg.png;
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
    } else if (msg.type === "runs") {
      renderRuns(msg.items || []);
    } else if (msg.type === "run_events") {
      if (msg.path === currentRun) appendRunEvents(msg.events || [], msg.next || 0);
    } else if (msg.type === "selection") {
      copyFromPage(msg.text || "");
    } else if (msg.type === "console") {
      pushError(msg.level + ": " + msg.text);
    } else if (msg.type === "pageerror") {
      pushError("pageerror: " + msg.text);
    } else if (msg.type === "pick") {
      // Server owns the selection: it accumulates one entry per locked
      // element and echoes the whole stack after every change.
      const before = picks.map(keyOf).join("|");
      const beforeIdx = detailIndex >= 0 && picks[detailIndex]
        ? keyOf(picks[detailIndex]) : "";
      picks = Array.isArray(msg.picks) ? msg.picks : [];
      picks.forEach((p) => {
        const k = keyOf(p);
        if (annotations[k] !== undefined) p.annotation = annotations[k];
      });
      // A fresh lock is the whole point of the detail block, so show it; a
      // removal just keeps the block pointing at the same element when that
      // element is still locked, and falls back to the newest one when it is
      // the element that just went away.
      if (picks.map(keyOf).join("|") !== before) {
        const stillThere = picks.findIndex((p) => keyOf(p) === beforeIdx);
        if (stillThere >= 0) {
          detailIndex = stillThere;
          renderDetail();
        } else {
          focusNewestPick();
        }
      }
      renderPicks();
      renderMarkers();
      refreshReport();
    } else if (msg.type === "error") {
      pushError("server: " + msg.text);
    } else if (msg.type === "context") {
      if (msg.payload) {
        pageContext = Object.assign({}, pageContext, msg.payload);
        refreshReport();
      }
    } else if (msg.type === "closed") {
      pushError("connection closed");
    }
  }

  // Push a fresh frame immediately when the browser tab comes back, instead of
  // showing whatever was on screen when it was hidden.
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden && connected) send({ type: "measure_picks" });
  });

  function pushError(text) {
    liveErrors.push(text);
    while (liveErrors.length > 50) liveErrors.shift();
    refreshReport();
    const div = document.createElement("div");
    div.className = "err";
    div.textContent = text;
    ERRORS.prepend(div);
    while (ERRORS.children.length > 30) ERRORS.removeChild(ERRORS.lastChild);
    const count = $("err-count");
    if (count) count.textContent = String(liveErrors.length);
  }

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
    counters.moved++;
    const p = clientToPage(e);
    if (p) send({ type: "mouse_move", x: p.x, y: p.y });
  });
  MASK.addEventListener("pointerdown", (e) => {
    counters.pointerdown++;
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
    counters.mousedown++;
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
  let lastComposedAt = 0;
  MASK.addEventListener("compositionstart", () => { composing = true; });
  MASK.addEventListener("compositionupdate", (e) => {
    $("type-status").textContent = "输入中：" + (e.data || "");
  });
  MASK.addEventListener("compositionend", (e) => {
    composing = false;
    const text = e.data || MASK.textContent || "";
    MASK.textContent = "";
    $("type-status").textContent = "";
    lastComposedAt = Date.now();
    if (text) send({ type: "insert_text", text });
  });
  // Text the browser actually inserted into the mask (typing, IME leftovers).
  MASK.addEventListener("beforeinput", (e) => {
    counters.beforeinput++;
    const type = e.inputType || "";
    if (type === "insertCompositionText") return;   // IME composes in the mask
    if (type.startsWith("insert")) {
      e.preventDefault();
      let text = e.data || "";
      if (!text && e.dataTransfer) text = e.dataTransfer.getData("text/plain") || "";
      if (text) send({ type: "insert_text", text });
    }
  });
  // Safety net: never let the mask keep text (it is only a keyboard sink). The
  // composition guard matters: a browser that commits the composition into the
  // mask *after* compositionend would otherwise send the same text twice.
  MASK.addEventListener("input", () => {
    if (composing) return;
    const text = MASK.textContent;
    if (!text) return;
    MASK.textContent = "";
    if (Date.now() - lastComposedAt < 400) return;
    send({ type: "insert_text", text });
  });
  MASK.addEventListener("paste", (e) => {
    counters.pasted++;
    e.preventDefault();
    const text = (e.clipboardData && e.clipboardData.getData("text/plain")) || "";
    if (text) {
      lastPasteAt = Date.now();
      send({ type: "insert_text", text });
      $("type-status").textContent = "已粘贴 " + text.length + " 字";
    }
  });
  MASK.addEventListener("copy", (e) => {
    e.preventDefault();
    send({ type: "copy_selection" });
  });
  MASK.addEventListener("keydown", (e) => {
    counters.keydown++;
    renderDiag();
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
            if (text) {
              send({ type: "insert_text", text });
              $("type-status").textContent = "已粘贴 " + text.length + " 字";
            }
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
    if (!text) {
      $("type-status").textContent = "页面里没有选中内容";
      return;
    }
    const helper = $("clip-helper");
    helper.value = text;
    helper.select();
    let ok = false;
    try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
    if (!ok && navigator.clipboard) {
      navigator.clipboard.writeText(text).then(
        () => { $("type-status").textContent = "已复制 " + text.length + " 字"; },
        () => { $("type-status").textContent = "复制失败：" + text.slice(0, 40); });
      return;
    }
    $("type-status").textContent = ok
      ? "已复制 " + text.length + " 字"
      : "复制失败：" + text.slice(0, 40);
  }

  // The 「送入页面文字」 box is gone from the sidebar: a text field plus a send
  // button next to the picker read as one more thing to fill in, and typing into
  // the page is what the controlled browser itself is for. The status line stays
  // (paste/copy feedback reports there); the transport below still works for any
  // caller that brings its own box.
  function sendTypedText(text) {
    const value = String(text === undefined ? "" : text);
    if (!value) return;
    send({ type: "insert_text", text: value });
    const status = $("type-status");
    if (status) status.textContent = "已发送 " + value.length + " 字到页面";
  }

  $("btn-back").onclick = () => send({ type: "back" });
  $("btn-forward").onclick = () => send({ type: "forward" });
  $("btn-reload").onclick = () => send({ type: "reload" });
  $("btn-clear-picks").onclick = () => send({ type: "clear_picks" });
  $("btn-clear-errors").onclick = () => {
    ERRORS.textContent = "";
    liveErrors.length = 0;
    const count = $("err-count");
    if (count) count.textContent = "0";
    refreshReport();
  };
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
  // One card per locked element: what was locked (DOM), and a box for the
  // user's annotation. Cards accumulate; nothing here replaces a previous one.
  // --- the floating detail block -----------------------------------------
  // A locked element answers four questions — which one, how to reach it, what
  // it says, and who owns it — and then offers to hand that answer to whoever
  // is looking at this panel. It is rendered as the sticky top of the sidebar
  // so a fresh pick is readable without scrolling and its buttons never leave
  // reach, and it always describes the pick that just happened.
  // Standing down: the report builder already turns the whole stack into
  // Markdown; this block is the one-click path for the element in hand.
  let detailIndex = -1;        // index into picks, or -1 when nothing is locked
  let detailNote = "";         // the note typed in this block
  let statusTimer = null;

  function detailButtons(pick, index) {
    const text = (pick.text || "").trim();
    const html = (pick.outerHTML || "").trim();
    return [
      ["选择器", pick.selector || "", "selector"],
      ["HTML", html, "html"],
      ["文字", text, "text"],
    ].map(([label, value, kind]) => ({
      label, kind, value,
      title: value ? value.slice(0, 90) : "(空)",
      disabled: !value,
    })).concat([{
      label: "全部信息", kind: "info", value: "", title: "选择器 + 文字 + HTML + 归属",
      disabled: false, index,
    }]);
  }

  // One compact line of Markdown-ish text, which is what both the panel's own
  // report and the chat composer render well.
  function detailBlock(pick, noteOverride) {
    const lines = [];
    // noteOverride lets a stack card build ITS OWN block (its own annotation)
    // instead of borrowing whatever is typed in the floating detail block.
    const note = (noteOverride !== undefined ? noteOverride : detailNote || "").trim();
    if (note) lines.push(note);
    lines.push("- 选择器: `" + (pick.selector || "?") + "`");
    if (pick.text) lines.push("- 文字: “" + pick.text.slice(0, 160) + "”");
    if (pick.owner) lines.push("- 归属: " + ownerText(pick.owner));
    if (pick.outerHTML) lines.push("- HTML: `" + pick.outerHTML.replace(/`/g, "'").slice(0, 300) + "`");
    return lines.join("\n");
  }

  function actionPayload(pick, kind) {
    if (kind === "selector") return pick.selector || "";
    if (kind === "html") return (pick.outerHTML || "").trim();
    if (kind === "text") return (pick.text || "").trim();
    return detailBlock(pick);
  }

  // Hand the payload to whoever embedded this panel (dsh's own page, a browser
  // tab: anyone listening on window.parent). The panel never assumes a parent
  // is there — in a bare tab the buttons simply copy, which is the fallback.
  function forwardToHost(kind, text, pick, noteOverride) {
    let delivered = false;
    // The annotation travels as its OWN field, not merely baked into `text`:
    // the embedder decides where the note lands, and a per-field payload
    // (选择器 / HTML / 文字) carries no prose of its own to carry it in.
    // detailNote is what is typed in the box right now; pick.annotation is this
    // pick's stored note.
    const note = (noteOverride !== undefined
      ? noteOverride
      : (detailNote || (pick && pick.annotation) || "")).trim();
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

  function flashDetail(msg, ok) {
    const el = $("detail-status");
    if (!el) return;
    el.textContent = msg;
    el.className = ok ? "ok" : "";
    if (statusTimer) clearTimeout(statusTimer);
    statusTimer = setTimeout(() => {
      if (el) { el.textContent = ""; el.className = ""; }
      statusTimer = null;
    }, 2600);
  }

  function copyText(text) {
    if (!navigator.clipboard) return Promise.resolve(false);
    return navigator.clipboard.writeText(text).then(() => true, () => false);
  }

  function renderDetail() {
    const card = $("detail-card");
    if (!card) return;
    card.textContent = "";
    const pick = detailIndex >= 0 ? picks[detailIndex] : null;
    if (!pick) return;

    const box = document.createElement("div");
    box.className = "fcard";
    box.onmouseenter = () => { hotKey = keyOf(pick); renderMarkers(); };
    box.onmouseleave = () => { hotKey = null; renderMarkers(); };

    const head = document.createElement("div");
    head.className = "fcard-head";
    const num = document.createElement("span");
    num.className = "pick-num";
    num.textContent = String(detailIndex + 1);
    const title = document.createElement("span");
    title.className = "grow";
    const cls = pick.className
      ? "." + pick.className.split(/\s+/).filter(Boolean).slice(0, 3).join(".") : "";
    title.textContent = "<" + (pick.tag || "?") + ">" + (pick.id ? "#" + pick.id : "") + cls;
    const close = document.createElement("button");
    close.textContent = "×";
    // Delete, not collapse: the block is the one place a locked element is
    // managed, so its × drops the pick from the server's stack (the server
    // echoes the new stack back). Collapsing is what the panel's own footer
    // does when you pick the next element.
    close.title = "删除这个锁定元素";
    close.onclick = () => { send({ type: "remove_pick", selector: keyOf(pick) }); };

    // Clearing the whole stack lives up here, on the sticky block, because the
    // cards themselves sit under the fold of a long sidebar: deleting them one
    // by one means scrolling to each. This control never scrolls away.
    const clearAll = document.createElement("button");
    clearAll.className = "clear-all";
    clearAll.textContent = "清空 " + picks.length;
    clearAll.title = "删除全部锁定元素";
    clearAll.onclick = () => { send({ type: "clear_picks" }); };

    head.append(num, title, clearAll, close);

    const sel = document.createElement("div");
    sel.className = "fcard-sel";
    sel.textContent = pick.selector || "(no selector)";

    const meta = document.createElement("div");
    meta.className = "fcard-meta";
    meta.textContent = (pick.rect ? Math.round(pick.rect.w) + "×" + Math.round(pick.rect.h) : "?×?")
      + "  ·  role " + (pick.role || "?")
      + "  ·  children " + (pick.childCount != null ? pick.childCount : "?")
      + (pick.missing ? "  ·  ⚠ 已不在页面上" : "");

    const owner = document.createElement("div");
    owner.className = "fcard-meta";
    owner.textContent = "归属: " + (ownerText(pick.owner) || "(无)");

    box.append(head, sel, meta, owner);

    if (pick.outerHTML) {
      const html = document.createElement("div");
      html.className = "fcard-html";
      html.textContent = pick.outerHTML;
      html.title = pick.outerHTML;
      box.appendChild(html);
    }

    const anno = document.createElement("input");
    anno.className = "anno";
    anno.placeholder = "这里怎么了？（例：点了没反应）";
    if (annotations[detailKey()] === undefined && pick.annotation) {
      annotations[detailKey()] = pick.annotation;
    }
    anno.value = detailNote !== "" ? detailNote
      : (annotations[detailKey()] !== undefined ? annotations[detailKey()]
        : (pick.annotation || ""));
    anno.oninput = (ev) => {
      detailNote = ev.target.value;
      const key = detailKey();
      annotations[key] = detailNote;
      const item = picks.find((q) => keyOf(q) === key);
      if (item) item.annotation = detailNote;
      refreshReport();
      queueAnnotation(key, detailNote);
    };
    anno.onkeydown = (ev) => ev.stopPropagation();
    box.appendChild(anno);

    const acts = document.createElement("div");
    acts.className = "fcard-acts";
    detailButtons(pick, detailIndex).forEach((spec) => {
      const b = document.createElement("button");
      b.textContent = spec.label;
      b.title = spec.title;
      b.disabled = spec.disabled;
      if (spec.kind === "info") {
        b.className = "wide";
        b.onclick = () => {
          const payload = detailBlock(pick);
          if (forwardToHost("info", payload, pick)) {
            flashDetail("已送到对话：等它在输入框里出现再决定发送", true);
            return;
          }
          copyText(payload).then((ok) => flashDetail(
            ok ? "没有可插入的页面，已复制全部信息" : "复制失败", ok));
        };
        acts.appendChild(b);
        return;
      }
      b.onclick = () => {
        const payload = actionPayload(pick, spec.kind);
        if (!payload) return;
        // Inside dsh the panel pushes into the chat composer; on its own it
        // copies, which is the same information with one extra paste.
        if (forwardToHost("insert-text", payload, pick)) {
          flashDetail("已送到对话输入框：" + spec.label, true);
          return;
        }
        copyText(payload).then((ok) => flashDetail(
          ok ? "已复制 " + spec.label + "（" + payload.length + " 字）" : "复制失败", ok));
      };
      acts.appendChild(b);
    });
    box.appendChild(acts);

    const status = document.createElement("div");
    status.id = "detail-status";
    box.appendChild(status);

    card.appendChild(box);
  }

  function detailKey() {
    const pick = detailIndex >= 0 ? picks[detailIndex] : null;
    return pick ? keyOf(pick) : "";
  }

  // A new pick is what this block describes: jump to the newest one, and keep
  // the sidebar at its top so the block is on screen when the pick arrives.
  function focusNewestPick() {
    detailNote = "";
    if (!picks.length) { detailIndex = -1; renderDetail(); return; }
    detailIndex = picks.length - 1;
    renderDetail();
    const right = $("right");
    if (right) right.scrollTop = 0;
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

      const head = document.createElement("div");
      head.className = "pick-head";
      const num = document.createElement("span");
      num.className = "pick-num";
      num.textContent = String(i + 1);
      const label = document.createElement("span");
      const cls = p.className ? "." + p.className.split(/\s+/).filter(Boolean).slice(0, 3).join(".") : "";
      label.textContent = "<" + (p.tag || "?") + ">" + (p.id ? "#" + p.id : "") + cls;
      const sel = document.createElement("div");
      sel.className = "pick-sel";
      sel.textContent = p.selector || "(no selector)";
      head.append(num, label, sel);

      const meta = document.createElement("div");
      meta.className = "pick-meta";
      const locked = p.lockedAt ? new Date(p.lockedAt).toLocaleTimeString() : "—";
      meta.textContent = (p.rect ? Math.round(p.rect.w) + "×" + Math.round(p.rect.h) : "?×?")
        + "  ·  role " + (p.role || "?")
        + "  ·  children " + (p.childCount != null ? p.childCount : "?")
        + "  ·  locked " + locked
        + (p.missing ? "  ·  ⚠ 元素已不在页面上" : "");
      const path = document.createElement("div");
      path.className = "pick-meta";
      path.textContent = "path: " + (p.path || p.selector || "");

      const attrs = document.createElement("div");
      attrs.className = "pick-meta";
      const attrText = Object.entries(p.attrs || {})
        .map(([k, v]) => k + '="' + v + '"').join("  ");
      attrs.textContent = "attrs: " + (attrText || "(none)");

      const text = document.createElement("div");
      text.className = "pick-meta";
      text.textContent = "text: " + (p.text ? "“" + p.text.slice(0, 120) + "”" : "(empty)");

      const html = document.createElement("div");
      html.className = "pick-html";
      html.textContent = p.outerHTML || "(no outerHTML)";

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
        if (item) item.annotation = value;      // keep the live report honest
        refreshReport();
        queueAnnotation(key, value);
      };
      ta.onkeydown = (ev) => ev.stopPropagation();

      // Each card carries its own pair: DOM info + ITS annotation, straight to
      // the chat composer. The annotation is sent as a field as well, so the
      // embedder never has to parse it back out of the prose.
      const acts = document.createElement("div");
      acts.className = "pick-acts";
      const put = document.createElement("button");
      put.textContent = "插入对话";
      put.title = "把这张卡片的 DOM 信息 + annotation 送进对话输入框";
      const putStatus = document.createElement("span");
      putStatus.className = "pick-status";
      put.onclick = (ev) => {
        ev.stopPropagation();
        const note = annotations[key] !== undefined ? annotations[key] : (p.annotation || "");
        const payload = detailBlock(p, note);
        if (forwardToHost("info", payload, p, note)) {
          putStatus.textContent = "已送到对话输入框";
          return;
        }
        copyText(payload).then((ok) => {
          putStatus.textContent = ok ? "已复制（没有可插入的页面）" : "复制失败";
        });
      };
      acts.append(put, putStatus);

      card.append(close, head, meta, path, attrs, text, html, ta, acts);
      stack.appendChild(card);
      if (key === focusedSel && caret != null) ta.focus();
    });
    if (focusedSel && caret != null) {
      try { document.activeElement.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
    }
  }

  // --- 运行记录: what the automated side did -------------------------------
  // `vco webclick` / `vco webrun` write one JSON line per step into
  // <run>/events.jsonl and leave step screenshots next to it. This is a
  // read-only view of that stream: the panel is where you look at a page by
  // hand, so it is also where you look at what the automation did to one.
  let runsItems = [];
  let currentRun = null;
  let runCursor = 0;
  let runPollTimer = null;
  const KIND_LABELS = {
    step_start: "开始", observation: "看到", action_result: "动作",
    page_error: "报错", user_click: "人工点击", annotation: "批注", run_end: "结束",
  };

  function renderRuns(items) {
    runsItems = items;
    const sel = $("runs");
    if (!sel) return;
    const keep = currentRun;
    sel.textContent = "";
    if (!items.length) {
      const opt = document.createElement("option");
      opt.value = "";
      opt.textContent = "（还没有 run：跑一次 vco webclick 就会出现在这里）";
      sel.appendChild(opt);
      currentRun = null;
      $("run-events").textContent = "";
      return;
    }
    items.forEach((r) => {
      const opt = document.createElement("option");
      opt.value = r.path;
      const when = new Date((r.mtime || 0) * 1000).toLocaleTimeString();
      opt.textContent = r.path + "  ·  " + when + (r.first_summary ? "  ·  " + r.first_summary : "");
      sel.appendChild(opt);
    });
    if (keep && items.some((r) => r.path === keep)) {
      sel.value = keep;
    } else {
      currentRun = items[0].path;
      sel.value = currentRun;
      openRun(currentRun);
    }
    startRunPolling();
  }

  function openRun(path) {
    currentRun = path;
    runCursor = 0;
    $("run-events").textContent = "";
    $("run-preview").textContent = "";
    send({ type: "run_poll", path: path, after: 0 });
  }

  function appendRunEvents(events, next) {
    runCursor = next;
    const box = $("run-events");
    if (!events.length) {
      if (!box.children.length) {
        box.textContent = "";
        const empty = document.createElement("div");
        empty.className = "pick-meta";
        empty.textContent = "（这个 run 还没有事件）";
        box.appendChild(empty);
      }
      return;
    }
    if (box.firstChild && box.firstChild.className === "pick-meta") box.textContent = "";
    events.forEach((ev) => {
      const row = document.createElement("div");
      row.className = "run-event" + (ev.kind === "page_error" ? " err" : "");
      const t = document.createElement("span");
      t.className = "t";
      t.textContent = ev.ts ? new Date(ev.ts).toLocaleTimeString() : "--:--:--";
      const k = document.createElement("span");
      k.className = "k";
      k.textContent = KIND_LABELS[ev.kind] || ev.kind || "?";
      const sum = document.createElement("span");
      sum.className = "s";
      sum.textContent = [ev.step != null ? "#" + ev.step : "", ev.summary || ""]
        .filter(Boolean).join(" ");
      row.append(t, k, sum);
      if (ev.image) {
        const img = document.createElement("img");
        img.className = "thumb";
        img.src = "/runs/" + currentRun + "/" + ev.image;
        img.title = ev.image;
        img.onclick = () => {
          $("run-preview").textContent = "";
          const big = document.createElement("img");
          big.src = img.src;
          $("run-preview").appendChild(big);
        };
        row.appendChild(img);
      }
      box.appendChild(row);
      box.scrollTop = box.scrollHeight;
    });
  }

  function pollRun() {
    if (currentRun) send({ type: "run_poll", path: currentRun, after: runCursor });
  }

  function startRunPolling() {
    if (runPollTimer) return;
    runPollTimer = setInterval(pollRun, 2000);
  }

  $("runs").onchange = (e) => openRun(e.target.value);
  $("btn-runs-refresh").onclick = () => send({ type: "runs_list" });

  // --- export: 哪里错了 + 谁错了 ------------------------------------------
  // This is the whole product of the panel. It is a report, not a prompt: the
  // page it happened on, then for every element the user locked — where it is,
  // what it is, which part of the UI owns it, and what the user says is wrong —
  // plus the console/network evidence that usually names the culprit.
  // Sending is debounced; *exporting* must not be, or a note typed a moment
  // earlier is missing from the report the user just copied.
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

  function ownerText(o) {
    if (!o) return "";
    let t = "<" + o.tag + ">" + (o.id ? "#" + o.id : "")
      + (o.cls ? "." + o.cls.split(".").join(".") : "");
    const bits = [];
    if (o.role) bits.push("role=" + o.role);
    if (o.label) bits.push('aria-label="' + o.label + '"');
    if (o.hook) bits.push(o.hook);
    if (bits.length) t += "  (" + bits.join(", ") + ")";
    if (o.hops) t += "   ↑" + o.hops + " 层";
    return t;
  }

  function reportTime() {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return d.getFullYear() + "-" + pad(d.getMonth() + 1) + "-" + pad(d.getDate())
      + " " + pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  }

  function buildReport() {
    const { w, h } = frameSize();
    const out = [];
    out.push("# vco 现场报告 — " + reportTime());
    out.push("");
    out.push("- 页面: " + (pageContext.url || "(未知)"));
    out.push("- 标题: " + (pageContext.title || "(无)"));
    out.push("- 视口: " + w + "×" + h);
    out.push("- 锁定元素: " + picks.length);
    out.push("");
    if (!picks.length) {
      out.push("_还没有锁定任何元素：在视口里 ⌘/Ctrl + 点击出问题的元素，");
      out.push("然后在卡片里写一句“这里怎么了”。_");
      out.push("");
    }
    picks.forEach((p, i) => {
      const r = p.rect || { w: 0, h: 0, x: 0, y: 0 };
      const note = annotations[keyOf(p)] !== undefined
        ? annotations[keyOf(p)] : (p.annotation || "");
      out.push("## " + (i + 1) + ". " + (note ? note : "（未写标注）"));
      out.push("");
      out.push("- 选择器: `" + (p.selector || "?") + "`");
      out.push("- 位置: " + Math.round(r.w) + "×" + Math.round(r.h)
        + " @ (" + Math.round(r.x) + ", " + Math.round(r.y) + ")"
        + (p.visible === false || p.missing ? "  **不可见/已不在页面上**" : ""));
      out.push("- 是谁: " + ownerText(p.owner) || "- 是谁: (没找到可识别的容器)");
      out.push("- 路径: " + (p.path || ""));
      out.push("- 元素: `" + (p.outerHTML || "").replace(/`/g, "'").slice(0, 300) + "`");
      if (p.text) out.push("- 文本: " + JSON.stringify(p.text));
      const attrs = Object.entries(p.attrs || {}).map(([k, v]) => k + '="' + v + '"');
      if (attrs.length) out.push("- 属性: " + attrs.join(" "));
      if (p.lockedAt) out.push("- 锁定时间: " + new Date(p.lockedAt).toLocaleString());
      out.push("");
    });
    const errs = liveErrors.slice(-10);
    if (errs.length) {
      out.push("## 控制台错误（最近 " + errs.length + " 条）");
      out.push("");
      errs.forEach((e) => out.push("- " + e));
      out.push("");
    }
    const failed = (pageContext.failedRequests || []).slice(-10);
    if (failed.length) {
      out.push("## 失败请求（最近 " + failed.length + " 条）");
      out.push("");
      failed.forEach((f) => out.push("- " + (f.status || f.error || "?") + "  " + f.url));
      out.push("");
    }
    return out.join("\n");
  }

  // The report is live in the sidebar, and pulling the page context (console
  // errors, failed requests) is throttled: it is one evaluate round trip and
  // nothing changes that fast.
  let lastContextAt = 0;
  function requestContext() {
    if (Date.now() - lastContextAt < 1000) return;
    lastContextAt = Date.now();
    send({ type: "context" });
  }

  function refreshReport() {
    const el = $("report-preview");
    if (el) el.textContent = buildReport();
    requestContext();
  }

  function downloadBlob(blob, filename) {
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 4000);
  }

  function stampedName(ext) {
    const d = new Date();
    const pad = (n) => String(n).padStart(2, "0");
    return "vco-" + d.getFullYear() + pad(d.getMonth() + 1) + pad(d.getDate())
      + "-" + pad(d.getHours()) + pad(d.getMinutes()) + pad(d.getSeconds()) + ext;
  }

  // The annotated screenshot answers "哪里" without words: same frame the user
  // is looking at, with the locked elements boxed and numbered like the cards.
  function downloadAnnotatedShot() {
    const { w, h } = frameSize();
    const canvas = document.createElement("canvas");
    canvas.width = w;
    canvas.height = h;
    const g = canvas.getContext("2d");
    g.drawImage(FRAME, 0, 0, w, h);
    g.lineWidth = 3;
    g.font = "bold 16px monospace";
    picks.forEach((p, i) => {
      if (!p.rect) return;
      g.strokeStyle = "#f97316";
      g.strokeRect(p.rect.x, p.rect.y, p.rect.w, p.rect.h);
      const label = "#" + (i + 1);
      const tw = g.measureText(label).width + 10;
      let ly = p.rect.y - 20;
      if (ly < 0) ly = p.rect.y + 2;
      g.fillStyle = "#f97316";
      g.fillRect(p.rect.x, ly, tw, 20);
      g.fillStyle = "#fff";
      g.fillText(label, p.rect.x + 5, ly + 15);
    });
    canvas.toBlob((blob) => {
      if (!blob) {
        $("export-status").textContent = "截图导出失败";
        return;
      }
      downloadBlob(blob, stampedName(".png"));
      $("export-status").textContent = "已下载标注截图（" + picks.length + " 个框）";
    }, "image/png");
  }

  $("btn-copy-report").onclick = () => {
    flushAnnotations();
    const text = buildReport();
    if (navigator.clipboard) {
      navigator.clipboard.writeText(text).then(
        () => { $("export-status").textContent = "已复制报告（" + text.length + " 字）"; },
        () => { $("export-status").textContent = "复制失败，报告已显示在下方，手动选吧"; });
    }
  };
  $("btn-download-report").onclick = () => {
    flushAnnotations();
    const text = buildReport();
    downloadBlob(new Blob([text], { type: "text/markdown;charset=utf-8" }),
                 stampedName(".md"));
    $("export-status").textContent = "已下载报告（" + text.length + " 字）";
  };
  $("btn-download-shot").onclick = downloadAnnotatedShot;
  document.addEventListener("visibilitychange", () => { if (document.hidden) flushAnnotations(); });
  window.addEventListener("pagehide", flushAnnotations);

  fitStage();
  renderDiag();
  renderDetail();
  refreshReport();
  connect();
})();
