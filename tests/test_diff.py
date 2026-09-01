"""Tests for vco.diff image-region verification."""

from PIL import Image, ImageDraw
from unittest import TestCase

from vco.diff import crop_around, pixel_similarity, verify_target_stable


class DiffTests(TestCase):
    def test_identical_images_are_fully_similar(self):
        image = Image.new("RGB", (100, 100), color=(50, 50, 50))
        self.assertEqual(pixel_similarity(image, image), 1.0)

    def test_different_images_are_low_similarity(self):
        a = Image.new("RGB", (100, 100), color=(0, 0, 0))
        b = Image.new("RGB", (100, 100), color=(255, 255, 255))
        self.assertLess(pixel_similarity(a, b), 0.1)

    def test_crop_around_clamps_to_bounds(self):
        image = Image.new("RGB", (50, 50))
        crop = crop_around(image, (5, 5), (40, 40))
        # crop is shifted to stay inside the image while keeping requested size
        self.assertEqual(crop.size, (40, 40))

    def test_small_change_keeps_high_similarity(self):
        a = Image.new("RGB", (100, 100), color=(100, 100, 100))
        b = a.copy()
        draw = ImageDraw.Draw(b)
        draw.rectangle((40, 40, 60, 60), fill=(200, 200, 200))
        sim = pixel_similarity(a, b)
        self.assertGreater(sim, 0.8)

    def test_verify_target_stable_passes_for_identical(self):
        image = Image.new("RGB", (100, 100), color=(80, 80, 80))
        stable, sim = verify_target_stable(image, image, (50, 50))
        self.assertTrue(stable)
        self.assertEqual(sim, 1.0)

    def test_verify_target_stable_fails_for_large_change(self):
        reference = Image.new("RGB", (100, 100), color=(0, 0, 0))
        current = Image.new("RGB", (100, 100), color=(255, 255, 255))
        stable, sim = verify_target_stable(reference, current, (50, 50))
        self.assertFalse(stable)
        self.assertLess(sim, 0.5)
