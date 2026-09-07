"""Snap nameplate OCR to the ladder's expected names for this lobby.

darwinstalker answers open-draft with `lobby` — each known player with every
string they may appear as (canonical name, aliases, today's Steam persona) —
and tesseract reads the player bar as "‘saibu", "shiftv", "Rob0cop". This
module folds both sides the same way the ladder does (game/ds_lifecycle →
src/db/identity.rs `ocr_fold`) and replaces a read with the player's
canonical name when it matches exactly one player. Anything ambiguous or
unknown is kept verbatim: a wrong snap is worse than a raw read, which the
ladder's own resolver still gets a go at.
"""
from __future__ import annotations

_FOLD = {"1": "l", "i": "l", "0": "o", "y": "v", "5": "s", "8": "b", "q": "g"}


def ocr_fold(name: str) -> str:
    """Same fold as the ladder: lowercase, alphanumerics only, the glyphs the
    bar's OCR confuses collapsed (1/i→l, 0→o, y→v, 5→s, 8→b, q→g)."""
    out = []
    for ch in (name or "").lower():
        if ch.isalnum():
            out.append(_FOLD.get(ch, ch))
    return "".join(out)


class NameSnapper:
    """fold(handle) → canonical player name, built from open-draft's `lobby`."""

    MIN_FOLD = 3        # "M" never snaps
    MIN_SUBSTR = 5      # a handle inside a longer read ("F0 ayitbunny") must be this long

    def __init__(self, lobby: list[dict] | None):
        self._exact: dict[str, str] = {}
        self._players: list[tuple[str, str]] = []   # (fold, canonical) for substring matching
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
        """Snap a whole bar; a player can only be claimed by one slot."""
        out: list[str] = []
        taken: set[str] = set()
        for r in reads:
            s = self.snap(r)
            if s != r and s in taken:
                s = r
            if s != r:
                taken.add(s)
            out.append(s)
        return out
