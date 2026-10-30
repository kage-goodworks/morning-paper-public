"""本文取得4段のモック。

**フォールバックそのものはモックしない。**段ごとの成否だけをフィクスチャの
有無で決め、順に試す判断は本番と同じ `src/extract.py` が行う。

フィクスチャは意図的に歯抜けにしてある（`scripts/gen_fixtures.py`）。
一部の記事は trafilatura 段のHTMLが無く jina が拾い、さらに一部は firecrawl
だけが拾い、いくつかは4段とも取れない。4段の分岐が毎回踏まれる。
"""

from __future__ import annotations

from ...models import Article, ExtractResult
from ..parsers import article_html
from .context import MockContext


class _PageExtractor:
    """保存済みHTMLを1段分として返す共通実装。"""

    name = "none"
    subdir = ""

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def extract(self, article: Article) -> ExtractResult:
        html = self.ctx.read_text("pages", self.subdir, f"{article.url_hash}.html")
        if html is None:
            return ExtractResult(ok=False)
        body, links = article_html.to_markdown(html)
        if not body:
            return ExtractResult(ok=False)
        return ExtractResult(ok=True, body_md=body, extracted_by=self.name,
                             outbound_links=links)


class MockRssFulltext:
    """1段目。フィードに全文があればそれを使う（AC-14）。"""

    name = "rss"

    def __init__(self, ctx: MockContext) -> None:
        self.ctx = ctx

    def extract(self, article: Article) -> ExtractResult:
        if article.extracted_by == "rss" and article.body_md.strip():
            return ExtractResult(ok=True, body_md=article.body_md,
                                 extracted_by="rss", outbound_links=[])
        return ExtractResult(ok=False)


class MockTrafilatura(_PageExtractor):
    name = "trafilatura"
    subdir = "trafilatura"


class MockJinaReader(_PageExtractor):
    name = "jina"
    subdir = "jina"


class MockFirecrawl(_PageExtractor):
    name = "firecrawl"
    subdir = "firecrawl"


def build(ctx: MockContext) -> list:
    """4段を仕様の順に並べて返す。"""
    return [MockRssFulltext(ctx), MockTrafilatura(ctx),
            MockJinaReader(ctx), MockFirecrawl(ctx)]
