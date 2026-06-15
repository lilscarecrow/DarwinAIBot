"""
calibrate_zone_colors.py — Sample zone colors from the big zone map

WORKFLOW
--------
1. Start a match as Director.
2. Run: python calibrate_zone_colors.py
3. Grab a zone_close card (shift+click+hold) so the big map appears.
4. While holding the card, press Enter in this terminal.
5. The script samples all zone_map_sample_points, prints colors per zone,
   then asks you to label each zone's current state (open/closing/closed).
6. It writes the suggested thresholds to calibration_log.json and prints
   the config snippet to paste into config.json.

You can run this multiple times (once with open zones, once with closed) to
collect samples for all three states. The script averages per-zone samples
to produce a single representative RGB value per state.
"""

import json
import time
import datetime
from pathlib import Path

import cv2
import numpy as np
import pyautogui


CONFIG_PATH = Path("config.json")
OUT_DIR = Path("calibration_screenshots")
OUT_DIR.mkdir(parents=True, exist_ok=True)


def take_screenshot() -> np.ndarray:
    img = pyautogui.screenshot()
    return cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)


def sample_points(screenshot: np.ndarray, points: list) -> list[tuple]:
    """Return RGB for each (x, y) point."""
    colors = []
    for x, y in points:
        if 0 <= y < screenshot.shape[0] and 0 <= x < screenshot.shape[1]:
            bgr = screenshot[y, x]
            colors.append((int(bgr[2]), int(bgr[1]), int(bgr[0])))
        else:
            print(f"  WARNING: point ({x},{y}) is out of screenshot bounds — skipping")
    return colors


def average_rgb(colors: list[tuple]) -> tuple:
    if not colors:
        return (0, 0, 0)
    r = round(sum(c[0] for c in colors) / len(colors))
    g = round(sum(c[1] for c in colors) / len(colors))
    b = round(sum(c[2] for c in colors) / len(colors))
    return (r, g, b)


def save_debug_image(screenshot: np.ndarray, sample_points_map: dict, stamp: str):
    """Save screenshot with zone center markers for verification."""
    debug = screenshot.copy()
    for zone_id, points in sample_points_map.items():
        for x, y in points:
            cv2.circle(debug, (x, y), 8, (0, 255, 0), 2)
        # Label zone at first point
        if points:
            cx = round(sum(p[0] for p in points) / len(points))
            cy = round(sum(p[1] for p in points) / len(points))
            cv2.putText(debug, f"Z{zone_id}", (cx - 10, cy + 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.6, (255, 255, 0), 2)
    path = OUT_DIR / f"zone_color_debug_{stamp}.png"
    cv2.imwrite(str(path), debug)
    print(f"  Debug image saved: {path}")
    return path


def main():
    config = json.loads(CONFIG_PATH.read_text())
    sample_points_cfg = config.get("zone_map_sample_points", {})

    if not sample_points_cfg or all(v is None for v in sample_points_cfg.values()):
        print("ERROR: zone_map_sample_points not configured in config.json.")
        print("Run the bot calibration first to set zone coordinates.")
        return

    print(__doc__)
    print("="*60)
    print("Grab the zone_close card to show the big map, then press Enter...")
    input()

    stamp = datetime.datetime.now().strftime("%H-%M-%S")
    screenshot = take_screenshot()

    # Save full screenshot for reference
    full_path = OUT_DIR / f"zone_color_capture_{stamp}.png"
    cv2.imwrite(str(full_path), screenshot)
    print(f"\nFull screenshot saved: {full_path}")

    # Sample all zones
    print("\nSampling zone colors:")
    zone_samples: dict[str, list[tuple]] = {}
    for zone_id, points in sample_points_cfg.items():
        if not points:
            continue
        colors = sample_points(screenshot, points)
        zone_samples[zone_id] = colors
        avg = average_rgb(colors)
        print(f"  Zone {zone_id}: avg RGB={avg}  (samples: {colors})")

    # Save debug image with markers
    save_debug_image(screenshot, sample_points_cfg, stamp)

    # Ask user to label each zone's current state
    print("\nLabel each zone's current state.")
    print("Enter: o=open, c=closed, k=closing, ?=unknown/skip")
    print()

    state_samples: dict[str, list[tuple]] = {"open": [], "closing": [], "closed": []}
    for zone_id in sorted(zone_samples.keys(), key=int):
        colors = zone_samples[zone_id]
        avg = average_rgb(colors)
        label = input(f"  Zone {zone_id} (avg {avg}): [o/c/k/?] ").strip().lower()
        if label == "o":
            state_samples["open"].extend(colors)
        elif label == "c":
            state_samples["closed"].extend(colors)
        elif label == "k":
            state_samples["closing"].extend(colors)

    # Compute per-state averages
    thresholds = {}
    print("\nSuggested zone_color_thresholds:")
    for state in ("open", "closing", "closed"):
        samples = state_samples[state]
        if samples:
            avg = average_rgb(samples)
            thresholds[state] = list(avg)
            print(f"  {state}: {avg}")
        else:
            thresholds[state] = None
            print(f"  {state}: no samples — run again with {state} zones visible")

    # Save results
    log_entry = {
        "timestamp": stamp,
        "screenshot": str(full_path),
        "zone_samples": {k: [list(c) for c in v] for k, v in zone_samples.items()},
        "suggested_thresholds": thresholds,
    }
    log_path = Path("calibration_log_zone_colors.json")
    existing = []
    if log_path.exists():
        try:
            existing = json.loads(log_path.read_text())
        except Exception:
            pass
    existing.append(log_entry)
    log_path.write_text(json.dumps(existing, indent=2))
    print(f"\nLog saved: {log_path}")

    # Offer to update config
    print("\nTo update config.json with these thresholds, paste this into the zone_color_thresholds section:")
    print(json.dumps({"zone_color_thresholds": thresholds}, indent=4))

    update = input("\nUpdate config.json now? [y/N]: ").strip().lower()
    if update == "y":
        for state, val in thresholds.items():
            if val is not None:
                config["zone_color_thresholds"][state] = val
        CONFIG_PATH.write_text(json.dumps(config, indent=4))
        print("config.json updated.")
    else:
        print("config.json not changed — update manually from the snippet above.")


if __name__ == "__main__":
    main()
