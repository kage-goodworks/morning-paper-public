"""本文抽出の単体テスト。AC-13 / AC-14。

`parsers.article_html.to_markdown` は I/O を持たないため純粋関数として扱う
（Spec のテスト対象一覧には無い。docs/PROGRESS.md の Q4 を参照）。
"""

from __future__ import annotations

from pathlib import Path

from src.clients.mock import extract as mock_extract
from src.clients.mock.context import MockContext
from src.clients.parsers import article_html, feed
from tests.conftest import make_article

FIXTURES = Path(__file__).parent / "fixtures"

PAGE = """<!doctype html><html><body>
<nav class="global-nav">ホーム 検索 ログイン</nav>
<div class="ad-banner">広告 いますぐ登録</div>
<article>
  <h2>記事の見出し</h2>
  <p>本文の1段落目である。</p>
  <p>関連は<a href="https://other.invalid/x">こちらの発表</a>を参照。</p>
  <aside class="related-articles">関連記事 おすすめ</aside>
</article>
<footer>会社概要 プライバシーポリシー</footer>
</body></html>"""


def test_ac13_本文だけが抽出されナビゲーションと広告が混ざらない():
    body, links = article_html.to_markdown(PAGE)

    assert "本文の1段落目である。" in body
    assert "記事の見出し" in body

    for noise in ("ホーム", "検索", "ログイン", "広告", "いますぐ登録",
                  "関連記事", "おすすめ", "会社概要", "プライバシーポリシー"):
        assert noise not in body, f"{noise} が混ざっている"


def test_ac13_本文中の外部リンクが抽出される():
    _, links = article_html.to_markdown(PAGE)
    assert links == ["https://other.invalid/x"]


def test_ac13_空のHTMLは空を返し例外にならない():
    assert article_html.to_markdown("") == ("", [])
    assert article_html.to_markdown("   ") == ("", [])


def test_ac14_rssに全文があればtrafilaturaを呼ばずrssが採用される():
    xml = (FIXTURES / "rss" / "day1" / "rust_blog.xml").read_bytes()
    articles = feed.parse(xml, source_id="rust_blog", default_category="lang",
                          fulltext=True)

    assert articles
    assert articles[0].extracted_by == "rss"
    assert articles[0].body_md

    ctx = MockContext()
    stage = mock_extract.MockRssFulltext(ctx)
    result = stage.extract(articles[0])

    assert result.ok is True
    assert result.extracted_by == "rss"


def test_rss標準日時をJSTのISO形式へ直し全文の段落と文字参照を保つ():
    xml = """<?xml version="1.0"?><rss version="2.0"
      xmlns:content="http://purl.org/rss/1.0/modules/content/">
      <channel><item>
        <title>標準RSS</title><link>https://example.invalid/standard</link>
        <pubDate>Mon, 31 Aug 2026 23:00:00 +0000</pubDate>
        <content:encoded><![CDATA[<p>A &amp; B</p><p>次の段落</p>]]></content:encoded>
      </item></channel></rss>""".encode("utf-8")

    article = feed.parse(xml, "rss", "ai", fulltext=True)[0]

    assert article.published_at == "2026-09-01T08:00:00+09:00"
    assert "A & B" in article.body_md
    assert "次の段落" in article.body_md
    assert "\n\n" in article.body_md


def test_広告判定はleadやroadmapという通常クラスを落とさない():
    body, _ = article_html.to_markdown(
        '<main><p class="lead">導入文</p><div class="roadmap">本文</div></main>')
    assert "導入文" in body
    assert "本文" in body


def test_ac14_全文が無ければrss段は失敗して次へ進む():
    ctx = MockContext()
    stage = mock_extract.MockRssFulltext(ctx)
    result = stage.extract(make_article(body_md="", extracted_by="none"))
    assert result.ok is False


def test_フィードのフォールバックは4段が仕様どおりの順で並ぶ():
    ctx = MockContext()
    stages = mock_extract.build(ctx)
    assert [s.name for s in stages] == ["rss", "trafilatura", "jina", "firecrawl"]


def test_必須フィールドが欠けたitemは落とされる():
    xml = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>タイトルのみ</title></item>
      <item><link>https://example.invalid/ok</link></item>
      <item><title>両方ある</title><link>https://example.invalid/x</link></item>
    </channel></rss>""".encode("utf-8")
    articles = feed.parse(xml, source_id="s", default_category="ai")
    assert len(articles) == 1
    assert articles[0].title == "両方ある"


def test_壊れたxmlは例外にせず空を返す():
    assert feed.parse(b"<rss><channel><item>", "s", "ai") == []
    assert feed.parse(b"", "s", "ai") == []


def test_atom形式も同じパーサーで読める():
    xml = (FIXTURES / "rss" / "day1" / "openai_news.xml").read_bytes()
    articles = feed.parse(xml, source_id="openai_news", default_category="ai")
    assert articles
    assert all(a.url.startswith("https://") for a in articles)


def test_youtubeのvideoIdからURLが組める():
    xml = (FIXTURES / "rss" / "youtube" / "sample_channel.xml").read_bytes()
    articles = feed.parse(xml, source_id="yt", default_category="ai")
    assert articles
    assert "youtube.com/watch?v=" in articles[0].url


# --------------------------------------------------------------------------
# フィードの形式（実際のフィードで踏んだ不具合の回帰防止）
# --------------------------------------------------------------------------

RDF_FEED = """<?xml version="1.0" encoding="UTF-8"?>
<rdf:RDF xmlns="http://purl.org/rss/1.0/"
         xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
         xmlns:dc="http://purl.org/dc/elements/1.1/"
         xmlns:content="http://purl.org/rss/1.0/modules/content/">
 <channel rdf:about="https://example.invalid/feed.rdf">
  <title>サンプル</title>
  <items><rdf:Seq><rdf:li rdf:resource="https://example.invalid/a"/></rdf:Seq></items>
 </channel>
 <item rdf:about="https://example.invalid/a">
  <title>RDF形式の記事</title>
  <link>https://example.invalid/a</link>
  <dc:date>2026-09-14T10:50:57+09:00</dc:date>
  <content:encoded><![CDATA[<p>本文がフィードに入っている。</p>]]></content:encoded>
 </item>
</rdf:RDF>"""


def test_RSS1_0のRDF形式を読める():
    """PC Watch・窓の杜・JPCERTなど、日本のサイトで広く使われている形式。

    `<item>` は `<channel>` の外に並び、既定名前空間が付く。名前空間を見ない
    実装だと**まるごと0件になり、情報源が黙って欠ける。**
    """
    articles = feed.parse(RDF_FEED.encode("utf-8"), source_id="pc_watch",
                          default_category="gadget", fulltext=True)

    assert len(articles) == 1
    article = articles[0]
    assert article.title == "RDF形式の記事"
    assert article.url == "https://example.invalid/a"
    assert article.published_at == "2026-09-14T10:50:57+09:00"
    assert "本文がフィードに入っている" in article.body_md
    assert article.extracted_by == "rss"


def test_Atomのcontentからも全文を取れる():
    """Atomは `content:encoded` ではなく `<content>` に本文を入れる。"""
    atom = """<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <entry>
    <title>Atomの記事</title>
    <link rel="alternate" href="https://example.invalid/b"/>
    <published>2026-09-14T09:00:00+09:00</published>
    <content type="html">&lt;p&gt;Atomの本文。&lt;/p&gt;</content>
  </entry>
</feed>"""
    articles = feed.parse(atom.encode("utf-8"), source_id="go_blog",
                          default_category="lang", fulltext=True)

    assert len(articles) == 1
    assert articles[0].url == "https://example.invalid/b"
    assert "Atomの本文" in articles[0].body_md
    assert articles[0].extracted_by == "rss"


def test_3つの形式が同じArticleへそろう():
    """RSS 2.0 / Atom / RDF のどれでも呼び出し側は違いを知らない。"""
    rss2 = """<?xml version="1.0"?><rss version="2.0"><channel>
      <item><title>T</title><link>https://example.invalid/c</link>
      <pubDate>2026-09-14T08:00:00+09:00</pubDate></item></channel></rss>"""
    atom = """<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
      <entry><title>T</title><link href="https://example.invalid/c"/>
      <published>2026-09-14T08:00:00+09:00</published></entry></feed>"""

    results = [feed.parse(x.encode("utf-8"), source_id="s",
                          default_category="ai")
               for x in (rss2, atom, RDF_FEED)]

    for articles in results:
        assert len(articles) == 1
        assert articles[0].url.startswith("https://example.invalid/")
        assert articles[0].published_at
        assert articles[0].category == "ai"



def _mock_run(tmp_path):
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.pipeline import Run
    from src.retry import NO_SLEEP

    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path)
    return Run(config=cfg, clients=clients, date_str="2026-09-01",
               out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)


def test_時間の上限を過ぎた記事は本文を取りに行かず見出しだけで残る(tmp_path):
    """遅いサイトが何件あっても朝のジョブを15分に収める（NFR-6）。"""
    import time
    from src import extract

    run = _mock_run(tmp_path)
    calls = {"n": 0}

    class Slow:
        name = "slow"

        def extract(self, article):
            calls["n"] += 1
            return extract.ExtractResult(ok=True, body_md="本文", extracted_by="slow",
                                         outbound_links=[])

    run.clients.extractors[:] = [Slow()]
    articles = [make_article(id=f"a{i}", url=f"https://example.test/{i}",
                             title=f"記事{i}") for i in range(4)]

    # 期限をすでに過ぎた状態で呼ぶ
    past = time.monotonic() - 1
    out = [extract.extract_one(run, a, past) for a in articles]

    assert calls["n"] == 0
    assert all(a.extracted_by == "none" for a in out)
    assert all(extract.SKIPPED_DEADLINE in a.errors for a in out)

    # 期限内なら普通に取れる
    future = time.monotonic() + 60
    got = extract.extract_one(run, make_article(id="ok", url="https://example.test/ok",
                                                title="取れる"), future)
    assert got.extracted_by == "slow"


def test_上限に達したら取得状況に見送り件数が載る(tmp_path, monkeypatch):
    from src import extract

    run = _mock_run(tmp_path)
    original = run.config.get
    monkeypatch.setattr(
        run.config, "get",
        lambda key, default=None: 0 if key == "limits.extract_seconds"
        else original(key, default))
    articles = [make_article(id=f"b{i}", url=f"https://example.test/b{i}",
                             title=f"記事{i}") for i in range(3)]
    extract.extract_all(run, articles)
    assert any("時間の上限" in line and "3件" in line for line in run.status)


# --- 翻訳の指示と返答の突き合わせ（本番で行数不一致になった） ---

def test_動画の要約に失敗しても通知は出さず_件数だけ取得状況に載る(tmp_path):
    """Gemini は非公開・限定の動画を 403 で断る。鍵の問題ではないので騒がない。"""
    from src import extract
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.pipeline import Run
    from src.retry import NO_SLEEP

    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path, fail="gemini")
    run = Run(config=cfg, clients=clients, date_str="2026-09-01",
              out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)
    videos = [make_article(id=f"v{i}", url=f"https://www.youtube.com/watch?v=abc{i}",
                           title=f"動画{i}") for i in range(2)]

    out = extract.extract_all(run, videos)

    assert all("youtube_summary_failed" in a.errors for a in out)
    assert run.reports == []                                  # Slack へは通知しない
    assert any(line.startswith("YouTube: 2件の動画は要約できませんでした")
               for line in run.status)


def test_翻訳の指示文は番号付きで翻訳と短縮を求める():
    from src import extract
    prompt = extract.build_translate_prompt(["Hello world", "Rust 1.90 released"])
    assert "翻訳" in prompt and "短縮見出し" in prompt
    assert "1. TITLE: Hello world" in prompt and "2. TITLE: Rust 1.90 released" in prompt
    assert extract.HEADLINE_SEP in prompt


def test_訳文と短縮見出しを区切りで拾い_区切りが無ければ同じ文にする():
    from src import extract
    text = ("はい。\n1. こんにちは世界 || 世界に挨拶\n"
            "2. TITLE: Rust 1.90 がリリース||Rust 1.90\n3. 区切りなし")
    assert extract.parse_headlines(text, 3) == {
        1: ("こんにちは世界", "世界に挨拶"),
        2: ("Rust 1.90 がリリース", "Rust 1.90"),
        3: ("区切りなし", "区切りなし"),
    }
    # 番号が無く行数が一致すればモックの形（行順）
    assert extract.parse_headlines("[翻訳]a\n[翻訳]b", 2) == {
        1: ("[翻訳]a", "[翻訳]a"), 2: ("[翻訳]b", "[翻訳]b")}
    assert extract.parse_headlines("何か違うもの", 3) == {}


def test_見出しは英語だけ訳され_短縮は全記事に付き_ノートの題名は変わらない(tmp_path):
    from src import extract
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.pipeline import Run
    from src.retry import NO_SLEEP

    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path)
    run = Run(config=cfg, clients=clients, date_str="2026-09-01",
              out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)
    articles = [make_article(id="e1", url="https://e.test/1", title="Rust 1.90 released"),
                make_article(id="j1", url="https://e.test/2", title="日本語の見出し")]

    out = extract.translate_titles(run, articles)
    english, japanese = out
    assert english.title == "[翻訳]Rust 1.90 released"
    assert english.title_original == "Rust 1.90 released"
    assert english.title_short == "Rust 1.90 released"
    assert japanese.title == "日本語の見出し" and japanese.title_original is None
    assert japanese.title_short == "日本語の見出し"
    assert any(line.startswith("見出し: 2件を短くしました") for line in run.status)


def test_見出しは60件ずつに分けて呼ぶ(tmp_path):
    from src import extract
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.pipeline import Run
    from src.retry import NO_SLEEP

    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path)
    run = Run(config=cfg, clients=clients, date_str="2026-09-01",
              out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)
    calls: list[int] = []
    inner = run.clients.llms["gemini"]

    class Counting:
        name = "gemini"

        def complete(self, prompt, purpose):
            calls.append(prompt.count(". TITLE: "))     # 見出しの行だけ数える
            return inner.complete(prompt, purpose)

    run.clients.llms["gemini"] = Counting()
    articles = [make_article(id=f"a{i}", url=f"https://e.test/{i}", title=f"見出し{i}")
                for i in range(130)]
    extract.translate_titles(run, articles)
    assert calls == [60, 60, 10]
    assert all(a.title_short for a in articles)


def test_返答は番号で拾い_前置きや欠けに耐える():
    from src import extract
    text = "はい、翻訳します。\n\n1. こんにちは世界\n3. Rust 1.90 がリリース\n（以上）"
    assert extract.parse_translations(text, 3) == {1: "こんにちは世界", 3: "Rust 1.90 がリリース"}
    # 全角の番号記号や TITLE: の混入も許す
    assert extract.parse_translations("１．TITLE: 訳", 1) == {1: "訳"}
    # 番号が無く行数が一致すればモックの形（行順）
    assert extract.parse_translations("[翻訳]a\n[翻訳]b", 2) == {1: "[翻訳]a", 2: "[翻訳]b"}
    # 番号も無く行数も違えば失敗
    assert extract.parse_translations("何か違うもの", 3) == {}


# --- 代表画像（og:image） ---

def test_og_imageを拾い_属性の順序が逆でも拾い_httpは使わない():
    from src.clients.parsers import article_html as ah
    assert ah.representative_image(
        '<head><meta property="og:image" content="https://img.test/a.jpg?x=1&amp;y=2"></head>'
    ) == "https://img.test/a.jpg?x=1&y=2"
    assert ah.representative_image(
        '<meta content="https://img.test/b.png" name="twitter:image">') == "https://img.test/b.png"
    assert ah.representative_image('<meta property="og:image" content="http://img.test/c.png">') == ""
    assert ah.representative_image("<p>no meta</p>") == ""


def test_取れた画像は記事に付き_ノートを往復しても残る(tmp_path):
    from src import extract, note
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.models import ExtractResult
    from src.pipeline import Run
    from src.retry import NO_SLEEP

    cfg = load_config()
    run = Run(config=cfg, clients=build_mock(scenario="normal", day=1, out_dir=tmp_path),
              date_str="2026-09-01", out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)

    class WithImage:
        name = "img"
        def extract(self, article):
            return ExtractResult(ok=True, body_md="本文", extracted_by="img",
                                 image_url="https://img.test/hero.jpg")

    run.clients.extractors[:] = [WithImage()]
    a = extract.extract_one(run, make_article(id="p1", url="https://e.test/1", title="t"))
    assert a.image_url == "https://img.test/hero.jpg"

    text = note.build_article(a, story_id="s_1", date_str="2026-09-01")
    assert "image_url: https://img.test/hero.jpg" in text
    back = note.parse_article(text)
    assert back is not None and back.image_url == "https://img.test/hero.jpg"


# --- 写真集（個人投稿） ---

GALLERY_XML = """<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:media="http://search.yahoo.com/mrss/">
  <entry><title>Seal x OG Doubleshot</title><link href="https://r.test/1"/>
    <published>2026-09-17T01:00:00+09:00</published>
    <media:thumbnail url="https://preview.redd.it/abc123.jpg?width=140&amp;height=140&amp;s=sig"/>
    <content type="html">&lt;table&gt;&lt;tr&gt;&lt;td&gt;真鍮ケース。 submitted by /u/someone &lt;a href="x"&gt;[link]&lt;/a&gt;&lt;/td&gt;&lt;/tr&gt;&lt;/table&gt;</content>
  </entry>
  <entry><title>no image post</title><link href="https://r.test/2"/>
    <published>2026-09-17T02:00:00+09:00</published></entry>
</feed>""".encode("utf-8")


def test_写真集の投稿は原寸の画像URLを持ち_画像の無い投稿は落ち_投稿者名は残らない():
    from src.clients.parsers import feed
    arts = feed.parse(GALLERY_XML, source_id="r_kb", default_category="keyboard", kind="gallery")
    assert len(arts) == 1
    a = arts[0]
    assert a.kind == "gallery"
    assert a.image_url == "https://i.redd.it/abc123.jpg"       # 署名付きサムネイル → 原寸
    assert "真鍮ケース" in a.body_md
    assert "submitted by" not in a.body_md and "/u/" not in a.body_md
    # 通常の記事として読めば画像は使わず、画像の無い項目も残る
    plain = feed.parse(GALLERY_XML, source_id="r_kb", default_category="keyboard")
    assert len(plain) == 2 and plain[0].kind == "article" and plain[0].image_url == ""
