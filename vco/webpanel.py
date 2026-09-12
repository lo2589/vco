"""`vco webclick --panel`: run a web click inside the debug panel's browser.

`vco webclick` normally launches its own headless Chromium, so its clicks happen
in a browser nobody can see. With `--panel http://127.0.0.1:8767` it drives the
browser the panel already owns instead, which means the person watching the
panel sees the target get ringed, the click land, and the page react — and the
panel's own DOM inspector works on the very page being clicked.

The contract is deliberately identical to the standalone path (unique-match or
refuse, mark ids, `--expect` verification, same result JSON, same run events),
so switching between them is a flag, not a different tool.
"""

from __future__ import annotations

import json
from pathlib import Path

from .dommarks import marks_prompt_table
from .events import emit
from .panel_client import PanelClient, PanelError


def _emit(run_dir: Path, kind: str, summary: str, image: str | None = None,
          step: int | None = None) -> None:
    emit(run_dir, kind, step=step, summary=summary, image=image)


def _match_marks(marks: list[dict], target: str, contains: bool) -> list[dict]:
    """Same matching rule as the standalone path: exact first, then contains."""

    exact = [m for m in marks if (m.get("text") or "").strip() == target]
    if exact:
        return exact
    if contains:
        return [m for m in marks if target in (m.get("text") or "")]
    return []


def click_via_panel(
    url: str,
    target: str | None,
    *,
    panel: str,
    selector: str | None = None,
    mark_id: str | None = None,
    contains: bool = False,
    fills: list[str] | None = None,
    settle: float = 0.5,
    timeout: float = 30.0,
    expect: str | None = None,
    expect_timeout: float = 10.0,
    run_dir: Path | None = None,
    debug: bool = False,
    want_before: str | None = None,
    want_after: str | None = None,
) -> dict:
    """Click ``target`` in the panel's browser and report what happened."""

    run_dir = Path(run_dir or "cache/webclick-panel")
    run_dir.mkdir(parents=True, exist_ok=True)

    result: dict = {
        "clicked": False,
        "url": url,
        "target": mark_id or selector or target,
        "via": "panel:" + panel,
        "candidate_count": 0,
        "candidates": [],
        "before": None,
        "after": None,
        "x": None,
        "y": None,
    }

    try:
        with PanelClient(panel, timeout=max(timeout, 20.0)) as client:
            if want_before:
                client.call("shot", path=str(Path(want_before).resolve()))
                result["before"] = want_before
            client.call("goto", url=url)
            _emit(run_dir, "observation", f"打开 {url}", image=Path(want_before).name
                  if want_before else None)

            fill_errors = []
            for spec in fills or []:
                placeholder, _, value = spec.partition("=")
                try:
                    client.call("dom_fill", placeholder=placeholder, value=value)
                except PanelError as exc:
                    fill_errors.append(f"{placeholder}: {exc}")
            result["fill_errors"] = fill_errors

            marks = client.call("dom_marks", max_normal=60).get("marks", [])
            if selector or mark_id:
                # Addressed directly: let the panel resolve and click it.
                pass
            else:
                matched = _match_marks(marks, str(target or ""), contains)
                result["candidate_count"] = len(matched)
                result["candidates"] = [
                    {"text": m.get("text") or "", "bbox": m.get("bbox")} for m in matched[:8]
                ]
                if debug:
                    result["marks"] = marks_prompt_table(marks)
                if len(matched) != 1:
                    result["error"] = ("no match" if not matched
                                       else f"ambiguous: {len(matched)} matches")
                    _emit(run_dir, "page_error", result["error"])
                    return result
                mark_id = matched[0].get("id")

            clicked = client.call(
                "dom_click",
                id=mark_id,
                selector=selector,
                text=None if (mark_id or selector) else target,
                contains=contains,
                settle=settle,
            )
            result.update({
                "clicked": bool(clicked.get("clicked")),
                "candidate_count": clicked.get("candidate_count", result["candidate_count"]),
                "candidates": clicked.get("candidates", result["candidates"]),
                "x": clicked.get("x"),
                "y": clicked.get("y"),
                "url": clicked.get("url", url),
            })
            if not result["clicked"]:
                result["error"] = clicked.get("error") or "click failed"
                _emit(run_dir, "page_error", result["error"])
                return result

            verified = None
            if expect:
                verified = bool(
                    client.call("dom_wait_text", text=expect,
                                timeout=expect_timeout).get("found")
                )
            result["expect"] = expect
            result["verified"] = verified

            if want_after:
                client.call("shot", path=str(Path(want_after).resolve()))
                result["after"] = want_after

            summary = f"点击 {result['target']}"
            if verified is not None:
                summary += "（已验证）" if verified else "（未出现预期内容）"
            _emit(run_dir, "action_result", summary,
                  image=Path(want_after).name if want_after else None)
            result["events"] = str(run_dir / "events.jsonl")
            return result
    except PanelError as exc:
        result["error"] = str(exc)
        _emit(run_dir, "page_error", f"面板不可用：{exc}")
        return result


def main(argv: list[str] | None = None) -> int:  # pragma: no cover - CLI shim
    """Small entry point so the module is runnable for manual checks."""

    import sys

    if not argv:
        print(__doc__)
        return 0
    url, panel = argv[0], argv[1]
    print(json.dumps(click_via_panel(url, argv[2] if len(argv) > 2 else None,
                                     panel=panel), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
