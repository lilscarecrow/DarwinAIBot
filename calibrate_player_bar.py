"""
calibrate_player_bar.py — check the player-bar detector against a saved screenshot.

Runs the bot's OWN card detection, alive/dead sampling and name OCR (the V2
geometry detector, game/player_cards_v2.py — no calibration needed) on a
native-resolution frame and shows what it sees. Works on any machine (no
game, no display needed). See docs/PLAYER_BAR_CALIBRATION.md.

    python calibrate_player_bar.py frame.png
    python calibrate_player_bar.py frame.png --expected 8
    python calibrate_player_bar.py frame.png --no-ocr --out annotated.png

Writes <frame>.annotated.png (each detected card boxed, alive/dead, name).
"""
import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game.player_bar_calibration import analyze_v2, annotate_v2, format_report_v2  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frame", help="screenshot PNG at the game's native resolution (1920x1080)")
    ap.add_argument("--config", default="config.json", help="config.json to take overrides from (default: ./config.json)")
    ap.add_argument("--no-ocr", action="store_true", help="skip name OCR")
    ap.add_argument("--out", help="annotated image path (default: <frame>.annotated.png)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    ap.add_argument("--expected", type=int, help="the lobby's player count, if known — only flags a mismatch in the report, doesn't change which count is picked (see game/player_cards_v2.py::detect_cards)")
    a = ap.parse_args()

    img = cv2.imread(a.frame)
    if img is None:
        print(f"could not read {a.frame}", file=sys.stderr)
        return 2
    config = {}
    if Path(a.config).exists():
        try:
            config = json.loads(Path(a.config).read_text(encoding="utf-8"))
        except Exception as e:
            print(f"warning: could not parse {a.config}: {e}", file=sys.stderr)

    rep = analyze_v2(img, config, expected=a.expected, ocr=not a.no_ocr)
    out = a.out or (str(Path(a.frame).with_suffix("")) + ".annotated.png")
    cv2.imwrite(out, annotate_v2(img, rep))

    if a.json:
        print(json.dumps({
            "cards": [{"index": c.index, "x": c.x, "alive": c.alive} for c in rep.cards],
            "names": rep.names, "ocr_available": rep.ocr_available,
            "problems": rep.problems, "annotated": out,
        }, indent=2))
    else:
        print(format_report_v2(rep))
        print(f"annotated image -> {out}")
    return 0 if not rep.problems else 1


if __name__ == "__main__":
    sys.exit(main())
