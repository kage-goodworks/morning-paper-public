"""render.py のHTML生成のテスト。AC-15。

Playwright は起動しない。`build_html` は純粋関数で、テンプレートを引数で
渡せるためファイルも読まない。
"""

from __future__ import annotations

from src import paginate, render

TEMPLATE = ("<html><head><style>{{CSS}}</style><title>{{TITLE}}</title></head>"
            "<body><div class='date'>{{DATE}}</div>"
            "<div class='page'>{{PAGE}}/{{TOTAL}}</div>"
            "{{ARTICLES}}</body></html>")


def page_with(*articles) -> paginate.Page:
    return paginate.Page(index=1, articles=list(articles))


def make(**kwargs) -> paginate.PaperArticle:
    base = dict(story_id="s_1", title="話題の見出し",
                body_md="## 小見出し\n\n本文の段落。",
                sources=["https://example.invalid/1"],
                published_dates=["2026-09-01"])
    base.update(kwargs)
    return paginate.PaperArticle(**base)


def build(page, total=1) -> str:
    return render.build_html(title="朝刊", date_str="2026-09-03", page=page,
                             total_pages=total, css="body{}", template=TEMPLATE)


def test_ac15_記事3本を含む紙面が生成され1本も落ちない():
    articles = [make(story_id=f"s_{i}", title=f"話題{i}") for i in range(3)]
    html = build(page_with(*articles))

    for i in range(3):
        assert f"話題{i}" in html
    assert html.count("<article class=\"story\">") == 3


def test_ac15_複数記事の話題は時系列が分かる形になる():
    html = build(page_with(make(
        published_dates=["2026-09-01", "2026-09-02", "2026-09-03"])))

    assert "経過:" in html
    assert "2026-09-01 → 2026-09-02 → 2026-09-03" in html


def test_日付が1つだけなら経過を出さない():
    html = build(page_with(make(published_dates=["2026-09-01"])))
    assert "経過:" not in html


def test_出典が紙面に載る():
    html = build(page_with(make(sources=["https://example.invalid/1",
                                         "https://example.invalid/2"])))
    assert "出典:" in html
    assert "https://example.invalid/1" in html
    assert "https://example.invalid/2" in html


def test_ページ番号と総ページ数が入る():
    html = render.build_html(title="朝刊", date_str="2026-09-03",
                             page=paginate.Page(index=2, articles=[make()]),
                             total_pages=3, css="", template=TEMPLATE)
    assert "2/3" in html


def test_本文のMarkdownが見出しと段落へ変換される():
    html = build(page_with(make(body_md="## 小見出し\n\n1段落目。\n\n2段落目。")))
    assert "<h2>小見出し</h2>" in html
    assert "<p>1段落目。</p>" in html
    assert "<p>2段落目。</p>" in html


def test_HTMLがエスケープされ差し込みが起きない():
    html = build(page_with(make(title="<script>alert(1)</script>",
                                body_md="<img onerror=x>")))
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html
    assert "<img onerror=x>" not in html


def test_記事が0本でも紙面が組める():
    html = build(paginate.Page(index=1, articles=[]))
    assert "<html>" in html
    assert "朝刊" in html


def test_実テンプレートは日本語フォントとグレースケールAAを指定している():
    """PNGの色にじみを避けるための指定が落ちていないことの確認。"""
    css = (render.TEMPLATE_DIR / "paper.css").read_text(encoding="utf-8")
    assert "-webkit-font-smoothing: antialiased" in css
    assert "Noto Sans JP" in css
    assert "Noto Serif JP" in css


def test_pdf用htmlは全ページを1文書にまとめ_出典と見出しが押せるリンクになる():
    """Slack へは PDF 1つを送る。文字が選べ、リンクが埋まる。"""
    from src import render
    from src.paginate import Page, PaperArticle

    def page(i, url):
        return Page(index=i, articles=[PaperArticle(story_id=f"s{i}",
            title=f"見出し{i}", body_md="本文", sources=[url], published_dates=[])])

    pages = [page(1, "https://a.test/1"), page(2, "javascript:alert(1)")]
    html = render.build_pdf_html(pages, title="朝刊", date_str="2026-09-03")
    assert html.count('<div class="sheet">') == 2
    assert html.count("<html") == 1 and html.count("<body>") == 1
    assert "@page { size: 1240px 1754px" in html
    assert '<a href="https://a.test/1">見出し1</a>' in html
    assert '<a href="https://a.test/1">https://a.test/1</a>' in html
    assert 'href="javascript' not in html          # 危険なURLはリンクにしない
    assert "見出し2" in html
