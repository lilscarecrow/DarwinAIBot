import datetime
import logging
import threading
import time
from pathlib import Path

import cv2

logger = logging.getLogger(__name__)

_FPS = 0.5
_FOURCC = cv2.VideoWriter_fourcc(*"avc1")
_FRAME_INTERVAL = 2.0
_OUTPUT_DIR = Path("screenshots/recordings")


class VideoRecorder:
    """
    Records a cropped region of match footage in a background thread.

    Crop region is read from config key recording_crop_region: [x, y, w, h].
    If not set, the full frame is recorded.

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
        self._crop: tuple[int, int, int, int] | None = None

    def _apply_crop(self, frame):
        if self._crop is None:
            return frame
        x, y, w, h = self._crop
        return frame[y:y + h, x:x + w]

    def start(self):
        from game.screen_detection import take_screenshot
        _OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        self._output_path = str(_OUTPUT_DIR / f"match_{ts}.mp4")
        self._stop.clear()

        crop_cfg = self._config.get("recording_crop_region")
        self._crop = tuple(crop_cfg) if crop_cfg else None

        # Grab one frame to determine output dimensions after crop
        first = self._apply_crop(take_screenshot())
        h, w = first.shape[:2]

        self._writer = cv2.VideoWriter(self._output_path, _FOURCC, _FPS, (w, h))
        if not self._writer.isOpened():
            logger.error("VideoRecorder: could not open writer at %s — recording disabled", self._output_path)
            self._writer = None
            return

        crop_info = f"crop={self._crop}" if self._crop else "full frame"
        logger.info("VideoRecorder: started → %s (%dx%d @ %.1ffps, %s)", self._output_path, w, h, _FPS, crop_info)
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
