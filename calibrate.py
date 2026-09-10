"""
calibrate.py — Live match calibration helper for DarwinDirector

Run this in a separate terminal WHILE the game (and optionally the bot) is running.
It uses Win32 GetAsyncKeyState so hotkeys fire even when the game window has focus.

HOTKEYS
-------
F2  — Snapshot: saves full screenshot + logs mouse (x,y) + pixel RGB to calibration_log.json
      (tag as zone_color or generic — see TAG_CHOICES)
F3  — Zone colour shortcut: same as F2's zone_color tag, one prompt instead of two
F4  — Template crop: saves a 200x80 px region centred on mouse as a template PNG
F5  — Results screen: save screenshot tagged as 'results_screen' (use at match end)
F6  — Quit and print a config.json snippet from everything collected
F7  — Zone map snapshot (silent, for reference while holding a card)
F8  — Player bar check (see MENU below)

Auto-save: every 20 seconds a screenshot is taken silently (files named auto_*.png)

Card tray layout, per-card drop targets, and zone drop coordinates are no longer
calibrated here — they're hardcoded constants in game/match_runner.py (see CLAUDE.md's
Config Reference and Calibration Checklist), proven stable at 1920×1080. This tool now
only calibrates zone_color_thresholds (still genuinely uncalibrated) and the player-bar
keys (via F8).

WORKFLOW
--------
1. Start the match via Discord /start.
2. Run: python calibrate.py
3. When the zone map is visible (first zone close ~30s in):
     - Hover over an open zone hex colour and press F3 (or F2 → zone_color).
     - Same for a closing and a closed zone as they change.
4. At match end, press F5 on the results screen.
5. Press F6 to quit and see the config snippet.

All screenshots go to: calibration_screenshots/
Log file: calibration_log.json
"""

import ctypes
import json
import time
import datetime
import sys
import threading
from pathlib import Path

import pyautogui
import cv2
import numpy as np

# ── Win32 helpers ─────────────────────────────────────────────────────────────
user32 = ctypes.windll.user32

VK_F2 = 0x71
VK_F3 = 0x72
VK_F4 = 0x73
VK_F5 = 0x74
VK_F6 = 0x75
VK_F7 = 0x76
VK_F8 = 0x77

_key_was_down: dict[int, bool] = {}


def _pressed(vk: int) -> bool:
    """Return True once per physical key-down event (rising edge)."""
    down = bool(user32.GetAsyncKeyState(vk) & 0x8000)
    was = _key_was_down.get(vk, False)
    _key_was_down[vk] = down
    return down and not was


# ── Screenshot helpers ────────────────────────────────────────────────────────
OUT_DIR = Path("calibration_screenshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = Path("calibration_log.json")
_log: list[dict] = []


def _take() -> np.ndarray:
    img = pyautogui.screenshot()
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def _stamp() -> str:
    return datetime.datetime.now().strftime("%H-%M-%S")


def _mouse() -> tuple[int, int]:
    return pyautogui.position()


def _pixel(img: np.ndarray, x: int, y: int) -> tuple[int, int, int]:
    bgr = img[y, x]
    return (int(bgr[2]), int(bgr[1]), int(bgr[0]))


def _save_img(img: np.ndarray, name: str) -> Path:
    path = OUT_DIR / name
    cv2.imwrite(str(path), img)
    return path


# ── Auto-screenshot thread ────────────────────────────────────────────────────
_stop_auto = threading.Event()


def _auto_loop():
    count = 0
    while not _stop_auto.wait(20):
        img = _take()
        _save_img(img, f"auto_{_stamp()}_{count:03d}.png")
        count += 1


# ── Calibration session data ──────────────────────────────────────────────────
_snapshots: list[dict] = []


def _snapshot(img: np.ndarray, tag: str, label: str):
    x, y = _mouse()
    rgb = _pixel(img, x, y)
    ts = _stamp()
    fname = f"{tag}_{ts}.png"
    path = _save_img(img, fname)
    entry = {
        "tag": tag,
        "label": label,
        "file": str(path),
        "mouse_x": x,
        "mouse_y": y,
        "pixel_rgb": list(rgb),
        "time": ts,
    }
    _snapshots.append(entry)
    print(f"  [{tag}] {label}  mouse=({x},{y})  rgb={rgb}  -> {fname}")


def _crop_template(img: np.ndarray, cx: int, cy: int, w: int = 200, h: int = 80) -> np.ndarray:
    x0 = max(0, cx - w // 2)
    y0 = max(0, cy - h // 2)
    x1 = min(img.shape[1], x0 + w)
    y1 = min(img.shape[0], y0 + h)
    return img[y0:y1, x0:x1]


# ── Config builder ────────────────────────────────────────────────────────────

def _build_config_snippet() -> dict:
    """Assemble a partial config.json from collected snapshots — paste into your config.

    card_slots/cards/zone_close_card_slot/zone_sample_coordinates/zone_drop_coordinates
    were removed from here (2026-09-07): the first three were never real config keys the
    bot actually read (card tray layout and per-card drop targets are hardcoded constants
    in game/match_runner.py — see _CARD_TRAY_CENTER_X/_CARD_DROP_TARGETS), and the other
    two were moved to hardcoded constants (_ZONE_DROP_COORDINATES) or deleted outright
    (zone_sample_coordinates was never read by any code, calibrated or not). Only
    zone_color_thresholds remains a real, still-editable config key.
    """
    snippet: dict = {
        "_note": "Partial calibration output — merge into config.json manually",
        "zone_color_thresholds": {"open": None, "closing": None, "closed": None},
    }

    for s in _snapshots:
        tag, label = s["tag"], s["label"].lower().strip()
        rgb = s["pixel_rgb"]

        if tag == "zone_color":
            state = label  # 'open', 'closing', or 'closed'
            if state in ("open", "closing", "closed"):
                snippet["zone_color_thresholds"][state] = rgb

    return snippet


# ── Main loop ─────────────────────────────────────────────────────────────────

MENU = """
==========================================================
          DarwinDirector Calibration Helper
==========================================================
  F2 -> Snapshot (generic -- you'll name it)
  F3 -> Zone colour sample (feeds zone_color_thresholds)
  F4 -> Save 200x80 template crop around mouse
  F5 -> Results screen snapshot (full, tagged)
  F6 -> Quit + write calibration_log.json + config snippet
  F7 -> Zone map snapshot (press while holding card — saves
        full screenshot tagged 'zone_map_state' for analysis)
  F8 -> Player bar check (press IN A MATCH with the card strip
        visible): saves the frame, runs the bot's own V2 card /
        alive / name detection on it, prints what it saw, writes
        player_bar_*.annotated.png. See docs/PLAYER_BAR_CALIBRATION.md
==========================================================

Auto-screenshots every 20 s -> calibration_screenshots/auto_*.png
Waiting for hotkeys... (game window can be focused)
"""

TAG_CHOICES = """
Tag this snapshot:
  1  zone_color      (the colour of a zone in a specific state)
  2  generic         (just a labelled screenshot for reference)
Choice [1-2]: """


def _ask_tag() -> tuple[str, str]:
    tag_map = {
        "1": "zone_color",
        "2": "generic",
    }
    while True:
        choice = input(TAG_CHOICES).strip()
        if choice in tag_map:
            tag = tag_map[choice]
            break
        print("  Invalid choice, try again.")

    label_hint = {
        "zone_color": "'open', 'closing', or 'closed'",
        "generic":    "any description",
    }
    label = input(f"  Label ({label_hint[tag]}): ").strip()
    return tag, label


def main():
    print(MENU)

    auto_thread = threading.Thread(target=_auto_loop, daemon=True)
    auto_thread.start()

    try:
        while True:
            time.sleep(0.05)  # 50ms poll — light on CPU

            if _pressed(VK_F2):
                print("\n[F2] Generic snapshot — hover mouse on target, then answer:")
                img = _take()
                tag, label = _ask_tag()
                _snapshot(img, tag, label)

            elif _pressed(VK_F3):
                # Quick zone colour shortcut — feeds zone_color_thresholds, the one
                # remaining real config key this tool calibrates.
                print("\n[F3] Zone colour sample — hover mouse on zone hex:")
                img = _take()
                label = input("  State — 'open', 'closing', or 'closed': ").strip()
                _snapshot(img, "zone_color", label)

            elif _pressed(VK_F4):
                print("\n[F4] Template crop — hover mouse on centre of element:")
                img = _take()
                x, y = _mouse()
                label = input("  Template name (e.g. 'placement_badge'): ").strip()
                crop = _crop_template(img, x, y)
                # Save to calibration_screenshots/ for reference
                tpl_path = OUT_DIR / f"template_{label}_{_stamp()}.png"
                cv2.imwrite(str(tpl_path), crop)
                # Also save directly to templates/ so the bot can use it immediately
                templates_dir = Path("templates")
                templates_dir.mkdir(exist_ok=True)
                live_path = templates_dir / f"{label}.png"
                cv2.imwrite(str(live_path), crop)
                _snapshot(img, "template", label)
                print(f"  Template saved -> {tpl_path}")
                print(f"  Live template  -> {live_path}  <- bot will use this")

            elif _pressed(VK_F5):
                print("\n[F5] Results screen snapshot — saving full screenshot...")
                img = _take()
                fname = f"results_screen_{_stamp()}.png"
                _save_img(img, fname)
                entry = {
                    "tag": "results_screen",
                    "label": "results_screen",
                    "file": str(OUT_DIR / fname),
                    "mouse_x": _mouse()[0],
                    "mouse_y": _mouse()[1],
                    "pixel_rgb": list(_pixel(img, *_mouse())),
                    "time": _stamp(),
                }
                _snapshots.append(entry)
                print(f"  Saved -> {fname}")
                print("  Note: OCR regions must be identified from this image manually.")

            elif _pressed(VK_F7):
                # Silent snapshot — no terminal input needed so it works while holding a card
                img = _take()
                ts = _stamp()
                fname = f"zone_map_state_{ts}.png"
                _save_img(img, fname)
                entry = {
                    "tag": "zone_map_state",
                    "label": "zone_map_state",
                    "file": str(OUT_DIR / fname),
                    "mouse_x": _mouse()[0],
                    "mouse_y": _mouse()[1],
                    "pixel_rgb": list(_pixel(img, *_mouse())),
                    "time": ts,
                }
                _snapshots.append(entry)
                # Print to console (won't interrupt drag since game window is focused)
                print(f"\n[F7] Zone map snapshot saved -> {fname}")

            elif _pressed(VK_F8):
                # Silent capture (no terminal input) so it works mid-match, then
                # the same analysis calibrate_player_bar.py does offline.
                img = _take()
                ts = _stamp()
                fname = f"player_bar_{ts}.png"
                _save_img(img, fname)
                _snapshots.append({
                    "tag": "player_bar", "label": "player_bar", "file": str(OUT_DIR / fname),
                    "mouse_x": _mouse()[0], "mouse_y": _mouse()[1],
                    "pixel_rgb": list(_pixel(img, *_mouse())), "time": ts,
                })
                print(f"\n[F8] Player bar frame saved -> {fname}")
                try:
                    from game.player_bar_calibration import analyze_v2, annotate_v2, format_report_v2
                    cfg = {}
                    if Path("config.json").exists():
                        cfg = json.loads(Path("config.json").read_text(encoding="utf-8"))
                    rep = analyze_v2(img, cfg)
                    print(format_report_v2(rep))
                    ann = OUT_DIR / f"player_bar_{ts}.annotated.png"
                    cv2.imwrite(str(ann), annotate_v2(img, rep))
                    print(f"  annotated -> {ann}")
                except Exception as e:
                    print(f"  analysis failed: {e} — run: python calibrate_player_bar.py {OUT_DIR / fname}")

            elif _pressed(VK_F6):
                print("\n[F6] Quitting...")
                break

    except KeyboardInterrupt:
        print("\nCtrl-C received — quitting...")

    finally:
        _stop_auto.set()

        # Write log
        LOG_FILE.write_text(json.dumps(_snapshots, indent=2))
        print(f"\nCalibration log written -> {LOG_FILE}")

        # Write config snippet
        snippet = _build_config_snippet()
        snippet_path = Path("calibration_config_snippet.json")
        snippet_path.write_text(json.dumps(snippet, indent=4))
        print(f"Config snippet written -> {snippet_path}")
        print("\n--- Config snippet preview ---")
        print(json.dumps(snippet, indent=4))


if __name__ == "__main__":
    main()
