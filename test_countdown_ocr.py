"""
Quick test for the lobby countdown OCR. Run while in the Darwin lobby.
Saves debug images to calibration_screenshots/ so you can see exactly
what the OCR is working with.
"""
import cv2
import numpy as np
import pyautogui
from game.ocr import read_lobby_countdown

print("Taking screenshot...")
img = pyautogui.screenshot()
screenshot = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)

result = read_lobby_countdown(screenshot, debug=True)

if result is not None:
    m, s = divmod(result, 60)
    print(f"Countdown parsed: {m}:{s:02d} ({result}s)")
else:
    print("OCR failed — check calibration_screenshots/countdown_*.png for the debug images")
