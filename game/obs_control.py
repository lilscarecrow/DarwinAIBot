import logging
import threading

logger = logging.getLogger(__name__)

# Serializes start/stop calls — /custom, /quit, the idle-close watcher, and match-end
# reset paths all run on different threads/executors and could otherwise open
# overlapping short-lived connections to the same OBS instance.
_lock = threading.Lock()

_enabled: bool = False
_host: str = "localhost"
_port: int = 4455
_password: str = ""
_connect_timeout: float = 5.0


def configure(enabled: bool, host: str = "localhost", port: int = 4455, password: str = "") -> None:
    """Call once at startup."""
    global _enabled, _host, _port, _password
    _enabled = bool(enabled)
    _host = host
    _port = port
    _password = password
    if _enabled:
        logger.info("OBS streaming configured — host: %s  port: %d", host, port)
    else:
        logger.info("OBS streaming disabled (obs_stream_enabled is false)")


def is_enabled() -> bool:
    return _enabled


def _connect():
    import obsws_python as obs
    return obs.ReqClient(host=_host, port=_port, password=_password, timeout=_connect_timeout)


def start_stream() -> bool:
    """
    Best-effort: start the Twitch stream in OBS (assumes OBS is already running with
    its Stream settings pointed at Twitch, and obs-websocket enabled under
    Tools > WebSocket Server Settings). No-ops if obs_stream_enabled is false.

    Returns True on success (including "already streaming"). Never raises — a missing
    OBS/websocket connection is logged as a warning and swallowed, same as the other
    fire-and-forget integrations in this codebase (ds_ingest, noble-hopper sync).
    """
    if not _enabled:
        return False
    with _lock:
        try:
            client = _connect()
            try:
                if client.get_stream_status().output_active:
                    logger.info("OBS: stream already active — leaving it running")
                    return True
                client.start_stream()
                logger.info("OBS: stream started")
                return True
            finally:
                client.disconnect()
        except Exception as e:
            logger.warning("OBS: could not start stream: %s", e)
            return False


def stop_stream() -> bool:
    """
    Best-effort: stop the Twitch stream in OBS. No-ops if obs_stream_enabled is false.

    Returns True on success (including "already stopped"). Never raises — see
    start_stream() for the swallow-and-log rationale.
    """
    if not _enabled:
        return False
    with _lock:
        try:
            client = _connect()
            try:
                if not client.get_stream_status().output_active:
                    return True
                client.stop_stream()
                logger.info("OBS: stream stopped")
                return True
            finally:
                client.disconnect()
        except Exception as e:
            logger.warning("OBS: could not stop stream: %s", e)
            return False


def set_source_visible(source_name: str, visible: bool) -> bool:
    """
    Best-effort: show/hide a source by name, in whichever scene is currently the
    active program scene (looked up fresh each call rather than a hardcoded scene
    name, so this doesn't break if the scene is ever renamed or a new one added).
    No-ops if obs_stream_enabled is false.

    Returns True on success. Never raises — see start_stream() for the rationale.
    """
    if not _enabled:
        return False
    with _lock:
        try:
            client = _connect()
            try:
                scene_name = client.get_current_program_scene().scene_name
                item_id = client.get_scene_item_id(scene_name, source_name).scene_item_id
                client.set_scene_item_enabled(scene_name, item_id, visible)
                logger.info("OBS: set '%s' visible=%s in scene '%s'", source_name, visible, scene_name)
                return True
            finally:
                client.disconnect()
        except Exception as e:
            logger.warning("OBS: could not set '%s' visibility: %s", source_name, e)
            return False
