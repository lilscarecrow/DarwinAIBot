"""
Live match test: drag slot 7 of 8 remaining cards.
Visual index 6: x = 966 - (7/2)*76 + 6*76 = 1156, y = 943
Drop target: screen center (960, 540)
"""
import sys, time
import pyautogui
import win32api, win32con, win32gui

pyautogui.FAILSAFE = False
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

_VK_SHIFT    = 0x10
_SCAN_SHIFT  = 0x2A
_LPARAM_DOWN = (_SCAN_SHIFT << 16) | 1
_LPARAM_UP   = (_SCAN_SHIFT << 16) | 0xC0000001

SLOT_X   = 1156
SLOT_Y   = 943
TARGET_X = 960
TARGET_Y = 540

def find_darwin():
    results = []
    def cb(hwnd, _):
        try:
            if win32gui.GetWindowText(hwnd).strip() == "Darwin":
                results.append(hwnd)
        except Exception:
            pass
    win32gui.EnumWindows(cb, None)
    return results[0] if results else None

hwnd = find_darwin()
if not hwnd:
    print("Darwin window not found.")
    sys.exit(1)

win32gui.SetForegroundWindow(hwnd)
time.sleep(0.5)

print(f"SHIFT down → move to ({SLOT_X}, {SLOT_Y}) → drag to ({TARGET_X}, {TARGET_Y})...")
pyautogui.keyDown("shift")
time.sleep(0.05)
win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, _VK_SHIFT, _LPARAM_DOWN)
time.sleep(0.15)

pyautogui.moveTo(SLOT_X, SLOT_Y, duration=0.2)
time.sleep(0.1)
pyautogui.dragTo(TARGET_X, TARGET_Y, duration=0.5, button="left")
time.sleep(0.3)

win32api.PostMessage(hwnd, win32con.WM_KEYUP, _VK_SHIFT, _LPARAM_UP)
pyautogui.keyUp("shift")
print("Done.")
