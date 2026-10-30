"""paginate.py の単体テスト。AC-15（記事を落とさない）。"""

from __future__ import annotations

from src import paginate

PARA = "これは紙面に載る本文の段落です。" * 3


def article(story_id: str, paragraphs: int = 6, **kwargs):
    base = dict(story_id=story_id, title=f"{story_id} の見出し",
                body_md="\n\n".join([PARA] * paragraphs),
                sources=[f"https://example.invalid/{story_id}"])
    base.update(kwargs)
    return paginate.PaperArticle(**base)


def test_ac15_入りきらなければ2枚目3枚目へ送り1本も落とさない():
    articles = [article(f"s_{i}") for i in range(12)]
    pages = paginate.paginate(articles)

    assert len(pages) > 1
    assert paginate.story_ids(pages) == [a.story_id for a in articles]


def test_ac15_1本が長すぎる場合も落とさず続きへ送る():
    articles = [article("s_long", paragraphs=60)]
    pages = paginate.paginate(articles)

    assert paginate.story_ids(pages) == ["s_long"]
    body = "".join(a.body_md for p in pages for a in p.articles)
    assert body.count("これは紙面に載る本文の段落です。") == 60 * 3


def test_ac15_続きのページには続きと分かる見出しが付く():
    pages = paginate.paginate([article("s_long", paragraphs=60)])
    titles = [a.title for p in pages for a in p.articles]
    assert titles[0] == "s_long の見出し"
    assert any("（続き）" in t for t in titles[1:])


def test_ac15_時系列が分かるよう複数記事の日付を保持する():
    dates = ["2026-09-01", "2026-09-02", "2026-09-03"]
    pages = paginate.paginate([article("s_1", published_dates=dates)])
    assert pages[0].articles[0].published_dates == dates


def test_出典は続きの側に付く():
    pages = paginate.paginate([article("s_long", paragraphs=60)])
    last = [a for p in pages for a in p.articles][-1]
    assert last.sources == ["https://example.invalid/s_long"]


def test_記事が0本でも1ページ返る():
    pages = paginate.paginate([])
    assert len(pages) == 1
    assert pages[0].articles == []


def test_ページ番号が1から連番になる():
    pages = paginate.paginate([article(f"s_{i}") for i in range(12)])
    assert [p.index for p in pages] == list(range(1, len(pages) + 1))


def test_行数の見積りは段落数に応じて増える():
    short = paginate.estimate_lines(article("s", paragraphs=1))
    long = paginate.estimate_lines(article("s", paragraphs=10))
    assert long > short
