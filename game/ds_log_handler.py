"""
DsLogHandler — forwards the bot's own log lines to the ladder's event stream.

Why: every ladder-side diagnosis so far needed this bot's local log file, which
only the operator can read. With this handler the lines that matter show up on
darwinstalker.com/admin/observability as `bot.*` events next to the server's
own receipts.

What is forwarded:
  - every WARNING/ERROR/CRITICAL record from any logger          → kind "warning" / "error"
  - INFO records from the ladder loggers game.ds_lifecycle and
    game.ingest (open/roster/results/close lines)                 → kind "lifecycle"
Everything else (card plays, screen polls, ...) stays local.

Never raises, never recurses: records from game.ds_relay are ignored (a failed
log POST warns there, and that must not become another log POST), and a
thread-local flag drops anything emitted while this handler is already
forwarding on the same thread. Rate limiting lives in DsRelay.
"""
import logging
import threading

INFO_LOGGERS = frozenset({"game.ds_lifecycle", "game.ingest"})
IGNORED_PREFIXES = ("game.ds_relay",)


class DsLogHandler(logging.Handler):
    def __init__(self, lifecycle):
        super().__init__(level=logging.INFO)
        self._ds = lifecycle
        self._tl = threading.local()

    def emit(self, record: logging.LogRecord) -> None:
        if getattr(self._tl, "busy", False):
            return
        name = record.name or ""
        if name.startswith(IGNORED_PREFIXES):
            return
        if record.levelno >= logging.ERROR:
            level, kind = "error", "error"
        elif record.levelno >= logging.WARNING:
            level, kind = "warn", "warning"
        elif record.levelno >= logging.INFO and name in INFO_LOGGERS:
            level, kind = "info", "lifecycle"
        else:
            return
        self._tl.busy = True
        try:
            message = record.getMessage()[:500]
            self._ds.log(level, kind, message, {"logger": name})
        except Exception:
            pass
        finally:
            self._tl.busy = False
