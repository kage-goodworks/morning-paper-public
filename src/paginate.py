"""紙面のページ割り。

**外部I/Oを持たない純粋関数（NFR-1）。**記事を落とさない ―― 入りきらなければ
2枚目、3枚目へ送る（AC-15）。文字数から行数を見積もって切るだけで、
LLMは関与しない。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: A4縦・2段組で1ページに収まる行数の見積り。`config.yml` ではなく紙面の
#: 物理的な制約なのでここに置く。テンプレート（paper.css）と対で調整する。
LINES_PER_PAGE = 92
CHARS_PER_LINE = 30
TITLE_LINES = 3
SOURCE_LINES = 2
GAP_LINES = 2


@dataclass
class PaperArticle:
    """紙面に載せる1本。"""
    story_id: str
    title: str
    body_md: str
    sources: list[str] = field(default_factory=list)
    published_dates: list[str] = field(default_factory=list)
    is_continuation: bool = False
    category: str = ""
    image_url: str = ""
    #: article = 記事 / gallery = 写真集（`gallery` に項目が入る）
    kind: str = "article"
    #: 写真集の項目。{title, url, image_url, note}
    gallery: list[dict] = field(default_factory=list)


@dataclass
class Page:
    index: int
    articles: list[PaperArticle] = field(default_factory=list)


def estimate_lines(article: PaperArticle) -> int:
    """1本が占める行数を見積もる。"""
    lines = TITLE_LINES + SOURCE_LINES + GAP_LINES
    for paragraph in (article.body_md or "").split("\n"):
        text = paragraph.strip()
        if not text:
            lines += 1
            continue
        lines += max(1, -(-len(text) // CHARS_PER_LINE))
    return lines


def split_body(article: PaperArticle, available: int) -> tuple[PaperArticle, PaperArticle | None]:
    """記事を2つに割る。入りきらない分を次ページへ送るため。

    段落の境界でだけ割る。文の途中では切らない。
    """
    overhead = TITLE_LINES + SOURCE_LINES + GAP_LINES
    budget = available - overhead
    if budget <= 2:
        return article, None

    head: list[str] = []
    tail: list[str] = []
    used = 0
    for paragraph in (article.body_md or "").split("\n"):
        cost = max(1, -(-len(paragraph.strip()) // CHARS_PER_LINE)) \
            if paragraph.strip() else 1
        if tail or used + cost > budget:
            tail.append(paragraph)
        else:
            head.append(paragraph)
            used += cost

    if not head:
        return article, None
    if not [t for t in tail if t.strip()]:
        return article, None

    first = PaperArticle(story_id=article.story_id, title=article.title,
                         body_md="\n".join(head).strip(), sources=[],
                         published_dates=article.published_dates,
                         is_continuation=article.is_continuation)
    rest = PaperArticle(story_id=article.story_id, title=article.title + "（続き）",
                        body_md="\n".join(tail).strip(), sources=article.sources,
                        published_dates=article.published_dates,
                        is_continuation=True)
    return first, rest


def paginate(articles: list[PaperArticle], *,
             lines_per_page: int = LINES_PER_PAGE) -> list[Page]:
    """ページへ割り付ける。**1本も落とさない**（AC-15）。"""
    pages: list[Page] = []
    current = Page(index=1)
    used = 0

    queue = list(articles)
    while queue:
        article = queue.pop(0)
        need = estimate_lines(article)
        remaining = lines_per_page - used

        if need <= remaining:
            current.articles.append(article)
            used += need
            continue

        # 残りが少なすぎるならページを改める
        if remaining < (TITLE_LINES + SOURCE_LINES + GAP_LINES + 3):
            if current.articles:
                pages.append(current)
                current = Page(index=len(pages) + 1)
                used = 0
            queue.insert(0, article)
            continue

        head, rest = split_body(article, remaining)
        if rest is None:
            if current.articles:
                pages.append(current)
                current = Page(index=len(pages) + 1)
                used = 0
            current.articles.append(article)
            used = estimate_lines(article)
            continue

        current.articles.append(head)
        pages.append(current)
        current = Page(index=len(pages) + 1)
        used = 0
        queue.insert(0, rest)

    if current.articles:
        pages.append(current)
    return pages or [Page(index=1)]


def one_per_page(articles: list[PaperArticle]) -> list[Page]:
    """1ページ1記事（2026-09-16 の紙面）。落とさない（AC-15）。

    長さの調整はページ割りではなく、描画側の文字サイズ調整で行う。
    """
    return [Page(index=i, articles=[a]) for i, a in enumerate(articles, start=1)] \
        or [Page(index=1)]


def story_ids(pages: list[Page]) -> list[str]:
    """載った話題のid（重複を除く）。落ちていないことの確認に使う。"""
    out: list[str] = []
    for page in pages:
        for article in page.articles:
            if article.story_id not in out:
                out.append(article.story_id)
    return out
