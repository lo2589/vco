"""Append-only event sink so runs can be observed without coupling to the monitor."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

# Event kinds used across the codebase (the monitor renders these as labels):
# step_start / observation / action_result / page_error / user_click /
# annotation / run_end


def emit(
    run_dir: Path,
    kind: str,
    *,
    step: int | None = None,
    summary: str = "",
    data: dict | None = None,
    image: str | None = None,
) -> None:
    """Append one JSON line to ``run_dir/events.jsonl``.

    Observability must never break the main flow, so every failure (missing
    directory, full disk, permissions) is swallowed silently.
    """

    try:
        record = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "kind": kind,
            "step": step,
            "summary": summary,
            "data": data,
            "image": image,
        }
        with open(run_dir / "events.jsonl", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass
