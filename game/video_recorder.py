import datetime
import logging
import threading
import time
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

_FPS = 4
_FOURCC = cv2.VideoWriter_fourcc(*"avc1")
_FRAME_INTERVAL = 1.0 / _FPS
_OUTPUT_DIR = Path("screenshots/recordings")

# Crop region [x, y, w, h] at 1920×1080 — tight center band on the kill feed area.
# Moved out of config.json (2026-09-07), same rationale as the other 1920×1080
# calibration constants in game/match_runner.py.
_CROP_REGION = (755, 175, 410, 200)


class VideoRecorder:
    """
    Records a cropped region (_CROP_REGION) of match footage in a background thread.

    Usage:
        recorder = VideoRecorder(config)
        recorder.start()          # begins capturing
        path = recorder.stop()    # finalizes file, returns path or None on failure
    """

    def __init__(self, config: dict):
        self._config = config
        self._writer: cv2.VideoWriter | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._output_path: str | None = None
        self._crop: tuple[int, int, int, int] = _CROP_REGION

    def _apply_crop(self, frame):
        x, y, w, h = self._crop
        return frame[y:y + h, x:x + w]

    def start(self):
        from game.screen_detection import take_screenshot
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._output_path = str(_OUTPUT_DIR / f"match_{ts}.mp4")
        self._stop.clear()

        # Grab one frame to determine output dimensions after crop
        first = self._apply_crop(take_screenshot())
        h, w = first.shape[:2]

        self._writer = cv2.VideoWriter(self._output_path, _FOURCC, _FPS, (w, h))
        if not self._writer.isOpened():
            logger.error("VideoRecorder: could not open writer at %s — recording disabled", self._output_path)
            self._writer = None
            return

        logger.info("VideoRecorder: started → %s (%dx%d @ %.1ffps, crop=%s)", self._output_path, w, h, _FPS, self._crop)
        self._thread = threading.Thread(target=self._capture_loop, daemon=True, name="VideoRecorder")
        self._thread.start()

    def stop(self) -> str | None:
        """Stop recording and finalize the file. Returns the output path, or None if recording failed."""
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        if self._writer is not None:
            self._writer.release()
            self._writer = None
            logger.info("VideoRecorder: finalized %s", self._output_path)
            return self._output_path
        return None

    def _capture_loop(self):
        from game.screen_detection import take_screenshot
        while not self._stop.is_set():
            frame_start = time.monotonic()
            try:
                frame = self._apply_crop(take_screenshot())
                if self._writer is not None:
                    self._writer.write(frame)
            except Exception as e:
                logger.warning("VideoRecorder: frame capture error: %s", e)
            elapsed = time.monotonic() - frame_start
            self._stop.wait(max(0.0, _FRAME_INTERVAL - elapsed))
