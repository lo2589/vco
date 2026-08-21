"""Text-model web agent loop: aria snapshot -> model JSON action -> DOM execute.

Unlike the desktop loop, perception (accessibility tree) and action (DOM
events) are both deterministic; the model only decides what to do next.
A text-only model is sufficient.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from .browser import _PageLog, _apply_fills, _close, _load_pw, _open

SYSTEM_PROMPT = """You operate a web page through structured actions. You receive the
page's accessibility tree (roles and visible text). Reply with exactly one JSON
object and nothing else. Allowed actions:
{"action":"click","target":"exact visible text of the element to click"}
{"action":"click","selector":"css selector"}  (only when no visible text works)
{"action":"fill","placeholder":"the input placeholder","text":"content to type"}
{"action":"done","reason":"why the task is complete"}"""


@dataclass
class WebRunResult:
    status: str
    steps: int
    reason: str
    history: list = field(default_factory=list)
    video: str | None = None

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "steps": self.steps,
            "reason": self.reason,
            "history": self.history,
            "video": self.video,
        }


def _parse_decision(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError(f"model returned no JSON object: {text[:200]!r}")
    decision = json.loads(text[start : end + 1])
    if not isinstance(decision, dict) or "action" not in decision:
        raise ValueError(f"decision must contain an action field: {decision!r}")
    if decision["action"] == "click" and not (
        decision.get("target") or decision.get("selector")
    ):
        raise ValueError("click requires target or selector")
    if decision["action"] == "fill" and not (
        decision.get("placeholder") and decision.get("text") is not None
    ):
        raise ValueError("fill requires placeholder and text")
    if decision["action"] not in {"click", "fill", "done"}:
        raise ValueError(f"unknown action: {decision['action']!r}")
    return decision


def run(
    task: str,
    url: str,
    provider,
    *,
    max_steps: int = 8,
    settle: float = 0.7,
    width: int = 1280,
    height: int = 800,
    timeout: float = 15.0,
    profile: str | None = None,
    headless: bool = True,
    hold: float = 0.0,
    record: bool = False,
    artifact_dir: Path | None = None,
) -> WebRunResult:
    sync_playwright = _load_pw()
    log = _PageLog()
    history: list[dict] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    video_path = None
    if record and artifact_dir is not None:
        artifact_dir.mkdir(parents=True, exist_ok=True)
        video_path = artifact_dir / f"webrun-{stamp}.webm"
    result: WebRunResult | None = None
    with sync_playwright() as pw:
        browser, context, page = _open(
            pw, url, width=width, height=height, timeout=timeout,
            profile=profile, log=log, headless=headless,
            record_dir=None if video_path is None else str(artifact_dir),
        )
        try:
            for step in range(1, max_steps + 1):
                tree = page.locator("body").aria_snapshot()
                prompt = (
                    f"Task: {task}\n"
                    f"Step: {step}/{max_steps}\n"
                    f"Page URL: {page.url}\n"
                    f"Accessibility tree:\n{tree[:6000]}\n"
                    f"Console errors: {json.dumps(log.console_errors[:5], ensure_ascii=False)}\n"
                    f"Page errors: {json.dumps(log.page_errors[:5], ensure_ascii=False)}\n"
                    f"Previous actions: {json.dumps(history, ensure_ascii=False)}\n"
                    "Return the next action JSON."
                )
                raw = provider.chat(prompt, system=SYSTEM_PROMPT)
                try:
                    decision = _parse_decision(raw)
                except ValueError as exc:
                    history.append({"step": step, "error": str(exc), "raw": raw[:300]})
                    continue

                record: dict = {"step": step, "decision": decision}
                if decision["action"] == "done":
                    record["metadata"] = getattr(provider, "last_metadata", None)
                    history.append(record)
                    _save(artifact_dir, task, history, page=page, stamp=stamp)
                    if hold > 0:
                        page.wait_for_timeout(int(hold * 1000))
                    result = WebRunResult(
                        "done", step, decision.get("reason", ""), history
                    )
                    return result

                error = None
                if decision["action"] == "click":
                    if decision.get("selector"):
                        locator = page.locator(decision["selector"])
                    else:
                        locator = page.get_by_text(decision["target"], exact=True)
                        if locator.count() == 0:
                            locator = page.get_by_text(decision["target"])
                    if locator.count() != 1:
                        error = f"click target matched {locator.count()} elements"
                    else:
                        if not headless:
                            box = locator.first.bounding_box()
                            if box is not None:
                                from .browser import _flash_ring

                                _flash_ring(
                                    page,
                                    box["x"] + box["width"] / 2,
                                    box["y"] + box["height"] / 2,
                                    max(14.0, min(32.0, float(min(box["width"], box["height"])))),
                                )
                                page.wait_for_timeout(900)
                        locator.first.click()
                elif decision["action"] == "fill":
                    errors = _apply_fills(
                        page,
                        [f'{decision["placeholder"]}={decision["text"]}'],
                        visible=not headless,
                    )
                    error = errors[0] if errors else None
                record["error"] = error
                record["metadata"] = getattr(provider, "last_metadata", None)
                history.append(record)
                if settle > 0:
                    page.wait_for_timeout(int(settle * 1000))

            _save(artifact_dir, task, history, page=page, stamp=stamp)
            result = WebRunResult(
                "max_steps", max_steps, f"stopped after max_steps={max_steps}", history
            )
            return result
        finally:
            video = _close(browser, context, page=page, video_path=None if video_path is None else str(video_path))
            if video and result is not None:
                result.video = video


def _save(artifact_dir: Path | None, task: str, history: list, page=None,
          stamp: str | None = None) -> None:
    if artifact_dir is None:
        return
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d-%H%M%S-%f")[:-3]
    (artifact_dir / f"webrun-{stamp}.json").write_text(
        json.dumps({"task": task, "history": history}, indent=2, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    if page is not None:
        page.screenshot(path=str(artifact_dir / f"webrun-{stamp}.final.png"))
