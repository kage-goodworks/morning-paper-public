"""無料で取れる反応の合成（HN・はてブ・Reddit）。

縮退の単位は「無料の反応」ひとまとまり。**個別のサービスが落ちても、その項目が
0になるだけ**で呼び出し側の分岐は増えない。3つのうち1つでも取れれば返す。
正規化は `parsers/reactions.py` の共通関数を通す。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable

from ...models import Article, Reactions
from ..parsers import reactions as reactions_parser
from . import hatena, hn, reddit


#: 取得に失敗した印。「反応が0件」（正常な応答）と区別する
FAILED = object()


def _safe(call: Callable[[], Any]) -> Any:
    """1つのサービスが落ちても他を捨てない。失敗は FAILED で返す。"""
    try:
        return call()
    except Exception:  # noqa: BLE001 - 失敗した項目は前回の値を保つ
        return FAILED


class RealReactionClient:
    """`ReactionClient`。3つの生レスポンスを1つにまとめる。"""

    def fetch(self, article: Article) -> Reactions:
        url = article.url
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [
                pool.submit(_safe, lambda: hn.fetch(url)),
                pool.submit(_safe, lambda: hatena.fetch(url)),
                pool.submit(_safe, lambda: reddit.fetch(url)),
            ]
            hn_raw, hatena_raw, reddit_raw = (f.result() for f in futures)

        failed = [raw is FAILED for raw in (hn_raw, hatena_raw, reddit_raw)]
        if all(failed):
            # 通信そのものが落ちている。0件として返すと、週次レビューが
            # 「反応が減った」と誤判定する。失敗として上へ投げる
            raise RuntimeError("反応の取得先3つとも失敗（通信障害の可能性）")

        merged = reactions_parser.merge(
            None if failed[0] else hn_raw,
            None if failed[1] else hatena_raw,
            None if failed[2] else reddit_raw)
        # 一部だけ失敗したら、その項目は前回の値を保つ（0 にしない）
        before = article.reactions
        if failed[0]:
            merged.hn_points, merged.hn_comments = before.hn_points, before.hn_comments
        if failed[1]:
            merged.hatena = before.hatena
        if failed[2]:
            merged.reddit = before.reddit
        merged.x_summary = before.x_summary
        merged.x_citations = list(before.x_citations)
        return merged
