import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Optional

from zones.zone_logic import valid_closeable_zones, ZoneState
from zones.strategy_factory import get_strategy
from session.state import SessionState

logger = logging.getLogger(__name__)


@dataclass
class CardEvent:
    name: str
    trigger_seconds: int
    slot_coord: Optional[tuple[int, int]]
    drop_target: Optional[tuple[int, int]]
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
    ):
        self._config = config
        self._session = session
        self._on_action_update = on_action_update
        self._stop = threading.Event()
        self._bypass = config.get("ahk_bypass_mode", False)
        self._strategy = get_strategy(config.get("zone_selection_strategy", "weighted_outer"))
        self._zone_states: dict[int, str] = {i: ZoneState.OPEN for i in range(1, 8)}

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
        # Pre-match deck verification
        self._update("Pre-match deck check", "Press B to start")
        if not self._pre_match_deck_check():
            return "Deck mismatch detected before match start. Match not started."

        if self._stop.is_set():
            return "Match aborted before start."

        # Press B to start the match
        self._update("Pressing B to start", "Waiting for countdown")
        self._press("b")

        # 5-second sync delay to align with in-game countdown
        if self._stop.wait(5):
            return "Match aborted during countdown."

        self._update("Match in progress", "Electromania at 2:00")
        start_time = time.monotonic()

        card_schedule = self._build_card_schedule()
        poll_interval = self._config.get("screen_poll_interval_seconds", 12)
        zone_check_interval = 30
        last_zone_check = start_time

        # ------------------------------------------------------------------
        # Main match loop
        # ------------------------------------------------------------------
        while not self._stop.is_set():
            elapsed = time.monotonic() - start_time

            # Fire any card events whose time has arrived
            for event in card_schedule:
                if not event.done and elapsed >= event.trigger_seconds:
                    self._fire_card_event(event, card_schedule)

            # Periodic zone close attempt
            if time.monotonic() - last_zone_check >= zone_check_interval:
                self._attempt_zone_close()
                last_zone_check = time.monotonic()

            # Poll for match end (placement badge on screen)
            if self._match_has_ended():
                logger.info("Match end detected at %.1fs elapsed", elapsed)
                break

            self._stop.wait(poll_interval)

        if self._stop.is_set():
            return "Match ended early (force stopped)."

        # ------------------------------------------------------------------
        # Capture and return results
        # ------------------------------------------------------------------
        self._update("Reading results screen", "Post to Discord")
        return self._capture_results()

    # ------------------------------------------------------------------
    # Pre-match
    # ------------------------------------------------------------------

    def _pre_match_deck_check(self) -> bool:
        # TODO: navigate to deck tab, screenshot, compare against configured card list
        logger.info("Pre-match deck check: skipped (pending calibration)")
        return True

    # ------------------------------------------------------------------
    # Card schedule
    # ------------------------------------------------------------------

    def _build_card_schedule(self) -> list[CardEvent]:
        cards_config = self._config.get("cards", {})
        card_slots = self._config.get("card_slots", {})
        events = []

        for name, data in cards_config.items():
            trigger = data.get("play_time_seconds")
            if trigger is None:
                continue

            slot_key = str(data.get("slot", ""))
            raw_slot = card_slots.get(slot_key)
            slot_coord = tuple(raw_slot) if raw_slot else None

            raw_target = data.get("drop_target")
            drop_target = tuple(raw_target) if raw_target else None

            events.append(CardEvent(
                name=name,
                trigger_seconds=trigger,
                slot_coord=slot_coord,
                drop_target=drop_target,
            ))

        return sorted(events, key=lambda e: e.trigger_seconds)

    def _fire_card_event(self, event: CardEvent, all_events: list[CardEvent]):
        event.done = True
        mins, secs = divmod(event.trigger_seconds, 60)
        logger.info("Firing card event: %s at %d:%02d", event.name, mins, secs)

        if not event.slot_coord or not event.drop_target:
            logger.warning("Card '%s' missing slot or drop target — skipping play", event.name)
            self._save_error_screenshot(f"card_skipped_{event.name}")
        else:
            from game.card_actions import play_card
            play_card(
                slot_coordinate=event.slot_coord,
                target_coordinate=event.drop_target,
                card_name=event.name,
                bypass_mode=self._bypass,
            )

        # Update next action label
        next_event = next((e for e in all_events if not e.done), None)
        if next_event:
            nm, ns = divmod(next_event.trigger_seconds, 60)
            next_label = f"{next_event.name} at {nm}:{ns:02d}"
        else:
            next_label = "Zone closes + match end polling"
        self._update(f"Played {event.name} at {mins}:{secs:02d}", next_label)

    # ------------------------------------------------------------------
    # Zone closes
    # ------------------------------------------------------------------

    def _attempt_zone_close(self):
        self._refresh_zone_states()
        valid = valid_closeable_zones(self._zone_states)
        if not valid:
            logger.info("Zone close check: no valid zones to close")
            return

        zone_id = self._strategy.select_zone(valid, self._zone_states)
        if zone_id is None:
            return

        logger.info("Attempting to close zone %d", zone_id)
        success = self._play_zone_close(zone_id)
        if success:
            self._zone_states[zone_id] = ZoneState.CLOSING
            self._update(f"Closed zone {zone_id}", "Continue match")

    def _play_zone_close(self, zone_id: int) -> bool:
        from game.card_actions import play_card

        raw_slot = self._config.get("zone_close_card_slot")
        slot_coord = tuple(raw_slot) if raw_slot else None

        zone_drop_coords = self._config.get("zone_drop_coordinates", {})
        raw_target = zone_drop_coords.get(str(zone_id))
        drop_target = tuple(raw_target) if raw_target else None

        if not slot_coord or not drop_target:
            logger.warning("Zone %d close skipped — slot or drop coordinate not calibrated", zone_id)
            return False

        return play_card(
            slot_coordinate=slot_coord,
            target_coordinate=drop_target,
            card_name=f"close_zone_{zone_id}",
            bypass_mode=self._bypass,
        )

    def _refresh_zone_states(self):
        from game.screen_detection import sample_pixel_color, color_within_threshold

        sample_coords = self._config.get("zone_sample_coordinates", {})
        thresholds = self._config.get("zone_color_thresholds", {})

        if not sample_coords or not all(thresholds.get(k) for k in ("open", "closed", "closing")):
            logger.debug("Zone state detection skipped — not yet calibrated")
            return

        for zone_id_str, coord in sample_coords.items():
            if coord is None:
                continue
            zone_id = int(zone_id_str)

            # Skip zones already confirmed closed
            if self._zone_states.get(zone_id) == ZoneState.CLOSED:
                continue

            color = sample_pixel_color(coord[0], coord[1])
            closed_thresh = tuple(thresholds["closed"])
            closing_thresh = tuple(thresholds["closing"])

            if color_within_threshold(color, closed_thresh):
                self._zone_states[zone_id] = ZoneState.CLOSED
            elif color_within_threshold(color, closing_thresh):
                self._zone_states[zone_id] = ZoneState.CLOSING
            else:
                self._zone_states[zone_id] = ZoneState.OPEN

    # ------------------------------------------------------------------
    # Match end detection
    # ------------------------------------------------------------------

    def _match_has_ended(self) -> bool:
        from game.screen_detection import poll_for_match_end
        return poll_for_match_end(
            badge_template_path="templates/placement_badge.png",
            threshold=0.75,
        )

    # ------------------------------------------------------------------
    # Results
    # ------------------------------------------------------------------

    def _capture_results(self) -> str:
        from game.screen_detection import take_screenshot
        from game.ocr import parse_results_screen, format_results_for_discord

        ocr_regions = self._config.get("results_ocr_regions")
        if not ocr_regions:
            logger.warning("results_ocr_regions not configured — skipping OCR")
            return "Match ended. Results OCR regions not yet configured."

        screenshot = take_screenshot()
        results = parse_results_screen(screenshot, ocr_regions)
        if not results:
            self._save_error_screenshot("ocr_no_results")
            return "Match ended. Failed to parse results (see error screenshot)."

        return format_results_for_discord(results)

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
