"""RSS / Atom / YouTubeチャンネルRSS の取得。

正規化は `parsers/feed.py` の共通関数を通し、Mock と処理を重複させない。
"""

from __future__ import annotations

import os
import urllib.parse
from typing import Any

from ...models import Article
from ..parsers import feed as feed_parser
from ..parsers import youtube_api
from . import http

YOUTUBE_API = "https://www.googleapis.com/youtube/v3/playlistItems"


class RealFeedClient:
    """`FeedClient`。取得したXMLを `parsers.feed.parse` へ渡す。"""

    def __init__(self, timeout: float = http.DEFAULT_TIMEOUT) -> None:
        self.timeout = timeout

    def fetch_articles(self, source: dict[str, Any]) -> list[Article]:
        url = str(source.get("url") or "")
        if not url:
            return []
        if source.get("type") == "youtube":
            via_api = self._fetch_youtube_api(source)
            if via_api is not None:
                return via_api
        raw = http.fetch_bytes(url, timeout=self.timeout)
        return feed_parser.parse(
            raw,
            source_id=str(source.get("id") or ""),
            default_category=str(source.get("category") or "industry"),
            fulltext=bool(source.get("fulltext")),
            origin=str(source.get("origin") or "config"),
            kind="gallery" if source.get("type") == "gallery" else "article",
        )

    def _fetch_youtube_api(self, source: dict[str, Any]) -> list[Article] | None:
        """YouTube Data API で取る。キーが無ければ None（RSS へ落ちる）。

        RSS は非公式で、データセンターの IP からは日によって 404/500 が
        返る。公式の API は自動取得のための口なので安定する。API が落ちた
        ときも None を返して RSS を試す。
        """
        key = os.environ.get("YOUTUBE_API_KEY")
        channel_id = youtube_api.channel_id_of(str(source.get("url") or ""))
        playlist = youtube_api.uploads_playlist_id(channel_id)
        if not key or not playlist:
            return None
        query = urllib.parse.urlencode({
            "part": "snippet", "playlistId": playlist, "maxResults": 15, "key": key})
        try:
            payload = http.fetch_json(f"{YOUTUBE_API}?{query}", timeout=self.timeout)
        except http.FetchError:
            return None   # 理由は RSS 側の失敗として記録される
        return youtube_api.parse_playlist_items(
            payload, source_id=str(source.get("id") or ""),
            default_category=str(source.get("category") or "ai"),
            origin=str(source.get("origin") or "config"))
