"""DOM marks: layering, idempotence, click interception, annotations.

These run against the real scroll-bug fixture in headless Chromium because
the whole point of the module is what the page sees — badges, data-vco-id
attributes, captured clicks — and none of that exists short of a browser.
"""

from pathlib import Path

import pytest

pytest.importorskip("playwright.sync_api")
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import sync_playwright

from vco.dommarks import annotate, clear_marks, marks_prompt_table, snapshot_marks

FIXTURE = Path(__file__).resolve().parents[1] / "examples" / "scroll_bug_fixture.html"


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except PlaywrightError as exc:
            pytest.skip(f"Chromium not installed: {exc}")
        with browser.new_page(viewport={"width": 1280, "height": 720}) as page:
            page.goto(FIXTURE.as_uri())
            yield page
        browser.close()


def _badge_count(page) -> int:
    return page.evaluate(
        "() => window.__vcoMarksLayer ? window.__vcoMarksLayer.children.length : 0"
    )


def test_snapshot_marks_interactive_elements(page):
    result = snapshot_marks(page)
    assert result["marks"], "fixture has buttons and an input, marks must not be empty"
    assert result["clicks"] == []
    ids = {mark["id"] for mark in result["marks"]}
    assert len(ids) == len(result["marks"]), "ids must be unique"
    by_text = {mark["text"]: mark for mark in result["marks"]}
    send = by_text["Send"]
    assert send["tag"] == "button"
    assert send["id"].startswith("N")
    assert send["fixed"] is False
    assert send["selector"] == "#send"
    x0, y0, x1, y1 = send["bbox"]
    assert x1 > x0 and y1 > y0


def _ensure_fixed_nav(page) -> None:
    # The fixture itself has no fixed chrome, so fixed-layer tests inject one.
    page.evaluate(
        """() => {
            if (document.getElementById('fixed-nav')) return;
            const nav = document.createElement('div');
            nav.id = 'fixed-nav';
            nav.style.cssText = 'position:fixed;top:0;left:0;z-index:1000;';
            nav.innerHTML = '<button id="nav-cta">Nav action</button>';
            document.body.appendChild(nav);
        }"""
    )


def test_fixed_elements_get_full_f_prefix_numbering(page):
    _ensure_fixed_nav(page)
    result = snapshot_marks(page)
    fixed = [mark for mark in result["marks"] if mark["fixed"]]
    assert fixed, "the injected fixed bar must be numbered"
    assert all(mark["id"].startswith("F") for mark in fixed)
    assert [mark["id"] for mark in fixed] == [f"F{i}" for i in range(1, len(fixed) + 1)]
    assert any(mark["text"] == "Nav action" for mark in fixed)
    normal = [mark for mark in result["marks"] if not mark["fixed"]]
    assert all(mark["id"].startswith("N") for mark in normal)


def test_snapshot_is_idempotent(page):
    snapshot_marks(page)
    result = snapshot_marks(page)
    assert _badge_count(page) == len(result["marks"])
    tagged = page.evaluate(
        "() => document.querySelectorAll('[data-vco-id]').length"
    )
    assert tagged == len(result["marks"])


def test_normal_marks_are_truncated_by_max_normal(page):
    result = snapshot_marks(page, max_normal=2)
    normal = [mark for mark in result["marks"] if not mark["fixed"]]
    assert len(normal) == 2


def test_clicks_are_intercepted_and_harvested(page):
    first = snapshot_marks(page)
    send = next(mark for mark in first["marks"] if mark["text"] == "Send")
    page.click("#send")
    second = snapshot_marks(page)
    hits = [click for click in second["clicks"] if click["id"] == send["id"]]
    assert hits, f"click on #send ({send['id']}) missing from {second['clicks']}"
    assert hits[0]["x"] > 0 and hits[0]["y"] > 0 and hits[0]["ts"] > 0
    assert snapshot_marks(page)["clicks"] == [], "harvested log must be drained"


def test_annotate_registers_and_pins(page):
    result = snapshot_marks(page)
    mark = result["marks"][0]
    assert annotate(page, mark["id"], "点了没反应") is True
    stored = page.evaluate(
        "([id]) => window.__vcoMarks[id].annotations", [mark["id"]]
    )
    assert stored == ["点了没反应"]
    assert _badge_count(page) == len(result["marks"]) + 1  # the visible pin
    assert annotate(page, "N999", "no such mark") is False


def test_prompt_table_carries_ids_text_and_annotations(page):
    _ensure_fixed_nav(page)
    result = snapshot_marks(page)
    send = next(mark for mark in result["marks"] if mark["text"] == "Send")
    annotate(page, send["id"], "点了没反应")
    stored = page.evaluate("() => window.__vcoMarks")
    table = marks_prompt_table(
        [{"id": mark_id, **entry} for mark_id, entry in stored.items()]
    )
    line = next(row for row in table.splitlines() if row.startswith(f"#{send['id']} "))
    assert "[button]" in line and '"Send"' in line and "bbox=(" in line
    assert '⚠ "点了没反应"' in line
    fixed_row = next(row for row in table.splitlines() if row.startswith("#F"))
    assert fixed_row.endswith("fixed") or " fixed " in fixed_row


def test_clear_marks_removes_everything(page):
    snapshot_marks(page)
    clear_marks(page)
    assert page.evaluate("() => window.__vcoMarksLayer") is None
    assert page.evaluate("() => document.querySelectorAll('[data-vco-id]').length") == 0
    assert page.evaluate("() => window.__vcoClickLog") is None
    # The listener is gone too: a click after clear is never recorded.
    page.click("#send")
    assert page.evaluate("() => window.__vcoClickLog") is None
