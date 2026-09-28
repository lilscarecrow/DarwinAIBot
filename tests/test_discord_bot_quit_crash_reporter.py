"""/quit as a manual crash-reporter recovery path (2026-09-28) -- MatchRunner's
own auto-close (see CLAUDE.md's Match Runner section) only checks for the
crash reporter inside a live match's main loop. _EndConfirmView._do_end (the
Yes/No confirmation behind /quit) now checks and closes it too, so there's a
manual way to recover if a crash happens outside that window (or the
auto-close was somehow missed) -- same is_crash_reporter_open()/
close_crash_reporter() pair from game/launcher.py, called right alongside the
existing close_game() call.

No pytest-asyncio in this project's dev deps -- async methods are driven
directly with asyncio.run(), same convention as tests/test_discord_bot_lock.py.
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from bot.discord_bot import _EndConfirmView


def make_cog():
    cog = MagicMock()
    cog._stop_event = MagicMock()
    cog._active_runner = None
    cog._reset_session = MagicMock()
    return cog


def make_interaction():
    interaction = MagicMock()
    interaction.response.edit_message = AsyncMock()
    return interaction


def run(coro):
    return asyncio.run(coro)


def test_do_end_closes_crash_reporter_when_open():
    view = _EndConfirmView(make_cog())
    with patch("game.launcher.is_crash_reporter_open", return_value=True) as is_open, \
         patch("game.launcher.close_crash_reporter") as close_reporter, \
         patch("game.launcher.close_game") as close_game:
        run(view._do_end(make_interaction()))
    is_open.assert_called_once()
    close_reporter.assert_called_once()
    close_game.assert_called_once()


def test_do_end_skips_closing_crash_reporter_when_not_open():
    view = _EndConfirmView(make_cog())
    with patch("game.launcher.is_crash_reporter_open", return_value=False) as is_open, \
         patch("game.launcher.close_crash_reporter") as close_reporter, \
         patch("game.launcher.close_game") as close_game:
        run(view._do_end(make_interaction()))
    is_open.assert_called_once()
    close_reporter.assert_not_called()
    close_game.assert_called_once()


def test_do_end_still_resets_session_and_stops_active_runner():
    cog = make_cog()
    runner = MagicMock()
    cog._active_runner = runner
    view = _EndConfirmView(cog)
    with patch("game.launcher.is_crash_reporter_open", return_value=True), \
         patch("game.launcher.close_crash_reporter"), \
         patch("game.launcher.close_game"):
        run(view._do_end(make_interaction()))
    runner.stop.assert_called_once()
    cog._reset_session.assert_called_once_with("quit")
