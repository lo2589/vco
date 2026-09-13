"""Local debug server: drive a real browser through an inspector panel.

Usage from CLI: ``vco debug [url]``. Spawns a local HTTP server plus a
Playwright browser (persistent profile) and:

* opens a viewport in the side panel: screenshots stream down through a
  WebSocket; mouse / keyboard / wheel events flow back up, so the page inside
  the panel is usable like a page — plain clicks and typing go through
* injects a hover/click inspector into every page the user visits
  (works across origins, since it's CDP-level, not iframe-level)
* records what the user locks (⌘/Ctrl+click): the element's DOM, where it sits,
  which part of the UI owns it, and the user's own annotation. The panel is the
  *information* layer and its product is an exportable incident report.

It deliberately does not call a model or patch the page: it captures the scene
faithfully, and something else decides what to do with it.

The server is intentionally local-only. If the user binds a non-loopback
host, a bearer token is required (mirroring ``vco watch``).
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import re
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

from . import monitor
from .dommarks import _SNAPSHOT_JS as MARKS_JS

# `VCO_DEBUG_TRACE=1` prints every panel→browser message to stderr; any other
# value is treated as a log file path (keeps the panel's own stdout clean while
# still recording what a real browser sent). The panel is a remote control for a
# real browser: when a click "does nothing", watching this side is the only way
# to tell a lost message from a rejected pick — and `client_pulse` shows whether
# the browser ever delivered the input event at all.
TRACE_VALUE = os.environ.get("VCO_DEBUG_TRACE", "")


def _trace(*parts: object) -> None:
    if not TRACE_VALUE:
        return
    line = "[vco-debug] " + " ".join(str(p) for p in parts)
    if TRACE_VALUE == "1":
        print(line, file=sys.stderr, flush=True)
        return
    try:
        with open(TRACE_VALUE, "a", encoding="utf-8") as fh:
            fh.write(time.strftime("%H:%M:%S ") + line + "\n")
    except OSError:
        pass


DEBUG_ASSETS = Path(__file__).parent / "debug_assets"

# --- harness (`dsh web`) browser auth ---------------------------------------
#
# The harness GUI (``dsh web``, usually http://127.0.0.1:3080) refuses every
# request without a signed browser cookie:
#
#   name  = "dsh-auth-" + base64url(sha256(authority))
#   value = "v1." + base64url(json payload) + "." + base64url(hmac_sha256(secret, body))
#   payload = {"version":1, "authority":host[:port], "issuedAt":ms, "expiresAt":ms}
#
# The token query parameter printed by ``dsh web`` is process-local and cannot
# be recovered afterwards, so a controlled Chromium can never obtain the cookie
# by "logging in". The signing secret *is* persisted in the harness home, so we
# mint the cookie ourselves: same algorithm, same secret, zero user steps.
# Without this, the panel can only ever show the harness's plain-text 401 page.

DSH_CREDENTIALS = Path.home() / ".dsh" / ".credentials.yaml"
DSH_AUTH_RECORD = "client-connection/browser-session"
DSH_COOKIE_PREFIX = "dsh-auth-"
DSH_COOKIE_VERSION = 1
DSH_COOKIE_MAX_AGE_DAYS = 30


def _b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64url(text: str) -> bytes | None:
    if not re.fullmatch(r"[A-Za-z0-9_-]*", text or "") or len(text) % 4 == 1:
        return None
    try:
        return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))
    except Exception:  # noqa: BLE001
        return None


def load_dsh_secret(path: Path | None = None) -> bytes | None:
    """Read the harness browser-session signing secret, or None if absent."""

    creds = Path(path) if path is not None else DSH_CREDENTIALS
    try:
        text = creds.read_text(encoding="utf-8")
    except OSError:
        return None
    idx = text.find(DSH_AUTH_RECORD + ":")
    if idx < 0:
        return None
    match = re.search(r"^\s+secret:\s*(\S+)\s*$", text[idx:], re.MULTILINE)
    if not match:
        return None
    secret = _unb64url(match.group(1))
    return secret if secret and len(secret) == 32 else None


def dsh_cookie_name(authority: str) -> str:
    import hashlib

    return DSH_COOKIE_PREFIX + _b64url(hashlib.sha256(authority.encode()).digest())


def dsh_cookie_value(authority: str, secret: bytes, *, max_age_days: int = DSH_COOKIE_MAX_AGE_DAYS,
                     now_ms: int | None = None) -> str:
    import hashlib
    import hmac
    import time as _time

    issued = int(now_ms if now_ms is not None else _time.time() * 1000)
    payload = {
        "version": DSH_COOKIE_VERSION,
        "authority": authority,
        "issuedAt": issued,
        "expiresAt": issued + max_age_days * 24 * 3600 * 1000,
    }
    body = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = _b64url(hmac.new(secret, body.encode("ascii"), hashlib.sha256).digest())
    return "v1." + body + "." + sig


def _with_scheme(url: str) -> str:
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", url or ""):
        return url
    return "http://" + (url or "")


def authority_of(url: str) -> str:
    """Return ``host[:port]`` exactly as the harness derives it from Host."""

    parts = urlsplit(_with_scheme(url))
    host = parts.hostname or ""
    if not host:
        return ""
    if ":" in host:  # IPv6 literal
        host = "[" + host + "]"
    default = {"http": 80, "https": 443}.get(parts.scheme)
    if parts.port and parts.port != default:
        return host + ":" + str(parts.port)
    return host


def is_loopback(url: str) -> bool:
    return (urlsplit(_with_scheme(url)).hostname or "") in {"127.0.0.1", "localhost", "::1"}


def dsh_cookie_for(url: str, secret: bytes) -> dict | None:
    """Build a Playwright cookie dict authenticating ``url``'s authority."""

    url = _with_scheme(url)
    authority = authority_of(url)
    if not authority:
        return None
    return {
        "name": dsh_cookie_name(authority),
        "value": dsh_cookie_value(authority, secret),
        # Playwright accepts either `url` or `domain`+`path`, never both.
        "url": url,
        "httpOnly": True,
        # The harness sets its own cookie SameSite=Strict, which means a
        # top-level navigation *from another origin* (clicking a link to :3080
        # while inspecting some other page) arrives without the cookie and gets
        # the 401 page. The harness only validates name+signature, so Lax —
        # which still sends on top-level navigations — just removes that trap.
        "sameSite": "Lax",
        "expires": time.time() + DSH_COOKIE_MAX_AGE_DAYS * 24 * 3600,
    }


def dsh_cookie_urls(url: str) -> list[str]:
    """Loopback origins worth pre-authenticating for ``url`` (host aliases too)."""

    parts = urlsplit(_with_scheme(url))
    if not is_loopback(url):
        return []
    scheme = parts.scheme or "http"
    port = parts.port or (443 if scheme == "https" else 80)
    suffix = "" if port in (80, 443) else ":" + str(port)
    return [
        scheme + "://" + host + suffix + "/"
        for host in ("127.0.0.1", "localhost", "[::1]")
    ]


# --- websocket helper (RFC 6455, minimal) -----------------------------------

def _ws_handshake_headers(key: str) -> dict:
    import hashlib
    accept = base64.b64encode(
        hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode()).digest()
    ).decode("ascii")
    return {
        "Upgrade": "websocket",
        "Connection": "Upgrade",
        "Sec-WebSocket-Accept": accept,
    }


# Every message the panel may send. Anything else is dropped, so a kind
# missing here is a feature that silently does nothing — which is exactly how
# annotations were lost: the handler existed, the panel send them, and this
# whitelist threw them away. tests/test_debug_panel.py cross-checks the set
# against every `send({type: ...})` in panel.js.
CLIENT_MESSAGES = frozenset({
    "mouse_move", "mouse_down", "mouse_up", "mouse_up_with_pick", "click", "wheel",
    "key", "insert_text", "copy_selection",
    "goto", "back", "forward", "reload",
    "pick_at", "remove_pick", "clear_picks", "measure_picks", "annotate",
    "context", "client_info", "client_pulse",
    "runs_list", "run_poll",
    # Remote-control verbs: another process (e.g. `vco webclick --panel`) drives
    # *this* browser, so its actions are visible in the panel instead of
    # happening in a second, invisible browser of its own. Every one of them
    # answers with {type:"cmd_result", req_id, ...} when the sender asks for a
    # reply, which is what makes them awaitable.
    "dom_marks", "dom_click", "dom_fill", "dom_wait_text", "flash", "shot",
})


def _ws_decode_frame(buf: bytes) -> tuple[int, bytes, int] | None:
    """Decode the first ws frame in ``buf``.

    Returns ``(opcode, payload, consumed)`` or None if the buffer does not hold
    a whole frame yet. ``consumed`` matters: a client that fires two messages
    back to back (a move followed by a click) gets them coalesced into one TCP
    read, and a decoder that does not report how much it ate silently throws
    the second message away — which looks exactly like "my click did nothing".
    """

    if len(buf) < 2:
        return None
    b1, b2 = buf[0], buf[1]
    opcode = b1 & 0x0F
    masked = (b2 & 0x80) != 0
    length = b2 & 0x7F
    idx = 2
    if length == 126:
        if len(buf) < 4:
            return None
        length = struct.unpack(">H", buf[2:4])[0]
        idx = 4
    elif length == 127:
        if len(buf) < 10:
            return None
        length = struct.unpack(">Q", buf[2:10])[0]
        idx = 10
    if masked:
        if len(buf) < idx + 4:
            return None
        mask = buf[idx:idx + 4]
        idx += 4
    else:
        mask = None
    if len(buf) < idx + length:
        return None
    payload = bytes(buf[idx:idx + length])
    if mask:
        payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
    return opcode, payload, idx + length


def _ws_encode(payload: bytes, opcode: int = 1) -> bytes:
    header = bytes([0x80 | (opcode & 0x0F)])
    n = len(payload)
    if n < 126:
        header += bytes([n])
    elif n < 65536:
        header += bytes([126]) + struct.pack(">H", n)
    else:
        header += bytes([127]) + struct.pack(">Q", n)
    return header + payload


# --- vision Q&A client (used by `vco debug-ask`) ----------------------------

def _call_chat_text(
    *, base_url: str, api_key: str, model: str, system: str, user: str,
    image_b64: str | None = None,
) -> str:
    """Plain-text chat call (no JSON schema). Optionally attach one image."""

    import urllib.error
    import urllib.request

    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        url = base
    elif base.endswith("/v1"):
        url = base + "/chat/completions"
    else:
        url = base + "/chat/completions"
    user_content = []
    if image_b64:
        user_content.append({
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64," + image_b64},
        })
    user_content.append({"type": "text", "text": user})
    body = {
        "model": model,
        "temperature": 0,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_content},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": "Bearer " + api_key,
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        raw = resp.read().decode("utf-8")
    data = json.loads(raw)
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("LLM response missing choices[0].message.content: " + raw[:200]) from exc
    if isinstance(content, list):
        return "".join(p.get("text", "") for p in content if isinstance(p, dict))
    return str(content)


# --- main debug session -----------------------------------------------------

class DebugSession:
    """Owns a Playwright browser (async), the WebSocket clients, and the screenshot loop.

    All Playwright calls run on a single asyncio event loop in a dedicated
    thread. The HTTP server runs in the main thread; cross-thread calls go
    through ``loop.call_soon_threadsafe`` or ``asyncio.run_coroutine_threadsafe``.
    """

    INJECT_SCRIPT = (DEBUG_ASSETS / "inject.js").read_text(encoding="utf-8")

    def __init__(self, *, profile_dir: Path, headful: bool, target: str | None,
                 use_chrome_profile: str | None = None,
                 dsh_auth: bool = False, extra_headers: dict | None = None,
                 runs_root: Path | None = None):
        self.profile_dir = profile_dir
        self.headful = headful
        self.target = target
        self.use_chrome_profile = use_chrome_profile
        # Where `vco webclick` / `vco webrun` / `vco watch` leave their run
        # directories. The panel reads them so an automated run shows up in the
        # same place you inspect pages by hand.
        self.runs_root = Path(runs_root) if runs_root else Path("cache")
        self.dsh_auth = dsh_auth
        self.extra_headers = dict(extra_headers or {})
        self.dsh_secret: bytes | None = load_dsh_secret() if dsh_auth else None
        self._dsh_done: set[str] = set()
        self.ws_clients: list[tuple] = []  # (handler, send_lock, wfile)
        self.ws_lock = threading.Lock()
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: threading.Thread | None = None
        self._stopping = False
        self.last_pick: dict | None = None
        self.last_picks: list[dict] = []
        # selector -> the user's note. Kept beside the picks so that re-locking
        # an element, re-measuring rects, or clearing and re-locking the stack
        # never silently discards what the user wrote.
        self.annotations: dict[str, str] = {}
        self.last_url: str = ""
        self._last_hover_ts = 0.0
        self._inject_ok = False
        self.client_info: dict = {}
        self.client_pulse: dict = {}
        self._last_shot_error = ""

    # --- browser lifecycle --------------------------------------------------

    def start(self) -> None:
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(
            target=self._run_loop, name="vco-debug-pw", daemon=True
        )
        self.loop_thread.start()
        # Wait for the loop to be ready.
        fut = asyncio.run_coroutine_threadsafe(self._setup_browser(), self.loop)
        fut.result(timeout=60)

    def stop(self) -> None:
        self._stopping = True
        with self.ws_lock:
            for _, _, sock in self.ws_clients:
                try:
                    sock.sendall(_ws_encode(b'{"type":"closed"}'))
                except Exception:  # noqa: BLE001
                    pass
            self.ws_clients.clear()
        if self.loop and self.loop.is_running():
            try:
                fut = asyncio.run_coroutine_threadsafe(self._teardown_browser(), self.loop)
                fut.result(timeout=10)
            except Exception:  # noqa: BLE001
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread:
            self.loop_thread.join(timeout=5)

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    async def _setup_browser(self) -> None:
        from playwright.async_api import async_playwright

        self._pw = await async_playwright().start()
        if self.use_chrome_profile:
            # Run a plain chromium with a copy of the user's Chrome
            # profile. We can't open the live profile in a sandboxed
            # process (write access is denied), so vco debug --import-
            # chrome copies the relevant bits into the vco profile first
            # and points the browser at the copy.
            self._browser = await self._pw.chromium.launch_persistent_context(
                user_data_dir=self.use_chrome_profile,
                headless=not self.headful,
                viewport={"width": 1024, "height": 768},
            )
        else:
            self.profile_dir.mkdir(parents=True, exist_ok=True)
            self._browser = await self._pw.chromium.launch_persistent_context(
                user_data_dir=str(self.profile_dir),
                headless=not self.headful,
                viewport={"width": 1024, "height": 768},
            )
        self._ctx = self._browser
        if not self._ctx.pages:
            self._page = await self._ctx.new_page()
        else:
            self._page = self._ctx.pages[0]
        await self._ctx.add_init_script(self.INJECT_SCRIPT)
        if self.extra_headers:
            await self._ctx.set_extra_http_headers(self.extra_headers)
        self._page.on("console", self._on_console)
        self._page.on("pageerror", self._on_pageerror)
        self._page.on("framenavigated", self._on_navigated)
        if self.target:
            try:
                await self._prime_auth(self.target)
                await self._page.goto(self.target, wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:  # noqa: BLE001
                self._broadcast({"type": "error", "text": "initial goto: " + str(exc)})
        # Start the screenshot loop on the same loop.
        self._loop_task = asyncio.ensure_future(self._screenshot_loop())

    # --- auth / injection helpers ------------------------------------------

    async def _prime_auth(self, url: str) -> None:
        """Install every credential the controlled browser needs for ``url``.

        Today that means the harness (`dsh web`) signed cookie: without it the
        panel can only render the harness 401 page. Cookies are minted from the
        persisted signing secret, so this works even when the harness was
        started long before the panel.
        """

        if not (self.dsh_auth and url and is_loopback(url)):
            return
        if self.dsh_secret is None:
            self.dsh_secret = load_dsh_secret()
        if not self.dsh_secret:
            return
        cookies = []
        for origin in dsh_cookie_urls(url):
            if origin in self._dsh_done:
                continue
            cookie = dsh_cookie_for(origin, self.dsh_secret)
            if cookie:
                cookies.append(cookie)
                self._dsh_done.add(origin)
        if cookies:
            try:
                await self._ctx.add_cookies(cookies)
                names = ", ".join(sorted(c["name"] for c in cookies))
                print(f"[vco] dsh web auth cookie installed for {url} ({names})", flush=True)
            except Exception as exc:  # noqa: BLE001
                self._broadcast({"type": "error", "text": "dsh auth cookie: " + str(exc)})

    async def _ensure_inject(self) -> None:
        """Guarantee window.__vcoDebug exists in the current document."""

        if self._inject_ok:
            return
        try:
            ready = await self._page.evaluate("() => !!(window.__vcoDebug && window.__vcoDebug.describeAt)")
        except Exception:  # noqa: BLE001
            return
        if ready:
            self._inject_ok = True
            return
        try:
            await self._page.evaluate(self.INJECT_SCRIPT)
            self._inject_ok = True
        except Exception:  # noqa: BLE001
            pass

    def _reply(self, req_id, result, error: str | None = None) -> None:
        """Answer a remote-control verb, if its sender asked for an answer."""

        if not req_id:
            return
        payload = {"type": "cmd_result", "req_id": req_id, "ok": error is None,
                   "result": result}
        if error:
            payload["error"] = error
        self._broadcast(payload)

    async def _flash(self, cx: float, cy: float, radius: float) -> None:
        """Orange halo at a point — what makes an automated click followable."""

        try:
            await self._page.evaluate(
                """([x, y, r]) => {
                    const el = document.createElement('div');
                    el.style.cssText = 'position:fixed;pointer-events:none;'
                        + 'z-index:2147483647;left:' + (x - r) + 'px;top:' + (y - r) + 'px;'
                        + 'width:' + (2 * r) + 'px;height:' + (2 * r) + 'px;'
                        + 'border-radius:50%;background:radial-gradient(circle,'
                        + ' rgba(255,140,0,0) 30%, rgba(255,140,0,0.85) 55%,'
                        + ' rgba(255,140,0,0.30) 75%, rgba(255,140,0,0) 100%);';
                    document.body.appendChild(el);
                    setTimeout(() => el.remove(), 1400);
                }""",
                [cx, cy, radius],
            )
        except Exception:  # noqa: BLE001
            pass

    async def _dom_click(self, msg: dict) -> dict:
        """Click a DOM target inside *this* browser (the panel's).

        Same contract as `vco webclick`: address by mark id, selector, or exact
        text; refuse an ambiguous text target; click with a real mouse event so
        the page behaves exactly as if a person clicked it.
        """

        mark_id = msg.get("id")
        selector = msg.get("selector")
        text = msg.get("text")
        contains = bool(msg.get("contains"))
        if mark_id:
            locator = self._page.locator('[data-vco-id="%s"]' % mark_id)
            target = "#" + str(mark_id)
        elif selector:
            locator = self._page.locator(str(selector))
            target = str(selector)
        elif text:
            locator = self._page.get_by_text(str(text), exact=True)
            target = str(text)
            if await locator.count() == 0 and contains:
                locator = self._page.get_by_text(str(text))
        else:
            return {"clicked": False, "error": "need id, selector or text"}

        count = await locator.count()
        candidates = []
        for i in range(min(count, 8)):
            item = locator.nth(i)
            box = await item.bounding_box()
            candidates.append({
                "text": ((await item.text_content()) or "").strip()[:120],
                "bbox": None if box is None else [
                    box["x"], box["y"], box["x"] + box["width"], box["y"] + box["height"],
                ],
            })
        out = {
            "clicked": False, "url": self._page.url, "target": target,
            "candidate_count": count, "candidates": candidates,
        }
        if count != 1:
            out["error"] = "no match" if count == 0 else "ambiguous: %d matches" % count
            return out

        box = await locator.first.bounding_box()
        if box is not None:
            cx = box["x"] + box["width"] / 2
            cy = box["y"] + box["height"] / 2
            # A human cannot watch a click that has no tell: mark it, wait,
            # then click, so the panel shows what is about to be hit.
            await self._flash(cx, cy, max(14.0, min(32.0, float(min(box["width"], box["height"])))))
            await asyncio.sleep(0.9)
        else:
            cx = cy = None
        await locator.first.click()
        await asyncio.sleep(float(msg.get("settle") or 0.5))
        out.update({
            "clicked": True,
            "x": None if cx is None else round(cx),
            "y": None if cy is None else round(cy),
            "url": self._page.url,
        })
        return out

    async def _describe_at(self, x: float, y: float) -> dict | None:
        await self._ensure_inject()
        try:
            return await self._page.evaluate(
                "([x, y]) => (window.__vcoDebug && window.__vcoDebug.describeAt(x, y)) || null",
                [x, y],
            )
        except Exception:  # noqa: BLE001
            return None

    async def _teardown_browser(self) -> None:
        try:
            if hasattr(self, "_loop_task") and self._loop_task:
                self._loop_task.cancel()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._browser:
                await self._browser.close()
        except Exception:  # noqa: BLE001
            pass
        try:
            if self._pw:
                await self._pw.stop()
        except Exception:  # noqa: BLE001
            pass

    # --- event forwarders ---------------------------------------------------

    def _on_console(self, msg):
        try:
            self._broadcast({"type": "console", "level": msg.type, "text": msg.text})
        except Exception:  # noqa: BLE001
            pass

    def _on_pageerror(self, err):
        try:
            self._broadcast({"type": "pageerror", "text": str(err)})
        except Exception:  # noqa: BLE001
            pass

    def _on_navigated(self, frame):
        if frame != self._page.main_frame:
            return
        try:
            self.last_url = frame.url
            # Force the next screenshot to be broadcast even if the
            # previous frame happened to hash the same.
            self._last_digest = b""
            self._broadcast({"type": "navigated", "url": frame.url, "title": ""})
            # The init script runs again on the new document; re-check cheaply
            # rather than assuming, then re-measure the locked elements so the
            # panel's markers either follow them or go dashed.
            self._inject_ok = False
            # Kick a screenshot capture immediately so the panel doesn't
            # have to wait up to 0.25s for the loop to tick. The actual
            # rendering of the new page may take a moment, so we also
            # let the loop's next iteration broadcast a fresh frame
            # once the new DOM is in place.
            if self.loop and self._page and not self._page.is_closed():
                async def _snap():
                    try:
                        png = await self._page.screenshot(type="png", full_page=False)
                        import hashlib as _hl
                        self._last_digest = _hl.md5(png).digest()
                        self._broadcast({
                            "type": "frame",
                            "png": base64.b64encode(png).decode("ascii"),
                        })
                    except Exception:  # noqa: BLE001
                        pass
                asyncio.run_coroutine_threadsafe(_snap(), self.loop)
        except Exception:  # noqa: BLE001
            pass

    def _broadcast(self, msg: dict) -> None:
        payload = _ws_encode(json.dumps(msg).encode("utf-8"))
        n = 0
        with self.ws_lock:
            dead = []
            for i, (_, lock, sock) in enumerate(self.ws_clients):
                try:
                    with lock:
                        sock.sendall(payload)
                    n += 1
                except Exception:  # noqa: BLE001
                    dead.append(i)
            for i in reversed(dead):
                self.ws_clients.pop(i)

    # --- screenshot loop ----------------------------------------------------

    async def _screenshot_loop(self) -> None:
        import hashlib
        # Skip broadcasting a frame when the page hasn't changed visually
        # — this is what stops the panel from flickering on a static page.
        # _last_digest may be reset to b"" by ws connect / navigation to
        # force a fresh frame to the new client.
        self._last_digest = b""
        while not self._stopping:
            try:
                if not self._page or self._page.is_closed():
                    await asyncio.sleep(0.2)
                    continue
                png = await self._page.screenshot(type="png", full_page=False)
                self._last_shot_error = ""
                digest = hashlib.md5(png).digest()
                if digest == self._last_digest:
                    await asyncio.sleep(0.25)
                    continue
                self._last_digest = digest
                self._broadcast({
                    "type": "frame",
                    "png": base64.b64encode(png).decode("ascii"),
                })
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                # A page that is still loading (or is mid-navigation) can make
                # Chromium refuse a screenshot for a while. Say it once, back
                # off, and keep trying — the panel must not fill its error list
                # with hundreds of copies of the same line.
                first_line = str(exc).split("\n", 1)[0]
                if first_line != self._last_shot_error:
                    self._last_shot_error = first_line
                    self._broadcast({"type": "error", "text": "screenshot: " + first_line})
                await asyncio.sleep(1.0)
                continue
            await asyncio.sleep(0.25)

    # --- client commands ----------------------------------------------------

    def handle_client_message(self, raw: bytes) -> None:
        try:
            msg = json.loads(raw.decode("utf-8"))
        except Exception:  # noqa: BLE001
            return
        kind = msg.get("type")
        if not self.loop:
            return
        if TRACE_VALUE and kind != "mouse_move":
            _trace("recv", kind, {k: v for k, v in msg.items() if k != "type"})
        if kind in CLIENT_MESSAGES:
            asyncio.run_coroutine_threadsafe(self._handle(kind, msg), self.loop)
        else:
            _trace("dropped unknown client message", kind)

    async def _handle(self, kind: str, msg: dict) -> None:
        try:
            if kind == "client_info":
                # Which browser is driving the panel matters: an engine that
                # never delivers pointer/IME events looks exactly like "the
                # panel is broken" from the user's side.
                self.client_info = msg
                _trace("client_info", json.dumps(msg, ensure_ascii=False)[:400])
                return
            req_id = msg.get("req_id")

            if kind == "dom_marks":
                await self._ensure_inject()
                marks = await self._page.evaluate(MARKS_JS, [int(msg.get("max_normal") or 60)])
                self._reply(req_id, marks)
            elif kind == "dom_click":
                self._reply(req_id, await self._dom_click(msg))
            elif kind == "dom_fill":
                placeholder = str(msg.get("placeholder") or "")
                value = str(msg.get("value") or "")
                if not placeholder:
                    self._reply(req_id, None, "dom_fill needs a placeholder")
                else:
                    try:
                        await self._page.get_by_placeholder(placeholder).first.fill(
                            value, timeout=5000)
                        self._reply(req_id, {"ok": True})
                    except Exception as exc:  # noqa: BLE001
                        self._reply(req_id, None, f"{placeholder}: {exc}".split("\n")[0])
            elif kind == "dom_wait_text":
                found = False
                try:
                    await self._page.get_by_text(str(msg.get("text") or "")).first.wait_for(
                        state="visible",
                        timeout=int(float(msg.get("timeout") or 10) * 1000),
                    )
                    found = True
                except Exception:  # noqa: BLE001
                    found = False
                self._reply(req_id, {"found": found})
            elif kind == "flash":
                await self._flash(
                    float(msg.get("x") or 0), float(msg.get("y") or 0),
                    float(msg.get("radius") or 20),
                )
                self._reply(req_id, {"ok": True})
            elif kind == "shot":
                target = Path(str(msg.get("path") or ""))
                if not target.is_absolute():
                    self._reply(req_id, None, "shot needs an absolute path")
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    await self._page.screenshot(path=str(target), full_page=False)
                    self._reply(req_id, {"path": str(target)})
            elif kind == "runs_list":
                runs = monitor.list_runs(self.runs_root)
                self._broadcast({"type": "runs", "items": runs})
            elif kind == "run_poll":
                rel = msg.get("path") or ""
                run_dir = monitor.resolve_run_path(self.runs_root, rel)
                if run_dir is None:
                    self._broadcast({"type": "error", "text": "unknown run: " + rel})
                else:
                    try:
                        after = int(msg.get("after") or 0)
                    except (TypeError, ValueError):
                        after = 0
                    payload = monitor.read_events(run_dir, max(0, after))
                    self._broadcast({
                        "type": "run_events",
                        "path": rel,
                        "events": payload["events"],
                        "next": payload["next"],
                    })
            elif kind == "client_pulse":
                self.client_pulse = msg
                _trace("client_pulse", json.dumps(msg, ensure_ascii=False)[:400])
                return
            if kind == "mouse_move":
                await self._page.mouse.move(msg["x"], msg["y"])
                # Feed the panel's "element under the mouse" readout, throttled:
                # a describe() per mousemove would flood both sockets.
                now = time.monotonic()
                if now - self._last_hover_ts > 0.12:
                    self._last_hover_ts = now
                    desc = await self._describe_at(msg["x"], msg["y"])
                    self._broadcast({"type": "hover", "payload": desc})
            elif kind == "mouse_down":
                await self._page.mouse.down(button=msg.get("button", "left"))
            elif kind == "mouse_up":
                await self._page.mouse.up(button=msg.get("button", "left"))
            elif kind == "mouse_up_with_pick":
                await self._page.mouse.up(button=msg.get("button", "left"))
                # Treat the up as a click; query the element at the same coords.
                desc = await self._page.evaluate(
                    """([x, y]) => {
                        const el = document.elementFromPoint(x, y);
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        const attrs = {};
                        for (const a of el.attributes || []) attrs[a.name] = a.value;
                        const sel = (function compute(e) {
                          if (!e || e.nodeType !== 1) return '';
                          if (e.id) return '#' + CSS.escape(e.id);
                          const parts = [];
                          let c = e;
                          while (c && c.nodeType === 1 && parts.length < 5) {
                            let p = c.tagName.toLowerCase();
                            if (c.classList && c.classList.length) {
                              p += '.' + Array.from(c.classList).slice(0, 2)
                                .map(x => CSS.escape(x)).join('.');
                            }
                            const par = c.parentElement;
                            if (par) {
                              const same = Array.from(par.children).filter(
                                s => s.tagName === c.tagName);
                              if (same.length > 1) {
                                p += ':nth-of-type(' + (same.indexOf(c) + 1) + ')';
                              }
                            }
                            parts.unshift(p);
                            c = par;
                          }
                          return parts.join('>');
                        })(el);
                        return {
                          selector: sel, tag: el.tagName.toLowerCase(),
                          id: el.id || '', className: typeof el.className === 'string' ? el.className : '',
                          attrs, text: (el.innerText || '').slice(0, 200),
                          role: el.getAttribute('role') || el.tagName.toLowerCase(),
                          rect: { x: r.x, y: r.y, w: r.width, h: r.height },
                          visible: r.width > 0 && r.height > 0,
                        };
                    }""",
                    [msg["x"], msg["y"]],
                )
                if desc:
                    self.last_pick = desc
                    self._broadcast({"type": "pick", "payload": desc})
            elif kind == "click":
                await self._page.mouse.click(
                    msg["x"], msg["y"], button=msg.get("button", "left")
                )
            elif kind == "wheel":
                await self._page.mouse.wheel(msg.get("dx", 0), msg.get("dy", 0))
            elif kind == "key":
                if "text" in msg:  # legacy batch-typing path
                    await self._page.keyboard.type(str(msg["text"]))
                key = msg.get("key")
                if key:
                    mods = [
                        m for m in (msg.get("modifiers") or [])
                        if m in {"Control", "Meta", "Alt", "Shift"}
                    ]
                    # Hold the modifiers for real: a synthetic key event that
                    # only *claims* ctrlKey does not trigger the page's
                    # shortcuts, and a bare "v" instead of ⌘V types a letter.
                    held = []
                    try:
                        for mod in mods:
                            await self._page.keyboard.down(mod)
                            held.append(mod)
                        await self._page.keyboard.press(str(key))
                    finally:
                        for mod in reversed(held):
                            try:
                                await self._page.keyboard.up(mod)
                            except Exception:  # noqa: BLE001
                                pass
            elif kind == "insert_text":
                text = msg.get("text")
                if isinstance(text, str) and text:
                    # The only path that can carry IME/composed text: key events
                    # cannot express a Chinese composition, and a paste is an
                    # edit command Chromium will not run for a synthetic key.
                    await self._page.keyboard.insert_text(text)
            elif kind == "copy_selection":
                text = await self._page.evaluate(
                    """() => {
                        const el = document.activeElement;
                        if (el && (el.tagName === 'INPUT' || el.tagName === 'TEXTAREA')
                            && typeof el.selectionStart === 'number') {
                          return (el.value || '').slice(el.selectionStart, el.selectionEnd);
                        }
                        const sel = window.getSelection();
                        return sel ? sel.toString() : '';
                    }"""
                )
                self._broadcast({"type": "selection", "text": text or ""})
            elif kind == "goto":
                url = msg.get("url")
                if isinstance(url, str) and url:
                    await self._prime_auth(url)
                    await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    if req_id:
                        self._reply(req_id, {"url": self._page.url})
            elif kind == "back":
                await self._page.go_back()
            elif kind == "forward":
                await self._page.go_forward()
            elif kind == "reload":
                await self._prime_auth(self._page.url)
                await self._page.reload()
            elif kind == "pick_at":
                desc = await self._describe_at(msg["x"], msg["y"])
                if TRACE_VALUE:
                    _trace("pick_at", (msg.get("x"), msg.get("y")), "->",
                           (desc or {}).get("selector"))
                if desc:
                    # A modifier click always *adds* a card. Re-locking an
                    # element that is already in the stack refreshes that card
                    # in place (and keeps its annotation) instead of stacking a
                    # duplicate — that is what makes "⌘/Ctrl+click another one"
                    # accumulate cards instead of replacing them.
                    sel = desc.get("selector") or ""
                    desc["lockedAt"] = int(time.time() * 1000)
                    # Where this element was locked. Stamped HERE, at lock time,
                    # because the panel used to read the *current* page when
                    # building a payload: lock an element on one page, navigate
                    # away, and the block it produced named the wrong page.
                    # A re-broadcast keeps the stored value, so it stays true.
                    try:
                        desc["pageUrl"] = self._page.url
                        desc["pageTitle"] = await self._page.title()
                    except Exception:  # noqa: BLE001
                        desc["pageUrl"] = self.last_url or ""
                        desc["pageTitle"] = ""
                    if sel in self.annotations:
                        desc["annotation"] = self.annotations[sel]
                    merged = False
                    stack: list[dict] = []
                    for item in self.last_picks:
                        if (item.get("selector") or "") == sel and not merged:
                            fresh = dict(desc)
                            if item.get("annotation"):
                                fresh["annotation"] = item["annotation"]
                            fresh["lockedAt"] = item.get("lockedAt") or desc["lockedAt"]
                            stack.append(fresh)
                            merged = True
                        else:
                            stack.append(item)
                    if not merged:
                        stack.append(desc)
                    self.last_picks = stack
                    self.last_pick = desc
                    if TRACE_VALUE:
                        _trace("picks=", [((p.get("selector") or "")[:26],
                                           bool(p.get("annotation")))
                                          for p in self.last_picks])
                    self._broadcast({
                        "type": "pick",
                        "picks": list(self.last_picks),
                        "payload": desc,
                        "added": not merged,
                    })
            elif kind == "remove_pick":
                sel = msg.get("selector") or ""
                self.annotations.pop(sel, None)   # the card is gone, so is its note
                self.last_picks = [
                    p for p in self.last_picks if (p.get("selector") or "") != sel
                ]
                self._broadcast({
                    "type": "pick", "picks": list(self.last_picks), "payload": None,
                })
            elif kind == "clear_picks":
                self.last_picks = []
                self.last_pick = None
                self._broadcast({"type": "pick", "picks": [], "payload": None})
            elif kind == "annotate":
                # Text typed into a card's textarea. Stored but never echoed
                # back: re-broadcasting while the user types would fight the
                # focused textarea for the caret.
                sel = msg.get("selector") or ""
                text = str(msg.get("text") or "")
                if sel:
                    self.annotations[sel] = text
                hit = False
                for item in self.last_picks:
                    if (item.get("selector") or "") == sel:
                        item["annotation"] = text
                        hit = True
                if TRACE_VALUE:
                    _trace("annotate", "matched" if hit else "NO MATCH",
                           "of", len(self.last_picks), "picks")
            elif kind == "measure_picks":
                selectors = [p.get("selector") or "" for p in self.last_picks]
                if selectors:
                    await self._ensure_inject()
                    measured = await self._page.evaluate(
                        """(sels) => sels.map((s) => {
                            let el = null;
                            try { el = document.querySelector(s); } catch (e) { el = null; }
                            if (!el) return null;
                            const r = el.getBoundingClientRect();
                            return { x: r.x, y: r.y, w: r.width, h: r.height,
                                     visible: r.width > 0 && r.height > 0 };
                        })""",
                        selectors,
                    )
                    for item, rect in zip(self.last_picks, measured or []):
                        item["missing"] = rect is None
                        if rect:
                            item["rect"] = rect
                    self._broadcast({
                        "type": "pick", "picks": list(self.last_picks), "payload": None,
                    })
            elif kind == "context":
                ctx = await self._page.evaluate(
                    "() => window.__vcoDebug && window.__vcoDebug.context() || null"
                )
                self._broadcast({"type": "context", "payload": ctx})
        except Exception as exc:  # noqa: BLE001
            if TRACE_VALUE:
                import traceback
                _trace("handler error in", kind)
                traceback.print_exc()
            self._broadcast({"type": "error", "text": str(exc)})
            # A remote caller is blocked on cmd_result: answer it with the
            # failure instead of letting it time out with no explanation.
            self._reply(msg.get("req_id"), None, f"{kind}: {exc}")


# --- HTTP handler -----------------------------------------------------------

_CONTENT_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".webp": "image/webp", ".gif": "image/gif", ".json": "application/json",
    ".html": "text/html; charset=utf-8", ".txt": "text/plain; charset=utf-8",
}


def _content_type(path: Path) -> str:
    return _CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def render_panel_html() -> str:
    return (DEBUG_ASSETS / "panel.html").read_text(encoding="utf-8")


def render_panel_js() -> str:
    return (DEBUG_ASSETS / "panel.js").read_text(encoding="utf-8")


def render_inject_js() -> str:
    return (DEBUG_ASSETS / "inject.js").read_text(encoding="utf-8")


def make_handler(session: DebugSession, *, api_key: str | None = None):
    class DebugHandler(BaseHTTPRequestHandler):
        server_version = "VCO-Debug/1"
        # RFC 6455 requires the upgrade response to be "HTTP/1.1 101". BaseHTTPRequestHandler
        # defaults to HTTP/1.0, which Chromium tolerates but Safari rejects outright
        # ("There was a bad response from the server") — leaving the panel permanently
        # blank in Safari while every Chrome-based test passed. Every response below
        # sets Content-Length, so HTTP/1.1 keep-alive is safe.
        protocol_version = "HTTP/1.1"

        def _json(self, status: int, payload):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _html(self, status: int, markup: str):
            body = markup.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _js(self, status: int, source: str):
            body = source.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/javascript; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if not api_key:
                return True
            # Loopback connections are always trusted: the local browser
            # opening the panel is presumed to be the operator. The bearer
            # token only matters for non-loopback clients.
            host, _port = self.client_address
            if host in {"127.0.0.1", "::1", "localhost"}:
                return True
            if self.headers.get("Authorization") == "Bearer " + api_key:
                return True
            self._json(401, {"error": "unauthorized"})
            return False

        def do_GET(self):  # noqa: N802
            if not self._authorized():
                return
            path = self.path.split("?", 1)[0]
            if path == "/" or path == "/index.html":
                self._html(200, render_panel_html())
            elif path == "/static/panel.js":
                self._js(200, render_panel_js())
            elif path == "/static/inject.js":
                self._js(200, render_inject_js())
            elif path.startswith("/runs/"):
                # Run artifacts (step screenshots) for the run timeline. Reuses
                # the watcher's traversal-safe resolver.
                target = monitor.resolve_run_path(
                    session.runs_root, unquote(path[len("/runs/"):])
                )
                if target is None or not target.is_file():
                    self._json(404, {"error": "not found"})
                    return
                try:
                    body = target.read_bytes()
                except OSError as exc:
                    self._json(404, {"error": str(exc)})
                    return
                self.send_response(200)
                self.send_header("Content-Type", _content_type(target))
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            elif path == "/ws":
                self._handle_websocket()
            else:
                self._json(404, {"error": "not found"})

        def _handle_websocket(self) -> None:
            # Bare-minimum ws upgrade. Runs in its own thread per client.
            upgrade = (self.headers.get("Upgrade") or "").lower() == "websocket"
            key = self.headers.get("Sec-WebSocket-Key")
            if not (upgrade and key):
                self._json(400, {"error": "websocket upgrade required"})
                return
            # Send 101 Switching Protocols BEFORE the upgrade headers; otherwise
            # the browser sees a malformed HTTP response.
            self.send_response(101, "Switching Protocols")
            for k, v in _ws_handshake_headers(key).items():
                self.send_header(k, v)
            self.end_headers()
            sock = self.connection
            rfile = sock.makefile("rb")
            send_lock = threading.Lock()
            _trace("panel client connected",
                   "ua=" + (self.headers.get("User-Agent") or "?")[:160],
                   "origin=" + (self.headers.get("Origin") or "?"))
            with session.ws_lock:
                session.ws_clients.append((self, send_lock, sock))
            # After the new client is registered, push the current page
            # state and force a fresh screenshot so the panel has
            # something to show. Reset the dedup digest so the next
            # screenshot loop iteration will send a frame.
            if session.loop and session._page and not session._page.is_closed():
                async def _hello():
                    try:
                        title = await session._page.title()
                        url = session._page.url
                        # Reset dedup so the next screenshot is broadcast
                        # to this newly-registered client.
                        session._last_digest = b""
                        png = await session._page.screenshot(type="png", full_page=False)
                        import hashlib as _hl
                        session._last_digest = _hl.md5(png).digest()
                        session._broadcast({"type": "navigated", "url": url, "title": title})
                        session._broadcast({
                            "type": "frame",
                            "png": base64.b64encode(png).decode("ascii"),
                        })
                    except Exception:  # noqa: BLE001
                        pass
                asyncio.run_coroutine_threadsafe(_hello(), session.loop)
            try:
                buf = b""
                while True:
                    chunk = rfile.read1(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        decoded = _ws_decode_frame(buf)
                        if decoded is None:
                            break
                        opcode, payload, consumed = decoded
                        # Keep whatever else arrived in the same read: dropping
                        # it loses the follow-up message of a click pair.
                        buf = buf[consumed:]
                        if opcode == 0x8:  # close
                            return
                        if opcode == 0x9:  # ping
                            with send_lock:
                                sock.sendall(_ws_encode(b"", opcode=0xA))
                            continue
                        if opcode == 0x1:  # text
                            session.handle_client_message(payload)
            except Exception:  # noqa: BLE001
                pass
            finally:
                # This socket carried a WebSocket, not HTTP: never try to parse
                # another HTTP request from it (HTTP/1.1 would keep it alive).
                self.close_connection = True
                with session.ws_lock:
                    session.ws_clients = [
                        c for c in session.ws_clients if c[0] is not self
                    ]

        def log_message(self, format, *args):
            return

    return DebugHandler


def serve_debug(
    target: str | None,
    *,
    profile: Path,
    headful: bool = False,
    host: str = "127.0.0.1",
    port: int = 8767,
    api_key: str | None = None,
    use_chrome_profile: str | None = None,
    dsh_auth: bool = True,
    extra_headers: dict | None = None,
    runs_root: Path | None = None,
) -> None:
    if host not in {"127.0.0.1", "localhost", "::1"} and not api_key:
        raise ValueError("a bearer token is required when binding outside localhost")
    # Chromium refuses to capture a screenshot while a page still reports
    # pending webfonts (`document.fonts.ready`), and Playwright turns that into
    # "Unable to capture screenshot … waiting for fonts to load". On a page with
    # a font request that never settles — the harness GUI is one — the viewport
    # would simply stay blank, which reads to the user as "it can't render this
    # page at all". A debug inspector cares about pixels, not font readiness.
    os.environ.setdefault("PW_TEST_SCREENSHOT_NO_FONTS_READY", "1")
    session = DebugSession(
        profile_dir=profile, headful=headful, target=target,
        use_chrome_profile=use_chrome_profile,
        dsh_auth=dsh_auth, extra_headers=extra_headers, runs_root=runs_root,
    )
    session.start()
    server = ThreadingHTTPServer((host, port), make_handler(session, api_key=api_key))
    print(f"VCO debug panel:  http://{host}:{port}/")
    print(f"Browser profile:  {profile}  (cookies & storage persist across runs)")
    print(f"Headless mode:    {'no (you can see the browser)' if headful else 'yes'}")
    if api_key:
        print(f"Bearer token:     required (Authorization: Bearer <...>)")
    if dsh_auth:
        if session.dsh_secret:
            print(f"dsh web auth:     on  (cookie minted from {DSH_CREDENTIALS})")
        else:
            print(f"dsh web auth:     on  (no browser-session secret in {DSH_CREDENTIALS}; "
                  f"harness pages will return 401)")
    if target:
        print(f"Opened at start:  {target}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        session.stop()
        server.server_close()


async def _ask_against_session(
    session: "DebugSession",
    question: str,
    *,
    base_url: str,
    api_key: str,
    model: str,
    system_hint: str = "",
) -> str:
    """Snapshot the current page, ask the given endpoint, return its text reply.

    This is `vco debug-ask`, a standalone one-shot command — the panel itself
    never talks to a model.
    """

    if not (base_url and api_key and model):
        raise RuntimeError(
            "LLM endpoint not configured: pass --llm-base/--llm-key-env/--llm-model"
        )
    if not session._page or session._page.is_closed():
        raise RuntimeError("debug session has no open page")
    png = await session._page.screenshot(type="png", full_page=False)
    page_url = session._page.url
    title = await session._page.title()
    system = (
        "You are a helpful web debugging assistant. The user shows you a "
        "screenshot of a page and asks a question. Answer concisely. "
        "Reference visible elements by their position or text. "
        + system_hint
    ).strip()
    user = (
        f"Page URL: {page_url}\n"
        f"Page title: {title}\n"
        f"Question: {question}"
    )
    return _call_chat_text(
        base_url=base_url, api_key=api_key, model=model,
        system=system, user=user,
        image_b64=base64.b64encode(png).decode("ascii"),
    )


def serve_debug_ask(
    question: str,
    *,
    profile: Path,
    target: str | None,
    llm_base: str,
    llm_key: str,
    llm_model: str,
    use_chrome_profile: str | None = None,
) -> int:
    """Open the persistent profile, optionally navigate, then ask the LLM."""

    session = DebugSession(
        profile_dir=profile, headful=False, target=target,
        use_chrome_profile=use_chrome_profile,
    )
    session.start()
    try:
        fut = asyncio.run_coroutine_threadsafe(
            _ask_against_session(
                session, question,
                base_url=llm_base, api_key=llm_key, model=llm_model,
            ),
            session.loop,
        )
        answer = fut.result(timeout=180)
    finally:
        session.stop()
    print(answer)
    return 0
