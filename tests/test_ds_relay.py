"""DsRelay: batching, grouping, drops, log rate limit, synchronous flush, swallowed errors."""
import threading

from game.ds_relay import DsRelay


class FakeTransport:
    def __init__(self, ok=True):
        self.events_calls = []   # (draft_id, game_index, [events])
        self.log_calls = []      # ([entries])
        self.ok = ok

    def post_events(self, draft_id, game_index, events, base_url, token):
        self.events_calls.append((draft_id, game_index, list(events), base_url, token))
        return self.ok() if callable(self.ok) else self.ok

    def post_log(self, entries, base_url, token):
        self.log_calls.append((list(entries), base_url, token))
        return self.ok() if callable(self.ok) else self.ok


def make(draft_id=7, ok=True):
    t = FakeTransport(ok=ok)
    r = DsRelay(
        get_draft_id=lambda: draft_id,
        get_args=lambda: {"base_url": "https://ds.test", "token": "tok"},
        transport=t,
        flush_interval=60,   # the thread never fires on its own in tests
    )
    return r, t


def test_events_batched_and_grouped_by_game_index_in_order():
    r, t = make()
    r.enqueue_event(1, {"kind": "a"})
    r.enqueue_event(1, {"kind": "b"})
    r.enqueue_event(2, {"kind": "c"})
    r.enqueue_event(1, {"kind": "d"})
    r.flush()
    assert [(c[0], c[1], [e["kind"] for e in c[2]]) for c in t.events_calls] == [
        (7, 1, ["a", "b", "d"]),
        (7, 2, ["c"]),
    ]
    assert t.events_calls[0][3:] == ("https://ds.test", "tok")
    assert r.posted_events == 4 and r.dropped_events == 0


def test_events_capped_at_100_per_post():
    r, t = make()
    for i in range(230):
        r.enqueue_event(1, {"kind": f"e{i}"})
    r.flush()
    sizes = [len(c[2]) for c in t.events_calls]
    assert sizes == [100, 100, 30]
    assert [e["kind"] for c in t.events_calls for e in c[2]] == [f"e{i}" for i in range(230)]


def test_events_dropped_when_no_draft_open():
    r, t = make(draft_id=None)
    r.enqueue_event(1, {"kind": "a"})
    r.enqueue_log("info", "lifecycle", "still sent")
    r.flush()
    assert t.events_calls == []
    assert r.dropped_events == 1
    assert len(t.log_calls) == 1 and t.log_calls[0][0][0]["message"] == "still sent"


def test_queue_overflow_drops_and_counts():
    r, t = make()
    for i in range(DsRelay.MAX_QUEUE + 5):
        r.enqueue_event(1, {"kind": "x"})
    assert r.dropped_events == 5
    assert r.pending()[0] == DsRelay.MAX_QUEUE


def test_log_entries_shape_and_batch_size():
    r, t = make()
    for i in range(60):
        r.enqueue_log("warn", "warning", f"m{i}", {"logger": "x"})
    r.flush()
    assert [len(c[0]) for c in t.log_calls] == [50, 10]
    e = t.log_calls[0][0][0]
    assert e["level"] == "warn" and e["kind"] == "warning" and e["message"] == "m0"
    assert e["fields"] == {"logger": "x"} and isinstance(e["at"], int)


def test_log_rate_limit_then_one_counter_entry():
    r, t = make()
    for i in range(75):
        r.enqueue_log("info", "lifecycle", f"m{i}")
    assert r.dropped_logs == 15
    r.flush()
    entries = [e for c in t.log_calls for e in c[0]]
    assert len(entries) == 61
    rl = [e for e in entries if e["kind"] == "log_ratelimited"]
    assert len(rl) == 1 and rl[0]["fields"]["dropped"] == 15 and rl[0]["level"] == "warn"


def test_log_message_truncated_and_bad_level_defaults():
    r, t = make()
    r.enqueue_log("silly", "k", "x" * 900)
    r.flush()
    e = t.log_calls[0][0][0]
    assert e["level"] == "info" and len(e["message"]) == 500


def test_flush_drains_synchronously_without_thread():
    r, t = make()
    r.enqueue_event(1, {"kind": "a"})
    r.enqueue_log("info", "k", "m")
    assert r.pending() == (1, 1)
    r.flush()
    assert r.pending() == (0, 0)
    assert len(t.events_calls) == 1 and len(t.log_calls) == 1


def test_transport_exceptions_and_failures_are_swallowed_and_counted():
    def boom():
        raise RuntimeError("down")
    r, t = make(ok=boom)
    r.enqueue_event(1, {"kind": "a"})
    r.enqueue_log("info", "k", "m")
    r.flush()  # must not raise
    assert r.dropped_events == 1 and r.dropped_logs == 1
    r2, t2 = make(ok=False)
    r2.enqueue_event(1, {"kind": "a"})
    r2.flush()
    assert r2.dropped_events == 1


def test_enqueue_never_raises_even_with_bad_input():
    r, t = make()
    r.enqueue_event(None, None)   # dict(None) raises → counted, not raised
    assert r.dropped_events == 1
    r.enqueue_log(None, None, None)
    r.flush()


def test_thread_start_idempotent_and_stop_flushes():
    r, t = make()
    r._interval = 0.05
    r.start()
    first = r._thread
    r.start()
    assert r._thread is first and first.is_alive()
    r.enqueue_event(1, {"kind": "a"})
    r.stop(timeout=1.0)
    assert not first.is_alive()
    assert len(t.events_calls) == 1


def test_background_thread_flushes_on_its_own():
    r, t = make()
    r._interval = 0.05
    r.start()
    r.enqueue_event(3, {"kind": "tick"})
    deadline = threading.Event()
    for _ in range(40):
        if t.events_calls:
            break
        deadline.wait(0.05)
    r.stop()
    assert t.events_calls and t.events_calls[0][1] == 3
