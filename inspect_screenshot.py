"""
inspect_screenshot.py — Interactive screenshot inspector for calibration.

Usage:
    python inspect_screenshot.py <path_to_screenshot.png>

Opens the screenshot in a resizable window.
Mouse interaction:
  - Hover: shows (x, y) and RGB at cursor in the title bar
  - Left-click: prints and logs the coordinate + RGB
  - Right-click-drag: draws a rectangle and prints (x,y,w,h) for OCR regions
  - Middle-click: copies current coordinate to clipboard

Press:
  S — save annotated screenshot with all clicked points marked
  C — clear all marked points
  Q / Esc — quit
"""

import sys
import json
import cv2
import numpy as np
import pyperclip
from pathlib import Path


def main():
    if len(sys.argv) < 2:
        # Default to the most recent results_screen screenshot
        candidates = sorted(Path("calibration_screenshots").glob("results_screen_*.png"), reverse=True)
        if not candidates:
            print("Usage: python inspect_screenshot.py <screenshot.png>")
            sys.exit(1)
        img_path = str(candidates[0])
        print(f"No path given — using most recent results screen: {img_path}")
    else:
        img_path = sys.argv[1]

    src = cv2.imread(img_path)
    if src is None:
        print(f"Could not read: {img_path}")
        sys.exit(1)

    overlay = src.copy()
    points: list[dict] = []
    regions: list[dict] = []

    drag_start = None
    drag_rect = None  # (x, y, w, h) of current drag in progress

    def _update_title(x: int, y: int):
        if 0 <= y < src.shape[0] and 0 <= x < src.shape[1]:
            bgr = src[y, x]
            rgb = (int(bgr[2]), int(bgr[1]), int(bgr[0]))
            cv2.setWindowTitle(win, f"Inspector  ({x}, {y})  RGB={rgb}  | L-click=log  R-drag=region  M=clip  S=save  Q=quit")

    def _mouse_cb(event, x, y, flags, _param):
        nonlocal drag_start, drag_rect, overlay

        _update_title(x, y)

        if event == cv2.EVENT_LBUTTONDOWN:
            bgr = src[y, x]
            rgb = (int(bgr[2]), int(bgr[1]), int(bgr[0]))
            entry = {"x": x, "y": y, "rgb": list(rgb)}
            points.append(entry)
            print(f"  Point [{len(points)}]  ({x}, {y})  RGB={rgb}")
            # Draw dot
            overlay = src.copy()
            _redraw()

        elif event == cv2.EVENT_MBUTTONDOWN:
            bgr = src[y, x]
            rgb = (int(bgr[2]), int(bgr[1]), int(bgr[0]))
            clip = f"[{x}, {y}]"
            try:
                pyperclip.copy(clip)
                print(f"  Copied to clipboard: {clip}  RGB={rgb}")
            except Exception:
                print(f"  ({x}, {y})  RGB={rgb}  (clipboard unavailable)")

        elif event == cv2.EVENT_RBUTTONDOWN:
            drag_start = (x, y)

        elif event == cv2.EVENT_MOUSEMOVE and drag_start and (flags & cv2.EVENT_FLAG_RBUTTON):
            x0, y0 = drag_start
            drag_rect = (min(x0, x), min(y0, y), abs(x - x0), abs(y - y0))
            overlay = src.copy()
            _redraw()
            cv2.rectangle(overlay, (drag_rect[0], drag_rect[1]),
                          (drag_rect[0] + drag_rect[2], drag_rect[1] + drag_rect[3]),
                          (0, 255, 0), 1)
            cv2.imshow(win, overlay)

        elif event == cv2.EVENT_RBUTTONUP and drag_start:
            x0, y0 = drag_start
            rx = min(x0, x)
            ry = min(y0, y)
            rw = abs(x - x0)
            rh = abs(y - y0)
            drag_start = None
            drag_rect = None
            if rw > 5 and rh > 5:
                label = input(f"\n  Region ({rx},{ry},{rw},{rh}) — label (e.g. 'placement_col' / 'player_name_row0'): ").strip()
                entry = {"label": label, "x": rx, "y": ry, "w": rw, "h": rh}
                regions.append(entry)
                print(f"  Saved region: {entry}")
            overlay = src.copy()
            _redraw()

    def _redraw():
        nonlocal overlay
        for i, p in enumerate(points):
            cv2.circle(overlay, (p["x"], p["y"]), 4, (0, 0, 255), -1)
            cv2.putText(overlay, str(i + 1), (p["x"] + 5, p["y"] - 5),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.4, (0, 0, 255), 1)
        for r in regions:
            cv2.rectangle(overlay, (r["x"], r["y"]), (r["x"] + r["w"], r["y"] + r["h"]),
                          (0, 200, 100), 1)
            cv2.putText(overlay, r["label"][:20], (r["x"] + 2, r["y"] - 4),
                        cv2.FONT_HERSHEY_SIMPLEX, 0.35, (0, 200, 100), 1)
        cv2.imshow(win, overlay)

    win = "Inspector"
    cv2.namedWindow(win, cv2.WINDOW_NORMAL)
    cv2.resizeWindow(win, min(src.shape[1], 1600), min(src.shape[0], 900))
    cv2.setMouseCallback(win, _mouse_cb)
    cv2.imshow(win, overlay)

    print(f"\nOpened: {img_path}  ({src.shape[1]}×{src.shape[0]})")
    print("L-click = log point | R-drag = define region | M-click = copy [x,y] | S = save | Q = quit\n")

    while True:
        key = cv2.waitKey(20) & 0xFF
        if key in (ord('q'), ord('Q'), 27):
            break
        elif key in (ord('s'), ord('S')):
            out = Path(img_path).stem + "_annotated.png"
            cv2.imwrite(out, overlay)
            print(f"  Annotated screenshot saved → {out}")
        elif key in (ord('c'), ord('C')):
            points.clear()
            regions.clear()
            overlay = src.copy()
            cv2.imshow(win, overlay)
            print("  Cleared all points.")

    cv2.destroyAllWindows()

    if points or regions:
        result = {"source": img_path, "points": points, "regions": regions}
        out_json = Path(img_path).stem + "_inspection.json"
        Path(out_json).write_text(json.dumps(result, indent=2))
        print(f"\nInspection data saved → {out_json}")

        # Pretty-print regions as config-ready format
        if regions:
            print("\n--- Regions (config-ready) ---")
            grouped: dict[str, list] = {}
            for r in regions:
                grouped.setdefault(r["label"], []).append([r["x"], r["y"], r["w"], r["h"]])
            print(json.dumps(grouped, indent=4))


if __name__ == "__main__":
    main()
