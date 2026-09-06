"""
Headless test bootstrap.

The bot's game/ and bot/ packages import Windows/screen-only libraries at module
level (pyautogui, cv2, pywin32, ...). None of the code under test here touches a
screen, so those modules are replaced with MagicMock stand-ins BEFORE anything
under game/ is imported. Tests must never import bot/discord_bot.py (needs a
discord.py install and a token) — the cog only wires DraftLifecycle in.
"""
import os
import sys
from unittest.mock import MagicMock

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

_STUBBED = [
    "pyautogui", "cv2", "pytesseract", "pyperclip", "psutil",
    "win32api", "win32gui", "win32con", "win32process", "pywintypes", "win32com", "win32com.client",
    "sounddevice", "soundfile", "edge_tts", "obsws_python", "twitchio", "PIL", "PIL.Image",
    "numpy",
]
for _name in _STUBBED:
    try:
        __import__(_name)
    except Exception:
        sys.modules[_name] = MagicMock()
