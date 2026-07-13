import logging
import threading
import time
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
    ):
        self._config = config
        self._session = session
        self._on_action_update = on_action_update
        self._stop = threading.Event()
        self._skip_start = skip_start
        self._profile = profile
        self._bypass = config.get("ahk_bypass_mode", False)
        self._strategy = get_strategy(config.get("zone_selection_strategy", "weighted_outer"))
        self._zone_states: dict[int, str] = {i: ZoneState.OPEN for i in range(1, 8)}
        self._deck_played: set[int] = set()
        self._player_slot_xs: list[int] = []
        self._player_names: list[str] = []
        self._player_alive: list[bool] = []
        self._first_blood_logged: bool = False
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

        # Capture player roster from lobby nameplates before the match countdown begins.
        # The bar layout is identical in the lobby and in-match; this is the most stable
        # moment to read names and establish slot order.
        self._init_player_bar()

        from game import tts

        if self._skip_start:
            # Game auto-started — B press is skipped but the 5s in-game countdown still runs
            self._update("Waiting for match countdown", "Starting card timers")
            tts.speak("Match is starting. Good luck.", broadcast=False)
            if self._stop.wait(5):
                return "Match aborted during countdown."
        else:
            # Press B to start the match
            self._update("Pressing B to start", "Waiting for countdown")
            self._press("b")
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

        card_schedule = self._build_card_schedule(_profile)
        phrases = self._build_tts_phrases(card_schedule)
        profile_announce = f"Using profile: {_profile['display_name']}"
        phrases.append(profile_announce)
        tts.precache_async(phrases)
        if not self._stop.wait(5):
            self._announce_card_lineup(profile_announce)
        poll_interval = self._config.get("screen_poll_interval_seconds", 12)

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

                # Poll for match end (placement badge on screen)
                if self._match_has_ended():
                    logger.info("Match end detected at %.1fs elapsed", elapsed)
                    break

                # Poll player bar for first blood
                if not self._first_blood_logged and self._player_slot_xs:
                    from game.screen_detection import take_screenshot as _take_ss
                    self._poll_player_bar(_take_ss())

                # Sleep until the next card trigger, but no longer than poll_interval
                now = time.monotonic()
                now_elapsed = now - start_time
                pending_times = [
                    e.trigger_seconds - now_elapsed
                    for e in card_schedule if not e.done
                ]
                sleep_time = min(poll_interval, min(pending_times)) if pending_times else poll_interval
                self._stop.wait(max(0.1, sleep_time))
        finally:
            recording_path = recorder.stop()

        if self._stop.is_set():
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

    def _init_player_bar(self):
        """
        Snapshot the player bar from the lobby screen before the match starts.
        Detects slot count, x-positions, and OCRs player names in slot order.
        Called once; results are reused for first-blood tracking during the match.
        """
        from game.screen_detection import take_screenshot, detect_player_slot_xs
        from game.ocr import ocr_player_names

        screenshot = take_screenshot()
        self._player_slot_xs = detect_player_slot_xs(screenshot, self._config)
        if not self._player_slot_xs:
            logger.warning(
                "Player bar snapshot: no slots detected — player tracking disabled. "
                "Set player_count in config if auto-detect fails."
            )
            return

        self._player_names = ocr_player_names(screenshot, self._player_slot_xs, self._config)
        self._player_alive = [True] * len(self._player_slot_xs)

        logger.info("Player bar snapshot — %d players:", len(self._player_slot_xs))
        for i, (name, x) in enumerate(zip(self._player_names, self._player_slot_xs)):
            logger.info("  slot %d  x=%-4d  %s", i + 1, x, name or "(unread)")

    def _poll_player_bar(self, screenshot):
        """
        Check alive/eliminated status for each player slot.
        On the first death (first blood), logs the victim and attempts to OCR
        the kill notification text to identify the killer.
        """
        if not self._player_slot_xs or self._first_blood_logged:
            return

        from game.screen_detection import sample_player_alive

        new_alive = [
            sample_player_alive(screenshot, x, self._config)
            for x in self._player_slot_xs
        ]

        prev_dead = sum(1 for a in self._player_alive if not a)
        curr_dead = sum(1 for a in new_alive if not a)

        if prev_dead == 0 and curr_dead >= 1:
            newly_dead = [
                i for i, (was, now) in enumerate(zip(self._player_alive, new_alive))
                if was and not now
            ]
            victim_name = (
                self._player_names[newly_dead[0]]
                if self._player_names and newly_dead
                else f"slot {newly_dead[0] if newly_dead else '?'}"
            )
            from game.screen_detection import take_screenshot as _take_ss
            from game.ocr import ocr_kill_notification
            notif = ocr_kill_notification(_take_ss(), self._config)
            logger.info(
                "FIRST BLOOD — victim: %s | kill notification: %r",
                victim_name, notif,
            )
            self._first_blood_logged = True

        self._player_alive = new_alive

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

            card_cfg = self._config.get("cards", {}).get(card_type, {})
            raw_target = card_cfg.get("drop_target")
            drop_target = tuple(raw_target) if raw_target else None

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
        log as confirmed; otherwise pip-1 still wins as the safer value.
        Falls back to whichever source is available.
        """
        from game.ocr import count_director_point_pips, read_director_points
        pip_count = None
        ocr_count = None

        pips_cfg = self._config.get("director_points_pips")
        if pips_cfg:
            raw = count_director_point_pips(screenshot, pips_cfg)
            if raw is not None:
                pip_count = max(0, raw - 1)

        region = self._config.get("director_points_region")
        if region:
            ocr_count = read_director_points(screenshot, tuple(region))

        if pip_count is not None:
            if ocr_count is not None:
                if ocr_count == pip_count:
                    logger.debug("Points confirmed: %d (pips and OCR agree)", pip_count)
                elif ocr_count == pip_count + 1:
                    logger.debug("Points: pip-1=%d ocr=%d — OCR confirms last pip full, trusting OCR", pip_count, ocr_count)
                    return ocr_count
                else:
                    logger.debug("Points: pip-1=%d ocr=%d — discrepancy, using pip-1", pip_count, ocr_count)
            return pip_count

        return ocr_count

    def _wait_for_points(self, needed: int, card_name: str,
                         broadcast_open: bool = False, card_label: str = "") -> bool:
        """Block until the director has enough points. No-op if neither pip nor OCR config is set.

        If broadcast_open and we actually need to wait, announces 'Waiting on points for X'
        async and immediately closes the broadcast so the 90s cooldown starts ticking.
        Returns True if the broadcast was closed (caller should try to reopen when ready).
        """
        if needed == 0:
            return False
        if not self._config.get("director_points_pips") and not self._config.get("director_points_region"):
            return False
        from game.screen_detection import take_screenshot
        closed_broadcast = False
        while not self._stop.is_set():
            current = self._read_points(take_screenshot())
            if current is None:
                logger.warning("Points read failed for '%s' — proceeding anyway", card_name)
                return closed_broadcast
            if current >= needed:
                logger.info("Points ready for '%s': %d/%d", card_name, current, needed)
                return closed_broadcast
            logger.info("Waiting for points: have %d, need %d for '%s'", current, needed, card_name)
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
            zone_drop_coords = self._config.get("zone_drop_coordinates", {})
            available = {k: v for k, v in zone_drop_coords.items() if v}
            if not available:
                logger.warning("Card event '%s': no zone_drop_coordinates calibrated — skipping", event.name)
            else:
                zone_id = random.choice(list(available.keys()))
                target = tuple(available[zone_id])
                logger.info("Zone-targeted card '%s' → zone %s at %s", event.name, zone_id, target)
                broadcast_open = tts.try_open_broadcast()
                if event.points_cost is not None:
                    if self._wait_for_points(event.points_cost, event.name,
                                             broadcast_open=broadcast_open, card_label=card_label):
                        broadcast_open = tts.try_open_broadcast()
                if not self._stop.is_set():
                    tts.speak_cable(f"Deploying {card_label}")
                    slot_coord = self._deck_pos_to_screen(event.deck_position)
                    from game.card_actions import play_card
                    played = False
                    if self._bypass:
                        play_card(slot_coordinate=slot_coord, target_coordinate=target,
                                  card_name=event.name, bypass_mode=True)
                        self._deck_played.add(event.deck_position)
                        played = True
                    else:
                        from game.card_actions import shift_down, shift_up
                        from game.screen_detection import take_screenshot, save_error_screenshot
                        tray_configured = all([
                            self._config.get("card_tray_center_x"),
                            self._config.get("card_tray_card_width"),
                            self._config.get("card_tray_card_y"),
                        ])
                        if tray_configured:
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
                        else:
                            if play_card(slot_coordinate=slot_coord, target_coordinate=target,
                                         card_name=event.name):
                                self._deck_played.add(event.deck_position)
                                played = True
                    if not self._stop.is_set():
                        if played:
                            ann = self._next_card_announce(next_event)
                            if ann:
                                tts.speak_cable(ann)
                        else:
                            tts.speak_cable(f"Sorry, failed to deploy {card_label}")
                        if broadcast_open:
                            tts.queue_close_broadcast()
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
                    slot_coord = self._deck_pos_to_screen(event.deck_position)
                    from game.card_actions import play_card
                    played = False
                    if self._bypass:
                        play_card(slot_coordinate=slot_coord, target_coordinate=target,
                                  card_name=event.name, bypass_mode=True)
                        self._deck_played.add(event.deck_position)
                        played = True
                    else:
                        from game.card_actions import shift_down, shift_up
                        from game.screen_detection import take_screenshot, save_error_screenshot
                        tray_configured = all([
                            self._config.get("card_tray_center_x"),
                            self._config.get("card_tray_card_width"),
                            self._config.get("card_tray_card_y"),
                        ])
                        if tray_configured:
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
                        else:
                            if play_card(slot_coordinate=slot_coord, target_coordinate=target,
                                         card_name=event.name):
                                self._deck_played.add(event.deck_position)
                                played = True
                    if not self._stop.is_set():
                        if played:
                            ann = self._next_card_announce(next_event)
                            if ann:
                                tts.speak_cable(ann)
                        else:
                            tts.speak_cable(f"Sorry, failed to deploy {card_label}")
                        if broadcast_open:
                            tts.queue_close_broadcast()
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
                slot_coord = self._deck_pos_to_screen(event.deck_position)
                from game.card_actions import play_card
                played = False
                if self._bypass:
                    play_card(slot_coordinate=slot_coord, target_coordinate=event.drop_target,
                              card_name=event.name, bypass_mode=True)
                    self._deck_played.add(event.deck_position)
                    played = True
                else:
                    from game.card_actions import play_card, shift_down, shift_up
                    from game.screen_detection import take_screenshot, save_error_screenshot
                    tray_configured = all([
                        self._config.get("card_tray_center_x"),
                        self._config.get("card_tray_card_width"),
                        self._config.get("card_tray_card_y"),
                    ])
                    if tray_configured:
                        # Hold shift once for the entire attempt block:
                        # before-screenshot → play → after-screenshot → [retry plays] → release.
                        shift_down()
                        time.sleep(0.25)
                        before = take_screenshot()
                        for attempt in range(1, 3):
                            if attempt > 1:
                                tts.speak_cable("Retrying")
                            play_card(slot_coordinate=slot_coord, target_coordinate=event.drop_target,
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
                    else:
                        if play_card(slot_coordinate=slot_coord, target_coordinate=event.drop_target,
                                     card_name=event.name):
                            self._deck_played.add(event.deck_position)
                            played = True

                if not self._stop.is_set():
                    if played:
                        ann = self._next_card_announce(next_event)
                        if ann:
                            tts.speak_cable(ann)
                    else:
                        tts.speak_cable(f"Sorry, failed to deploy {card_label}")
                    if broadcast_open:
                        tts.queue_close_broadcast()

        next_label = next_event.name if next_event else "Match end polling"
        self._update(f"Played {event.name}", next_label)

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
        Every 30s: grab the zone_close card to reveal the big zone map, read zone states
        from multiple sample points per tile, pick the best zone, then play or cancel.
        In bypass mode, uses cached zone states (no grab possible without a real game).
        Returns True if a zone was successfully closed, False otherwise.
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
        zone_drop_coords = self._config.get("zone_drop_coordinates", {})

        center_x = self._config.get("card_tray_center_x")
        card_width = self._config.get("card_tray_card_width")
        card_y = self._config.get("card_tray_card_y")
        tray_configured = all([center_x, card_width, card_y])

        for i, zone_id in enumerate(zones_to_try):
            if self._stop.is_set():
                _pag.mouseUp()
                break

            raw_target = zone_drop_coords.get(str(zone_id))
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

        zone_drop_coords = self._config.get("zone_drop_coordinates", {})
        raw_target = zone_drop_coords.get(str(zone_id))
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
        map_points = self._config.get("zone_map_sample_points", {})

        if not map_points or not all(thresholds.get(k) for k in ("open", "closed", "closing")):
            logger.debug("Zone map detection skipped — zone_map_sample_points or thresholds not calibrated")
            return

        for zone_id_str, points in map_points.items():
            if not points:
                continue
            zone_id = int(zone_id_str)
            if self._zone_states.get(zone_id) == ZoneState.CLOSED:
                continue
            state = self._vote_zone_state(screenshot, points, thresholds)
            if state != self._zone_states.get(zone_id):
                logger.info("Zone %d: %s → %s", zone_id, self._zone_states.get(zone_id), state)
            self._zone_states[zone_id] = state

    def _verify_card_removed(self, slot_coord: tuple, before_screenshot, after_screenshot) -> bool:
        """
        Compare the pixel at slot_coord between two shift-held screenshots.
        Before: the specific card should be visible at that position.
        After play: the slot is empty or re-centered to a different card — clear delta.
        After failed play: same card returns — delta near zero.
        Returns True (unverifiable) if tray config is missing.
        """
        if not self._config.get("card_tray_card_y"):
            logger.debug("Card tray config missing — skipping play verification")
            return True

        x_check, y_check = slot_coord
        bgr_before = before_screenshot[y_check, x_check]
        bgr_after = after_screenshot[y_check, x_check]
        delta = int(sum(abs(int(a) - int(b)) for a, b in zip(bgr_before, bgr_after)))

        logger.info(
            "Tray verify: slot=(%d,%d) before=%s after=%s delta=%d — %s",
            x_check, y_check,
            tuple(int(v) for v in bgr_before),
            tuple(int(v) for v in bgr_after),
            delta,
            "verified" if delta > 40 else "unchanged",
        )
        return delta > 40

    def _positions_for_card_type(self, card_type: str) -> list[int]:
        """Return all visual deck positions that contain cards of the given type."""
        return [i for i, c in enumerate(self._deck_layout) if c == card_type]

    def _deck_pos_to_screen(self, deck_pos: int) -> tuple[int, int]:
        """Convert a visual deck position to current screen (x, y) in 1920×1080."""
        remaining = [i for i in range(len(self._deck_layout)) if i not in self._deck_played]
        visual_index = remaining.index(deck_pos)
        n = len(remaining)
        center_x = self._config.get("card_tray_center_x", 966)
        card_width = self._config.get("card_tray_card_width", 76)
        card_y = self._config.get("card_tray_card_y", 943)
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

    def _save_error_screenshot(self, label: str):
        from game.screen_detection import save_error_screenshot
        save_error_screenshot(label)
