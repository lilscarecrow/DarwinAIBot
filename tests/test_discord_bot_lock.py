"""Session-lock check-and-acquire race fix (2026-09-15) -- /launch, /custom,
/menu, /start (DirectorCog._session_lock) and /role add, /role remove
(ScrimCog._role_lock) all guard their long-running work with a lock, but the
old `_lock_check()` only ever peeked at `.locked()` and returned -- the real
acquire (`async with self._session_lock`) happened several lines later,
after at least one genuine `await` (`interaction.response.defer()`, or --
for `/custom` -- the scrim-roster-capture calls before that). Two commands
invoked close together could both pass that early peek during the gap
before either had actually claimed the lock, since nothing yet
distinguished "checked" from "held".

The shared module-level `_acquire_lock_or_reject()` closes this by checking
and acquiring back-to-back with no `await` in between; DirectorCog and
ScrimCog each have a thin per-lock wrapper around it. These tests prove the
shared mechanism directly (two callers racing via `asyncio.gather` -- the
closest headless approximation of "two Discord commands invoked at the same
time" -- always resolves to exactly one winner), then confirm each cog's
wrapper actually uses its own lock and message.

No pytest-asyncio in this project's dev deps -- async methods are driven
directly with asyncio.run() from plain sync test functions instead (see
tests/test_twitch_bot_favorite_reward.py for the same convention).
"""
import asyncio
from unittest.mock import AsyncMock, MagicMock

from bot.discord_bot import DirectorCog, ScrimCog, _acquire_lock_or_reject


def make_interaction():
    interaction = MagicMock()
    interaction.response.send_message = AsyncMock()
    return interaction


def run(coro):
    return asyncio.run(coro)


# ---- the shared mechanism, tested directly -------------------------------------

def test_acquires_when_free():
    lock = asyncio.Lock()
    interaction = make_interaction()
    result = run(_acquire_lock_or_reject(lock, interaction, "busy"))
    assert result is True
    assert lock.locked()
    interaction.response.send_message.assert_not_called()


def test_rejects_when_already_locked():
    lock = asyncio.Lock()

    async def scenario():
        await lock.acquire()
        interaction = make_interaction()
        result = await _acquire_lock_or_reject(lock, interaction, "busy")
        return result, interaction

    result, interaction = run(scenario())
    assert result is False
    interaction.response.send_message.assert_called_once_with("busy", ephemeral=True)


def test_does_not_touch_the_lock_state_on_rejection():
    """A rejected caller must not release or otherwise disturb a lock it
    never actually held."""
    lock = asyncio.Lock()

    async def scenario():
        await lock.acquire()
        await _acquire_lock_or_reject(lock, make_interaction(), "busy")
        return lock.locked()

    assert run(scenario()) is True


def test_only_one_of_two_concurrent_callers_acquires():
    """Two callers racing for the lock at the same instant -- exactly one
    must win, never both, never neither. This is the scenario the old
    check-then-later-acquire sequence got wrong: both could pass the early
    `.locked()` peek before either had actually claimed it."""
    lock = asyncio.Lock()
    interaction_a = make_interaction()
    interaction_b = make_interaction()

    async def scenario():
        return await asyncio.gather(
            _acquire_lock_or_reject(lock, interaction_a, "busy"),
            _acquire_lock_or_reject(lock, interaction_b, "busy"),
        )

    results = run(scenario())
    assert sorted(results) == [False, True]

    winner_interaction = interaction_a if results[0] else interaction_b
    loser_interaction = interaction_b if results[0] else interaction_a
    winner_interaction.response.send_message.assert_not_called()
    loser_interaction.response.send_message.assert_called_once()


def test_three_concurrent_callers_exactly_one_wins():
    """Same race, more contenders -- guards against a fix that happens to
    work for exactly two callers by accident."""
    lock = asyncio.Lock()
    interactions = [make_interaction() for _ in range(3)]

    async def scenario():
        return await asyncio.gather(
            *(_acquire_lock_or_reject(lock, i, "busy") for i in interactions)
        )

    results = run(scenario())
    assert results.count(True) == 1
    assert results.count(False) == 2

    for interaction, won in zip(interactions, results):
        if won:
            interaction.response.send_message.assert_not_called()
        else:
            interaction.response.send_message.assert_called_once()


def test_a_second_caller_succeeds_once_the_first_releases():
    """Not a permanent lockout -- once the winner releases, the lock is
    available again for the next command."""
    lock = asyncio.Lock()

    async def scenario():
        won_first = await _acquire_lock_or_reject(lock, make_interaction(), "busy")
        lock.release()
        won_second = await _acquire_lock_or_reject(lock, make_interaction(), "busy")
        return won_first, won_second

    won_first, won_second = run(scenario())
    assert won_first is True
    assert won_second is True


# ---- DirectorCog's wrapper: uses self._session_lock ----------------------------

class FakeDirectorCog:
    """Minimal stand-in exposing only what _acquire_lock_or_reject touches --
    not a real DirectorCog, which needs a live discord.py Bot to construct."""
    def __init__(self):
        self._session_lock = asyncio.Lock()
        self.bot = MagicMock()
        self.bot.session.status_message.return_value = "Status: IDLE"


def test_director_cog_wrapper_uses_the_session_lock():
    cog = FakeDirectorCog()
    interaction = make_interaction()
    result = run(DirectorCog._acquire_lock_or_reject(cog, interaction))
    assert result is True
    assert cog._session_lock.locked()


def test_director_cog_wrapper_rejects_with_session_status():
    cog = FakeDirectorCog()

    async def scenario():
        await cog._session_lock.acquire()
        interaction = make_interaction()
        result = await DirectorCog._acquire_lock_or_reject(cog, interaction)
        return result, interaction

    result, interaction = run(scenario())
    assert result is False
    interaction.response.send_message.assert_called_once_with(
        "Another operation is already running. Status: IDLE", ephemeral=True,
    )


# ---- ScrimCog's wrapper: uses its OWN lock, separate from DirectorCog's --------

class FakeScrimCog:
    """Minimal stand-in exposing only what _acquire_role_lock_or_reject
    touches -- not a real ScrimCog, which needs a live discord.py Bot to
    construct."""
    def __init__(self):
        self._role_lock = asyncio.Lock()


def test_scrim_cog_wrapper_uses_its_own_role_lock():
    cog = FakeScrimCog()
    interaction = make_interaction()
    result = run(ScrimCog._acquire_role_lock_or_reject(cog, interaction))
    assert result is True
    assert cog._role_lock.locked()


def test_scrim_cog_wrapper_rejects_with_a_role_specific_message():
    cog = FakeScrimCog()

    async def scenario():
        await cog._role_lock.acquire()
        interaction = make_interaction()
        result = await ScrimCog._acquire_role_lock_or_reject(cog, interaction)
        return result, interaction

    result, interaction = run(scenario())
    assert result is False
    interaction.response.send_message.assert_called_once()
    (message,), kwargs = interaction.response.send_message.call_args
    assert "role" in message.lower()
    assert kwargs.get("ephemeral") is True


def test_scrim_cog_and_director_cog_locks_are_independent():
    """/role add running must not block /launch, /custom, /menu, or /start,
    and vice versa -- these are separate resource domains (Discord role/
    message management vs. game automation), so they must use separate
    locks, not share DirectorCog's _session_lock."""
    director = FakeDirectorCog()
    scrim = FakeScrimCog()

    async def scenario():
        director_won = await DirectorCog._acquire_lock_or_reject(director, make_interaction())
        scrim_won = await ScrimCog._acquire_role_lock_or_reject(scrim, make_interaction())
        return director_won, scrim_won

    director_won, scrim_won = run(scenario())
    assert director_won is True
    assert scrim_won is True  # not blocked by the other lock being held
