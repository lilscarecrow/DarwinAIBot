# darwinstalker.com draft lifecycle — handoff

For the bot operator and for any AI agent working in this repo. You do not need
to have seen either codebase before. Read top to bottom once; afterwards the
"How to verify" and "Contract" sections are the ones you come back to.

## 1. What changed and why (2026-09-06)

The Twitch embed on the ladder's LIVE tab (https://darwinstalker.com, LIVE tab)
never appeared, even with `ds_ingest_twitch_channel` set. Three independent
causes, all fixed:

1. **Wrong field name for a week.** The bot sent `player_names`, the server
   wanted `players`. The server rejected every open-draft call with HTTP 422
   before even checking the token, and the bot logged it as a warning and moved
   on. The server now accepts both keys; the bot now sends `players`.
2. **Silent skip when lobby OCR found no names.** `open_set_draft` returned
   without sending anything if the nameplate OCR came back empty, and logged
   nothing. The draft was then created by the results screenshot at the end of
   game 1, which carries no Twitch channel. Now an open-draft is sent at
   `/custom` time with an empty roster, and an empty OCR at match start is a
   logged WARNING, never a silent skip.
3. **No close call.** Nothing told the ladder a lobby was over, so an abandoned
   lobby sat as an open draft until a moderator clicked "discard empty drafts".
   Now every path back to IDLE closes the draft; the server discards it if it
   is still empty, or keeps the games for review and drops the embed.

Everything the bot sends is still fire-and-forget: a dead ladder never stops a
match.

## 2. The lifecycle, end to end

```
/custom succeeds  ──► open-draft {players: [], twitch_channel}   ──► LIVE tab: embed + "lobby forming"
/start (each game) ─► open-draft {players: OCR names, draft_id}  ──► roster appears
                      (no names? WARNING, no call, roster kept)
match ends ─────────► screenshot {draft_id}                       ──► scorecard for that game
4th game ───────────► nothing more; moderators publish the set  ──► LIVE tab retires
/quit, abort, idle timeout, error reset ─► close-draft {draft_id, reason}
                      empty draft → discarded    has games → kept for review, embed drops
```

All four calls live in one class: `game/ds_lifecycle.py::DraftLifecycle`.
The Discord cog owns one instance (`DirectorCog._ds`) and the match runner is
handed the same instance. Nothing else in the bot talks to the ladder.

## 3. The contract

Base URL: `https://darwinstalker.com` (config `ds_ingest_base_url`). Every call
sends `Authorization: Bearer <ds_ingest_token>`. JSON unless noted.

### POST /api/ingest/open-draft

```json
{"platform": "pc", "players": ["Alpha", "Bravo"], "twitch_channel": "yourchannel", "draft_id": 245,
 "roster": ["123456789012345678", "234567890123456789"]}
```

- `players`: 0–20 names, **empty list allowed** (`player_names` is accepted as
  an alias but the bot sends `players`).
- `twitch_channel`: optional; the bot omits it when the config value is empty.
- `draft_id`: optional; when it names an open draft owned by this token, that
  draft is reused (names added, channel refreshed). Otherwise the server uses
  the token's most recent open draft, or creates a fresh one.
- `roster`: optional, ≤20 Discord IDs — the scrim signup reactors the bot
  captures at `/custom`. The lobby-time open sends them; the server pre-seeds
  every id that is linked to a ladder player with that player's canonical
  name (so the card shows real names before any OCR runs) and skips unlinked
  ids. The match-start open sends OCR names only; the server matches them
  against the seeded roster by identity, so a nameplate read of a seeded
  player never becomes a duplicate row.
- `tournament_slug`: optional — the darwinstalker.com tournament this lobby
  belongs to. Sent only while tournament mode is on (see "Tournament mode"
  below); the server answers `400 {"error": "unknown tournament"}` for a slug
  it does not know, which the bot logs like any other open failure (and the
  draft is then not opened — fix the slug and re-run `/custom`).
- `200 {"draft_id": 245, "created": true, "rows": 2, "roster_resolved": 2}` —
  `roster_resolved` = how many Discord IDs became rows.

### The open-draft reply is the lobby briefing (2026-09-07)

`roster` entries may now be `{"id": "...", "names": ["nick", "display name",
"username"]}` — `bot/discord_bot.py` builds them from the signup reactors —
and the server links an unlinked account inline when one of its names matches
exactly one of the lobby's players. The reply carries:

- `lobby`: each known member, `{discord_id, player, player_id, persona, names}`
  — `names` is everything that player may appear as (canonical, aliases, and
  their Steam persona refreshed at open time = the nameplate).
- `expected_names`: the flat snap set.
- `unlinked`: `{discord_id, names}` for members the ladder cannot place.

`DraftLifecycle` keeps these (`lobby`, `expected_names`, `unlinked`) and
exposes `snap_names(reads)` — `game/name_snap.py` folds the player-bar OCR the
way the ladder does and replaces a read with the canonical name when exactly
one player matches (a stream tag glued to a handle, "F0 ayitbunny", is handled;
anything ambiguous stays verbatim). `MatchRunner` snaps at both player-bar
inits (V1 and V2), so `match_start.slots`, eliminations and the match-start
roster push all carry ladder names. `/custom` adds a "Not on the ladder yet"
field mentioning the unlinked members with the claim instructions.

### POST /api/ingest/events (live match events)

```json
{"draft_id": 245, "game_index": 3, "events": [
  {"kind": "eliminated", "elapsed_ms": 412300, "at": 1788730000, "slot": 4, "player": "Bael", "data": {"alive": 6}}
]}
```

- ≤100 events per call; `draft_id` optional (server falls back to this token's
  open draft, `404` if none), `game_index` optional 1–4. The server floors it
  at the draft's next unrecorded game: a game whose results are already on
  the draft is over, so events claiming it are counted as the next game's
  (the response's `game_index` is the one actually used).
- Each event: `kind` (`[a-z0-9_]{1,40}`), optional `elapsed_ms` (from match
  start), `at` (epoch seconds), `slot` (0-based), `player` (≤64 chars), `data`
  (any JSON ≤2 KB).
- `200 {"draft_id": 245, "game_index": 3, "recorded": 1}`. `400` validation,
  `401` token, `404` no open draft, `422` malformed.

### POST /api/ingest/log (bot log relay)

```json
{"entries": [{"level": "warn", "kind": "warning", "message": "state.json unavailable", "at": 1788730000, "fields": {"logger": "game.match_runner"}}]}
```

- ≤50 entries per call; `level` is `info` | `warn` | `error`; `message` ≤500
  chars. `200 {"recorded": 1}`. Shows on the ladder's `/admin/observability`
  as kind `bot.<kind>`.
- `400` validation (bad platform, a name over 64 chars, channel empty or over
  64 chars), `401` bad token, `422` malformed body.

### POST /api/ingest/close-draft

```json
{"draft_id": 245, "reason": "quit"}
```

- `200 {"draft_id": 245, "closed": true, "discarded": true}` — no games and no
  screenshot: the server discarded it.
- `200 {"draft_id": 245, "closed": true, "discarded": false}` — had games or a
  screenshot: kept for moderator review, Twitch channel cleared.
- `200 {"draft_id": 245, "closed": false, "status": "approved"}` — was already
  published/rejected; harmless.
- `404` — no such draft, or not owned by this token.

### POST /api/ingest/screenshot (unchanged)

Multipart form: `screenshot` (PNG), `platform`, optional `roster` (JSON string
of Discord ids), optional `draft_id`. `200 {"draft_id", "game_index", "ocr_error"}`.

### GET /api/live?platform=pc (public, what the LIVE tab polls)

`{"drafts": [{"players": [...], "n_games": 0, "twitch_channel": "yourchannel", ...}]}`.
A bot draft that carries a channel is listed even with zero players.

## 3a. Live match events

The bot streams what it sees during a match to the draft, and the ladder's
LIVE card renders it (alive count, elimination order, first blood, match
clock, the director's card plays and megaphone lines). Nothing here blocks
the match: `DraftLifecycle.event(kind, ...)` appends to an in-memory queue
and a daemon thread (`game/ds_relay.py`) POSTs batches once a second, ≤100
events per call, grouped by game. Events are dropped (and counted) when no
draft is open; a queue over 1000 drops the newest.

| kind          | when                                   | top-level         | data                                   |
|---------------|----------------------------------------|-------------------|----------------------------------------|
| `match_start` | after the lobby nameplates are read    | `elapsed_ms: 0`   | `slots`: name or null per slot          |
| `first_blood` | first alive→dead flip (once per game)  | `slot`, `player`  | `notification`: OCR'd kill text or null |
| `eliminated`  | every alive→dead flip, all match       | `slot`, `player`  | `alive`: players still alive after it   |
| `detector_v2` | at match start when `player_bar_detector` is `shadow` or `v2` | `elapsed_ms: 0` | `drive`, `n`, `alive`, `xs`, `names` — what the V2 card detector saw (docs/PLAYER_BAR_CALIBRATION.md) |
| `eliminated_v2` | shadow mode only: every flip V2 saw   | `slot`, `player`  | `alive` — compare with `eliminated` (V1) and the HUD counter |
| `match_end`   | placement badge detected               | `elapsed_ms`      | —                                       |
| `card_play`   | the director fires a card              | —                 | `card`: card_type, `name`: event name   |
| `say`         | `/say` megaphone                       | —                 | `text`, `by` (Discord display name)     |
| `status`      | every `MatchRunner._update`            | —                 | `last`, `next`                          |
| `aborted`     | the match loop was force-stopped       | —                 | `reason`                                |

Player-targeted cards pick a screen coordinate, not a slot, so `card_play`
carries no slot. The game index is tracked by the lifecycle: 1 from
`open_lobby`, +1 after each results upload (the game's last events are
flushed BEFORE the screenshot goes up), 0 after `close`. `close()` flushes
the queues before the close-draft call so nothing is lost with the draft.

The bot now polls the player bar for the whole match (it used to stop after
first blood) — one extra screenshot per poll interval.

## 3b. Bot log relay

`game/ds_log_handler.py::DsLogHandler` is attached to the root logger when
the cog starts. It forwards, through the same relay, every WARNING / ERROR
from any logger (kind `warning` / `error`) and the INFO lines of the ladder
loggers `game.ds_lifecycle` and `game.ingest` (kind `lifecycle`: open, roster
push, results upload, close). They appear on
`https://darwinstalker.com/admin/observability` as `bot.warning`,
`bot.error`, `bot.lifecycle`, actor = this bot's token. Everything else
(card plays, screen polls) stays in `logs/darwin_bot.log`. Rate limit: 60
lines a minute, then one `bot.log_ratelimited` entry with the dropped count.
Lines from `game.ds_relay*` are never forwarded (a failing relay POST must
not become another relay POST).

## 3c. Tournament mode

`/tournament on slug:<slug>` stores the darwinstalker.com tournament slug as
`ds_ingest_tournament_slug` (lower-cased) alongside `tournament_mode: true`;
every draft opened while tournament mode is on is tagged with it, and the
ladder then resolves names against that tournament's checked-in roster. The
reply embed names the slug in effect, or warns when none is set. `/tournament
off` keeps the slug for next time. An unknown slug is refused by the server
(`400 unknown tournament`) — the open fails, the bot logs it (relayed too),
and the draft is not opened until the slug is corrected.

## 4. Config (`config.json`, gitignored)

```json
"ds_ingest_base_url": "https://darwinstalker.com",
"ds_ingest_token": "<issued by the ladder admins>",
"ds_ingest_platform": "pc",
"ds_ingest_twitch_channel": "yourchannel",
"ds_ingest_tournament_slug": ""
```

- `ds_ingest_tournament_slug` is written by `/tournament on slug:<slug>`; it
  only matters while `tournament_mode` is true.

- `ds_ingest_token` empty ⇒ the whole lifecycle is off (no calls at all).
- `ds_ingest_twitch_channel` **must be non-empty** or there is no embed. The
  bot warns at every lobby open when it is empty: `draft N opened WITHOUT a
  twitch_channel`.
- Config is read once at startup. **Restart the bot after editing config.json.**

## 5. Where the calls live

| What | Where |
|---|---|
| HTTP calls (`open_set_draft`, `close_set_draft`, `post_results_screenshot`) | `game/ingest.py` |
| State machine (`open_lobby`, `on_match_start`, `post_results`, `close`) | `game/ds_lifecycle.py::DraftLifecycle` |
| Instance creation | `bot/discord_bot.py::DirectorCog.__init__` → `self._ds` |
| Open at lobby creation | `DirectorCog.custom` → `_open_ds_draft()` right after the `IN_CUSTOM` transition |
| Roster at match start | `game/match_runner.py::MatchRunner.run` → `self._ds.on_match_start(self._player_names)` (constructor arg `draft_lifecycle`) |
| Screenshot after each game | `DirectorCog._post_results_to_ingest` → `self._ds.post_results` (both the `/start` and auto-start paths) |
| Close on every path to IDLE | `DirectorCog._reset_session(reason)` → `_close_ds_draft(reason)`; reasons: `quit`, `idle timeout`, `aborted: match safety timeout`, default `session reset` |

The blocking HTTP runs on the executor (`run_in_executor`), never on the event loop.

## 6. How to verify after a restart

`logs/darwin_bot.log`:

```
darwinstalker open-draft ok: draft_id=246 created=True rows=0 twitch_channel=yourchannel   ← at /custom
darwinstalker open-draft ok: draft_id=246 created=False rows=8 twitch_channel=yourchannel  ← at /start
darwinstalker ingest ok: draft_id=246 game_index=1 ocr_error=None                          ← after game 1
darwinstalker close-draft ok: draft_id=246 discarded=False reason=quit                    ← at /quit
```

Things that mean trouble:

- `open-draft failed: HTTP 401` — token wrong or revoked.
- `open-draft failed: HTTP 400/422` — contract drift; compare the body against §3.
- `draft N opened WITHOUT a twitch_channel` — config value empty.
- `lobby OCR returned no names; draft N keeps its current roster` — nameplate
  OCR failed for this match. Not fatal: the results screenshot still fills the
  scorecard. If it happens every match, calibration is off.
- Nothing at all — `ds_ingest_token` is empty, or the bot was not restarted.

On the site, LIVE tab:

| When | You should see |
|---|---|
| right after `/custom` | the Twitch player, "lobby forming", no names yet |
| after the first `/start` | the roster |
| after each game | the scorecard for that game |
| after `/quit` or an abort | embed gone; an empty lobby vanishes, a partial set stays for moderators |

The ladder admins can also confirm from the server side: every open-draft
success records an `ingest.open_draft` event (with the channel) and every
rejected call an `ingest.rejected` event, both visible in their observability
console.

## 7. Running the tests

```
python -m venv .venv
. .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest tests -q
```

Headless: no game, no Discord, no screen. `tests/conftest.py` stubs the
Windows/screen-only libraries so `game/` imports anywhere.

- `tests/test_ingest.py` — the exact wire shapes and failure handling of the
  three HTTP calls (mocked HTTP).
- `tests/test_ds_lifecycle.py` — the state machine with a fake transport.
- `tests/test_contract_darwin_stalker.py` — the real ingest module against a
  real darwin-stalker. **Skipped unless** `DS_BASE_URL` and `DS_INGEST_TOKEN`
  are set. It creates and closes real drafts, so point it ONLY at a local
  darwin-stalker on a scratch database, never at darwinstalker.com:

  ```
  DS_BASE_URL=http://127.0.0.1:3111 DS_INGEST_TOKEN=... pytest tests/test_contract_darwin_stalker.py -v
  ```

Any change to `game/ingest.py`, `game/ds_lifecycle.py`, or the wiring points in
§5 should keep `pytest tests -q` green.

## 8. Known gaps and the server's safety net

- If the bot dies mid-lobby (crash, power loss) no close is sent. The server
  sweeps hourly: an empty bot draft untouched for 6 hours is auto-discarded,
  and a draft's Twitch channel is cleared after 3 hours without activity, so
  the embed cannot linger past a dead stream. A close that fails for any other
  reason is likewise left to the sweep — the bot never retries.
- The roster pushed at match start is whatever the nameplate OCR reads; the
  results screenshot corrects it at the end of game 1.
- `open_lobby` runs at every `/custom` and always replaces the remembered draft
  id with the server's answer. The previous draft is closed only if a reset ran
  in between; after a completed 4-game set there is no reset and no close — that
  draft is full and sits in the moderator queue, which is the intended path.
