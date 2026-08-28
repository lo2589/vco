"""A front-end debugger that reproduces and reports, and never repairs.

Fixing needs the whole codebase in your head and is where the risk lives.
Locating is where the hours go: by the time you know which line moved the
value, the edit is usually obvious. So this half is automated and the other
half is not.

It drives the page the way a person does — real clicks, a real wheel, text
typed a character at a time — because a page can tell those apart from
assignments, and the bugs worth chasing are exactly the ones that behave
differently for each.

The report is assembled from the recorded evidence, not from the model's
closing statement. A stack it never captured cannot appear in the findings.
"""

from __future__ import annotations

from pathlib import Path

from .webagent import WebRunResult, run

DEBUG_PROMPT = """You are diagnosing a front-end bug. You reproduce and report; you
never fix anything. Reply with exactly one JSON object and nothing else.

Actions (every "selector" is a real CSS selector such as #log or .list — the
Scrollable elements listing above gives you the ones that matter, so take them
from there rather than guessing):
{"action":"read","selector":"#log","props":["scrollTop","scrollHeight"]}
{"action":"arm","selector":"#log","prop":"scrollTop"}
{"action":"collect","selector":"#log","prop":"scrollTop"}
{"action":"click","target":"visible text"}   or  {"action":"click","selector":"#refresh"}
{"action":"fill","placeholder":"the placeholder","text":"what to type"}
{"action":"press","key":"Enter"}             add "selector" to press inside one element
{"action":"scroll","selector":"#log","dy":-400}   negative is up; a real wheel
{"action":"done","reason":"what you measured and where it came from"}

The method, in order:

1. MEASURE. read the value the complaint is about. A bug you cannot put a
   number on is a bug you cannot say you reproduced.
2. ARM before acting. arm installs two recorders on that property: one logs
   every assignment with the call stack behind it, the other samples for
   changes nobody assigned — a value also resets when a node is re-attached
   or re-laid-out, and that leaves no stack at all.
3. ACT like the user did. Click the button, turn the wheel, type the text.
4. COLLECT, then read again. collect returns everything caught since arming.
5. REPORT. Say the before and after numbers, and name the source from the
   stack you captured.

Rules that matter:
- Never name a cause you have no record for. If collect returns assignments,
  the top frame of the stack is your answer. If it returns only jumps, say
  the reset had no assignment behind it — that is a finding, not a failure.
- Arm before the action, never after. A recorder installed afterwards has
  nothing to see.
- If the value did not move, say so plainly. "Cannot reproduce" is a result.
- The complaint names things the way a person sees them ("the list", "the
  messages"). Match that to a selector from the Scrollable elements listing.
"""


# What is scrollable, and what each one is called. The accessibility tree has
# neither — it reports roles and text — so an agent working on a scrolling
# complaint would otherwise be guessing at the very thing it must address.
_SCROLLABLES_JS = r"""() => {
  const out = [];
  for (const el of document.querySelectorAll("*")) {
    const overflowY = el.scrollHeight - el.clientHeight;
    const overflowX = el.scrollWidth - el.clientWidth;
    if (overflowY < 8 && overflowX < 8) continue;
    let selector = el.tagName.toLowerCase();
    if (el.id) selector = "#" + el.id;
    else if (typeof el.className === "string" && el.className.trim())
      selector += "." + el.className.trim().split(/\s+/).join(".");
    out.push({selector: selector, scrollableY: overflowY, scrollableX: overflowX,
              scrollTop: Math.round(el.scrollTop),
              height: Math.round(el.clientHeight)});
  }
  return out.slice(0, 12);
}"""


def _scrollables(page) -> str:
    found = page.evaluate(_SCROLLABLES_JS)
    if not found:
        return "Scrollable elements: none\n"
    lines = ["Scrollable elements (selector, how far it can scroll, where it is now):"]
    for item in found:
        lines.append(
            f"  {item['selector']}  scrollableY={item['scrollableY']}"
            f" scrollableX={item['scrollableX']} scrollTop={item['scrollTop']}"
            f" height={item['height']}"
        )
    return "\n".join(lines) + "\n"


def _stack_top(frames: list) -> str | None:
    """First frame that is not the recorder we injected."""
    for frame in frames or []:
        text = str(frame)
        if "UtilityScript" in text or "evaluate" in text or "<anonymous>:" in text:
            continue
        return text
    return str(frames[0]) if frames else None


def summarise(result: WebRunResult) -> dict:
    """Turn the step history into findings, using only what was recorded."""
    assignments: list[dict] = []
    jumps: list[dict] = []
    readings: list[dict] = []
    acted: list[str] = []

    for entry in result.history:
        decision = entry.get("decision") or {}
        action = decision.get("action")
        value = entry.get("value") or {}
        if action in {"arm", "collect", "trace", "watch"}:
            for hit in value.get("hits") or []:
                assignments.append({
                    "selector": decision.get("selector"),
                    "prop": decision.get("prop"),
                    "from": hit.get("from"), "to": hit.get("to"),
                    "source": _stack_top(hit.get("stack")),
                    "stack": hit.get("stack"),
                })
            for jump in value.get("jumps") or []:
                jumps.append({
                    "selector": decision.get("selector"),
                    "prop": decision.get("prop"),
                    "from": jump.get("from"), "to": jump.get("to"),
                    "scrollHeight": jump.get("scrollHeight"),
                })
        elif action == "read":
            readings.append({"selector": decision.get("selector"), "value": value})
        elif action in {"click", "fill", "press", "scroll"}:
            detail = decision.get("target") or decision.get("selector") or ""
            if action == "fill":
                detail = f'{decision.get("placeholder")}={decision.get("text")!r}'
            if action == "press":
                detail = decision.get("key")
            if action == "scroll":
                detail = f'{detail} dy={decision.get("dy")}'
            acted.append(f"{action} {detail}".strip())

    moved = [item for item in assignments + jumps
             if item.get("from") is not None and item["from"] != item["to"]]
    if assignments:
        culprit = assignments[0]["source"]
        confidence = "assigned"
    elif jumps:
        culprit = None
        confidence = "no assignment — reset without a stack (re-attach or relayout)"
    else:
        culprit = None
        confidence = "nothing recorded"

    return {
        "reproduced": bool(moved),
        "culprit": culprit,
        "how": confidence,
        "assignments": assignments,
        "implicit_jumps": jumps,
        "readings": readings,
        "user_actions": acted,
        "model_summary": result.reason,
        "status": result.status,
        "steps": result.steps,
    }


def debug(task: str, url: str, provider, **kwargs) -> dict:
    """Run the diagnosis and return findings built from the evidence."""
    kwargs.setdefault("typing", True)
    result = run(task, url, provider, system_prompt=DEBUG_PROMPT,
                 extra_context=_scrollables, **kwargs)
    findings = summarise(result)
    findings["history"] = result.history
    return findings
