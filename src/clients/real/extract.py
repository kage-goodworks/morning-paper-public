"""本文取得の4段（RSS全文 / trafilatura / Jina Reader / Firecrawl）。

**順に試すのは呼び出し側**（`src/extract.py`）。ここは1段ずつの取得だけを行う。
モックでも本番でも同じフォールバックの判断を通る。

取りに行く先はフィードが返したURLなので、`http.fetch_*` のSSRF検査を必ず通す。
"""

from __future__ import annotations

import os

from ...models import Article, ExtractResult
from ..parsers import article_html
from . import http

JINA_ENDPOINT = "https://r.jina.ai/{url}"
FIRECRAWL_ENDPOINT = "https://api.firecrawl.dev/v1/scrape"


class RealRssFulltext:
    """1段目。フィードの `content:encoded` をそのまま使う。

    取りに行かないので最も速く、相手にも負荷をかけない。`config.yml` で
    `fulltext: true` にしたソースだけがここで済む。
    """

    name = "rss"

    def extract(self, article: Article) -> ExtractResult:
        body = (article.body_md or "").strip()
        if article.extracted_by == "rss" and body:
            return ExtractResult(ok=True, body_md=body, extracted_by="rss",
                                 outbound_links=[])
        return ExtractResult(ok=False)


class RealTrafilatura:
    """2段目。記事ページを取得して本文だけを抜く。

    `trafilatura` が入っていればそれを使い、無ければ `parsers/article_html.py`
    の抽出で代替する。**どちらでも同じ `ExtractResult` を返す**ので、
    呼び出し側は違いを知らない。
    """

    name = "trafilatura"

    def extract(self, article: Article) -> ExtractResult:
        html = http.fetch_text(article.url, timeout=20.0)
        body, links = _to_markdown(html)
        if not body:
            return ExtractResult(ok=False)
        return ExtractResult(ok=True, body_md=body, extracted_by=self.name,
                             outbound_links=links,
                             image_url=article_html.representative_image(html))


class RealJinaReader:
    """3段目。`https://r.jina.ai/<URL>` がMarkdownを返す。

    JavaScriptで描画するページでも本文が取れることがある。認証は不要だが、
    `JINA_API_KEY` があればレート制限が緩くなる。
    """

    name = "jina"

    def extract(self, article: Article) -> ExtractResult:
        headers = {"Accept": "text/plain"}
        token = os.environ.get("JINA_API_KEY")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        text = http.fetch_text(JINA_ENDPOINT.format(url=article.url),
                               timeout=30.0, headers=headers)
        body = (text or "").strip()
        if not body:
            return ExtractResult(ok=False)
        return ExtractResult(ok=True, body_md=body, extracted_by=self.name,
                             outbound_links=[])


class RealFirecrawl:
    """4段目。最後の手段。

    無料枠は月1,000クレジット（`limits.firecrawl_monthly` は900で余裕を見る）。
    ここまで落ちてくる記事は多くないが、使いすぎないよう1段目〜3段目を先に
    試す順序は崩さない。
    """

    name = "firecrawl"

    def extract(self, article: Article) -> ExtractResult:
        key = os.environ.get("FIRECRAWL_API_KEY")
        if not key:
            return ExtractResult(ok=False)

        payload = http.post_json(
            FIRECRAWL_ENDPOINT,
            {"url": article.url, "formats": ["markdown"], "onlyMainContent": True},
            headers={"Authorization": f"Bearer {key}"},
            timeout=60.0)

        data = (payload or {}).get("data") or {}
        body = str(data.get("markdown") or "").strip()
        if not body:
            return ExtractResult(ok=False)

        links = [u for u in (data.get("links") or []) if str(u).startswith("http")]
        return ExtractResult(ok=True, body_md=body, extracted_by=self.name,
                             outbound_links=links[:50])


def _to_markdown(html: str) -> tuple[str, list[str]]:
    """HTMLから本文を抜く。`trafilatura` があれば使う。"""
    try:
        import trafilatura
    except ImportError:
        return article_html.to_markdown(html)

    body = trafilatura.extract(html, include_links=False, include_comments=False,
                               output_format="markdown", favor_precision=True)
    if not body:
        return article_html.to_markdown(html)
    # リンクの抽出は自前の方が確実なので、本文だけ trafilatura を使う
    _, links = article_html.to_markdown(html)
    return body.strip(), links
