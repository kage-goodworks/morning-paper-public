"""はてなブックマーク件数の取得。認証は不要。"""

from __future__ import annotations

import urllib.parse
from typing import Any

from . import http

ENDPOINT = "https://bookmark.hatenaapis.com/count/entry"


def fetch(url: str) -> Any:
    """件数APIを叩き、生の値（数値）を返す。正規化はしない。"""
    query = urllib.parse.urlencode({"url": url})
    text = http.fetch_text(f"{ENDPOINT}?{query}", timeout=15.0).strip()
    # 件数APIは数値をそのまま返す。空なら0件。
    return int(text) if text.isdigit() else 0
