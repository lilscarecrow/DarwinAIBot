import logging
import subprocess
import time
import psutil

logger = logging.getLogger(__name__)

GAME_PROCESS_NAME = "DarwinProject.exe"


def launch_game(exe_path: str, timeout: int = 60) -> bool:
    """
    Start the game process and wait for it to appear.
    Returns True if the process appeared within timeout.
    """
    logger.info("Launching game: %s", exe_path)
    try:
        subprocess.Popen([exe_path])
    except FileNotFoundError:
        logger.error("Executable not found: %s", exe_path)
        return False
    except OSError as e:
        logger.error("Failed to launch game: %s", e)
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
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == GAME_PROCESS_NAME:
            return True
    return False


def close_game():
    for proc in psutil.process_iter(["name"]):
        if proc.info["name"] == GAME_PROCESS_NAME:
            logger.info("Terminating game process (pid %d)", proc.pid)
            proc.terminate()
            return
    logger.warning("close_game called but game process not found")


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
