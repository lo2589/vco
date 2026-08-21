import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "run_web_ui_matrix", ROOT / "debug" / "run_web_ui_matrix.py"
)
MATRIX = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = MATRIX
SPEC.loader.exec_module(MATRIX)


class WebUIMatrixTests(unittest.TestCase):
    def test_configuration_matrix(self):
        configs = MATRIX.configurations()
        self.assertEqual(len(configs), 29)
        self.assertEqual([item.grid_n for item in configs[:23]], list(range(8, 31)))
        self.assertEqual(
            {(item.grid_n, item.levels) for item in configs[23:]},
            {(4, 2), (5, 2), (6, 2), (4, 3), (5, 3), (6, 3)},
        )

    def test_marker_is_exactly_ten_by_ten(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source.png"
            destination = Path(temporary) / "marked.png"
            cv2.imwrite(str(source), np.zeros((40, 60, 3), dtype=np.uint8))
            MATRIX.mark_with_cv(source, destination, (25, 20))
            marked = cv2.imread(str(destination))
            green = np.all(marked == np.array([0, 255, 0]), axis=2)
            self.assertEqual(int(green.sum()), 100)


if __name__ == "__main__":
    unittest.main()
