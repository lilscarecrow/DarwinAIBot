"""DsLogHandler: which records are forwarded to the lifecycle's log relay, and no recursion."""
import logging

import pytest

from game.ds_log_handler import DsLogHandler


class FakeLifecycle:
    def __init__(self, reentrant_logger=None):
        self.calls = []
        self._reentrant = reentrant_logger

    def log(self, level, kind, message, fields=None):
        self.calls.append((level, kind, message, fields))
        if self._reentrant is not None:
            # Simulate the relay failing and warning while we forward.
            self._reentrant.warning("post failed while forwarding")


@pytest.fixture
def wired():
    ds = FakeLifecycle()
    h = DsLogHandler(ds)
    root = logging.getLogger()
    root.addHandler(h)
    old = root.level
    root.setLevel(logging.DEBUG)
    yield ds, h
    root.removeHandler(h)
    root.setLevel(old)


def test_warning_from_any_logger_forwarded(wired):
    ds, _ = wired
    logging.getLogger("game.match_runner").warning("state.json unavailable %s", 1)
    logging.getLogger("bot.discord_bot").error("boom")
    logging.getLogger("anything").critical("worse")
    assert ds.calls == [
        ("warn", "warning", "state.json unavailable 1", {"logger": "game.match_runner"}),
        ("error", "error", "boom", {"logger": "bot.discord_bot"}),
        ("error", "error", "worse", {"logger": "anything"}),
    ]


def test_info_only_from_ladder_loggers(wired):
    ds, _ = wired
    logging.getLogger("game.ds_lifecycle").info("ds open_lobby: sent 3 discord ids")
    logging.getLogger("game.ingest").info("darwinstalker open-draft ok: draft_id=1")
    logging.getLogger("game.match_runner").info("Firing card event: x")
    logging.getLogger("game.ds_lifecycle").debug("noise")
    assert [c[:3] for c in ds.calls] == [
        ("info", "lifecycle", "ds open_lobby: sent 3 discord ids"),
        ("info", "lifecycle", "darwinstalker open-draft ok: draft_id=1"),
    ]


def test_relay_loggers_ignored_even_at_warning(wired):
    ds, _ = wired
    logging.getLogger("game.ds_relay").warning("post failed")
    logging.getLogger("game.ds_relay.ingest").warning("darwinstalker log relay failed: HTTP 500")
    assert ds.calls == []


def test_message_truncated_to_500(wired):
    ds, _ = wired
    logging.getLogger("x").warning("y" * 700)
    assert len(ds.calls[0][2]) == 500


def test_no_recursion_when_forwarding_logs_a_warning():
    root = logging.getLogger()
    ds = FakeLifecycle(reentrant_logger=logging.getLogger("some.module"))
    h = DsLogHandler(ds)
    root.addHandler(h)
    try:
        logging.getLogger("z").warning("first")
    finally:
        root.removeHandler(h)
    # Only the original record was forwarded; the warning raised while
    # forwarding did not re-enter the handler.
    assert [c[2] for c in ds.calls] == ["first"]


def test_handler_never_raises():
    class Broken:
        def log(self, *a, **k):
            raise RuntimeError("no")
    h = DsLogHandler(Broken())
    rec = logging.LogRecord("n", logging.WARNING, "p", 1, "m", None, None)
    h.emit(rec)  # must not raise
