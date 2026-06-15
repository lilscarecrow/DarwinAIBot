import win32gui, win32con, win32api, time, sys

sys.stdout.reconfigure(encoding='utf-8', errors='replace')

def find_window_containing(title_fragment):
    results = []
    def cb(hwnd, _):
        try:
            t = win32gui.GetWindowText(hwnd)
            if title_fragment.lower() in t.lower():
                results.append((hwnd, t))
        except Exception:
            pass
    win32gui.EnumWindows(cb, None)
    return results

windows = find_window_containing("darwin")
print("Found windows:", [(h, t) for h, t in windows])

if not windows:
    print("Darwin window not found — is the game running?")
else:
    hwnd, title = windows[0]
    print(f"Using hwnd={hwnd}")

    VK_SHIFT = 0x10
    SCAN_SHIFT = 0x2A

    print("Sending WM_KEYDOWN SHIFT to game window for 3 seconds...")
    win32api.PostMessage(hwnd, win32con.WM_KEYDOWN, VK_SHIFT, (SCAN_SHIFT << 16) | 1)
    time.sleep(3)
    win32api.PostMessage(hwnd, win32con.WM_KEYUP, VK_SHIFT, (SCAN_SHIFT << 16) | 0xC0000001)
    print("Done. Did the card tray appear?")
