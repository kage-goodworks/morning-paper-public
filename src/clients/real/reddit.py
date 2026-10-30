"""Reddit のスコア取得。

公開の検索APIを使う。OAuthを使う場合は `Authorization` を足すだけで済むよう、
ヘッダを引数で受けられるようにしてある。
"""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import http

ENDPOINT = "https://www.reddit.com/search.json"


def fetch(url: str, *, headers: dict[str, str] | None = None) -> dict[str, Any]:
    """その記事URLに対する投稿を探し、生JSONを返す。正規化はしない。"""
    query = urllib.parse.urlencode({"q": f"url:{url}", "limit": 1, "sort": "top"})
    return http.fetch_json(f"{ENDPOINT}?{query}", timeout=15.0, headers=headers)
