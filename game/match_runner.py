import logging
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Optional

from zones.zone_logic import valid_closeable_zones, ZoneState
from zones.strategy_factory import get_strategy
from session.state import SessionState

logger = logging.getLogger(__name__)

# Cards that target a zone coordinate at runtime rather than a fixed drop_target
_ZONE_TARGETED_CARDS = frozenset({"lava_zone", "nuclear_blast", "open_zone", "spawn_electronic"})

# Cards that target a player card slot at the top of the screen
_PLAYER_TARGETED_CARDS = frozenset({"expose", "favorite_player", "give_leather", "give_wood",
                                     "man_hunt", "speed_boost", "warm_up"})

# Card tray layout, calibrated at 1920×1080 (the only resolution this bot supports —
# see "Future Enhancements: Multi-resolution support" in CLAUDE.md). Moved out of
# config.json (2026-09-07): these have proven stable across the UE5 update and every
# session since, and — unlike the pixel-region calibration data that's genuinely
# machine/version-specific — there is nothing here that would ever differ between
# two people running this bot at the one supported resolution.
_CARD_TRAY_CENTER_X = 966
_CARD_TRAY_CARD_Y = 943
_CARD_TRAY_CARD_WIDTH = 76

# Fixed drop target per "plain" card type (not zone-targeted, not player-targeted,
# not zone_close) — every one of these currently drops dead center of the screen.
# Kept as a per-card-type dict rather than one shared constant so a future card
# that needs a different target is just as easy to add here as it was in config.
_CARD_DROP_TARGETS: dict[str, tuple[int, int]] = {
    "electromania": (960, 540),
    "beach_party": (960, 540),
    "blood_moon": (960, 540),
    "anti_grav_storm": (960, 540),
    "telepathy": (960, 540),
}

# Zone-close static drop target — the game auto-picks the zone from here. Live path,
# calibrated at 1920×1080. Moved out of config.json (2026-09-07), same rationale as
# the card tray layout above.
_ZONE_CLOSE_DROP_TARGET = (1750, 1000)

# Director-points OCR crop — 2-digit numerator only (not the "/10"). Calibrated at
# 1920×1080. Moved out of config.json (2026-09-07).
_DIRECTOR_POINTS_REGION = (808, 1002, 20, 24)

# Director-points pip pixel-sampling calibration — dormant while director_points_use_pips
# (still a real config toggle) is false, kept for if pip reading is ever revisited.
# Moved out of config.json (2026-09-07).
_DIRECTOR_POINTS_PIPS = {"x_start": 862, "y": 1012, "spacing": 26, "count": 10}

# Damage/kill feed text patterns (2026-09-10) — each entry is (event_kind,
# compiled regex), checked against one physical feed line at a time (see
# _poll_damage_feed_worker). Add more entries here for future keyword
# matches — no other code changes needed. Named groups "killer"/"victim"
# (if present) are resolved against the match-start roster via
# game.name_snap.find_winning_slot() before the event is emitted; a pattern
# with no such groups still emits, just without a *_slot field. A "method"
# group (feed_kill below) is captured too if present, as plain text — no
# name resolution attempted on it, it's not a player. Every match still just
# emits its event_kind — feed_first_blood is the one exception, additionally
# queuing the first-blood reward (see _maybe_queue_first_blood_reward in
# _poll_damage_feed_worker, and _maybe_fire_first_blood_reward in the main
# loop) once its killer resolves confidently.
# Keyword boundaries use \s* (zero-or-more), not \s+ — found live 2026-09-10,
# after the outline-detection OCR rewrite made names finally legible: the
# game renders near-zero pixel gap between a colored player name and an
# immediately adjacent white word (but a normal gap between two white
# words), so tesseract sometimes reads e.g. "TWO KILLED THUGZ BY ARROW" as
# "TWOKILLEDTHUGZBY ARROW" — no space around "KILLED", one preserved
# before "ARROW" since that boundary is white-to-white. \s+ silently failed
# to match these even though every word in them reads correctly; \s*
# matches whether or not tesseract happened to preserve the gap, with no
# regression on lines that do have it (see tests/fixtures/feed_kill/).
_FEED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("feed_first_blood", re.compile(r"(?P<killer>.+?)\s*DREW FIRST BLOOD FROM\s*(?P<victim>.+)", re.IGNORECASE)),
    # "X KILLED Y BY Z" (2026-09-10) — every kill, not just the first. The
    # trailing "BY <method>" is optional (a non-capturing group with method
    # inside it) so a line that's missing it — a genuinely method-less kill,
    # or OCR clipping the tail — still matches with method=None rather than
    # not matching at all.
    ("feed_kill", re.compile(r"(?P<killer>.+?)\s*KILLED\s*(?P<victim>.+?)(?:\s*BY\s*(?P<method>.+))?$", re.IGNORECASE)),
]

# How many recent (kind, matched-line) pairs _poll_damage_feed_worker
# remembers, to avoid re-emitting the same feed line as a duplicate event if
# it's still on screen on a later poll (2026-09-10) — first blood's own
# emission never needed this (a permanent one-shot queue flag already
# prevented acting on it twice), but feed_kill has no such flag: kills
# happen repeatedly all match, and the feed can plausibly still show the
# same line on two consecutive polls. A plain bounded deque (membership
# check is O(n), n capped here) rather than a set, since a set would grow
# unbounded over a long match; a match realistically has at most a few
# dozen kills, so 50 is comfortably more than enough lookback.
_RECENT_FEED_MATCHES_MAXLEN = 50

# Bonus-card reward timing buffer (2026-09-10) — shared by every "queued
# bonus card" flow (first-blood's give_wood, Crowd Favorite's
# favorite_player): each one's own drag (~1-2s, same as any tray card play)
# must never be started this close to a real scheduled card's own trigger
# time, so the two never overlap. See _maybe_fire_first_blood_reward() and
# _maybe_fire_favorite_reward().
_BONUS_CARD_TIMING_BUFFER_SECONDS = 5

# How long _give_reward_card() waits after switching POV before dropping the
# reward card (2026-09-10, found in review — not verified against the live
# game). "Give X to player" cards target whoever is currently spectated at
# drop time; with verify_card_plays: false (this repo's own current
# setting) there was otherwise no delay at all between the POV keypress and
# the drag starting, resting entirely on the unverified assumption that
# Darwin Project's client applies a spectate-target switch instantly. This
# is a defensive margin, not a measured value — tighten or drop it if a
# live match confirms the switch is already effectively instant.
_POV_SWITCH_SETTLE_SECONDS = 0.3

# Ceiling on the main loop's own iteration cadence, independent of
# screen_poll_interval_seconds (2026-09-10 fix, found live). Kill-feed
# banners (feed_first_blood, feed_kill — see _poll_damage_feed) are
# transient: visible for only a few seconds, then gone, with no way for a
# later poll to retroactively catch one that's already faded. First blood
# in particular is a one-time event — it gets exactly one chance. At the
# previous cadence (capped at screen_poll_interval_seconds, 12s by
# default), a real first blood was missed entirely: the banner had already
# faded by the next iteration. Every other per-iteration check is either
# already independently time-gated on its own interval (director points —
# see self._last_points_sample_time) or a cheap pixel/template comparison
# that was already being run at whatever cadence the loop happened to hit
# (_poll_player_bar, _match_has_ended, card-fire timing), so iterating more
# often costs little beyond an extra screenshot or two per second.
_MAIN_LOOP_MAX_SLEEP_SECONDS = 3

# Zone map sample points and per-zone drop coordinates for the legacy per-zone
# selection path (_attempt_zone_close_legacy() / _attempt_zone_close_bypass() /
# _update_zone_states_from_screenshot()) — dormant, kept for reference/rollback, see
# those methods' docstrings. Moved out of config.json (2026-09-07): recalibrating this
# legacy path now means editing these dicts directly rather than running
# calibrate.py/calibrate_zone_colors.py, which still target config.json and are no
# longer wired to this data — acceptable since this path hasn't been needed since the
# static drop-area replaced it (2026-08-30).
_ZONE_MAP_SAMPLE_POINTS: dict[int, list[tuple[int, int]]] = {
    1: [(824, 231), (714, 341), (934, 341), (824, 451)],
    2: [(1081, 221), (971, 331), (1191, 331), (1081, 441)],
    3: [(716, 430), (606, 540), (826, 540), (716, 650)],
    4: [(959, 430), (849, 540), (1069, 540), (959, 650)],
    5: [(1178, 430), (1068, 540), (1288, 540), (1178, 650)],
    6: [(841, 622), (731, 732), (951, 732), (841, 842)],
    7: [(1084, 622), (974, 732), (1194, 732), (1084, 842)],
}
_ZONE_DROP_COORDINATES: dict[int, tuple[int, int]] = {
    1: (824, 341),
    2: (1081, 331),
    3: (716, 540),
    4: (959, 540),
    5: (1178, 540),
    6: (841, 732),
    7: (1084, 732),
}


@dataclass
class CardEvent:
    name: str
    card_type: str
    trigger_seconds: int
    play_time_seconds: int
    deck_position: Optional[int]
    drop_target: Optional[tuple[int, int]]
    points_cost: Optional[int] = None
    pending: bool = field(default=False, compare=False)
    done: bool = field(default=False, compare=False)


class MatchRunner:
    """
    Runs the full in-match automation sequence in a background thread.

    Call run() from run_in_executor. Call stop() from the Discord /end command
    to abort early — the match loop checks the stop flag between every action.
    """

    def __init__(
        self,
        config: dict,
        session: SessionState,
        on_action_update: Callable[[str, str], None],
        skip_start: bool = False,
        profile: Optional[dict] = None,
        draft_lifecycle=None,
    ):
        self._config = config
        # DraftLifecycle (game/ds_lifecycle.py) owned by the cog: the one place
        # that talks to the darwinstalker ladder. None = ingest not wired.
        self._ds = draft_lifecycle
        self._session = session
        # Also (re-)locked here, at construction — belt and suspenders on top of
        # DirectorCog.custom() already locking it the moment the lobby was
        # created (see SessionState._pov_locked's docstring for the full
        # lifecycle and why locking only here was found live to be too late).
        # Unlocked in run() right after the forced default-POV-1 press. If this
        # runner never reaches that press (aborted early), _reset_session()'s
        # session.reset() is the safety net that clears it so POV can never
        # stay stuck locked.
        self._session.lock_pov()
        self._on_action_update = on_action_update
        self._stop = threading.Event()
        self._skip_start = skip_start
        self._profile = profile
        self._bypass = config.get("ahk_bypass_mode", False)
        # Master toggle for the tray-pixel and zone-map verification/retry logic (kept
        # intact, not deleted) — card plays have proven reliable enough that the extra
        # screenshots and retries are no longer needed day-to-day. Distinct from
        # ahk_bypass_mode: that's a dry-run mode that never sends real input; this
        # still plays every card for real, it just trusts the play worked instead of
        # verifying it with a before/after pixel check.
        self._verify_plays = config.get("verify_card_plays", True)
        # Master toggle for the bonus-card reward system (first blood's
        # give_wood, Crowd Favorite's favorite_player — 2026-09-10), default
        # on. Deliberately does NOT gate the underlying feed_first_blood/
        # feed_kill event logging to the ladder (_poll_damage_feed) — that's
        # informational data with no gameplay side effect, not a card, so it
        # stays on regardless. Gated at the two "queue" entry points
        # (_maybe_queue_first_blood_reward, try_queue_favorite_reward)
        # rather than the "maybe fire" methods too — if nothing's ever
        # queued, those are already no-ops by construction.
        self._advanced_cards_enabled = bool(config.get("advanced_cards", True))
        # Anti-cheat minimap cover — see _fire_card_event's sibling logic in run() for the
        # show, and the main loop below for the timed hide. One-shot flag so the hide
        # only fires once per match even though the loop polls elapsed time repeatedly.
        self._minimap_cover_source = config.get("obs_minimap_cover_source", "Map Cover")
        self._minimap_cover_seconds = config.get("obs_minimap_cover_seconds", 120)
        self._minimap_uncovered = False
        # Tournament mode (toggled via Discord's /tournament, config key tournament_mode):
        # the minimap cover stays up for the entire match instead of revealing at
        # _minimap_cover_seconds — see the main loop below, which skips the timed hide
        # entirely when this is set.
        self._tournament_mode = bool(config.get("tournament_mode", False))
        self._strategy = get_strategy(config.get("zone_selection_strategy", "weighted_outer"))
        self._zone_states: dict[int, str] = {i: ZoneState.OPEN for i in range(1, 8)}
        self._deck_played: set[int] = set()
        self._player_slot_xs: list[int] = []
        self._player_names: list[str] = []
        self._player_alive: list[bool] = []
        self._first_blood_logged: bool = False
        # See _poll_damage_feed() — True while a background thread is
        # already OCRing the damage feed, so the main loop skips starting a
        # second one on top of it rather than letting them stack up.
        self._feed_poll_busy: bool = False
        # (kind, matched-line) pairs already emitted this match — see
        # _RECENT_FEED_MATCHES_MAXLEN's own comment. Only ever touched from
        # the feed-poll's own background thread (one at a time, per
        # self._feed_poll_busy above), so no lock needed despite being a
        # mutable container.
        self._recent_feed_matches: deque = deque(maxlen=_RECENT_FEED_MATCHES_MAXLEN)
        # First-blood reward (give_wood, 2026-09-10) — set from the feed-poll
        # thread once a killer resolves confidently (_maybe_queue_first_blood_reward),
        # consumed by the main loop (_maybe_fire_first_blood_reward). Plain
        # attribute assignment, no lock, same cross-thread-flag convention as
        # this class's other such flags (_lobby_captured, _slot_map_ready, etc.).
        self._first_blood_reward_slot: Optional[int] = None
        # True once the reward has been given, canceled, or otherwise settled
        # (deck lacks the card, etc.) — there is only one first blood per
        # match, so once this is set nothing acts on a first-blood reward
        # again even if a later poll re-detects the same feed line.
        self._first_blood_reward_resolved: bool = False
        # Crowd Favorite channel-points reward (2026-09-10) — set by
        # try_queue_favorite_reward(), called from the Twitch bot's event
        # loop thread (bot/twitch_bot.py, a different thread from this
        # class's own match thread — plain attribute mutation, no lock, same
        # cross-thread convention as the first-blood reward state above).
        # Only one redemption is ever allowed pending at a time by design —
        # unlike first blood (a true one-time event), a viewer could redeem
        # this repeatedly across a match as long as favorite_player copies
        # remain in the deck, but a second redemption while one is already
        # queued is rejected (refunded) rather than queued behind it. None
        # when nothing is pending; the main loop's own
        # _maybe_fire_favorite_reward() is what actually consumes this.
        self._favorite_reward_target: Optional[int] = None
        # Populated by run() once the match's card schedule is built (see
        # _past_last_scheduled_card()) — stays [] beforehand/in tests that
        # never call run(), which _past_last_scheduled_card() treats as "not
        # past anything" rather than "everything's done".
        self._card_schedule: list["CardEvent"] = []
        # Set by _log_slot_map_snapshot() (2026-09-09) once every card it detected got
        # a usable (non-empty) name — "10/10", "9/9", never a partial read. Written from
        # that method's background thread (see _fire_slot_map_snapshot()); simple
        # attribute assignment, no lock, same as this class's other cross-thread flags.
        # Future features that need a trustworthy full slot->name map should gate on
        # is_lobby_captured() rather than assuming _slot_map is populated — it stays {}
        # whenever the capture was partial, so there's nothing to accidentally trust.
        self._lobby_captured: bool = False
        self._slot_map: dict[str, str] = {}  # {"/pov" digit: name}, only meaningful once captured
        # Set (regardless of outcome) when _log_slot_map_snapshot() finishes — lets an
        # async waiter (e.g. a "should we open a prediction" check) block on the
        # snapshot completing instead of guessing at timing, without caring whether it
        # actually succeeded (check is_lobby_captured() for that after the wait).
        self._slot_map_ready = threading.Event()
        # monotonic clock at match start; every live event carries elapsed_ms from it
        self._match_started_at: Optional[float] = None
        # Latest confirmed director-points reading — see _update_points_reading().
        # Sampled continuously throughout the match (main loop, every
        # screen_poll_interval_seconds) as well as reactively in _wait_for_points(),
        # so a card that needs to check affordability usually already has a recent
        # confirmed value instead of needing a fresh read at that exact moment.
        # Invalidated (set back to None) immediately after every card play attempt in
        # _fire_card_event(), since a real decrease is expected right then.
        self._last_confirmed_points: Optional[int] = None
        self._last_points_sample_time: float = 0.0
        from game.deck_utils import deck_layout_from_state
        live_layout = deck_layout_from_state()
        self._deck_layout: list[str] = live_layout if live_layout else config.get("deck_layout", [])
        if live_layout:
            logger.info("Deck layout loaded from state.json (%d cards)", len(live_layout))
        else:
            logger.warning("state.json unavailable — falling back to config deck_layout")

    def stop(self):
        """Signal the match loop to exit at the next checkpoint."""
        logger.info("MatchRunner stop requested")
        self._stop.set()

    # ------------------------------------------------------------------
    # Entry point
    # ------------------------------------------------------------------

    def run(self) -> str:
        """
        Full match sequence. Blocking — run via run_in_executor.
        Returns a Discord-formatted result string.
        """
        if self._stop.is_set():
            return "Match aborted before start."

        # Capture player roster from lobby nameplates before the match countdown
        # begins, and push it to the ladder once read. Fire-and-forget
        # (2026-09-10 fix, found in review) — _init_player_bar() alone can do
        # up to one tesseract call per card (game/player_cards_v2.py::ocr_names,
        # each individually timeout-guarded up to 10s in game/ocr.py), which
        # was blocking the B-press below on every match, the exact same class
        # of bug already found live and fixed for the ladder roster push
        # (on_match_start_async, 2026-09-07) and the slot-map snapshot
        # (_fire_slot_map_snapshot, 2026-09-09) — just never applied here too.
        # _fire_player_bar_init() spawns a daemon thread that does the
        # detection, OCR, and both of those pushes together, and returns
        # immediately. self._player_slot_xs/_player_names/_player_alive stay
        # at their empty defaults until that thread finishes; _poll_player_bar()
        # already no-ops gracefully on an empty _player_slot_xs (same "not
        # ready yet" tolerance _log_slot_map_snapshot's own async state uses),
        # so a match's first poll or two simply detects nothing rather than
        # blocking anything waiting for it.
        self._fire_player_bar_init()

        from game import tts

        if self._skip_start:
            # Game auto-started — B press is skipped but the 5s in-game countdown still runs
            self._update("Waiting for match countdown", "Starting card timers")
            self._fire_slot_map_snapshot()
            tts.speak("Match is starting. Good luck.", broadcast=False)
            if self._stop.wait(5):
                return "Match aborted during countdown."
        else:
            # Press B to start the match
            self._update("Pressing B to start", "Waiting for countdown")
            self._press("b")
            self._fire_slot_map_snapshot()
            # Announce immediately after B press — players are still in the lobby
            tts.speak("Match is starting. Good luck.", broadcast=False)

            # 5-second sync delay to align with in-game countdown
            if self._stop.wait(5):
                return "Match aborted during countdown."

        if self._profile is not None:
            _profile = self._profile
            logger.info("Profile: %s (pre-resolved at lobby creation)", _profile["display_name"])
        else:
            from game.profiles import resolve_profile
            _active_key = self._config.get("active_profile", "standard")
            _profile = resolve_profile(_active_key)
            if _active_key == "randomizer":
                logger.info("Profile selected by randomizer: %s", _profile["display_name"])
            else:
                logger.info("Profile: %s", _profile["display_name"])
        _first = min(_profile["card_plays"], key=lambda p: p["play_time_seconds"])
        _m, _s = divmod(_first["play_time_seconds"], 60)
        _first_label = f"{_first['card'].replace('_', ' ').title()} at {_m}:{_s:02d}"
        self._update("Match in progress", _first_label)
        start_time = time.monotonic()
        self._match_started_at = start_time

        card_schedule = self._build_card_schedule(_profile)
        self._card_schedule = card_schedule
        phrases = self._build_tts_phrases(card_schedule)
        profile_announce = f"Using profile: {_profile['display_name']}"
        phrases.append(profile_announce)
        tts.precache_async(phrases)
        if not self._stop.wait(5):
            # Default the Director's camera to player 1's POV as soon as the match is
            # underway, so viewers see live gameplay immediately without needing a mod
            # to run /pov or !pov first. Same key as /pov's "1" choice.
            self._press("1")
            self._announce_card_lineup(profile_announce)
        # POV is safe to use manually from here on — see SessionState._pov_locked's
        # docstring. Unlocked unconditionally (even if the wait above was interrupted
        # and the press skipped) since the match is either underway or about to be
        # torn down either way; _reset_session() covers the teardown case regardless.
        self._session.unlock_pov()
        poll_interval = self._config.get("screen_poll_interval_seconds", 12)

        # recording_enabled: false skips creating/starting the recorder entirely — no
        # local file gets written, and recording_path stays None throughout, so
        # discord_bot.py's `if recording_path:` upload check downstream naturally
        # never fires either. One toggle covers both "no local recording" and "no
        # upload attempt" since there's nothing to upload without a file.
        recorder = None
        if self._config.get("recording_enabled", True):
            from game.video_recorder import VideoRecorder
            recorder = VideoRecorder(self._config)
            recorder.start()
        recording_path = None

        try:
            # ------------------------------------------------------------------
            # Main match loop
            # ------------------------------------------------------------------
            while not self._stop.is_set():
                elapsed = time.monotonic() - start_time

                # Fire any card events whose time has arrived
                for event in card_schedule:
                    if not event.done and elapsed >= event.trigger_seconds:
                        self._fire_card_event(event, card_schedule)

                # Anti-cheat: uncover the minimap once the covered window has elapsed.
                # One-shot — self._minimap_uncovered stops this from firing again on
                # every subsequent loop iteration for the rest of the match. Skipped
                # entirely in tournament mode — the cover stays up the whole match.
                if not self._tournament_mode and not self._minimap_uncovered and elapsed >= self._minimap_cover_seconds:
                    from game import obs_control
                    if obs_control.is_enabled():
                        obs_control.set_source_visible(self._minimap_cover_source, False)
                    self._minimap_uncovered = True

                # Poll for match end (placement badge on screen)
                if self._match_has_ended():
                    logger.info("Match end detected at %.1fs elapsed", elapsed)
                    self._emit("match_end", elapsed_ms=int(elapsed * 1000))
                    break

                # Poll the player bar all match: first blood once, and every
                # elimination as a live event for the ladder's LIVE card.
                # Fast (pixel/color sampling, no OCR) — safe to run inline.
                if self._player_slot_xs:
                    from game.screen_detection import take_screenshot as _take_ss
                    self._poll_player_bar(_take_ss())

                # Damage/kill feed OCR — fire-and-forget on its own thread
                # (see _poll_damage_feed), so a slow tesseract call can never
                # delay a scheduled card play. No-ops until _init_player_bar()
                # has populated a roster to resolve names against.
                self._poll_damage_feed()

                # Continuously sample director points throughout the match, not just
                # reactively when a card is about to fire — a small, steady cost (one
                # screenshot + OCR read every poll_interval) that means _wait_for_points()
                # usually already has a recent confirmed value to check against instead
                # of needing a fresh, possibly-failed read at the exact moment a card
                # needs to check affordability. Same ratchet-up guard as everywhere else
                # (see _update_points_reading) — a bad read here is disregarded, not
                # trusted, so this can only ever raise confidence, never lower it.
                now_ts = time.monotonic()
                if now_ts - self._last_points_sample_time >= poll_interval:
                    from game.screen_detection import take_screenshot as _take_ss2
                    sample = self._read_points(_take_ss2())
                    self._update_points_reading(sample, "background poll")
                    self._last_points_sample_time = now_ts

                # First-blood reward (give_wood) — see _maybe_fire_first_blood_reward
                # for the full "safe to fire" checklist. Cheap no-op most
                # iterations (nothing queued, or one of the conditions isn't
                # met yet); when it does fire, it blocks for the card's own
                # drag exactly like a real scheduled card play already does,
                # which is why it's checked after the points sample above —
                # freshest possible confirmed value for this iteration.
                self._maybe_fire_first_blood_reward(elapsed, card_schedule)

                # Crowd Favorite channel-points reward — see
                # _maybe_fire_favorite_reward for its own "safe to fire"
                # checklist (same shape as first blood's above, minus the
                # points check since favorite_player costs 0). elapsed is
                # recomputed here (2026-09-10 fix, found in review) rather
                # than reusing the value from the top of this iteration —
                # if first blood's reward just fired above, it blocked for
                # its own ~1-2s drag, and checking this reward's "no
                # scheduled card due within the buffer" against the
                # now-stale pre-drag elapsed would understate how close a
                # real card actually is by however long that drag took.
                elapsed = time.monotonic() - start_time
                self._maybe_fire_favorite_reward(elapsed, card_schedule)

                # Sleep until the next card trigger, but no longer than
                # poll_interval, and never longer than _MAIN_LOOP_MAX_SLEEP_SECONDS
                # regardless — see that constant's own comment for why
                # (screen_poll_interval_seconds alone was too coarse to
                # reliably catch a transient kill-feed banner).
                now = time.monotonic()
                now_elapsed = now - start_time
                pending_times = [
                    e.trigger_seconds - now_elapsed
                    for e in card_schedule if not e.done
                ]
                loop_cap = min(poll_interval, _MAIN_LOOP_MAX_SLEEP_SECONDS)
                sleep_time = min(loop_cap, min(pending_times)) if pending_times else loop_cap
                self._stop.wait(max(0.1, sleep_time))
        finally:
            if recorder is not None:
                recording_path = recorder.stop()

        if self._stop.is_set():
            self._emit("aborted", reason="force stopped")
            return "Match ended early (force stopped)."

        # ------------------------------------------------------------------
        # Capture and return results
        # ------------------------------------------------------------------
        from game import tts
        tts.speak("Match over.", broadcast=False)
        self._update("Reading results screen", "Post to Discord")
        return self._capture_results(), recording_path

    # ------------------------------------------------------------------
    # Player bar
    # ------------------------------------------------------------------

    def _fire_player_bar_init(self) -> None:
        """Fire-and-forget wrapper — see the call site in run() for why.
        Spawns a daemon thread running _init_player_bar_and_push() and
        returns immediately."""
        threading.Thread(
            target=self._init_player_bar_and_push, daemon=True, name="PlayerBarInit",
        ).start()

    def _init_player_bar_and_push(self) -> None:
        """Runs on its own thread — see _fire_player_bar_init(). Detects the
        roster (_init_player_bar()) then pushes it to the ladder, in that
        order, since the push needs the names _init_player_bar() just read.
        Wrapped in try/except: a failure here must never take down the match
        thread that spawned it."""
        try:
            self._init_player_bar()
            if self._ds is not None:
                self._ds.on_match_start_async(self._player_names)
                self._ds.event("match_start", elapsed_ms=0, slots=[n or None for n in self._player_names])
        except Exception as e:
            logger.warning("Player bar init/roster push failed: %s", e)

    def _init_player_bar(self):
        """
        Snapshot the player bar from the lobby screen before the match starts,
        via the V2 geometry detector (game/player_cards_v2.py — no per-machine
        calibration needed, proven on real VOD/match frames). Detects slot
        count, x-positions, and OCRs player names in slot order. Called once;
        results are reused for first-blood/elimination tracking during the
        match.
        """
        from game.screen_detection import take_screenshot
        from game import player_cards_v2 as v2

        screenshot = take_screenshot()
        expected = None
        if self._ds is not None:
            try:
                expected = self._ds.roster_size or None
            except Exception:
                expected = None
        try:
            cards = v2.detect_cards(screenshot, self._config, expected=expected)
        except Exception as e:
            logger.warning("Player bar: detection failed: %s", e)
            cards = []
        if not cards:
            logger.warning(
                "Player bar snapshot: no cards detected — player tracking disabled."
            )
            return

        try:
            names = v2.ocr_names(screenshot, cards, self._config)
        except Exception as e:
            logger.debug("Player bar: name OCR failed: %s", e)
            names = []
        if len(names) != len(cards):
            names = ["" for _ in cards]

        self._player_slot_xs = [c.x for c in cards]
        self._player_names = self._snap_names(names)
        self._player_alive = [c.alive for c in cards]

        logger.info("Player bar snapshot — %d players:", len(self._player_slot_xs))
        for i, (name, x) in enumerate(zip(self._player_names, self._player_slot_xs)):
            logger.info("  slot %d  x=%-4d  %s", i + 1, x, name or "(unread)")

    def _snap_names(self, names: list[str]) -> list[str]:
        """Nameplate OCR → the ladder's canonical names for this lobby, where
        one player matches unambiguously (DraftLifecycle.snap_names, fed by the
        open-draft reply). Verbatim when no lobby is known."""
        if self._ds is None or not names:
            return list(names)
        try:
            snapped = self._ds.snap_names(list(names))
        except Exception as e:
            logger.debug("name snap unavailable: %s", e)
            return list(names)
        for before, after in zip(names, snapped):
            if before != after:
                logger.info("  nameplate %r snapped to ladder name %r", before, after)
        return snapped

    def _fire_slot_map_snapshot(self) -> None:
        """Fire-and-forget wrapper (2026-09-09 fix, found live) — the actual
        snapshot does up to two tesseract calls per card (name + badge OCR),
        which was blocking the match thread synchronously right at match
        start and delaying the B-press/card-timer sequence by several
        seconds on a real lobby, exactly the "on_match_start" delay found
        and fixed 2026-09-07. Diagnostic work with no downstream consumer in
        run() must never sit inline in the match's own critical path — same
        rule as that fix, generalized: spawn a daemon thread and return
        immediately instead."""
        threading.Thread(
            target=self._log_slot_map_snapshot, daemon=True, name="SlotMapSnapshot",
        ).start()

    def _log_slot_map_snapshot(self) -> None:
        """Diagnostic only (2026-09-09) — reads each card's slot-number badge
        alongside its name at match start, logs the resulting slot→name map,
        and pushes it to the ladder as a "slot_map" live event (visible on
        darwinstalker.com next to match_start/eliminated — the ordinary log
        line stays local; DsLogHandler only forwards INFO from
        game.ds_lifecycle/game.ingest, see docs/DS_LIFECYCLE_HANDOFF.md).
        The badge is confirmed (2026-09-09, docs/PLAYER_BAR_CALIBRATION.md
        §8) to equal the /pov hotkey and to stay fixed to the same player for
        the whole match — an elimination only adds an X overlay, it never
        moves or renumbers a card — so this map, once built, should hold for
        the rest of the match. Still not wired into real match logic beyond
        the event push until real OCR output (not just the crop, which is now
        measured) has been checked against an actual match's logs. Nothing
        here feeds _player_slot_xs/_player_alive/_player_names or anything
        else the match actually uses, and it never raises, so a bad read here
        can never affect match start.

        Unlinked players (no ladder identity — see NameSnapper's docstring)
        can't be corrected against anything, so their slot just keeps
        whatever nameplate OCR read, verbatim — that was already snap_all()'s
        fallback. What's new here is making that explicit: `linked` marks
        which slots actually resolved to one of the ladder's known lobby
        players (self._ds.lobby) versus which are raw OCR standing in for an
        unlinked one, and the not-linked count is checked against
        self._ds.unlinked's known size as a sanity signal — a mismatch means
        either OCR missed a linked player too, or the roster has drifted
        since /custom captured it, not that the unlinked count is wrong.
        """
        try:
            from game.screen_detection import take_screenshot
            from game import player_cards_v2 as v2

            screenshot = take_screenshot()
            expected = None
            if self._ds is not None:
                try:
                    expected = self._ds.roster_size or None
                except Exception:
                    expected = None
            cards = v2.detect_cards(screenshot, self._config, expected=expected)
            if not cards:
                logger.info("Slot map snapshot: no cards detected")
                return
            names = v2.ocr_names(screenshot, cards, self._config)
            if len(names) != len(cards):
                names = ["" for _ in cards]
            names = self._snap_names(names)

            known_players: set[str] = set()
            unlinked_count: Optional[int] = None
            if self._ds is not None:
                try:
                    known_players = self._ds.known_players
                except Exception:
                    known_players = set()
                try:
                    unlinked_count = len(self._ds.unlinked)
                except Exception:
                    unlinked_count = None

            logger.info("Slot map snapshot — %d cards detected:", len(cards))
            slots: list[str] = []
            badge_ocrs: list[Optional[int]] = []
            linked_flags: list[bool] = []
            for c, name in zip(cards, names):
                # slot = card.index -> /pov digit, confirmed 2026-09-09, no OCR
                # needed (see slot_number_for_index()'s docstring). badge_ocr is
                # logged only as a secondary cross-check, not trusted — that OCR
                # proved unreliable at native resolution.
                slot = v2.slot_number_for_index(c.index)
                badge_ocr, raw_badge = v2.ocr_badge_number(screenshot, c, self._config)
                linked = bool(name) and name in known_players
                slots.append(slot)
                badge_ocrs.append(badge_ocr)
                linked_flags.append(linked)
                source = "ladder" if linked else ("ocr" if name else "unread")
                logger.info(
                    "  slot=%s  card index=%d  x=%-4d  name=%s  (%s)  alive=%s  (badge_ocr=%s raw %r)",
                    slot, c.index, c.x, name or "(unread)", source, c.alive,
                    badge_ocr if badge_ocr is not None else "?", raw_badge,
                )

            not_linked = sum(1 for f in linked_flags if not f)
            if unlinked_count is not None and not_linked != unlinked_count:
                logger.info(
                    "Slot map snapshot: %d slot(s) not matched to a known ladder player, "
                    "expected %d unlinked roster member(s) — mismatch (an OCR miss on a "
                    "linked player, or the roster has drifted since /custom captured it)",
                    not_linked, unlinked_count,
                )

            # "Fully captured": every card detect_cards() found has SOME name (ladder
            # or raw OCR, either counts — see the unlinked note above) — a straight
            # N/N, not a partial read. Self-referential to what was actually detected
            # on screen this time, not cross-checked against roster_size, since that's
            # only a Discord-signup estimate of who's supposed to be here, not proof of
            # who actually is. Written here, on this method's own background thread
            # (see _fire_slot_map_snapshot()); a plain bool + dict assignment, no lock
            # needed, same as this class's other cross-thread flags.
            captured = bool(cards) and all(bool(nm) for nm in names)
            self._lobby_captured = captured
            self._slot_map = dict(zip(slots, names)) if captured else {}
            if captured:
                logger.info("Slot map snapshot: lobby fully captured (%d/%d)", len(cards), len(cards))
            else:
                unread = sum(1 for nm in names if not nm)
                logger.info(
                    "Slot map snapshot: lobby NOT fully captured (%d/%d unread)",
                    unread, len(cards),
                )

            self._emit(
                "slot_map", n=len(cards), slots=slots,
                names=[nm or None for nm in names],
                xs=[c.x for c in cards], badge_ocr=badge_ocrs,
                linked=linked_flags, captured=captured,
            )
        except Exception as e:
            logger.warning("Slot map snapshot failed: %s", e)
        finally:
            # Set regardless of outcome (success, early "no cards detected"
            # return, or exception) — a waiter only cares that the attempt is
            # over, not whether it succeeded; check is_lobby_captured() for that.
            self._slot_map_ready.set()

    def is_lobby_captured(self) -> bool:
        """True once _log_slot_map_snapshot() resolved a usable name for every
        card it detected this match — see that method's docstring. Gate any
        future feature that needs a trustworthy full slot->name map on this
        rather than assuming slot_map() is populated; it returns {} whenever
        the capture was partial or hasn't run yet."""
        return self._lobby_captured

    def slot_map(self) -> dict[str, str]:
        """{"/pov" digit: name}, populated only once is_lobby_captured() is True."""
        return dict(self._slot_map)

    def _poll_player_bar(self, screenshot):
        """
        Check alive/eliminated status for each player slot.
        On the first death (first blood), logs the victim. Who got the kill
        is NOT looked up here (see _poll_damage_feed's feed_first_blood event
        for that) — this method does no OCR at all, just a color/pixel check
        on the screenshot it's given.
        """
        if not self._player_slot_xs:
            return

        from game import player_cards_v2 as v2
        new_alive = v2.cards_alive(screenshot, self._player_slot_xs, self._config)

        prev_dead = sum(1 for a in self._player_alive if not a)
        curr_dead = sum(1 for a in new_alive if not a)
        newly_dead = [
            i for i, (was, now) in enumerate(zip(self._player_alive, new_alive))
            if was and not now
        ]

        if not self._first_blood_logged and prev_dead == 0 and curr_dead >= 1:
            victim_name = (
                self._player_names[newly_dead[0]]
                if self._player_names and newly_dead
                else f"slot {newly_dead[0] if newly_dead else '?'}"
            )
            # notif used to come from an inline ocr_kill_notification() call
            # here — removed 2026-09-10 along with that function entirely
            # (game/ocr.py): it was a synchronous OCR call sitting inline in
            # the match loop, gated on a config key never calibrated on any
            # machine, and _poll_damage_feed's feed_first_blood event already
            # covers "who got the kill" better (multi-line, resolved against
            # the roster, off the match thread). Kept as None here so the
            # first_blood event's shape doesn't change.
            notif = None
            logger.info(
                "FIRST BLOOD — victim: %s | kill notification: %r",
                victim_name, notif,
            )
            self._first_blood_logged = True
            if newly_dead:
                self._emit("first_blood", slot=newly_dead[0], player=victim_name, notification=notif)

        # Every alive→dead flip is an elimination event; `alive` counts down as
        # this poll's flips are applied in slot order.
        alive_after = sum(1 for a in self._player_alive if a)
        for i in newly_dead:
            alive_after -= 1
            self._emit("eliminated", slot=i, player=self._slot_name(i), alive=alive_after)

        self._player_alive = new_alive

    def _poll_damage_feed(self) -> None:
        """Fire-and-forget (2026-09-10 fix, found in review before it shipped
        live): OCR is tesseract-backed, and game/ocr.py's own timeout guard
        (_OCR_TIMEOUT_SECONDS = 10) admits a single call can occasionally take
        real time — running that inline in the main loop, like the first cut
        of this feature did, would delay a scheduled card play by however
        long that call took, since the loop's next card-fire check only
        happens after whatever synchronous work the current iteration is
        doing finishes. Spawns a daemon thread that takes its own screenshot
        and does the OCR/matching independently — same pattern as
        _fire_slot_map_snapshot() for the same reason. self._feed_poll_busy
        (plain bool, no lock — same cross-thread-flag convention as
        self._lobby_captured etc.) skips starting a new poll while one is
        still in flight rather than letting them stack up; at the normal
        screen_poll_interval_seconds cadence there should only ever be zero
        or one in flight.

        No-ops if _init_player_bar() hasn't populated a roster yet — nothing
        to resolve names against.
        """
        if self._feed_poll_busy or not self._player_names:
            return
        self._feed_poll_busy = True
        threading.Thread(
            target=self._poll_damage_feed_worker, daemon=True, name="DamageFeedPoll",
        ).start()

    def _poll_damage_feed_worker(self) -> None:
        """Runs on its own thread — see _poll_damage_feed(). Checks the
        OCR'd feed text against _FEED_PATTERNS (currently "X DREW FIRST
        BLOOD FROM Y" and "X KILLED Y BY Z" — add more entries there for
        future keyword matches, each one gets checked the same way with no
        other code changes needed). Matched **per physical line**, not the
        whole block flattened to one string — the feed stacks several
        unrelated lines (other kills, zone events) in this crop, and
        flattening first would let an adjacent line's words bleed into a
        pattern's captured groups. Runs for the whole match, every poll —
        earlier drafts stopped after the first match found (there's only
        one first blood), but most patterns (kills, in particular) are
        expected to recur, so nothing here assumes "only fires once";
        self._recent_feed_matches instead skips re-emitting the exact same
        (kind, line) if it's still on screen on a later poll, and the
        busy-flag above bounds how much work is in flight at a time, not
        how many times a pattern can fire.
        """
        try:
            from game.screen_detection import take_screenshot
            from game.video_recorder import _CROP_REGION
            from game.ocr import ocr_feed_text

            screenshot = take_screenshot()
            text = ocr_feed_text(screenshot, _CROP_REGION)
            if not text:
                return
            slot_map = {str(i): name for i, name in enumerate(self._player_names) if name}
            for raw_line in text.splitlines():
                line = re.sub(r"\s+", " ", raw_line).strip()
                if not line:
                    continue
                for kind, pattern in _FEED_PATTERNS:
                    m = pattern.search(line)
                    if not m:
                        continue
                    match_key = f"{kind}|{line}"
                    if match_key in self._recent_feed_matches:
                        continue
                    self._recent_feed_matches.append(match_key)

                    fields: dict = {"text": line}
                    groups = m.groupdict()
                    for role in ("killer", "victim"):
                        if role in groups:
                            raw_name = groups[role].strip()
                            fields[f"{role}_raw"] = raw_name
                            fields[f"{role}_slot"] = self._resolve_feed_name(raw_name, slot_map)
                    if groups.get("method"):
                        fields["method"] = groups["method"].strip()
                    logger.info("Feed match [%s]: %r -> %s", kind, line, fields)
                    self._emit(kind, **fields)
                    if kind == "feed_first_blood":
                        self._maybe_queue_first_blood_reward(fields.get("killer_slot"))
                    self._save_feed_debug_images(screenshot, kind)
        except Exception as e:
            logger.debug("Damage feed poll failed: %s", e)
        finally:
            self._feed_poll_busy = False

    def _resolve_feed_name(self, raw_name: str, slot_map: dict[str, str]) -> Optional[str]:
        """Resolve a damage-feed killer/victim OCR read to a slot index.

        Tries the ladder's own known-alias data first, via
        DraftLifecycle.resolve_alias() — an exact match against a specific
        player's registered handles (canonical name, persona, any alias)
        catches something find_winning_slot()'s fuzzy score against
        slot_map can't: the read can be a clean, correctly-OCR'd name that
        just isn't this match's already-decided display name for that
        player. Found live 2026-09-11 — the feed read "CONNOR" plainly and
        correctly, but that player's slot had resolved to a different
        display name ("Mojo") for this match, and find_winning_slot() still
        matched an unrelated player ("Coen") by fuzzy score alone, giving
        them the first-blood reward instead. "connor" was one of that
        player's other known ladder aliases the whole time — resolve_alias()
        finds that directly, with no scoring involved.

        Falls back to find_winning_slot()'s fuzzy match against slot_map
        when no alias resolves (self._ds is None, an unlinked player with no
        registered aliases, or genuine OCR noise) — same as before this
        method existed.
        """
        if self._ds is not None:
            canonical = self._ds.resolve_alias(raw_name)
            if canonical is not None:
                for slot, name in slot_map.items():
                    if name == canonical:
                        return slot
        from game.name_snap import find_winning_slot
        return find_winning_slot(raw_name, slot_map)

    def _save_feed_debug_images(self, screenshot, kind: str) -> None:
        """Saves the raw-color feed crop (plus the processed image OCR
        actually used) to screenshots/errors/ whenever a feed pattern
        matches (2026-09-10, added specifically to chase the still-open
        colored-name OCR gap — see ocr_feed_text()'s "Known remaining gap"
        docstring note). Player names in this feed render orange/red, not
        white, and the current min-channel preprocessing can't separate
        orange/red text from a colored background (both have a low blue
        channel) — action words parse fine, names come back garbage. No
        amount of further guessing at a color threshold is worth it without
        real pixel samples of the name text; this gives the next live
        first-blood or kill line a saved raw crop to actually inspect.
        Own try/except — a debug-save failure must never break event
        emission or the first-blood reward queue that runs right before
        this in the caller.
        """
        try:
            import datetime
            from game.ocr import save_feed_debug_images
            from game.video_recorder import _CROP_REGION
            ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S_%f")
            save_feed_debug_images(screenshot, _CROP_REGION, "screenshots/errors", f"{ts}_{kind}")
        except Exception as e:
            logger.debug("Feed debug image save failed: %s", e)

    def _maybe_queue_first_blood_reward(self, killer_slot: Optional[str]) -> None:
        """Called from _poll_damage_feed_worker() (its own background
        thread) right after a feed_first_blood match. Queues the give_wood
        reward for the main loop (_maybe_fire_first_blood_reward) to act on
        once it's safe to — never fires anything itself, just records which
        card index to reward. No-ops if the killer couldn't be confidently
        resolved (never guess who to reward), if a reward has already been
        queued or resolved this match — there is only one first blood, so
        only one reward is ever queued, and a later re-detection of the same
        feed line (polling continues all match, see _poll_damage_feed_worker's
        own docstring) must not re-queue or overwrite it — or if
        advanced_cards (config) is off. The feed_first_blood event itself
        (emitted by the caller, not here) still fires either way — this
        toggle only gates the reward, not the ladder logging.
        """
        if not self._advanced_cards_enabled:
            return
        if killer_slot is None or self._first_blood_reward_resolved or self._first_blood_reward_slot is not None:
            return
        self._first_blood_reward_slot = int(killer_slot)
        logger.info(
            "First blood reward queued for slot index %d (%s)",
            self._first_blood_reward_slot, self._slot_name(self._first_blood_reward_slot),
        )

    def _maybe_fire_first_blood_reward(self, elapsed: float, card_schedule: list["CardEvent"]) -> None:
        """Called every main-loop iteration (match thread). Gives the
        first-blood killer a "Give Wood" card once three things are all
        true — checked cheaply, no blocking wait, so a match with none of
        them true yet just gets checked again next iteration:

        1. A reward is actually queued (_maybe_queue_first_blood_reward) and
           not already resolved.
        2. The target hasn't died since being queued — checked against
           self._player_alive, which _poll_player_bar() (called earlier this
           same iteration) already refreshed, so this needs no OCR/screenshot
           of its own. Cancels outright if so: a dead player never gets the
           reward, no partial/late attempt.
        3. Enough director points (self._last_confirmed_points, the same
           continuously-ratcheted value the main loop already maintains —
           no fresh read here) AND no real scheduled card due within
           _BONUS_CARD_TIMING_BUFFER_SECONDS, so the reward's own drag
           (~1-2s, same as any tray card play) never overlaps a scheduled
           card's.

        Claims the reward (sets self._first_blood_reward_resolved) BEFORE
        calling _give_first_blood_reward() — that call blocks for the
        drag, same as any other card play — so a later iteration can't
        re-enter and double-fire while it's in progress.
        """
        if self._first_blood_reward_slot is None or self._first_blood_reward_resolved:
            return
        slot = self._first_blood_reward_slot

        if slot < len(self._player_alive) and not self._player_alive[slot]:
            logger.info(
                "First blood reward canceled — %s was eliminated before it could be given",
                self._slot_name(slot),
            )
            self._first_blood_reward_resolved = True
            return

        pending_times = [e.trigger_seconds - elapsed for e in card_schedule if not e.done]
        if pending_times and min(pending_times) <= _BONUS_CARD_TIMING_BUFFER_SECONDS:
            return

        from game.deck_utils import CARD_POINT_COSTS
        cost = CARD_POINT_COSTS.get("give_wood", 0)
        if cost and (self._last_confirmed_points is None or self._last_confirmed_points < cost):
            return

        deck_pos = self._next_available_deck_pos(self._positions_for_card_type("give_wood"))
        if deck_pos is None:
            logger.warning("First blood reward: no 'give_wood' card available in the deck — canceling")
            self._first_blood_reward_resolved = True
            return

        self._first_blood_reward_resolved = True
        self._give_first_blood_reward(slot, deck_pos)

    def _give_reward_card(
        self, card_type: str, player_index: int, deck_pos: int, tts_prefix: str, event_label: str,
    ) -> None:
        """Shared by every "queued bonus card" reward (first-blood's
        give_wood, Crowd Favorite's favorite_player — see
        _give_first_blood_reward/_give_favorite_reward below): locks POV,
        switches the camera to player_index, and drops card_type at
        screen-center (960, 540) — deliberately NOT through the
        player-target-coordinate system (_PLAYER_TARGETED_CARDS, gated on
        player_target_coordinates, never calibrated on any machine): the
        game applies a "give X to player" card to whichever player is
        currently spectated when it's dropped at center, the same drop
        point electromania/beach_party/etc. use, so switching POV to the
        target first is what actually targets the reward at them.

        Locks POV the same way the match-start sequence does (see
        SessionState._pov_locked's docstring) so a viewer's own /pov can't
        switch the camera away mid-sequence; unlocks in a finally block so
        normal POV control resumes whether the card play succeeds or not.

        Builds a synthetic CardEvent to reuse _play_tray_card() (the same
        drag/verify/announce logic every scheduled card goes through) rather
        than duplicating it — trigger_seconds/play_time_seconds are unused
        outside the schedule so they're left at 0. tts_prefix is spoken as
        "{tts_prefix}! Rewarding X with Y."; event_label names the emitted
        card_play event as "{event_label} (Y)".
        """
        from game.player_cards_v2 import slot_number_for_index
        from game.deck_utils import CARD_POINT_COSTS
        from game import tts

        pov_key = slot_number_for_index(player_index)
        target_label = self._slot_name(player_index)
        card_label = tts.card_announce(card_type)
        logger.info(
            "Reward: switching POV to %s (key %s), then dropping %s at screen center",
            target_label, pov_key, card_label,
        )

        self._session.lock_pov()
        try:
            self._press(pov_key)
            # Let the spectate-target switch actually apply before dropping
            # a card that targets whoever's currently spectated — see
            # _POV_SWITCH_SETTLE_SECONDS' own comment. self._stop.wait()
            # rather than a bare sleep so a force-stop during this brief
            # window is still picked up promptly, same as every other wait
            # in this class.
            if self._stop.wait(_POV_SWITCH_SETTLE_SECONDS):
                return  # force-stopped during the settle wait — don't play into a dying match
            tts.speak_cable(f"{tts_prefix}! Rewarding {target_label} with {card_label}.")
            event = CardEvent(
                name=f"{event_label} ({card_label})",
                card_type=card_type,
                trigger_seconds=0,
                play_time_seconds=0,
                deck_position=deck_pos,
                drop_target=(960, 540),
                points_cost=CARD_POINT_COSTS.get(card_type),
            )
            # _fire_card_event() normally emits this before dispatching by
            # card_type — bypassed here (see docstring), so it's emitted
            # explicitly instead, to keep this reward visible on the
            # ladder's live feed like any other card play.
            self._emit("card_play", card=event.card_type, name=event.name)
            self._play_tray_card(event, (960, 540), card_label, next_event=None, broadcast_open=False)
        finally:
            self._session.unlock_pov()

    def _give_first_blood_reward(self, killer_index: int, deck_pos: int) -> None:
        """See _give_reward_card() — first-blood's give_wood reward."""
        self._give_reward_card("give_wood", killer_index, deck_pos,
                                tts_prefix="First blood", event_label="First Blood Reward")

    def _give_favorite_reward(self, player_index: int, deck_pos: int) -> None:
        """See _give_reward_card() — the Crowd Favorite channel-points
        reward's favorite_player card."""
        self._give_reward_card("favorite_player", player_index, deck_pos,
                                tts_prefix="Crowd favorite", event_label="Crowd Favorite Reward")

    def _past_last_scheduled_card(self) -> bool:
        """True once every card in self._card_schedule has fired (or if it's
        simply empty — before run() builds it, or in a test that never sets
        it — this returns False, not True: "we don't know the schedule yet"
        is not the same claim as "the schedule is exhausted").

        Crowd Favorite is gated on this (2026-09-10) because of the same
        limitation zone_close already lives with: the schedule always ends
        with a zone_close, and that card has no verification at all (see the
        Zone Logic section in CLAUDE.md) — if the match is already down to
        one zone by the time it fires, the game silently rejects it with
        nothing checking or caring, which is fine only because nothing else
        is scheduled afterward to be thrown off by it. Crowd Favorite isn't
        tied to the schedule the way zone_close is, so without this check a
        viewer could still redeem it well after the last card — right when
        the match is likely down to its final zone/players and a POV-switch-
        then-drag is at its least predictable. Once the schedule is
        exhausted, this stops treating "no card is due soon" as "safe to
        fire" — there's no longer a later card to avoid overlapping, just an
        end-of-match state nothing here was built to act into.
        """
        return bool(self._card_schedule) and all(e.done for e in self._card_schedule)

    def try_queue_favorite_reward(self, player_index: int) -> bool:
        """Reserves the Crowd Favorite reward for player_index, if — and
        only if — nothing is already pending, player_index is a real,
        currently-alive card index in this match's roster, and at least one
        'favorite_player' card remains unplayed in the deck. Only one
        redemption is ever allowed pending at a time, by explicit design
        choice — unlike first blood (a true one-time event), a viewer could
        otherwise redeem this repeatedly across a match as long as copies
        remain, but a second redemption while one is already queued is
        rejected here rather than queued behind it; the caller (Twitch bot)
        refunds on a False return.

        Deliberately does NOT require self._player_names[player_index] to be
        non-empty (2026-09-10 fix, found in review) — unlike first blood,
        this flow never matches a name at all; the viewer names the slot
        directly via /pov-style digit input, so a real, alive player whose
        nameplate simply failed to OCR at match start must not be rejected
        just because their name specifically didn't resolve. self._slot_name()
        already falls back to "slot N" for logging/TTS when the name is
        missing, so nothing downstream needs it either.

        Called from the Twitch bot's event loop thread (bot/twitch_bot.py)
        — a different thread from this class's own match thread. Plain
        attribute mutation, no lock, same cross-thread convention as this
        class's other such flags; safe here because Twitch redemptions are
        handled one at a time (no concurrent handler execution) and this
        method's check-then-set happens in one call with no await in
        between.

        Rejects immediately if advanced_cards (config) is off, or once the
        match's card schedule is exhausted (_past_last_scheduled_card(),
        2026-09-10 — see its own docstring) — the caller refunds exactly as
        it would for any other rejection, same as if the deck had no
        favorite_player cards left.
        """
        if not self._advanced_cards_enabled:
            return False
        if self._past_last_scheduled_card():
            return False
        if self._favorite_reward_target is not None:
            return False
        if not (0 <= player_index < len(self._player_alive) and self._player_alive[player_index]):
            return False
        if self._next_available_deck_pos(self._positions_for_card_type("favorite_player")) is None:
            return False
        self._favorite_reward_target = player_index
        logger.info("Crowd Favorite reward queued for %s", self._slot_name(player_index))
        return True

    def _maybe_fire_favorite_reward(self, elapsed: float, card_schedule: list["CardEvent"]) -> None:
        """Called every main-loop iteration (match thread) — the
        Crowd-Favorite counterpart to _maybe_fire_first_blood_reward()
        above, same shape minus the points check (favorite_player costs 0
        director points, so there's nothing to wait on):

        1. Something is actually queued (try_queue_favorite_reward).
        2. The target hasn't died since being queued — same
           self._player_alive check, already refreshed this same iteration
           by _poll_player_bar(). Cancels outright if so.
        3. The card schedule isn't already exhausted
           (_past_last_scheduled_card(), 2026-09-10) — cancels outright if
           so, same as the target dying. Covers the case where this was
           queued just before the last scheduled card fired and is still
           pending once it has.
        4. No real scheduled card due within _BONUS_CARD_TIMING_BUFFER_SECONDS
           — the reward's own drag must never overlap a scheduled card's.

        Once clear, resolves self._favorite_reward_target to None BEFORE
        calling _give_favorite_reward() (which blocks for the drag) so a
        later iteration can't re-enter and double-fire while it's in
        progress. Setting it back to None (rather than a separate
        "resolved" latch like first blood's) is deliberate: a NEW
        redemption should be accept-able again immediately once this one
        settles, since Crowd Favorite is a repeatable reward, not a
        one-time match event.
        """
        if self._favorite_reward_target is None:
            return
        target = self._favorite_reward_target

        if target < len(self._player_alive) and not self._player_alive[target]:
            logger.info(
                "Crowd Favorite reward canceled — %s was eliminated before it could be given",
                self._slot_name(target),
            )
            self._favorite_reward_target = None
            return

        if self._past_last_scheduled_card():
            logger.info(
                "Crowd Favorite reward canceled — the card schedule is already exhausted",
            )
            self._favorite_reward_target = None
            return

        pending_times = [e.trigger_seconds - elapsed for e in card_schedule if not e.done]
        if pending_times and min(pending_times) <= _BONUS_CARD_TIMING_BUFFER_SECONDS:
            return

        deck_pos = self._next_available_deck_pos(self._positions_for_card_type("favorite_player"))
        if deck_pos is None:
            logger.warning("Crowd Favorite reward: no 'favorite_player' card available in the deck — canceling")
            self._favorite_reward_target = None
            return

        self._favorite_reward_target = None
        self._give_favorite_reward(target, deck_pos)

    def _slot_name(self, i: int) -> str:
        if self._player_names and i < len(self._player_names) and self._player_names[i]:
            return self._player_names[i]
        return f"slot {i + 1}"

    def _elapsed_ms(self) -> int:
        if self._match_started_at is None:
            return 0
        return int((time.monotonic() - self._match_started_at) * 1000)

    def _emit(self, kind: str, **fields) -> None:
        """Queue a live event for the ladder (no-op without a lifecycle; never raises)."""
        if self._ds is None:
            return
        try:
            fields.setdefault("elapsed_ms", self._elapsed_ms())
            self._ds.event(kind, **fields)
        except Exception as e:
            logger.debug("live event %s dropped: %s", kind, e)

    # ------------------------------------------------------------------
    # Card schedule
    # ------------------------------------------------------------------

    def _build_card_schedule(self, profile: dict) -> list[CardEvent]:
        card_plays = sorted(profile.get("card_plays", []), key=lambda p: p["play_time_seconds"])

        lead = self._config.get("card_play_lead_time_seconds", 0)

        assigned: set[int] = set()
        events = []
        for play in card_plays:
            card_type = play["card"]
            t = max(0, play["play_time_seconds"] - lead)
            m, s = divmod(play["play_time_seconds"], 60)  # display uses original time

            all_positions = self._positions_for_card_type(card_type)
            deck_pos = next((p for p in all_positions if p not in assigned), None)
            if deck_pos is None:
                logger.warning("No unassigned deck slot for '%s' at %d:%02d — skipping", card_type, m, s)
            else:
                assigned.add(deck_pos)

            drop_target = _CARD_DROP_TARGETS.get(card_type)

            from game.deck_utils import CARD_POINT_COSTS
            events.append(CardEvent(
                name=f"{card_type.replace('_', ' ').title()} at {m}:{s:02d}",
                card_type=card_type,
                trigger_seconds=t,
                play_time_seconds=play["play_time_seconds"],
                deck_position=deck_pos,
                drop_target=drop_target,
                points_cost=CARD_POINT_COSTS.get(card_type),
            ))
        return events

    def _read_points(self, screenshot) -> int | None:
        """
        Read current director points conservatively.
        Pips are always decremented by 1 to guard against a partially-filled pip being
        counted as full. OCR is used for cross-validation: if it agrees with pip-1, we
        log as confirmed; if it's exactly one higher, the last pip is trusted as genuinely
        full. On any other disagreement we take the lower of the two, not pip-1 unconditionally
        — pip reads can be biased either direction (a partially-filled pip undercounts, but a
        single bright background pixel at a pip's sample coordinate overcounts), so only the
        lower reading is guaranteed safe against a false "enough points" pass, whichever source
        it came from.
        Falls back to whichever source is available.

        director_points_use_pips (config, default True) gates the pip path entirely — set to
        False to read OCR alone. Added 2026-09-04: single-pixel brightness sampling at some pip
        coordinates was catching bright background elements and over-reading, causing cards to
        be attempted before enough points actually existed. The pip-reading code and the
        director_points_pips calibration itself are both left in place (same "disconnected, not
        deleted" precedent as zone_close's legacy per-zone logic) in case pip reading is
        revisited later (e.g. multi-pixel voting instead of single-pixel sampling).
        """
        from game.ocr import count_director_point_pips, read_director_points
        pip_count = None
        ocr_count = None

        pips_cfg = _DIRECTOR_POINTS_PIPS if self._config.get("director_points_use_pips", True) else None
        if pips_cfg:
            raw = count_director_point_pips(screenshot, pips_cfg)
            if raw is not None:
                pip_count = max(0, raw - 1)

        ocr_count = read_director_points(screenshot, _DIRECTOR_POINTS_REGION)

        if pip_count is not None:
            if ocr_count is not None:
                if ocr_count == pip_count:
                    logger.debug("Points confirmed: %d (pips and OCR agree)", pip_count)
                elif ocr_count == pip_count + 1:
                    logger.debug("Points: pip-1=%d ocr=%d — OCR confirms last pip full, trusting OCR", pip_count, ocr_count)
                    return ocr_count
                else:
                    safe = min(pip_count, ocr_count)
                    logger.debug(
                        "Points: pip-1=%d ocr=%d — discrepancy, using the lower reading (%d)",
                        pip_count, ocr_count, safe,
                    )
                    return safe
            return pip_count

        return ocr_count

    def _update_points_reading(self, current: Optional[int], context: str) -> Optional[int]:
        """Merge a fresh points read into self._last_confirmed_points with a ratchet-up
        guard: a failed read (None), or one that comes back lower than the last
        confirmed value, is treated as noise (a transient OCR misread) and disregarded
        rather than trusted — the confirmed value only ever moves up from an actual
        higher reading. It never moves down on its own, because points only decrease
        when this bot plays a card, and that path (_fire_card_event) explicitly
        invalidates the confirmed value first so the next read — whatever it is — is
        trusted as the new baseline instead of being compared against a now-stale one.

        Returns the resulting confirmed value (possibly unchanged).
        """
        if current is None:
            logger.debug("Points read failed (%s) — keeping last known value (%s)", context, self._last_confirmed_points)
        elif self._last_confirmed_points is None or current >= self._last_confirmed_points:
            self._last_confirmed_points = current
        else:
            logger.debug(
                "Points read %d (%s) is lower than last known good %d — disregarding as a misread",
                current, context, self._last_confirmed_points,
            )
        return self._last_confirmed_points

    def _debit_points(self, cost: Optional[int]) -> None:
        """A card that just played spent `cost` points — subtract it directly
        from self._last_confirmed_points instead of invalidating the whole
        value and waiting on a fresh OCR read (2026-09-10). Every card's cost
        is already known (CARD_POINT_COSTS / CardEvent.points_cost), so once
        a play is confirmed there's no need to guess at a re-read when plain
        arithmetic gives an exact answer — this keeps a card fired immediately
        after another from blocking on `_wait_for_points()` for a fresh read
        that would just confirm what was already knowable.

        Falls back to invalidating (`None`, forcing the next read to be
        trusted as a fresh baseline — the old behavior) only when the cost
        isn't known (a card_type missing from CARD_POINT_COSTS) or there's no
        confirmed baseline yet to subtract from in the first place. Clamped
        at 0 as a defensive floor — points should never go negative, but a
        stale overestimate subtracting past zero shouldn't produce one.
        """
        if cost is None or self._last_confirmed_points is None:
            self._last_confirmed_points = None
            return
        self._last_confirmed_points = max(0, self._last_confirmed_points - cost)

    def _wait_for_points(self, needed: int, card_name: str,
                         broadcast_open: bool = False, card_label: str = "") -> bool:
        """Block until the director has enough points.

        Never fails open on a bad read — "ready" is only ever declared off
        self._last_confirmed_points (see _update_points_reading), which is fed both by
        this loop's own reads and by the main loop's continuous background sampling, so
        a card usually already has a recent confirmed value to check against instead of
        needing a fresh read at the exact moment it fires. Found live: a single failed
        read used to make this return immediately as if points were sufficient, and the
        card would then be attempted without actually having enough.

        If broadcast_open and we actually need to wait, announces 'Waiting on points for X'
        async and immediately closes the broadcast so the 90s cooldown starts ticking.
        Returns True if the broadcast was closed (caller should try to reopen when ready).
        """
        if needed == 0:
            return False
        from game.screen_detection import take_screenshot
        closed_broadcast = False
        while not self._stop.is_set():
            current = self._read_points(take_screenshot())
            confirmed = self._update_points_reading(current, card_name)

            if confirmed is not None and confirmed >= needed:
                logger.info("Points ready for '%s': %d/%d", card_name, confirmed, needed)
                return closed_broadcast

            logger.info("Waiting for points: have %s, need %d for '%s'", confirmed, needed, card_name)
            if not closed_broadcast and card_label:
                from game import tts as _tts
                _tts.speak_cable(f"Waiting on points for {card_label}")
                if broadcast_open:
                    _tts.queue_close_broadcast()
                closed_broadcast = True
            self._stop.wait(2.0)
        return closed_broadcast

    def _fire_card_event(self, event: CardEvent, all_events: list[CardEvent]):
        event.done = True
        logger.info("Firing card event: %s", event.name)
        # Player-targeted cards pick a screen coordinate, not a slot, so the
        # event names the card only; the target slot is unknown to the bot.
        self._emit("card_play", card=event.card_type, name=event.name)

        from game import tts
        next_event = next((e for e in all_events if not e.done), None)
        card_label = tts.card_announce(event.card_type)

        if event.card_type == "zone_close":
            from game.deck_utils import CARD_POINT_COSTS
            cost = CARD_POINT_COSTS.get("zone_close", 0)
            broadcast_open = tts.try_open_broadcast()
            if cost:
                if self._wait_for_points(cost, "zone_close",
                                         broadcast_open=broadcast_open, card_label=card_label):
                    broadcast_open = tts.try_open_broadcast()
            if self._stop.is_set():
                return
            zone_success = self._attempt_zone_close()
            if not self._stop.is_set():
                if zone_success:
                    ann = self._next_card_announce(next_event)
                    if ann:
                        tts.speak_cable(ann)
                if broadcast_open:
                    tts.queue_close_broadcast()
        elif event.deck_position is None:
            logger.warning("Card event '%s' has no deck position assigned — skipping", event.name)
        elif event.card_type in _ZONE_TARGETED_CARDS:
            import random
            zone_id = random.choice(list(_ZONE_DROP_COORDINATES.keys()))
            target = _ZONE_DROP_COORDINATES[zone_id]
            logger.info("Zone-targeted card '%s' → zone %s at %s", event.name, zone_id, target)
            broadcast_open = tts.try_open_broadcast()
            if event.points_cost is not None:
                if self._wait_for_points(event.points_cost, event.name,
                                         broadcast_open=broadcast_open, card_label=card_label):
                    broadcast_open = tts.try_open_broadcast()
            if not self._stop.is_set():
                tts.speak_cable(f"Deploying {card_label}")
                self._play_tray_card(event, target, card_label, next_event, broadcast_open)
        elif event.card_type in _PLAYER_TARGETED_CARDS:
            player_coords = self._config.get("player_target_coordinates") or []
            if not player_coords:
                logger.warning("Card event '%s': player_target_coordinates not calibrated — skipping", event.name)
            else:
                import random
                target = tuple(random.choice(player_coords))
                logger.info("Player-targeted card '%s' → player slot at %s", event.name, target)
                broadcast_open = tts.try_open_broadcast()
                if event.points_cost is not None:
                    if self._wait_for_points(event.points_cost, event.name,
                                             broadcast_open=broadcast_open, card_label=card_label):
                        broadcast_open = tts.try_open_broadcast()
                if not self._stop.is_set():
                    tts.speak_cable(f"Deploying {card_label}")
                    self._play_tray_card(event, target, card_label, next_event, broadcast_open)
        elif not event.drop_target:
            logger.warning("Card event '%s' has no drop_target configured — skipping", event.name)
        else:
            broadcast_open = tts.try_open_broadcast()
            if event.points_cost is not None:
                if self._wait_for_points(event.points_cost, event.name,
                                         broadcast_open=broadcast_open, card_label=card_label):
                    broadcast_open = tts.try_open_broadcast()
            if not self._stop.is_set():
                tts.speak_cable(f"Deploying {card_label}")
                self._play_tray_card(event, event.drop_target, card_label, next_event, broadcast_open)

        next_label = next_event.name if next_event else "Match end polling"
        self._update(f"Played {event.name}", next_label)

    def _play_tray_card(
        self,
        event: CardEvent,
        target: tuple[int, int],
        card_label: str,
        next_event: Optional[CardEvent],
        broadcast_open: bool,
    ) -> None:
        """
        Shared play/verify/announce logic for zone-targeted, player-targeted, and plain
        (fixed drop_target) cards — the only difference between those three cases is
        how `target` was computed by the caller.

        Three modes, checked in order:
        - self._bypass (ahk_bypass_mode): dry run, logs and waits for Enter, never
          touches the game.
        - not self._verify_plays (verify_card_plays: false): plays once and trusts it
          worked — no before/after screenshots, no retry loop.
        - default: the original before/after tray-pixel verify with up to 2 attempts.
        """
        from game import tts
        from game.card_actions import play_card

        slot_coord = self._deck_pos_to_screen(event.deck_position)
        played = False

        if self._bypass:
            play_card(slot_coordinate=slot_coord, target_coordinate=target,
                      card_name=event.name, bypass_mode=True)
            self._deck_played.add(event.deck_position)
            played = True
        elif not self._verify_plays:
            play_card(slot_coordinate=slot_coord, target_coordinate=target, card_name=event.name)
            self._deck_played.add(event.deck_position)
            played = True
        else:
            from game.card_actions import shift_down, shift_up
            from game.screen_detection import take_screenshot, save_error_screenshot
            # Hold shift once for the entire attempt block:
            # before-screenshot → play → after-screenshot → [retry plays] → release.
            shift_down()
            time.sleep(0.25)
            before = take_screenshot()
            for attempt in range(1, 3):
                if attempt > 1:
                    tts.speak_cable("Retrying")
                play_card(slot_coordinate=slot_coord, target_coordinate=target,
                          card_name=event.name, keep_shift=True)
                time.sleep(0.4)
                after = take_screenshot()
                if self._verify_card_removed(slot_coord, before, after):
                    self._deck_played.add(event.deck_position)
                    played = True
                    break
                logger.warning("Card '%s' not verified in tray (attempt %d/2)", event.name, attempt)
                if self._stop.is_set():
                    break
            if not played and not self._stop.is_set():
                logger.error("Card '%s' failed to play after 2 attempts", event.name)
                save_error_screenshot(f"card_play_failed_{event.name.replace(' ', '_').replace(':', '_')}")
            shift_up()

        if not self._stop.is_set():
            if played:
                # The card actually played and spent event.points_cost points —
                # debit that directly rather than invalidating the confirmed
                # reading and waiting on a fresh OCR read (see _debit_points).
                self._debit_points(event.points_cost)
                ann = self._next_card_announce(next_event)
                if ann:
                    tts.speak_cable(ann)
            else:
                # Nothing was spent — the known points value is left untouched.
                tts.speak_cable(f"Sorry, failed to deploy {card_label}")
            if broadcast_open:
                tts.queue_close_broadcast()

    def _build_tts_phrases(self, card_schedule: list[CardEvent]) -> list[str]:
        """Return every TTS phrase this match might speak, for pre-caching."""
        from game import tts as _tts
        phrases = [
            "Match is starting. Good luck.",
            "Retrying",
            "Match over.",
            "Deploying Zone Close",
            "Waiting on points for Zone Close",
            "Sorry, no zones available to close",
            "Sorry, failed to close a zone",
        ]
        for i in range(1, 8):
            phrases.append(f"Closing zone {i}")

        for i, event in enumerate(card_schedule):
            label = _tts.card_announce(event.card_type)
            if event.card_type != "zone_close":
                phrases.append(f"Deploying {label}")
                phrases.append(f"Waiting on points for {label}")
                phrases.append(f"Sorry, failed to deploy {label}")
            next_ev = card_schedule[i + 1] if i + 1 < len(card_schedule) else None
            if next_ev:
                next_label = _tts.card_announce(next_ev.card_type)
                m, s = divmod(next_ev.play_time_seconds, 60)
                time_str = str(m) if s == 0 else f"{m} {s}"
                phrases.append(f"Next card is {next_label} at {time_str}")

        return phrases

    def _build_lineup_text(self, card_schedule: list[CardEvent]) -> str:
        """Build the full card lineup as a single TTS-friendly string."""
        from game import tts as _tts
        parts = []
        for event in sorted(card_schedule, key=lambda e: e.play_time_seconds):
            label = _tts.card_announce(event.card_type)
            m, s = divmod(event.play_time_seconds, 60)
            time_str = str(m) if s == 0 else f"{m} {s}"
            parts.append(f"{label} at {time_str}")
        return ", ".join(parts)

    def _announce_card_lineup(self, text: str):
        """Announce the card lineup at match start. Plays over proximity audio regardless;
        also broadcasts globally if the cooldown allows."""
        from game import tts
        if not text or not tts.is_enabled():
            return
        broadcast_open = tts.try_open_broadcast()
        tts.speak_cable(text)
        if broadcast_open:
            tts.queue_close_broadcast()

    def _next_card_announce(self, next_event: Optional[CardEvent]) -> Optional[str]:
        """Format a 'Next card is X at Y' announcement for TTS."""
        if next_event is None:
            return None
        from game import tts
        m, s = divmod(next_event.play_time_seconds, 60)
        name = tts.card_announce(next_event.card_type)
        time_str = str(m) if s == 0 else f"{m} {s}"
        return f"Next card is {name} at {time_str}"

    # ------------------------------------------------------------------
    # Zone closes
    # ------------------------------------------------------------------

    def _attempt_zone_close(self) -> bool:
        """
        Drag the zone_close card to a single static drop area (zone_close_auto_drop_target
        in config.json) that the game itself resolves to a random valid zone — the bot no
        longer reads the zone map or picks a zone itself. Always "succeeds" once the drag
        completes: there is no tray-pixel verification here regardless of verify_card_plays,
        since the drop area handles zone validity on the game's side. Returns False only if
        no zone_close card remains in the deck or the drop target isn't calibrated yet.

        The old per-zone selection path (zone-map reading, ZoneState tracking, the
        pluggable zone_selection_strategy system in zones/) is left in place, disconnected,
        as _attempt_zone_close_legacy() below — not called from here, kept for reference/
        rollback in case the new drop area needs to be abandoned.
        """
        from game import tts
        from game.card_actions import play_card

        deck_pos = self._next_available_deck_pos(self._positions_for_card_type("zone_close"))
        if deck_pos is None:
            logger.info("Zone close: no ZoneClose cards remaining")
            return False

        target = _ZONE_CLOSE_DROP_TARGET

        tts.speak_cable("Deploying Zone Close")
        slot_coord = self._deck_pos_to_screen(deck_pos)

        play_card(
            slot_coordinate=slot_coord,
            target_coordinate=tuple(target),
            card_name="zone_close",
            bypass_mode=self._bypass,
        )
        self._deck_played.add(deck_pos)
        # No verification step here (see docstring above) — the play is always
        # assumed to have happened, so always debit its cost directly, same
        # reasoning as _play_tray_card()'s _debit_points call.
        from game.deck_utils import CARD_POINT_COSTS
        self._debit_points(CARD_POINT_COSTS.get("zone_close"))
        # No "Closing a zone" TTS confirmation here (2026-09-06) — this drag is never
        # verified regardless of verify_card_plays (the drop area handles zone validity
        # on the game's side, see the docstring above), so that line always spoke as if
        # confirmed even though nothing was actually checked. "Deploying Zone Close"
        # above still announces the attempt; nothing here claims it worked.
        self._update("Closed a zone", "Continue match")
        return True

    def _attempt_zone_close_legacy(self) -> bool:
        """
        Every 30s: grab the zone_close card to reveal the big zone map, read zone states
        from multiple sample points per tile, pick the best zone, then play or cancel.
        In bypass mode, uses cached zone states (no grab possible without a real game).
        Returns True if a zone was successfully closed, False otherwise.

        Superseded by _attempt_zone_close() above (2026-08-30) — kept, not deleted, in
        case the new static drop-area approach needs to be rolled back. Not called from
        anywhere; _fire_card_event() calls _attempt_zone_close() instead.
        """
        from game import tts

        deck_pos = self._next_available_deck_pos(self._positions_for_card_type("zone_close"))
        if deck_pos is None:
            logger.info("Zone close: no ZoneClose cards remaining")
            return False

        tts.speak_cable("Deploying Zone Close")

        slot_coord = self._deck_pos_to_screen(deck_pos)

        if self._bypass:
            return self._attempt_zone_close_bypass(slot_coord, deck_pos)

        import random
        import pyautogui as _pag
        from game.card_actions import grab_card, complete_drag, shift_down, shift_up
        from game.screen_detection import take_screenshot, save_error_screenshot

        # Hold shift and take the before-reference (tray visible, card in slot),
        # then immediately grab the card — zone map appears on mouseDown.
        # Shift stays held from this point through all drags and retries.
        shift_down()
        time.sleep(0.25)
        before_shift = take_screenshot()
        grab_card(slot_coord, shift_already_held=True)
        time.sleep(0.35)
        self._update_zone_states_from_screenshot(take_screenshot())

        valid = valid_closeable_zones(self._zone_states)
        if not valid:
            logger.info("Zone close: no valid zones — releasing card")
            _pag.mouseUp()
            shift_up()
            tts.speak_cable("Sorry, no zones available to close")
            return False

        zones_to_try = list(valid_closeable_zones(self._zone_states))
        random.shuffle(zones_to_try)
        zone_drop_coords = _ZONE_DROP_COORDINATES

        if not self._verify_plays:
            # verify_card_plays: false — single attempt, no pixel-delta check, no
            # retry across zones. Picks one valid zone (still via the per-zone
            # zone_drop_coordinates) and trusts the drag worked.
            for zone_id in zones_to_try:
                raw_target = zone_drop_coords.get(zone_id)
                if not raw_target:
                    continue
                complete_drag(target_coordinate=tuple(raw_target), card_name=f"close_zone_{zone_id}")
                self._zone_states[zone_id] = ZoneState.CLOSING
                self._deck_played.add(deck_pos)
                tts.speak_cable(f"Closing zone {zone_id}")
                self._update(f"Closed zone {zone_id}", "Continue match")
                return True
            logger.warning("Zone close: no candidate zone had a calibrated drop coordinate")
            _pag.mouseUp()
            shift_up()
            tts.speak_cable("Sorry, failed to close a zone")
            return False

        center_x = _CARD_TRAY_CENTER_X
        card_width = _CARD_TRAY_CARD_WIDTH
        card_y = _CARD_TRAY_CARD_Y
        tray_configured = True  # tray layout is now a hardcoded constant, always available

        for i, zone_id in enumerate(zones_to_try):
            if self._stop.is_set():
                _pag.mouseUp()
                break

            raw_target = zone_drop_coords.get(zone_id)
            if not raw_target:
                logger.warning("Zone %d skipped — drop coordinate not calibrated", zone_id)
                continue  # card still grabbed; drag to next valid zone

            # Drag to the zone; keep_shift=True so shift stays held for verification
            complete_drag(target_coordinate=tuple(raw_target), card_name=f"close_zone_{zone_id}",
                          keep_shift=True)
            time.sleep(0.8)  # zone map animation clears
            after_shift = take_screenshot()  # shift still held — tray visible

            verified = True
            if tray_configured:
                slot_x, slot_y = slot_coord
                bgr_before = before_shift[slot_y, slot_x]
                bgr_after = after_shift[slot_y, slot_x]
                delta = int(sum(abs(int(a) - int(b)) for a, b in zip(bgr_before, bgr_after)))
                logger.info(
                    "Zone close verify: slot=(%d,%d) before=%s after=%s delta=%d — %s",
                    slot_x, slot_y,
                    tuple(int(v) for v in bgr_before),
                    tuple(int(v) for v in bgr_after),
                    delta,
                    "verified" if delta > 80 else "unchanged",
                )
                verified = delta > 80

            if verified:
                self._zone_states[zone_id] = ZoneState.CLOSING
                self._deck_played.add(deck_pos)
                tts.speak_cable(f"Closing zone {zone_id}")
                self._update(f"Closed zone {zone_id}", "Continue match")
                shift_up()
                return True

            logger.warning("Zone %d close not verified — trying next zone", zone_id)

            if self._stop.is_set() or i == len(zones_to_try) - 1:
                break

            # Shift still held — re-grab for the next retry without releasing
            time.sleep(0.2)
            grab_card(slot_coord, shift_already_held=True)
            time.sleep(0.2)

        logger.error("Zone close failed for all %d candidate zones", len(zones_to_try))
        _pag.mouseUp()  # ensure card is released if loop exited via skip/stop
        save_error_screenshot("zone_close_failed")  # shift still held = tray visible
        shift_up()
        tts.speak_cable("Sorry, failed to close a zone")
        return False

    def _attempt_zone_close_bypass(self, slot_coord: tuple, deck_pos: int) -> bool:
        """Bypass-mode zone close: use cached zone states, log the action, pause."""
        from game.card_actions import play_card
        from game import tts

        valid = valid_closeable_zones(self._zone_states)
        if not valid:
            logger.info("[BYPASS] Zone close: no valid zones")
            return False

        zone_id = self._strategy.select_zone(valid, self._zone_states)
        if zone_id is None:
            return False

        raw_target = _ZONE_DROP_COORDINATES.get(zone_id)
        if not raw_target:
            logger.warning("[BYPASS] Zone %d close skipped — drop coordinate not calibrated", zone_id)
            return False

        play_card(
            slot_coordinate=slot_coord,
            target_coordinate=tuple(raw_target),
            card_name=f"close_zone_{zone_id}",
            bypass_mode=True,
        )
        self._zone_states[zone_id] = ZoneState.CLOSING
        self._deck_played.add(deck_pos)
        tts.speak_cable(f"Closing zone {zone_id}")
        self._update(f"Closed zone {zone_id}", "Continue match")
        return True

    def _update_zone_states_from_screenshot(self, screenshot):
        """Vote across multiple sample points per zone tile to determine each zone's state."""
        thresholds = self._config.get("zone_color_thresholds", {})
        map_points = _ZONE_MAP_SAMPLE_POINTS

        if not all(thresholds.get(k) for k in ("open", "closed", "closing")):
            logger.debug("Zone map detection skipped — zone_color_thresholds not calibrated")
            return

        for zone_id, points in map_points.items():
            if not points:
                continue
            if self._zone_states.get(zone_id) == ZoneState.CLOSED:
                continue
            state = self._vote_zone_state(screenshot, points, thresholds)
            if state != self._zone_states.get(zone_id):
                logger.info("Zone %d: %s → %s", zone_id, self._zone_states.get(zone_id), state)
            self._zone_states[zone_id] = state

    # Threshold for _verify_card_removed's single-pixel delta check. Lowered from 40
    # (2026-08-28): log analysis across ~130k lines / months of matches showed a
    # recurring false-negative band at delta 17-39 — always on the first tray-card
    # play of the match or the first play right after a new card naturally unlocks
    # (Electromania at 2:30, Beach Party at 4:00, Telepathy at 4:30/10:00), where the
    # tray recenters and the slot pixel shifts to a different-but-similar card color
    # instead of going stark. A missed verification here never gets the card's
    # deck_position added to self._deck_played, so every later _deck_pos_to_screen()
    # call computes one card-width off — the "tray out of sync" failure cascade
    # (next card's play AND its verification both land on the wrong slot, usually
    # reading near-zero delta since nothing meaningful is at that wrong pixel).
    # Genuine non-plays across the same log are all delta <= 15 (mostly 0-3, one
    # ambiguous 10); genuine clean detections are all delta >= 41. 16 sits in the
    # untouched gap between the two, so this only reclassifies the confirmed
    # false-negative band and leaves every previously-correct call unchanged.
    _TRAY_VERIFY_DELTA_THRESHOLD = 16

    def _verify_card_removed(self, slot_coord: tuple, before_screenshot, after_screenshot) -> bool:
        """
        Compare the pixel at slot_coord between two shift-held screenshots.
        Before: the specific card should be visible at that position.
        After play: the slot is empty or re-centered to a different card — clear delta.
        After failed play: same card returns — delta near zero.
        """
        x_check, y_check = slot_coord
        bgr_before = before_screenshot[y_check, x_check]
        bgr_after = after_screenshot[y_check, x_check]
        delta = int(sum(abs(int(a) - int(b)) for a, b in zip(bgr_before, bgr_after)))

        threshold = self._TRAY_VERIFY_DELTA_THRESHOLD
        logger.info(
            "Tray verify: slot=(%d,%d) before=%s after=%s delta=%d — %s",
            x_check, y_check,
            tuple(int(v) for v in bgr_before),
            tuple(int(v) for v in bgr_after),
            delta,
            "verified" if delta > threshold else "unchanged",
        )
        return delta > threshold

    def _positions_for_card_type(self, card_type: str) -> list[int]:
        """Return all visual deck positions that contain cards of the given type."""
        return [i for i, c in enumerate(self._deck_layout) if c == card_type]

    def _deck_pos_to_screen(self, deck_pos: int) -> tuple[int, int]:
        """Convert a visual deck position to current screen (x, y) in 1920×1080."""
        remaining = [i for i in range(len(self._deck_layout)) if i not in self._deck_played]
        visual_index = remaining.index(deck_pos)
        n = len(remaining)
        center_x = _CARD_TRAY_CENTER_X
        card_width = _CARD_TRAY_CARD_WIDTH
        card_y = _CARD_TRAY_CARD_Y
        first_x = center_x - (n - 1) / 2 * card_width
        x = round(first_x + visual_index * card_width)
        return (x, card_y)

    def _next_available_deck_pos(self, positions: list[int]) -> Optional[int]:
        """Return the first unplayed deck position from the given list, or None."""
        for pos in positions:
            if pos not in self._deck_played:
                return pos
        return None

    def _vote_zone_state(self, screenshot, points: list, thresholds: dict) -> str:
        """
        Sample each point in the list and return the majority zone state.
        Minority points covered by player icons won't flip the result.
        """
        from game.screen_detection import color_within_threshold

        closed_thresh = tuple(thresholds["closed"])
        closing_thresh = tuple(thresholds["closing"])
        counts = {ZoneState.CLOSED: 0, ZoneState.CLOSING: 0, ZoneState.OPEN: 0}

        for x, y in points:
            bgr = screenshot[y, x]
            color = (int(bgr[2]), int(bgr[1]), int(bgr[0]))
            if color_within_threshold(color, closed_thresh):
                counts[ZoneState.CLOSED] += 1
            elif color_within_threshold(color, closing_thresh):
                counts[ZoneState.CLOSING] += 1
            else:
                counts[ZoneState.OPEN] += 1

        return max(counts, key=counts.get)

    # ------------------------------------------------------------------
    # Match end detection
    # ------------------------------------------------------------------

    def _match_has_ended(self) -> bool:
        from game.screen_detection import poll_for_match_end
        if not poll_for_match_end(badge_template_path="templates/placement_badge.png", threshold=0.88):
            return False
        # Confirm with a second check 2 seconds later — rules out transient HUD false positives
        self._stop.wait(2.0)
        return poll_for_match_end(badge_template_path="templates/placement_badge.png", threshold=0.88)

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _capture_results(self) -> str:
        import cv2
        import datetime
        from pathlib import Path
        from game.screen_detection import take_screenshot

        screenshot = take_screenshot()
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out_dir = Path("screenshots/results")
        out_dir.mkdir(parents=True, exist_ok=True)
        path = str(out_dir / f"results_{ts}.png")
        cv2.imwrite(path, screenshot)
        logger.info("Results screenshot saved: %s", path)
        return path

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _press(self, key: str):
        from game.card_actions import press_key
        press_key(key, bypass_mode=self._bypass)

    def _update(self, last: str, next_: str):
        self._on_action_update(last, next_)
        logger.info("Match action — last: %s | next: %s", last, next_)
        self._emit("status", last=last, next=next_)

    def _save_error_screenshot(self, label: str):
        from game.screen_detection import save_error_screenshot
        save_error_screenshot(label)
