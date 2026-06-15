import asyncio
import io
import logging
import queue
import threading
import time
from typing import Optional

logger = logging.getLogger(__name__)

# In-game voice broadcast: G opens a 15s window, then 90s cooldown.
_BROADCAST_WINDOW   = 15.0
_BROADCAST_COOLDOWN = 90.0
_BROADCAST_CYCLE    = _BROADCAST_WINDOW + _BROADCAST_COOLDOWN

_CARD_ANNOUNCE: dict[str, str] = {
    "electromania":     "Electromania",
    "beach_party":      "Beach Party",
    "blood_moon":       "Blood Moon",
    "zone_close":       "Zone Close",
    "open_zone":        "Open Zone",
    "lava_zone":        "Lava Zone",
    "nuclear_blast":    "Nuclear Blast",
    "anti_grav_storm":  "Anti-Gravity Storm",
    "man_hunt":         "Man Hunt",
    "spawn_electronic": "Pylon",
    "telepathy":        "Telepathy",
    "expose":           "Expose",
    "warm_up":          "Warm up",
    "speed_boost":      "Speed boost",
    "give_wood":        "Give wood",
    "give_leather":     "Give leather",
    "favorite_player":  "Favorite player",
}

_device_name: Optional[str] = None
_voice: str = "en-US-ChristopherNeural"
_bypass: bool = False

_queue: queue.Queue = queue.Queue()
_worker_thread: Optional[threading.Thread] = None
_worker_lock = threading.Lock()

_broadcast_available_at: float = 0.0

# Discord voice — set by the bot on connect/disconnect
_voice_client = None   # discord.VoiceClient
_event_loop   = None   # asyncio event loop running the Discord bot

# Pre-generated audio cache: text -> (data, samplerate)
# Populated in a background thread before cards fire so all audio is cache-ready at card time.
_audio_cache: dict[str, tuple] = {}


def configure(
    device_name: Optional[str],
    voice: str = "en-US-ChristopherNeural",
    bypass: bool = False,
) -> None:
    """Call once at startup."""
    global _device_name, _voice, _bypass
    _device_name = device_name
    _voice = voice
    _bypass = bypass
    if device_name:
        _ensure_worker()
        logger.info("TTS configured — device: %r  voice: %s  bypass: %s", device_name, voice, bypass)
    else:
        logger.info("TTS disabled (no tts_device in config)")


def set_event_loop(loop) -> None:
    """Store the Discord bot's event loop so TTS threads can schedule voice playback on it."""
    global _event_loop
    _event_loop = loop


def set_voice_client(vc) -> None:
    """Set (or clear) the Discord VoiceClient for concurrent voice-channel playback."""
    global _voice_client
    _voice_client = vc
    if vc is not None:
        logger.info("TTS: Discord voice client connected — audio will mirror to voice channel")
    else:
        logger.info("TTS: Discord voice client cleared")


def is_enabled() -> bool:
    return _device_name is not None


def card_announce(card_type: str) -> str:
    return _CARD_ANNOUNCE.get(card_type, card_type.replace("_", " ").title())


def try_open_broadcast() -> bool:
    """Check cooldown and press G if available. Returns True if broadcast window opened."""
    global _broadcast_available_at
    if not _device_name:
        return False
    now = time.monotonic()
    if now < _broadcast_available_at:
        logger.info("TTS: broadcast on cooldown (%.0fs remaining)", _broadcast_available_at - now)
        return False
    _broadcast_available_at = now + _BROADCAST_CYCLE
    if not _bypass:
        from game.card_actions import press_key
        press_key("g")
        logger.info("TTS: broadcast window opened (G pressed)")
    else:
        logger.info("[BYPASS] TTS: would press G to open broadcast window")
    return True


def close_broadcast() -> None:
    """Press G to close the broadcast window early and start the 90s cooldown from now."""
    global _broadcast_available_at
    if not _device_name:
        return
    if not _bypass:
        from game.card_actions import press_key
        press_key("g")
        logger.info("TTS: broadcast window closed (G pressed)")
    else:
        logger.info("[BYPASS] TTS: would press G to close broadcast window")
    _broadcast_available_at = time.monotonic() + _BROADCAST_COOLDOWN


def speak_sync(text: str) -> None:
    """Synchronous TTS to CABLE Input. No G press. Blocks until audio finishes."""
    if not _device_name:
        return
    try:
        asyncio.run(_speak_on_cable(text))
    except Exception as e:
        logger.warning("TTS speak_sync failed for %r: %s", text, e)


def speak(text: str, broadcast: bool = True) -> None:
    """Queue a phrase for async playback.

    broadcast=True  — cooldown gate + G press + CABLE Input (in-match voice).
    broadcast=False — CABLE Input only, no G press (lobby / status announcements).
    """
    if not _device_name:
        return
    _ensure_worker()
    _queue.put((text, "broadcast" if broadcast else "cable"))


def speak_cable(text: str) -> None:
    """Queue audio to CABLE Input without a G press or cooldown."""
    if not _device_name:
        return
    _ensure_worker()
    _queue.put((text, "cable"))


def queue_close_broadcast() -> None:
    """Queue a broadcast-close sentinel that runs after all pending audio in the worker.

    Ensures G is not pressed until every previously queued phrase finishes playing,
    keeping the broadcast window open for the full announcement sequence.
    """
    if not _device_name:
        return
    _ensure_worker()
    _queue.put((None, "close_broadcast"))


def _ensure_worker() -> None:
    global _worker_thread
    with _worker_lock:
        if _worker_thread is None or not _worker_thread.is_alive():
            _worker_thread = threading.Thread(target=_worker_loop, daemon=True, name="tts-worker")
            _worker_thread.start()


def _worker_loop() -> None:
    global _broadcast_available_at
    while True:
        item = _queue.get()
        if item is None:
            break
        text, mode = item
        if mode == "close_broadcast":
            close_broadcast()
        elif mode == "broadcast":
            now = time.monotonic()
            if now < _broadcast_available_at:
                logger.info(
                    "TTS: broadcast on cooldown (%.0fs remaining) — skipping: %r",
                    _broadcast_available_at - now, text,
                )
                continue
            _broadcast_available_at = now + _BROADCAST_CYCLE
            try:
                asyncio.run(_speak_broadcast(text))
            except Exception as e:
                logger.warning("TTS broadcast failed for %r: %s", text, e)
        else:
            try:
                asyncio.run(_speak_on_cable(text))
            except Exception as e:
                logger.warning("TTS cable failed for %r: %s", text, e)


# ------------------------------------------------------------------
# Audio playback
# ------------------------------------------------------------------

async def _speak_broadcast(text: str) -> None:
    """Generate audio, press G, play to CABLE Input (+ Discord voice)."""
    data, samplerate, device_idx = await _generate(text)
    if data is None:
        return
    if not _bypass:
        from game.card_actions import press_key
        press_key("g")
    else:
        logger.info("[BYPASS] Would press G to open broadcast")
    await _play(data, samplerate, device_idx=device_idx)
    logger.info("TTS broadcast played: %r", text)


async def _speak_on_cable(text: str) -> None:
    """Generate and play to CABLE Input (+ Discord voice). No G press."""
    data, samplerate, device_idx = await _generate(text)
    if data is None:
        return
    await _play(data, samplerate, device_idx=device_idx)
    logger.info("TTS (sync): %r", text)



async def _play(data, samplerate: int, device_idx=None) -> None:
    """Play audio to sounddevice and Discord voice concurrently."""
    import sounddevice as sd

    # Fire Discord voice playback without blocking — sounddevice is the timing source.
    # _discord_play handles any overlap by stopping the previous clip before starting.
    vc = _voice_client
    loop = _event_loop
    if vc is not None and loop is not None:
        try:
            asyncio.run_coroutine_threadsafe(_discord_play(data, samplerate), loop)
        except Exception as e:
            logger.warning("TTS: failed to schedule Discord voice: %s", e)

    sd.play(data, samplerate, device=device_idx)
    sd.wait()


async def _discord_play(data, samplerate: int) -> None:
    """Coroutine that runs on the Discord event loop to play audio in the voice channel."""
    import discord
    import numpy as np

    vc = _voice_client
    if vc is None or not vc.is_connected():
        return

    pcm_bytes = _to_discord_pcm(data, samplerate)
    source = discord.PCMAudio(io.BytesIO(pcm_bytes))

    done = asyncio.Event()

    def _after(err):
        if err:
            logger.warning("TTS: Discord playback error: %s", err)
        done.set()

    if vc.is_playing():
        vc.stop()
    vc.play(source, after=_after)
    await done.wait()


def _to_discord_pcm(data, samplerate: int) -> bytes:
    """Convert numpy float audio to Discord's required format: 48 kHz stereo s16le."""
    import numpy as np

    data = np.asarray(data, dtype=np.float64)

    if samplerate != 48000:
        n_orig = len(data)
        n_new = int(n_orig * 48000 / samplerate)
        data = np.interp(
            np.linspace(0, n_orig - 1, n_new),
            np.arange(n_orig),
            data,
        )

    data = np.clip(data, -1.0, 1.0)
    pcm = (data * 32767).astype(np.int16)

    if pcm.ndim == 1:
        pcm = np.stack([pcm, pcm], axis=-1)

    return pcm.tobytes()


def precache_async(phrases: list[str]) -> None:
    """Pre-generate TTS audio for all phrases in a background thread.

    Call this right after match start so every queued phrase hits the cache
    instead of making a live network request to edge-tts.
    """
    if not _device_name:
        return
    unique = [p for p in dict.fromkeys(phrases) if p not in _audio_cache]
    if not unique:
        return

    def _do():
        asyncio.run(_precache(unique))

    threading.Thread(target=_do, daemon=True, name="tts-precache").start()
    logger.info("TTS: pre-caching %d phrase(s) in background", len(unique))


async def _precache(phrases: list[str]) -> None:
    for phrase in phrases:
        if phrase not in _audio_cache:
            try:
                data, samplerate, _ = await _generate(phrase)
                if data is not None:
                    logger.info("TTS cached: %r", phrase)
            except Exception as e:
                logger.warning("TTS pre-cache failed for %r: %s", phrase, e)


async def _generate(text: str):
    """Generate TTS audio. Returns (data, samplerate, device_idx) or (None, None, None)."""
    try:
        import edge_tts
        import sounddevice as sd
        import soundfile as sf
    except ImportError as e:
        logger.error("TTS dependency missing (%s) — run: pip install edge-tts sounddevice soundfile", e)
        return None, None, None

    devices = sd.query_devices()
    device_idx = next(
        (i for i, d in enumerate(devices)
         if _device_name.lower() in d["name"].lower() and d["max_output_channels"] > 0),
        None,
    )
    if device_idx is None:
        logger.warning("TTS: output device %r not found", _device_name)
        return None, None, None

    # Return cached audio if available — avoids the edge-tts network round-trip
    if text in _audio_cache:
        data, samplerate = _audio_cache[text]
        return data, samplerate, device_idx

    communicate = edge_tts.Communicate(text, voice=_voice)
    audio_bytes = io.BytesIO()
    async for chunk in communicate.stream():
        if chunk["type"] == "audio":
            audio_bytes.write(chunk["data"])
    audio_bytes.seek(0)

    data, samplerate = sf.read(audio_bytes)
    _audio_cache[text] = (data, samplerate)
    return data, samplerate, device_idx
