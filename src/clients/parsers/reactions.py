"""HN / はてブ / Reddit のレスポンス正規化。純粋関数。"""

from __future__ import annotations

from typing import Any

from ...models import Reactions


def merge(hn: dict[str, Any] | None, hatena: Any, reddit: dict[str, Any] | None) -> Reactions:
    r = Reactions()
    if hn:
        hits = hn.get("hits") or []
        if hits:
            top = hits[0]
            r.hn_points = int(top.get("points") or 0)
            r.hn_comments = int(top.get("num_comments") or 0)
    if hatena is not None:
        # はてブは件数が数値、またはブックマークJSON
        if isinstance(hatena, (int, float, str)):
            try:
                r.hatena = int(hatena)
            except (TypeError, ValueError):
                r.hatena = 0
        elif isinstance(hatena, dict):
            r.hatena = int(hatena.get("count") or len(hatena.get("bookmarks") or []))
    if reddit:
        children = (reddit.get("data") or {}).get("children") or []
        if children:
            r.reddit = int((children[0].get("data") or {}).get("score") or 0)
    return r
