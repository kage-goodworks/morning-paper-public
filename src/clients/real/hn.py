"""Hacker News（Algolia）の取得。認証は不要。"""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import http

ENDPOINT = "https://hn.algolia.com/api/v1/search"


def fetch(url: str) -> dict[str, Any]:
    """その記事URLに対する投稿を探し、生JSONを返す。正規化はしない。"""
    query = urllib.parse.urlencode({"query": url, "restrictSearchableAttributes": "url",
                                    "hitsPerPage": 1})
    return http.fetch_json(f"{ENDPOINT}?{query}", timeout=15.0)
