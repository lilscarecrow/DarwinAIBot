"""
Run this when the match results screen is visible.
Saves a full native screenshot for badge template extraction.
"""
import pyautogui
import cv2
import numpy as np

img = pyautogui.screenshot()
bgr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
cv2.imwrite("placement_screenshot.png", bgr)
print("Saved placement_screenshot.png (1920x1080 native)")
