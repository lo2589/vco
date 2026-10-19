"""Desktop-scripting commands: negative-coordinate argv handling, drag, wait.

These three all came out of real dual-monitor use (a display left of the main
one has negative global X), so the tests pin the exact shapes that broke:
`--at -1286,580` eaten by argparse, the drag gesture's modifier wrapping, and
wait's exit-code contract for shell scripts.
"""

import io
import json
import contextlib
import unittest
from unittest import mock

from vco.cli import _normalize_coord_args, build_parser, main
from vco.models import Region


def _run(argv):
    """main() with stdout captured; returns (exit_code, parsed_json_or_text)."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = main(argv)
    text = buf.getvalue().strip()
    try:
        return rc, json.loads(text)
    except json.JSONDecodeError:
        return rc, text


class NormalizeCoordArgsTest(unittest.TestCase):
    def test_negative_at_is_rewritten(self):
        self.assertEqual(
            _normalize_coord_args(["click", "--at", "-1286,580"]),
            ["click", "--at=-1286,580"],
        )

    def test_from_to_and_region_are_covered(self):
        self.assertEqual(
            _normalize_coord_args(
                ["drag", "--from", "-100,-200", "--to", "5,6"]),
            ["drag", "--from=-100,-200", "--to", "5,6"],
        )
        self.assertEqual(
            _normalize_coord_args(["shot", "--region", "-1286,0,100,200"]),
            ["shot", "--region=-1286,0,100,200"],
        )

    def test_a_real_flag_after_the_flag_is_untouched(self):
        # --at with no value: the next token is another flag, not coordinates.
        argv = ["click", "--at", "--double"]
        self.assertEqual(_normalize_coord_args(argv), argv)

    def test_a_plain_negative_number_is_not_a_coordinate(self):
        # --amount -5 is a legitimate value argparse handles on its own.
        argv = ["scroll", "--amount", "-5"]
        self.assertEqual(_normalize_coord_args(argv), argv)

    def test_rewritten_argv_parses(self):
        args = build_parser().parse_args(
            _normalize_coord_args(["click", "--at", "-1286,580"]))
        self.assertEqual(args.at, (-1286, 580))
        args = build_parser().parse_args(
            _normalize_coord_args(["shot", "--region", "-1286,0,100,200"]))
        self.assertEqual((args.region.x, args.region.y,
                          args.region.width, args.region.height),
                         (-1286, 0, 100, 200))


class DragTest(unittest.TestCase):
    def _pyautogui_sequence(self, argv):
        seq = mock.MagicMock()
        with mock.patch.multiple(
            "pyautogui",
            keyDown=seq.keyDown, moveTo=seq.moveTo,
            mouseDown=seq.mouseDown, mouseUp=seq.mouseUp, keyUp=seq.keyUp,
        ):
            rc, out = _run(argv)
        return rc, out, seq

    def test_drag_with_modifier_wraps_the_whole_gesture(self):
        rc, out, seq = self._pyautogui_sequence(
            ["drag", "--from", "-100,200", "--to", "-900,640", "--modifier", "ctrl"])
        self.assertEqual(rc, 0)
        # The negative --from also proves the argv normalization end-to-end.
        self.assertEqual(seq.mock_calls, [
            mock.call.keyDown("ctrl"),
            mock.call.moveTo(-100, 200),
            mock.call.mouseDown(button="left"),
            mock.call.moveTo(-900, 640, duration=0.5),
            mock.call.mouseUp(button="left"),
            mock.call.keyUp("ctrl"),
        ])
        self.assertEqual(out, {"dragged": True, "from": [-100, 200],
                               "to": [-900, 640], "modifier": ["ctrl"]})

    def test_modifiers_are_released_even_when_the_drag_blows_up(self):
        seq = mock.MagicMock()
        seq.moveTo.side_effect = [None, RuntimeError("display went away")]
        with mock.patch.multiple(
            "pyautogui",
            keyDown=seq.keyDown, moveTo=seq.moveTo,
            mouseDown=seq.mouseDown, mouseUp=seq.mouseUp, keyUp=seq.keyUp,
        ):
            with self.assertRaises(RuntimeError), contextlib.redirect_stdout(io.StringIO()):
                main(["drag", "--from", "1,2", "--to", "3,4", "--modifier", "cmd"])
        seq.keyUp.assert_called_once_with("command")


class WaitTest(unittest.TestCase):
    def _box(self, x, y):
        box = mock.MagicMock()
        box.center = (x, y)
        return box

    def _result(self, matches):
        result = mock.MagicMock()
        result.find_text.return_value = matches
        return result

    def _wait(self, argv, recognize_side_effect):
        backend = mock.MagicMock()
        backend.recognize.side_effect = recognize_side_effect
        region = Region(x=0, y=0, width=100, height=100)
        with mock.patch("vco.cli.create_ocr_backend", return_value=backend), \
                mock.patch("vco.cli._capture_for_args", return_value=(None, region)):
            rc, out = _run(argv)
        return rc, out, backend

    def test_appear_returns_as_soon_as_the_target_shows(self):
        hit = self._result([self._box(50, 60)])
        rc, out, backend = self._wait(
            ["wait", "--target", "OK", "--timeout", "5", "--interval", "0.01"],
            [self._result([]), self._result([]), hit],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["condition"], "appear")
        self.assertTrue(out["met"])
        self.assertEqual((out["x"], out["y"]), (50, 60))
        self.assertEqual(backend.recognize.call_count, 3)
        hit.find_text.assert_called_with("OK", exact=True)

    def test_contains_relaxes_the_match(self):
        hit = self._result([self._box(10, 10)])
        rc, out, backend = self._wait(
            ["wait", "--target", "OK", "--contains", "--timeout", "5", "--interval", "0.01"],
            [hit],
        )
        self.assertTrue(out["met"])
        hit.find_text.assert_called_with("OK", exact=False)

    def test_disappear_waits_until_the_target_is_gone(self):
        rc, out, _ = self._wait(
            ["wait", "--target", "加载中", "--disappear", "--timeout", "5", "--interval", "0.01"],
            [self._result([self._box(1, 1)]), self._result([])],
        )
        self.assertEqual(rc, 0)
        self.assertEqual(out["condition"], "disappear")
        self.assertTrue(out["met"])
        self.assertNotIn("x", out)

    def test_timeout_is_met_false_and_a_nonzero_exit(self):
        rc, out, _ = self._wait(
            ["wait", "--target", "never", "--timeout", "0.05", "--interval", "0.01"],
            [self._result([])] * 50,
        )
        self.assertEqual(rc, 2)
        self.assertFalse(out["met"])
        self.assertGreaterEqual(out["waited"], 0.0)


class ClickGuardTest(unittest.TestCase):
    """--guard-reference: a click computed from one screenshot must not land
    after the layout drifted. This came out of a real demo recording where a
    stale composer coordinate clicked into a table instead."""

    def _image(self, color):
        from PIL import Image

        return Image.new("RGB", (200, 200), color)

    def _reference(self, tmp, image):
        path = tmp / "reference.png"
        image.save(path)
        return path

    def _click(self, argv, current):
        region = Region(x=0, y=0, width=current.width, height=current.height)
        with mock.patch(
            "vco.cli._capture_for_args", return_value=(current, region)
        ), mock.patch("vco.cli.PyAutoGuiExecutor") as executor:
            rc, out = _run(argv)
        return rc, out, executor

    def test_guard_passes_when_the_region_still_matches(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            image = self._image((200, 220, 240))
            ref = self._reference(tmp, image)
            rc, out, executor = self._click(
                ["click", "--at=100,100", f"--guard-reference={ref}",
                 "--no-click-trace", "--dir", str(tmp)],
                image.copy(),
            )
        self.assertEqual(rc, 0)
        self.assertTrue(out["clicked"])
        executor.return_value.execute.assert_called_once()

    def test_guard_blocks_when_the_layout_drifted(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ref = self._reference(tmp, self._image((20, 20, 20)))
            rc, out, executor = self._click(
                ["click", "--at=100,100", f"--guard-reference={ref}",
                 "--no-click-trace", "--dir", str(tmp)],
                self._image((240, 240, 240)),
            )
            self.assertEqual(rc, 3)
            self.assertFalse(out["clicked"])
            self.assertEqual(out["guard"], "region-changed")
            executor.return_value.execute.assert_not_called()
            # Both crops are on disk so the drift is reviewable, not just a code.
            self.assertTrue((tmp / out["reference_crop"].split("/")[-1]).exists())
            self.assertTrue((tmp / out["current_crop"].split("/")[-1]).exists())

    def test_guard_reference_requires_at(self):
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            ref = self._reference(Path(td), self._image((0, 0, 0)))
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                main(["click", "--target", "OK", f"--guard-reference={ref}"])

    def test_guard_reference_size_must_match_the_capture(self):
        import tempfile
        from pathlib import Path
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            ref = self._reference(tmp, Image.new("RGB", (100, 100), (0, 0, 0)))
            with self.assertRaises(SystemExit), contextlib.redirect_stdout(io.StringIO()):
                self._click(
                    ["click", "--at=50,50", f"--guard-reference={ref}",
                     "--no-click-trace", "--dir", str(tmp)],
                    self._image((255, 255, 255)),
                )


if __name__ == "__main__":
    unittest.main()
