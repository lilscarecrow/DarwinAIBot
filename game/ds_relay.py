"""
DsRelay — background sender for live match events and relayed log lines.

The match loop and the Discord cog must never block on the ladder, so nothing
here does HTTP on the caller's thread. `enqueue_event` / `enqueue_log` append
to bounded in-memory queues and return; a daemon thread flushes them once a
second — events batched (≤100 per POST, grouped by game index) to
/api/ingest/events, log entries (≤50 per POST) to /api/ingest/log. `flush()`
drains synchronously and is what DraftLifecycle.close() calls so the last
events of a lobby land on its draft before the draft is closed.

Events need a draft: `get_draft_id` is asked at flush time and, when it
answers None (no lobby open), the events are dropped and counted. Log entries
never need a draft.

Log lines are rate-limited to LOG_RATE_PER_MINUTE; beyond that ONE
"log_ratelimited" entry per window carries the dropped count.

The HTTP functions come in through `transport` (defaults to game.ingest) so
tests inject a fake. Nothing in here ever raises; nothing here logs through
the loggers DsLogHandler forwards (this module's own logger is excluded there,
otherwise a failing log POST would log a warning that becomes a log POST).
"""
import collections
import logging
import threading
import time
from types import SimpleNamespace
from typing import Callable, Optional

logger = logging.getLogger(__name__)  # "game.ds_relay": DsLogHandler ignores it


def _default_transport():
    from game import ingest

    return SimpleNamespace(post_events=ingest.post_events, post_log=ingest.post_log)


class DsRelay:
    MAX_QUEUE = 1000
    EVENTS_PER_POST = 100
    LOGS_PER_POST = 50
    LOG_RATE_PER_MINUTE = 60
    FLUSH_INTERVAL = 1.0

    def __init__(
        self,
        get_draft_id: Callable[[], Optional[int]],
        get_args: Callable[[], dict],
        transport=None,
        flush_interval: float = FLUSH_INTERVAL,
    ):
        self._get_draft_id = get_draft_id
        self._get_args = get_args
        self._transport = transport if transport is not None else _default_transport()
        self._interval = flush_interval
        self._events: collections.deque = collections.deque()
        self._logs: collections.deque = collections.deque()
        self._qlock = threading.Lock()      # guards the deques
        self._flush_lock = threading.Lock() # one flusher at a time (thread vs. flush())
        self._wake = threading.Event()
        self._stopping = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.dropped_events = 0
        self.dropped_logs = 0
        self.posted_events = 0
        self.posted_logs = 0
        # log rate limiting
        self._window_start = time.monotonic()
        self._window_count = 0
        self._rl_entry: Optional[dict] = None

    # ---- thread ---------------------------------------------------------------

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stopping.clear()
        self._thread = threading.Thread(target=self._run, name="ds-relay", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stopping.set()
        self._wake.set()
        t = self._thread
        if t is not None and t.is_alive() and t is not threading.current_thread():
            t.join(timeout)
        self.flush(timeout)

    def _run(self) -> None:
        while not self._stopping.is_set():
            self._wake.wait(self._interval)
            self._wake.clear()
            try:
                self._flush_once()
            except Exception:  # never let the thread die
                pass

    # ---- enqueue (never raise, never block on HTTP) -----------------------------

    def enqueue_event(self, game_index: int, event: dict) -> None:
        try:
            with self._qlock:
                if len(self._events) >= self.MAX_QUEUE:
                    self.dropped_events += 1
                    return
                self._events.append((int(game_index or 0), dict(event)))
        except Exception:
            self.dropped_events += 1

    def enqueue_log(self, level: str, kind: str, message: str, fields: Optional[dict] = None) -> None:
        try:
            entry = {
                "level": level if level in ("info", "warn", "error") else "info",
                "kind": kind,
                "message": str(message)[:500],
                "at": int(time.time()),
            }
            if fields:
                entry["fields"] = fields
            with self._qlock:
                now = time.monotonic()
                if now - self._window_start >= 60.0:
                    self._window_start = now
                    self._window_count = 0
                    self._rl_entry = None
                if self._window_count >= self.LOG_RATE_PER_MINUTE:
                    self.dropped_logs += 1
                    if self._rl_entry is None:
                        self._rl_entry = {
                            "level": "warn",
                            "kind": "log_ratelimited",
                            "message": "bot log relay rate-limited; dropping entries this minute",
                            "at": int(time.time()),
                            "fields": {"dropped": 0},
                        }
                        self._logs.append(self._rl_entry)
                    self._rl_entry["fields"]["dropped"] += 1
                    return
                if len(self._logs) >= self.MAX_QUEUE:
                    self.dropped_logs += 1
                    return
                self._window_count += 1
                self._logs.append(entry)
        except Exception:
            self.dropped_logs += 1

    # ---- flush ------------------------------------------------------------------

    def flush(self, timeout: float = 2.0) -> None:
        """Drain both queues synchronously (best effort, bounded by timeout)."""
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline:
                with self._qlock:
                    empty = not self._events and not self._logs
                if empty:
                    return
                self._flush_once()
        except Exception:
            pass

    def _flush_once(self) -> None:
        with self._flush_lock:
            self._flush_events()
            self._flush_logs()

    def _flush_events(self) -> None:
        with self._qlock:
            batch = [self._events.popleft() for _ in range(min(len(self._events), self.EVENTS_PER_POST))]
        if not batch:
            return
        draft_id = None
        try:
            draft_id = self._get_draft_id()
        except Exception:
            pass
        if draft_id is None:
            self.dropped_events += len(batch)
            return
        # Group by game_index, preserving order within each group.
        groups: "collections.OrderedDict[int, list]" = collections.OrderedDict()
        for gi, ev in batch:
            groups.setdefault(gi, []).append(ev)
        args = self._safe_args()
        for gi, events in groups.items():
            try:
                ok = self._transport.post_events(draft_id, gi or None, events, **args)
            except Exception:
                ok = False
            if ok:
                self.posted_events += len(events)
            else:
                self.dropped_events += len(events)

    def _flush_logs(self) -> None:
        with self._qlock:
            batch = [self._logs.popleft() for _ in range(min(len(self._logs), self.LOGS_PER_POST))]
            if self._rl_entry is not None and any(e is self._rl_entry for e in batch):
                self._rl_entry = None  # a later drop opens a fresh counter entry
        if not batch:
            return
        try:
            ok = self._transport.post_log(batch, **self._safe_args())
        except Exception:
            ok = False
        if ok:
            self.posted_logs += len(batch)
        else:
            self.dropped_logs += len(batch)

    def _safe_args(self) -> dict:
        try:
            a = dict(self._get_args())
        except Exception:
            a = {}
        return {"base_url": a.get("base_url") or "https://darwinstalker.com", "token": a.get("token") or ""}

    # ---- introspection (tests) ----------------------------------------------------

    def pending(self) -> tuple[int, int]:
        with self._qlock:
            return len(self._events), len(self._logs)
