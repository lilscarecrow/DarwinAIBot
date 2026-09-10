"""save_feed_debug_images() (game/ocr.py, 2026-09-10) — captures the raw
natural-color feed crop plus the processed image tesseract actually sees,
whenever match_runner.py's _poll_damage_feed_worker() gets a pattern match.
Exists to gather real pixel data on the still-open colored-name OCR gap:
player names in the damage feed render orange/red, not white, and the
current min-channel preprocessing can't separate orange/red text from a
saturated colored background (both have a low blue channel) — see
ocr_feed_text()'s own "Known remaining gap" docstring note.
"""
import numpy as np

from game.ocr import _prepare_feed_crop, save_feed_debug_images

_REGION = (10, 10, 20, 8)


def _screenshot():
    img = np.zeros((100, 100, 3), dtype=np.uint8)
    img[10:18, 10:30] = (0, 128, 220)  # some saturated BGR fill, not black
    return img


def test_prepare_feed_crop_returns_none_when_region_is_out_of_bounds():
    tiny = np.zeros((5, 5, 3), dtype=np.uint8)
    crop, processed = _prepare_feed_crop(tiny, _REGION)
    assert crop is None and processed is None


def test_prepare_feed_crop_returns_raw_and_processed_images():
    crop, processed = _prepare_feed_crop(_screenshot(), _REGION)
    assert crop is not None and processed is not None
    assert crop.shape[:2] == (8, 20)  # unscaled — natural crop size
    assert processed.shape[:2] == (24, 60)  # 3x upscaled, per ocr_feed_text's preprocessing


def test_save_feed_debug_images_writes_both_files(tmp_path):
    save_feed_debug_images(_screenshot(), _REGION, str(tmp_path), "20260910_feed_first_blood")
    assert (tmp_path / "20260910_feed_first_blood_raw.png").exists()
    assert (tmp_path / "20260910_feed_first_blood_processed.png").exists()


def test_save_feed_debug_images_noops_silently_when_out_of_bounds(tmp_path):
    tiny = np.zeros((5, 5, 3), dtype=np.uint8)
    save_feed_debug_images(tiny, _REGION, str(tmp_path), "label")
    assert list(tmp_path.iterdir()) == []
