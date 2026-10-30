"""執筆結果の部品分解と、1ページ1記事の紙面組み。"""

from __future__ import annotations

from src import render
from src.clients.parsers import article_sections as secs
from src.paginate import Page, PaperArticle

SAMPLE = """LEAD: 一言のリード
RECAP:
前回まで: 先月に予告があった。

先行提供は限定的だった。
POINTS:
- 要点その1
- 要点その2
BODY:
本文の1段落目。

| 項目 | 前 | 後 |
|---|---|---|
| 上限 | 1 | 10 |

本文の3段落目。
WHY: 自分には設定変更が要らない。
TERMS:
- 一般提供: 誰でも使える状態
- 従量課金 — 使った分だけ払う
"""


def test_部品を見出しで分ける():
    s = secs.parse(SAMPLE)
    assert s.lead == "一言のリード"
    assert s.recap.startswith("先月に予告があった。")      # 「前回まで:」は落とす
    assert "先行提供は限定的だった。" in s.recap
    assert s.points == ["要点その1", "要点その2"]
    assert s.body.startswith("本文の1段落目。") and "| 上限 | 1 | 10 |" in s.body
    assert s.why == "自分には設定変更が要らない。"
    assert s.terms == [("一般提供", "誰でも使える状態"), ("従量課金", "使った分だけ払う")]
    assert s.is_structured


def test_見出しが無ければ全体を本文にする():
    s = secs.parse("ただの文章。\n\n2段落目。")
    assert s.body == "ただの文章。\n\n2段落目。"
    assert not s.is_structured and s.points == [] and s.lead == ""


def test_太字や全角コロンの見出しも受ける():
    s = secs.parse("**LEAD：** 太字の見出し\nBODY：\n本文")
    assert s.lead == "太字の見出し" and s.body == "本文"


def _article(body, **kw):
    base = dict(story_id="s1", title="見出し<b>", body_md=body,
                sources=["https://a.test/1"], category="ai")
    base.update(kw)
    return PaperArticle(**base)


def test_紙面は部品ごとの枠を持ち_欠けた部品の枠は出ない():
    html = render.build_article_sheet(
        _article(SAMPLE), date_str="2026-09-16", labels={"ai": "AI・機械学習"},
        index=1, total=3)
    assert 'class="sheet" style="--accent:#1f4e79"' in html
    assert ">AI・機械学習<" in html and "1 / 3" in html
    assert 'class="lead">一言のリード' in html
    assert '<h1 class="headline"><a href="https://a.test/1">見出し&lt;b&gt;</a></h1>' in html
    assert "前回まで" in html and "先月に予告があった。" in html
    assert "<li>要点その1</li>" in html
    assert "<table>" in html and "<th>項目</th>" in html and "<td>10</td>" in html
    assert "自分との関係" in html and "<dt>一般提供</dt>" in html

    plain = render.build_article_sheet(
        _article("本文だけ。"), date_str="2026-09-16", labels={}, index=1, total=1)
    for cls in ("lead", "recap", "points", "why", "terms"):
        assert f'class="{cls}"' not in plain
    assert "<p>本文だけ。</p>" in plain


def test_長い本文は2段組になる():
    long = "あ" * 800
    html = render.build_article_sheet(_article(long), date_str="d", labels={}, index=1, total=1)
    assert 'class="body two-columns"' in html
    short = render.build_article_sheet(_article("短い"), date_str="d", labels={}, index=1, total=1)
    assert 'class="body">' in short


def test_文書は全記事を1つにまとめ_印刷用の設定を持つ():
    pages = [Page(index=1, articles=[_article("一")]), Page(index=2, articles=[_article("二", story_id="s2")])]
    html = render.build_article_document(pages, date_str="2026-09-16", labels={},
                                         css="@page { size: 1240px 1754px; }", template="<x>{{CSS}}{{SHEETS}}</x>")
    assert html.count('class="sheet"') == 2
    assert "1 / 2" in html and "2 / 2" in html
    assert "@page" in html


def test_代表画像があれば見出しの下に図版が入り_無ければ枠ごと出ない():
    with_img = render.build_article_sheet(
        _article("本文", image_url="https://img.test/hero.jpg"),
        date_str="d", labels={}, index=1, total=1)
    assert '<figure class="hero"><img src="https://img.test/hero.jpg"' in with_img
    assert with_img.index('class="headline"') < with_img.index('class="hero"')
    without = render.build_article_sheet(_article("本文"), date_str="d", labels={}, index=1, total=1)
    assert "hero" not in without
    bad = render.build_article_sheet(_article("本文", image_url="javascript:x"),
                                     date_str="d", labels={}, index=1, total=1)
    assert "hero" not in bad


def test_写真集のページは写真と題名と一言が並び_本文は無い():
    items = [{"title": "Seal x OG", "url": "https://r.test/1", "image_url": "https://i.test/1.jpg", "note": "真鍮ケース"},
             {"title": "no note", "url": "https://r.test/2", "image_url": "javascript:x", "note": ""}]
    art = PaperArticle(story_id="g1", title="🖼 キーボードの写真集", body_md="", sources=[],
                       category="keyboard", kind="gallery", gallery=items)
    html = render.build_gallery_sheet(art, date_str="2026-09-17", labels={"keyboard": "キーボード"}, index=1, total=1)
    assert 'class="headline gallery-title">キーボードの写真集</h1>' in html   # 絵文字は落とす
    assert html.count('<figure class="cell">') == 2
    assert '<img src="https://i.test/1.jpg"' in html and "javascript" not in html
    assert '<a href="https://r.test/1">Seal x OG</a>' in html
    assert '<div class="note">真鍮ケース</div>' in html and html.count('class="note"') == 1
    assert 'class="body' not in html
    # 文書に混ぜると写真集のページとして組まれる
    doc = render.build_article_document([Page(index=1, articles=[art])], date_str="d", labels={},
                                        css="", template="<x>{{CSS}}{{SHEETS}}</x>")
    assert "gallery-sheet" in doc
