"""V2 card detection against real VOD strips (tests/fixtures/player_bar)."""
import os
from unittest.mock import MagicMock

import pytest

cv2 = pytest.importorskip("cv2")
np = pytest.importorskip("numpy")
if isinstance(cv2, MagicMock) or isinstance(np, MagicMock):
    pytest.skip("real opencv/numpy needed", allow_module_level=True)

from game import player_cards_v2 as v2  # noqa: E402

FIX = os.path.join(os.path.dirname(__file__), "fixtures", "player_bar")
TRUTH = {
    "vod_900.png": (10, {0, 4, 6}),
    "vod_2100.png": (10, {0, 1, 3, 5, 7, 9}),
    "vod_2700.png": (10, {0, 5, 7}),
    "vod_3300.png": (9, set(range(9)) - {1}),
    "vod_3600.png": (9, {2, 4, 5, 6, 7, 8}),
}


def load(name):
    img = cv2.imread(os.path.join(FIX, name))
    assert img is not None, name
    return img


@pytest.mark.parametrize("name", sorted(TRUTH))
def test_detects_count_and_alive_on_real_frames(name):
    n, alive = TRUTH[name]
    cards = v2.detect_cards(load(name))
    assert len(cards) == n, [(c.x, c.alive) for c in cards]
    assert {c.index for c in cards if c.alive} == alive
    assert [c.index for c in cards] == list(range(n))
    xs = [c.x for c in cards]
    assert all(120 < b - a < 145 for a, b in zip(xs, xs[1:])), "constant pitch"
    assert abs(sum(xs) / n - 960) < 4, "centred block"


@pytest.mark.parametrize("name", sorted(TRUTH))
def test_per_poll_alive_matches_detection(name):
    img = load(name)
    cards = v2.detect_cards(img)
    assert v2.cards_alive(img, [c.x for c in cards]) == [c.alive for c in cards]


def test_expected_count_is_a_lower_bound_the_strip_overrides():
    img = load("vod_3300.png")
    assert len(v2.detect_cards(img, expected=9)) == 9
    assert len(v2.detect_cards(img, expected=10)) == 9, "an expected count that does not fit is ignored"
    # A signup roster smaller than the strip (a late joiner) never shrinks the read.
    assert len(v2.detect_cards(img, expected=8)) == 9, "the strip wins over a smaller roster"
    assert len(v2.detect_cards(img, expected=None)) == 9


def test_dead_cards_carry_the_red_x_and_alive_ones_do_not():
    img = load("vod_900.png")
    cards = v2.detect_cards(img)
    assert all(c.red_x > 0.1 for c in cards if not c.alive)
    assert all(c.band_v > 150 and c.band_s > 200 for c in cards if c.alive)


def test_spectated_card_band_is_found_lower():
    cards = v2.detect_cards(load("vod_900.png"))
    assert cards[4].alive and cards[4].band_y > 125, "the enlarged card's band sits below the normal row"
    assert all(c.band_y < 120 for c in cards if c.alive and c.index != 4)


def test_blank_frame_yields_nothing():
    assert v2.detect_cards(np.zeros((1080, 1920, 3), dtype=np.uint8)) == []


def test_config_overrides_and_positions():
    cfg = v2.v2_config({"player_cards_pitch": 100, "player_cards_center_x": 500})
    assert v2.positions(3, cfg) == [400, 500, 600]
    assert v2.positions(2, cfg) == [450, 550]


def test_slot_number_for_index_and_its_inverse_round_trip():
    for index in range(10):
        key = v2.slot_number_for_index(index)
        assert v2.index_for_slot_number(key) == index
    assert v2.slot_number_for_index(9) == "0"  # the 10th slot is key "0"
    assert v2.index_for_slot_number("0") == 9
    assert v2.slot_number_for_index(0) == "1"
    assert v2.index_for_slot_number("1") == 0


# ── name OCR: runs only where a tesseract binary is reachable ──────────────
NAMES = {
    "vod_900.png": ["Nivoko", "SlyK", "Dom!n4toR", "BenHope", ":]", "Philipeace", "Justice", "coco pops", "-LAGADOU", "soid"],
    "vod_2100.png": ["Nivoko", "SlyK", "Dom!n4toR", "BenHope", ":]", "Philipeace", "Justice", "coco pops", "-LAGADOU", "soid"],
    "vod_2700.png": ["Nivoko", "SlyK", "Dom!n4toR", "BenHope", ":]", "Philipeace", "Justice", "coco pops", "-LAGADOU", "soid"],
    "vod_3300.png": ["Nivoko", "SlyK", "Dom!n4toR", "BenHope", ":]", "Philipeace", "Justice", "-LAGADOU", "soid"],
    "vod_3600.png": ["Nivoko", "SlyK", "Dom!n4toR", "BenHope", ":]", "Philipeace", "Justice", "-LAGADOU", "soid"],
}


def _tesseract_available():
    try:
        import pytesseract
        if isinstance(pytesseract, MagicMock):
            return False
        cmd = os.environ.get("TESSERACT_CMD")
        if cmd:
            pytesseract.pytesseract.tesseract_cmd = cmd
        pytesseract.get_tesseract_version()
        return True
    except Exception:
        return False


def _norm(s):
    return "".join(ch for ch in s.lower() if ch.isalnum())


@pytest.mark.skipif(not _tesseract_available(), reason="no tesseract binary (set TESSERACT_CMD to point at one)")
def test_name_ocr_reads_most_bands_exactly():
    """Floor measured with Windows tesseract 5.4 (the bot's binary): 35/48.
    The unreadable ones are the ':]' handle (a symbol, 5 crops) and single-
    glyph slips (SlvK) that the ladder's glyph fold resolves. Fail below 30
    so a preprocessing regression shows up wherever OCR can run."""
    hit = total = 0
    misses = []
    for name, truth in NAMES.items():
        img = load(name)
        cards = v2.detect_cards(img)
        got = v2.ocr_names(img, cards)
        assert len(got) == len(cards)
        for c, want in zip(cards, truth):
            total += 1
            ok = bool(_norm(want)) and (_norm(got[c.index]) == _norm(want) or _norm(want) in _norm(got[c.index]))
            hit += ok
            if not ok:
                misses.append((name, want, got[c.index]))
    assert hit >= 30, f"{hit}/{total} exact; misses: {misses}"
