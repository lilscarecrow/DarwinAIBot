import logging
import os
import time
import psutil

logger = logging.getLogger(__name__)

GAME_PROCESS_NAME = "Darwin-Win64-Shipping.exe"

# Darwin Project's Steam App ID (fixed — not machine-specific, see appmanifest_544920.acf).
STEAM_APP_ID = "544920"

# Unreal's own crash handler process, which launches as a separate GUI process
# ("Darwin Crash Reporter") when Darwin-Win64-Shipping.exe crashes — see
# close_crash_reporter(). Confirmed live 2026-09-28: ships under the game's own
# Engine folder (Engine\Binaries\Win64\CrashReportClient.exe), not
# machine-specific.
CRASH_REPORTER_PROCESS_NAME = "CrashReportClient.exe"


def launch_game(exe_path: str, timeout: int = 60) -> bool:
    """
    Start the game via the Steam client and wait for the process to appear.
    Returns True if the process appeared within timeout.

    exe_path is only used as a sanity check that the game is installed where
    config expects (see main.py startup validation) — the actual launch goes
    through steam://rungameid so the Steamworks/EAC bootstrap in Darwin.exe
    initializes the same way it does for a launch from the Steam client.
    Launching Darwin.exe directly via subprocess used to work, but as of the
    UE5 update it now exits immediately when not spawned by Steam.
    """
    if is_game_running():
        logger.info("Game already running — skipping launch")
        return True

    if not os.path.exists(exe_path):
        logger.error("Executable not found: %s", exe_path)
        return False

    logger.info("Launching game via Steam (appid %s)", STEAM_APP_ID)
    try:
        os.startfile(f"steam://rungameid/{STEAM_APP_ID}")
    except OSError as e:
        logger.error("Failed to launch game via Steam: %s", e)
        return False

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if is_game_running():
            logger.info("Game process detected")
            return True
        time.sleep(2)

    logger.error("Game process did not appear within %ds", timeout)
    return False


def is_game_running() -> bool:
    target = GAME_PROCESS_NAME.lower()
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() == target:
            return True
    return False


def close_game():
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == GAME_PROCESS_NAME:
            logger.info("Terminating game process (pid %d)", proc.pid)
            proc.terminate()
            return
    logger.warning("close_game called but game process not found")


def is_crash_reporter_open() -> bool:
    target = CRASH_REPORTER_PROCESS_NAME.lower()
    for proc in psutil.process_iter(["name"]):
        if (proc.info["name"] or "").lower() == target:
            return True
    return False


def close_crash_reporter():
    """Terminate the crash reporter process, mirroring close_game()'s pattern.

    Killing this process is enough to dismiss the "Darwin Crash Reporter"
    dialog it owns — no click/UI-automation needed, since this closes the
    whole window along with the process behind it (same effect "Close
    Without Sending" would have had, minus actually sending the report).
    """
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == CRASH_REPORTER_PROCESS_NAME:
            logger.info("Terminating crash reporter process (pid %d)", proc.pid)
            proc.terminate()
            return
    logger.warning("close_crash_reporter called but crash reporter process not found")


def monitor_game_process(on_unexpected_exit):
    """
    Blocking loop that watches the game process.
    Calls on_unexpected_exit() if the process disappears unexpectedly.
    Intended to run in a background thread.
    """
    while True:
        if not is_game_running():
            logger.error("Game process disappeared unexpectedly")
            on_unexpected_exit()
            return
        time.sleep(5)
