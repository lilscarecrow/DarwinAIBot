"""
Takes a full screenshot and saves it to screenshots/debug_current.png
Run while the game screen you want to capture is visible.
"""
import pyautogui
import cv2
import numpy as np
from pathlib import Path

pyautogui.FAILSAFE = False
Path("screenshots").mkdir(exist_ok=True)

img = pyautogui.screenshot()
bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
cv2.imwrite("screenshots/debug_current.png", bgr)
print("Saved to screenshots/debug_current.png")
