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
#: 本文を広告で区切って複数並べるサイト（Ars Technica）の本文の枠
_SPLIT_BODY = ("//div[contains(concat(' ', normalize-space(@class), ' '), ' post-content ')]"
               "[not(ancestor::div[contains(concat(' ', normalize-space(@class), ' '),"
               " ' post-content ')])]")


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
        # 続きのページは取れた分だけ足す。失敗しても1ページ目は本文として使える
        for page in article_html.next_page_urls(html, article.url):
            try:
                more, more_links = _to_markdown(http.fetch_text(page, timeout=20.0))
            except Exception:  # noqa: BLE001
                break
            if more:
                body = f"{body}\n\n{more}"
                links = [*links, *more_links]
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


def _join_split_body(html: str):
    """本文の枠が複数あれば1つにまとめる。

    trafilatura は最初の枠しか読まないので、Ars の本文が4分の1になっていた
    （2026-11-06）。枠が1つ以下なら元の文字列をそのまま返す。
    """
    from lxml import html as lxml_html

    try:
        tree = lxml_html.fromstring(html)
    except (ValueError, TypeError, SyntaxError):
        return html
    blocks = tree.xpath(_SPLIT_BODY)
    if len(blocks) < 2:
        return html
    for block in blocks[1:]:
        blocks[0].extend(list(block))
        block.getparent().remove(block)
    return tree


def _to_markdown(html: str) -> tuple[str, list[str]]:
    """HTMLから本文を抜く。`trafilatura` があれば使う。"""
    try:
        import trafilatura
    except ImportError:
        return article_html.to_markdown(html)

    linked = trafilatura.extract(_join_split_body(html), include_links=True,
                                 include_comments=False, output_format="markdown",
                                 favor_precision=True)
    if not linked:
        return article_html.to_markdown(html)
    # 本文の中のリンクだけを拾う。ページ全体から拾うと「人気記事」の欄まで入り、
    # 無関係な話題へ統合される（2026-11-05）
    return article_html.drop_markdown_links(linked).strip(), article_html.markdown_links(linked)
