"""
Compatibility shim: this repo's Discord bot module lives at bot/discord_bot.py
(package `bot`, imported elsewhere as `from bot.discord_bot import DarwinBot`,
e.g. main.py:78). Some external tooling imports it unqualified as
`import discord_bot`; without this shim that raises
`ModuleNotFoundError: No module named 'discord_bot'` since no such top-level
module has ever existed in this repo (confirmed via `git log --all -- discord_bot.py`
returning nothing).

This file makes `import discord_bot` transparently resolve to `bot.discord_bot`
by aliasing the module object in sys.modules, so both import spellings refer
to the exact same module instance (no duplicate class/singleton definitions).
"""
import sys

from bot import discord_bot as _bot_discord_bot

sys.modules[__name__] = _bot_discord_bot
