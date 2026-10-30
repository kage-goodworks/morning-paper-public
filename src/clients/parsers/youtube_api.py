"""YouTube Data API v3 の応答の正規化。純粋関数。

チャンネルの「アップロード」再生リスト（`UU` + チャンネルIDの `UC` 以降）を
`playlistItems.list` で読む。1回1ユニットで、1日1万ユニットの無料枠に
対して13チャンネルなら13ユニット。RSS と違って自動取得のための口なので、
データセンターの IP から叩いても絞られない。
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

from ...models import Article

JST = timezone(timedelta(hours=9))

_CHANNEL_ID = re.compile(r"channel_id=(UC[\w-]{22})")


def channel_id_of(feed_url: str) -> str:
    """`youtube.yml` のフィードURLからチャンネルIDを取る。無ければ空。"""
    m = _CHANNEL_ID.search(feed_url or "")
    return m.group(1) if m else ""


def uploads_playlist_id(channel_id: str) -> str:
    """チャンネルIDから「アップロード」再生リストのIDへ（UC… → UU…）。"""
    if channel_id.startswith("UC") and len(channel_id) == 24:
        return "UU" + channel_id[2:]
    return ""


def _to_jst(value: str) -> str:
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return ""
    return parsed.astimezone(JST).isoformat(timespec="seconds")


def parse_playlist_items(payload: Any, *, source_id: str, default_category: str,
                         origin: str = "config") -> list[Article]:
    """`playlistItems.list` の応答を Article へ。壊れた項目は落とす。"""
    out: list[Article] = []
    for item in (payload or {}).get("items") or []:
        snippet = item.get("snippet") or {}
        video_id = ((snippet.get("resourceId") or {}).get("videoId")
                    or (item.get("contentDetails") or {}).get("videoId") or "")
        title = (snippet.get("title") or "").strip()
        if not video_id or not title or title in ("Private video", "Deleted video"):
            continue
        thumbs = snippet.get("thumbnails") or {}
        image = ""
        for key in ("maxres", "standard", "high", "medium", "default"):
            if thumbs.get(key, {}).get("url"):
                image = str(thumbs[key]["url"])
                break
        out.append(Article(
            id="", url=f"https://www.youtube.com/watch?v={video_id}",
            title=title, source=source_id, category=default_category,
            published_at=_to_jst(snippet.get("publishedAt") or ""),
            origin=origin, image_url=image if image.startswith("https://") else "",
        ))
    return out
