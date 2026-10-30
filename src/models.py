"""ドメイン型。

`src/clients/base.py` のインタフェースはこの型だけで表現し、HTTPやファイルの
存在を露出しない。Mock と Real の差し替えが呼び出し側へ漏れないようにするため。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field, asdict
from typing import Any

CATEGORIES = [
    "ai", "lang", "devtools", "infra", "industry", "security",
    "gadget", "keyboard", "career",
]


def url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]


@dataclass
class Reactions:
    hn_points: int = 0
    hn_comments: int = 0
    hatena: int = 0
    reddit: int = 0
    x_summary: str | None = None
    x_citations: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Reactions":
        return cls(
            hn_points=int(d.get("hn_points") or 0),
            hn_comments=int(d.get("hn_comments") or 0),
            hatena=int(d.get("hatena") or 0),
            reddit=int(d.get("reddit") or 0),
            x_summary=d.get("x_summary"),
            x_citations=list(d.get("x_citations") or []),
        )


@dataclass
class Article:
    id: str
    url: str
    title: str
    source: str
    category: str
    published_at: str
    url_hash: str = ""
    title_original: str | None = None
    #: 一覧と紙面に出す短い見出し（重要語だけを残した形）。Vault のノートには
    #: 使わず、記事の題名（`title`）はそのまま残す。空なら `title` を使う
    title_short: str = ""
    origin: str = "config"
    reactions: Reactions = field(default_factory=Reactions)
    body_md: str = ""
    extracted_by: str = "none"
    outbound_links: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    #: 記事の代表画像（og:image など）。紙面に載せる。取れなければ空
    image_url: str = ""
    #: article = 通常の記事 / gallery = 個人投稿（写真集にまとめる。本文は書かない）
    kind: str = "article"

    def __post_init__(self) -> None:
        if not self.url_hash:
            self.url_hash = url_hash(self.url)
        if not self.id:
            self.id = self.url_hash[:8]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["reactions"] = self.reactions.to_dict()
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Article":
        d = dict(d)
        d["reactions"] = Reactions.from_dict(d.get("reactions") or {})
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class Story:
    id: str
    title: str
    category: str
    keywords: list[str] = field(default_factory=list)
    article_ids: list[str] = field(default_factory=list)
    first_seen: str = ""
    last_updated: str = ""
    listed_on: str | None = None
    status: str = "carryover"   # carryover | listed | written
    latest_url: str = ""
    #: story = 通常の話題 / gallery = 写真集（記事化すると1ページに数枚並ぶ）
    kind: str = "story"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Story":
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


@dataclass
class ExtractResult:
    """本文取得の1段分の結果。失敗は例外ではなく ok=False で表す段もあるため明示する。"""
    ok: bool
    body_md: str = ""
    extracted_by: str = "none"
    outbound_links: list[str] = field(default_factory=list)
    image_url: str = ""


@dataclass
class Quote:
    symbol: str
    close: float
    change_pct: float


@dataclass
class SocialResult:
    """Grok（X）から得た反応。citations が空でも「反応なし」として正当に成立する。"""
    summary: str | None = None
    citations: list[str] = field(default_factory=list)
    topics: list[str] = field(default_factory=list)


@dataclass
class Failure:
    """縮退の記録。`取得状況` と失敗通知の材料になる。"""
    target: str
    reason: str
    impact: str
    kind: str = "unknown"   # auth | rate_limit | gone | server | timeout | credit | unknown
