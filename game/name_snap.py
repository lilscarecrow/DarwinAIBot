"""Snap nameplate OCR to the ladder's expected names for this lobby.

darwinstalker answers open-draft with `lobby` — each known player with every
string they may appear as (canonical name, aliases, today's Steam persona) —
and tesseract reads the player bar as "‘saibu", "shiftv", "Rob0cop". This
module folds both sides the same way the ladder does (game/ds_lifecycle →
src/db/identity.rs `ocr_fold`) and replaces a read with the player's
canonical name when it matches exactly one player. Anything ambiguous or
unknown is kept verbatim: a wrong snap is worse than a raw read, which the
ladder's own resolver still gets a go at.

`snap_all()` (2026-09-09) adds a second, fuzzy pass over whatever the exact
pass above couldn't place — found live: longer names sometimes scroll/get
cut off in the card's fixed-width nameplate, so the OCR read is a genuine
truncation of the true name rather than noise, and an exact-fold-or-nothing
match was leaving those verbatim every time. See _similarity()'s and
snap_all()'s docstrings for how the graduated-confidence, multi-round
resolution works and where it refuses to guess.
"""
from __future__ import annotations

import difflib

_FOLD = {"1": "l", "i": "l", "0": "o", "y": "v", "5": "s", "8": "b", "q": "g"}

# snap_all()'s fuzzy pass: a read/player pair scoring below this is never
# snapped, no matter how few unclaimed players are left — a wrong guess is
# worse than leaving the read verbatim (see module docstring).
_MIN_FUZZY_CONFIDENCE = 0.6

# If the best and second-best candidate PLAYER for a read score within this
# of each other, the read is treated as ambiguous for this round rather than
# guessed — the fuzzy equivalent of the exact pass's "two players share this
# fold, never snap it."
_AMBIGUITY_MARGIN = 0.05


def ocr_fold(name: str) -> str:
    """Same fold as the ladder: lowercase, alphanumerics only, the glyphs the
    bar's OCR confuses collapsed (1/i→l, 0→o, y→v, 5→s, 8→b, q→g)."""
    out = []
    for ch in (name or "").lower():
        if ch.isalnum():
            out.append(_FOLD.get(ch, ch))
    return "".join(out)


def _similarity(a: str, b: str) -> float:
    """0.0-1.0 closeness between two already-folded strings. Plain sequence
    similarity alone punishes a length difference too heavily for a
    scrolled/cut-off nameplate read: a name that only got half-rendered is a
    contiguous CHUNK of the true one, not a typo of it, so this also checks
    substring containment in either direction and takes whichever score is
    higher — (shorter length / longer length) when one contains the other."""
    if not a or not b:
        return 0.0
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if a in b or b in a:
        shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
        ratio = max(ratio, len(shorter) / len(longer))
    return ratio


class NameSnapper:
    """fold(handle) → canonical player name, built from open-draft's `lobby`."""

    MIN_FOLD = 3        # "M" never snaps
    MIN_SUBSTR = 5      # a handle inside a longer read ("F0 ayitbunny") must be this long

    def __init__(self, lobby: list[dict] | None):
        self._exact: dict[str, str] = {}
        self._players: list[tuple[str, str]] = []   # (fold, canonical) for substring matching
        self._all_names: list[tuple[str, str]] = []  # (fold, canonical) — every alias, for snap_all()'s fuzzy pass
        for m in lobby or []:
            player = str(m.get("player") or "").strip()
            if not player:
                continue
            names = list(m.get("names") or [])
            for extra in (m.get("persona"), player):
                if extra and extra not in names:
                    names.append(extra)
            for n in names:
                f = ocr_fold(str(n))
                if len(f) < self.MIN_FOLD:
                    continue
                if f in self._exact and self._exact[f] != player:
                    self._exact[f] = ""            # two players share this fold → never snap it
                else:
                    self._exact.setdefault(f, player)
                if len(f) >= self.MIN_SUBSTR:
                    self._players.append((f, player))
                self._all_names.append((f, player))

    def __len__(self) -> int:
        return sum(1 for v in self._exact.values() if v)

    def snap(self, read: str) -> str:
        """The canonical name for one OCR read, or the read itself."""
        read = (read or "").strip()
        f = ocr_fold(read)
        if len(f) < self.MIN_FOLD:
            return read
        hit = self._exact.get(f)
        if hit:
            return hit
        if hit == "":
            return read
        # A stream tag or badge glued to the handle: exactly one known handle
        # is contained in the read, and no other player's is.
        inside = {p for (pf, p) in self._players if pf in f}
        if len(inside) == 1:
            return inside.pop()
        return read

    def snap_all(self, reads: list[str]) -> list[str]:
        """Snap a whole bar; a player can only be claimed by one slot.

        Two stages. First, snap()'s exact-fold-or-glued-substring match, same
        as always — a confirmed player is removed from the pool before the
        second stage even starts, so it can never be second-guessed by a
        shakier fuzzy match later. Second, a fuzzy pass over whatever's left:
        each round scores every still-unresolved read against every
        still-unclaimed player (_similarity(), which is substring-aware so a
        scrolled/cut-off nameplate read still scores high against the true,
        longer name), and claims only the single highest-confidence pair in
        that round — skipping any read whose best and second-best candidate
        PLAYER are within _AMBIGUITY_MARGIN of each other, or whose best score
        is below _MIN_FUZZY_CONFIDENCE. Claiming one player can resolve
        another read's ambiguity (its rival candidate is now taken), so this
        repeats until a round makes no progress; whatever's still unresolved
        at that point is left verbatim rather than guessed.
        """
        out = list(reads)
        taken: set[str] = set()
        unresolved: list[int] = []
        for i, r in enumerate(reads):
            s = self.snap(r)
            if s != r and s not in taken:
                out[i] = s
                taken.add(s)
            else:
                out[i] = r
                unresolved.append(i)

        folds = {i: ocr_fold(reads[i]) for i in unresolved}
        unresolved = [i for i in unresolved if len(folds[i]) >= self.MIN_FOLD]

        while unresolved:
            round_best: dict[int, tuple[float, str]] = {}
            for i in unresolved:
                f = folds[i]
                best_per_player: dict[str, float] = {}
                for pf, p in self._all_names:
                    if p in taken:
                        continue
                    score = _similarity(f, pf)
                    if score > best_per_player.get(p, 0.0):
                        best_per_player[p] = score
                if not best_per_player:
                    continue
                ranked = sorted(best_per_player.items(), key=lambda kv: -kv[1])
                top_player, top_score = ranked[0]
                if top_score < _MIN_FUZZY_CONFIDENCE:
                    continue
                if len(ranked) > 1 and ranked[1][1] >= top_score - _AMBIGUITY_MARGIN:
                    continue  # two players too close to call — try again next round
                round_best[i] = (top_score, top_player)

            if not round_best:
                break  # no read this round cleared the bar — stop, leave the rest verbatim

            # Only the single most confident pair claims this round: a player
            # who's the top pick for two reads must not be handed to whichever
            # happened to be scored first.
            i, (_, player) = max(round_best.items(), key=lambda kv: kv[1][0])
            out[i] = player
            taken.add(player)
            unresolved.remove(i)
        return out
