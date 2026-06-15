"""
Focuses the Darwin Project window, holds SHIFT to show the card tray,
takes a screenshot, then releases. Saves to calibration_screenshots/.
"""
import ctypes
import time
import cv2
import numpy as np
import pyautogui
from pathlib import Path

pyautogui.FAILSAFE = False
user32 = ctypes.windll.user32

# Find Darwin window
found = [None]

def _cb(hwnd, _):
    if user32.IsWindowVisible(hwnd):
        n = user32.GetWindowTextLengthW(hwnd)
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if "darwin" in buf.value.lower():
            found[0] = hwnd
            print(f"Found: '{buf.value}'  hwnd={hwnd}")
    return True

WNDENUMPROC = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_size_t, ctypes.c_size_t)
user32.EnumWindows(WNDENUMPROC(_cb), 0)

out_dir = Path(r"C:\Users\amand\OneDrive\Documents\Repos\DarwinAIBot\calibration_screenshots")
out_dir.mkdir(exist_ok=True)

if found[0]:
    user32.ShowWindow(found[0], 9)
    user32.SetForegroundWindow(found[0])
    time.sleep(1.0)   # let focus settle fully
    print("Game focused")
else:
    print("Darwin window not found — continuing anyway")
    time.sleep(1.0)

# Hold SHIFT → card tray appears → screenshot → release
print("Holding SHIFT…")
pyautogui.keyDown("shift")
time.sleep(0.8)

img = pyautogui.screenshot()
arr = cv2.cvtColor(np.array(img), cv2.COLOR_RGB2BGR)
path = out_dir / "shift_card_tray.png"
cv2.imwrite(str(path), arr)
print(f"Saved: {path}")

pyautogui.keyUp("shift")
print("Done")
