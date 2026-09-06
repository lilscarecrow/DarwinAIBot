"""player-bar analysis on a synthetic HUD frame: the bot's own detection, headless."""
from unittest.mock import MagicMock

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
if isinstance(cv2, MagicMock) or isinstance(np, MagicMock):  # conftest stubs when not installed
    pytest.skip("real opencv/numpy needed", allow_module_level=True)

from game import player_bar_calibration as pbc  # noqa: E402

BAR = [400, 20, 1520, 120]   # 11 cards of 100px + 10 separators of 2px = 1120px wide
CARD_W, SEP_W = 100, 2


def synthetic_frame(dead=(3, 7), portrait_y=35, name_y=62, name_h=14):
    """1920x1080 black frame with a card strip: card bodies mid-grey (so their
    max channel clears the separator threshold), 2px black separators, a
    saturated green portrait patch on alive cards and a grey one on dead cards,
    and a white name strip."""
    img = np.zeros((1080, 1920, 3), dtype=np.uint8)
    x0, y0, x1, y1 = BAR
    x = x0
    for i in range(11):
        img[y0:y1, x:x + CARD_W] = (70, 70, 70)
        patch = (120, 120, 120) if i in dead else (40, 200, 60)
        img[y0 + portrait_y - 6:y0 + portrait_y + 6, x + 20:x + 80] = patch
        img[y0 + name_y:y0 + name_y + name_h, x + 10:x + 90] = (255, 255, 255)
        x += CARD_W
        if i < 10:
            img[y0:y1, x:x + SEP_W] = (0, 0, 0)
            x += SEP_W
    return img


def test_detects_slots_alive_and_dead_with_defaults():
    img = synthetic_frame()
    rep = pbc.analyze(img, {"player_bar_region": BAR}, ocr=False)
    assert rep.n_slots_raw == 11
    assert len(rep.slots) == 10, "glitch slot dropped"
    dead = [s.index for s in rep.slots if not s.alive]
    # card 3 and 7 (glitch included) are real slots 2 and 6
    assert dead == [2, 6]
    assert all(s.saturation > 40 for s in rep.slots if s.alive)
    assert all(s.saturation < 40 for s in rep.slots if not s.alive)
    assert rep.problems == []


def test_missing_region_is_reported_not_crashed():
    rep = pbc.analyze(synthetic_frame(), {}, ocr=False)
    assert rep.slots == [] and rep.n_slots_raw == 0
    assert any("player_bar_region is not set" in p for p in rep.problems)


def test_wrong_region_reports_slot_count_and_sweep_finds_it():
    img = synthetic_frame()
    rep = pbc.analyze(img, {"player_bar_region": [0, 20, 1920, 120]}, ocr=False)
    assert rep.slots == []
    assert any("separator scan found" in p for p in rep.problems)
    rows = pbc.sweep(img, pbc.effective_config({"player_bar_region": BAR}))
    assert all(n == 11 for t, n in rows if t <= 60), rows


def test_portrait_row_off_the_portraits_is_flagged():
    img = synthetic_frame()
    rep = pbc.analyze(img, {"player_bar_region": BAR}, {"player_portrait_y_in_bar": 5}, ocr=False)
    assert len(rep.slots) == 10
    assert any("nearly uniform" in p for p in rep.problems)


def test_overrides_beat_config_beat_defaults():
    cfg = pbc.effective_config({"player_saturation_threshold": 55}, {"player_saturation_threshold": 60, "player_name_h": None})
    assert cfg["player_saturation_threshold"] == 60
    assert cfg["player_name_h"] == 14
    assert cfg["player_bar_region"] is None


def test_annotate_and_snippet():
    img = synthetic_frame()
    rep = pbc.analyze(img, {"player_bar_region": BAR}, ocr=False)
    out = pbc.annotate(img, rep)
    assert out.shape == img.shape and not np.array_equal(out, img)
    snip = pbc.config_snippet(rep)
    assert set(snip) == set(pbc.PLAYER_BAR_DEFAULTS)
    assert snip["player_bar_region"] == BAR
    text = pbc.format_report(rep, pbc.sweep(img, rep.config, thresholds=[25]))
    assert "11 slots" in text and "25→11" in text and "no problems" in text
