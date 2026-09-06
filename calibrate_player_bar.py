"""
calibrate_player_bar.py — check / tune the player-bar keys from a saved screenshot.

Runs the bot's OWN slot detection, alive/dead sampling and name OCR on a
native-resolution frame and shows what each stage sees. Works on any machine
(no game, no display needed). See docs/PLAYER_BAR_CALIBRATION.md.

    python calibrate_player_bar.py frame.png                       # with config.json's keys
    python calibrate_player_bar.py frame.png --bar 400 20 1520 120 # try a region
    python calibrate_player_bar.py frame.png --sweep               # separator threshold sweep
    python calibrate_player_bar.py frame.png --sat 30 --portrait-y 40 --out annotated.png

Writes <frame>.annotated.png (bar, separators, portrait samples, name strips)
and prints a config.json snippet with the values that were used.
"""
import argparse
import json
import sys
from pathlib import Path

import cv2

sys.path.insert(0, str(Path(__file__).resolve().parent))
from game.player_bar_calibration import analyze, annotate, config_snippet, format_report, sweep  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("frame", help="screenshot PNG at the game's native resolution (1920x1080)")
    ap.add_argument("--config", default="config.json", help="config.json to take the keys from (default: ./config.json)")
    ap.add_argument("--bar", nargs=4, type=int, metavar=("X0", "Y0", "X1", "Y1"), help="override player_bar_region")
    ap.add_argument("--sep", type=int, help="override player_separator_threshold")
    ap.add_argument("--sat", type=int, help="override player_saturation_threshold")
    ap.add_argument("--portrait-y", type=int, help="override player_portrait_y_in_bar")
    ap.add_argument("--name-y", type=int, help="override player_name_y_in_bar")
    ap.add_argument("--name-h", type=int, help="override player_name_h")
    ap.add_argument("--no-ocr", action="store_true", help="skip name OCR")
    ap.add_argument("--sweep", action="store_true", help="also try separator thresholds 10-85")
    ap.add_argument("--out", help="annotated image path (default: <frame>.annotated.png)")
    ap.add_argument("--json", action="store_true", help="print the report as JSON instead of text")
    ap.add_argument("--detector", choices=["v2", "v1", "both"], default="both",
                    help="v2 = geometry detector (recommended), v1 = separator scan, both (default)")
    ap.add_argument("--expected", type=int, help="v2: the lobby's player count, if known")
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
    overrides = {
        "player_bar_region": a.bar,
        "player_separator_threshold": a.sep,
        "player_saturation_threshold": a.sat,
        "player_portrait_y_in_bar": a.portrait_y,
        "player_name_y_in_bar": a.name_y,
        "player_name_h": a.name_h,
    }
    from game.player_bar_calibration import analyze_v2, annotate_v2, format_report_v2

    rep2 = analyze_v2(img, config, expected=a.expected, ocr=not a.no_ocr) if a.detector in ("v2", "both") else None
    rep = analyze(img, config, overrides, ocr=not a.no_ocr) if a.detector in ("v1", "both") else None
    rows = sweep(img, rep.config) if (rep is not None and a.sweep and rep.bar) else None
    out = a.out or (str(Path(a.frame).with_suffix("")) + ".annotated.png")
    ann = img
    if rep is not None:
        ann = annotate(ann, rep)
    if rep2 is not None:
        ann = annotate_v2(ann, rep2)
    cv2.imwrite(out, ann)
    if rep2 is not None and not a.json:
        print(format_report_v2(rep2))
        print("")
    if rep is None:
        print(f"annotated image -> {out}")
        return 0 if not rep2.problems else 1
    if a.json:
        print(json.dumps({
            "report": {"bar": rep.bar, "separators": rep.separators,
                       "slots": [s.__dict__ for s in rep.slots], "ocr_available": rep.ocr_available,
                       "problems": rep.problems},
            "sweep": rows, "snippet": config_snippet(rep), "annotated": out,
        }, indent=2))
    else:
        print(format_report(rep, rows))
        print(f"annotated image -> {out}")
        print("config.json snippet:")
        print(json.dumps(config_snippet(rep), indent=2))
    return 0 if not rep.problems and not (rep2 is not None and rep2.problems) else 1


if __name__ == "__main__":
    sys.exit(main())
