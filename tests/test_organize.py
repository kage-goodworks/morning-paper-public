"""organize.py の単体テスト。AC-1 / AC-2 / AC-7〜AC-12。"""

from __future__ import annotations

from src import organize
from tests.conftest import make_article, make_story

ORDER = ["ai", "lang", "devtools", "infra", "industry", "security",
         "gadget", "keyboard", "career"]


def test_ac1_utm_付きと無しの同一記事が1件に統合される():
    articles = [
        make_article(url="https://example.invalid/p?utm_source=news&utm_medium=rss",
                     title="OpenAI が新モデルを発表"),
        make_article(url="https://example.invalid/p",
                     title="OpenAI が新モデルを発表"),
    ]
    kept, dropped = organize.dedupe(articles)
    assert len(kept) == 1
    assert dropped == 1
    assert kept[0].url == "https://example.invalid/p"


def test_正規化後も記事idとurl_hashが同じURLを指す():
    article = make_article(
        url="https://www.example.invalid/p/amp/?utm_source=news",
        title="正規化対象")

    kept, _ = organize.dedupe([article])

    assert kept[0].id == kept[0].url_hash[:8]
    assert kept[0].id != ""


def test_ac1_fbclid_と_amp_も正規化される():
    assert organize.normalize_url(
        "https://www.example.invalid/news/amp/?fbclid=abc&id=7"
    ) == "https://example.invalid/news?id=7"


def test_ac2_9カテゴリ50本がカテゴリごとに新しい順で並ぶ():
    articles = []
    for index in range(50):
        category = ORDER[index % 9]
        articles.append(make_article(
            url=f"https://example.invalid/{index}",
            title=f"見出し{index}", category=category,
            published_at=f"2026-09-01T{index % 24:02d}:00:00+09:00"))

    grouped = organize.group_by_category(articles, ORDER)

    assert list(grouped.keys()) == [c for c in ORDER if grouped.get(c)]
    assert sum(len(v) for v in grouped.values()) == 50
    for items in grouped.values():
        ordered = organize.sort_newest_first(items)
        times = [a.published_at for a in ordered]
        assert times == sorted(times, reverse=True)


def test_ac7_固有名詞が6割重なる新着が繰り越し話題へ統合される():
    carry = make_story(keywords=["OpenAI", "GPT-5", "API", "Azure", "OSS"],
                       article_ids=["a1"])
    new = make_article(id="a2", url="https://example.invalid/follow",
                       title="OpenAI GPT-5 の API 提供が Azure でも開始",
                       category="ai")

    stories, merged = organize.build_stories([new], [carry],
                                             today="2026-09-02", overlap=0.5)

    assert merged == 1
    assert len(stories) == 1
    assert stories[0].article_ids == ["a1", "a2"]
    assert stories[0].last_updated == "2026-09-02"


def test_ac8_カテゴリが違えば統合しない():
    carry = make_story(keywords=["OpenAI", "GPT-5"], category="ai")
    new = make_article(id="a2", url="https://example.invalid/x",
                       title="OpenAI GPT-5 を使ったライブラリ", category="lang")

    stories, merged = organize.build_stories([new], [carry], today="2026-09-02")

    assert merged == 0
    assert len(stories) == 2


def test_ac9_listed_の話題とは統合しない():
    listed = make_story(keywords=["OpenAI", "GPT-5"], status="listed")
    new = make_article(id="a2", url="https://example.invalid/x",
                       title="OpenAI GPT-5 の続報", category="ai")

    stories, merged = organize.build_stories([new], [listed], today="2026-09-02")

    assert merged == 0
    assert len(stories) == 2


def test_ac9_written_の話題とも統合しない():
    written = make_story(keywords=["OpenAI", "GPT-5"], status="written")
    new = make_article(id="a2", url="https://example.invalid/x",
                       title="OpenAI GPT-5 の続報", category="ai")

    _, merged = organize.build_stories([new], [written], today="2026-09-02")

    assert merged == 0


def test_ac10_本文中のリンクが既存記事を指すとキーワード無関係に統合される():
    carry = make_story(keywords=["Rust", "Cargo"], article_ids=["a1"])
    new = make_article(id="a2", url="https://example.invalid/other",
                       title="まったく違う見出し", category="ai",
                       outbound_links=["https://example.invalid/original"])

    stories, merged = organize.build_stories(
        [new], [carry], today="2026-09-02",
        article_urls={"s_000001": ["https://example.invalid/original"]})

    assert merged == 1
    assert stories[0].article_ids == ["a1", "a2"]


def test_ac11_8日前の繰り越し話題は候補から外れる():
    old = make_story(first_seen="2026-08-25", status="carryover")
    fresh = make_story(id="s_000002", first_seen="2026-08-27",
                       status="carryover")

    kept, removed = organize.expire([old, fresh], today="2026-09-02",
                                    carryover_days=7)

    assert removed == 1
    assert [s.id for s in kept] == ["s_000002"]


def test_ac11_listed_の話題は期限で消えない():
    listed = make_story(first_seen="2026-08-01", status="listed")
    kept, removed = organize.expire([listed], today="2026-09-02",
                                    carryover_days=7)
    assert removed == 0
    assert len(kept) == 1


def test_ac12_seen_に記録済みのURLは一覧に出ない():
    article = make_article(url="https://example.invalid/known")
    seen = {organize.__dict__["hashlib"].sha256(
        b"https://example.invalid/known").hexdigest()[:16]: "2026-09-01"}

    kept, dropped = organize.dedupe([article], seen=seen)

    assert kept == []
    assert dropped == 1


def test_上限を超えたカテゴリは新しい順で切られる():
    stories = [make_story(id=f"s_{i:06d}", category="ai",
                          last_updated=f"2026-09-{(i % 28) + 1:02d}")
               for i in range(18)]

    shown, counts = organize.apply_limits(stories, order=ORDER, per_category=12)

    assert len(shown["ai"]) == 12
    assert counts["ai"] == (18, 12)


def test_固有名詞の抽出はストップワードを外す():
    keywords = organize.extract_keywords(
        "The OpenAI GPT-5 and Rust 1.90 owner/repo リリース",
        stopwords=["the", "and"])
    lowered = [k.lower() for k in keywords]
    assert "openai" in lowered
    assert "gpt-5" in lowered
    assert "1.90" in lowered
    assert "the" not in lowered
    assert "and" not in lowered


def test_seen_は30日で刈り取られる():
    seen = {"a": "2026-08-01", "b": "2026-09-01"}
    pruned = organize.prune_seen(seen, today="2026-09-05", days=30)
    assert pruned == {"b": "2026-09-01"}


# --- 対象期間（Requirements「前回成功時点以降。初回は直近24時間」） ---

def test_対象期間より前の記事は落ち_以降と日時なしは残る():
    articles = [
        make_article(id="old", published_at="2026-08-20T09:00:00+09:00"),
        make_article(id="edge", published_at="2026-08-31T05:00:00+09:00"),
        make_article(id="new", published_at="2026-09-01T02:00:00+09:00"),
        make_article(id="nodate", published_at=""),
    ]
    kept, dropped = organize.within_window(articles, "2026-08-31T05:00:00+09:00")
    assert [a.id for a in kept] == ["edge", "new", "nodate"]
    assert dropped == 1


def test_初回の対象期間は前日の05時JSTから():
    assert organize.default_window_start("2026-09-01") == "2026-08-31T05:00:00+09:00"


def test_UTC表記の日時も同じ物差しで比べられる():
    # 2026-08-31T05:00 JST = 2026-08-30T20:00Z
    articles = [make_article(id="z_before", published_at="2026-08-30T19:59:00Z"),
                make_article(id="z_after", published_at="2026-08-30T20:00:00Z")]
    kept, dropped = organize.within_window(articles, "2026-08-31T05:00:00+09:00")
    assert [a.id for a in kept] == ["z_after"]
    assert dropped == 1


def test_sinceが読めなければ絞らない():
    articles = [make_article(id="a", published_at="2000-01-01T00:00:00+09:00")]
    kept, dropped = organize.within_window(articles, "")
    assert len(kept) == 1 and dropped == 0


# --- 株価（Yahoo Finance chart の正規化） ---

def test_yahoo_chartの応答から終値と当日の変化率を取る():
    from src.clients.parsers import quotes
    payload = {"chart": {"result": [{
        "meta": {"symbol": "NVDA", "regularMarketPrice": 183.78},
        "indicators": {"quote": [{"open": [178.0, None, 180.0]}]},
    }]}}
    q = quotes.parse_chart(payload)
    assert q is not None
    assert q.symbol == "NVDA" and q.close == 183.78 and q.change_pct == 2.1


def test_yahoo_chartの形が違えばNone():
    from src.clients.parsers import quotes
    assert quotes.parse_chart({"chart": {"result": []}}) is None
    assert quotes.parse_chart({"chart": {"error": "x"}}) is None
    assert quotes.parse_chart({"chart": {"result": [{"meta": {"symbol": "X",
        "regularMarketPrice": 1.0}, "indicators": {"quote": [{"open": [None]}]}}]}}) is None


# --- キーワードは単語として一致したときだけ（本番で誤分類が出た） ---

def test_英字キーワードは部分一致しない():
    assert organize.keyword_hits("How Fyxer built an assistant people trust", "Rust") is False
    assert organize.keyword_hits("Google's James Manyika discuss space", "Go") is False
    assert organize.keyword_hits("NASA's decision on a Mars spacecraft", "CI") is False
    assert organize.keyword_hits("digital twins", "Git") is False
    assert organize.keyword_hits("Rust 1.90 released", "Rust") is True
    assert organize.keyword_hits("Go 1.26 is out", "Go") is True
    assert organize.keyword_hits("CI/CD pipelines", "CI") is True
    assert organize.keyword_hits("OpenAI announces GPT-6", "GPT") is True


def test_日本語キーワードは部分一致のまま():
    assert organize.keyword_hits("最新の脆弱性情報まとめ", "脆弱性") is True
    assert organize.keyword_hits("HHKB Studioレビュー", "HHKB") is True


# --- 前回成功時点（一部の記事を保存できなかったとき） ---

def test_保存できなかった記事があれば_その最古の公開時刻までしか進めない():
    collected = "2026-09-16T04:41:00+09:00"
    unsaved = [make_article(id="u1", published_at="2026-09-15T20:00:00+09:00"),
               make_article(id="u2", published_at="2026-09-16T01:00:00+09:00")]
    assert organize.success_point(collected, unsaved) == "2026-09-15T20:00:00+09:00"
    assert organize.success_point(collected, []) == collected
    # 公開時刻が読めない記事があれば進めない（空）
    assert organize.success_point(collected, [make_article(id="u3", published_at="")]) == ""
    # 収集時刻より新しい公開時刻なら収集時刻まで進めてよい
    assert organize.success_point(collected, [make_article(id="u4", published_at="2026-09-16T09:00:00+09:00")]) == collected


# --- 点検で見つかった3件（2026-09-16 その4） ---

def test_落ちていたソースは自分の前回成功時点から拾い_さかのぼりは3日まで():
    since = "2026-09-16T04:40:00+09:00"
    per_source = {"a": "2026-09-13T04:40:00+09:00",     # 3日落ちていた
                  "b": "2026-09-01T04:40:00+09:00"}     # 15日落ちていた
    arts = [
        make_article(id="a_new", source="a", published_at="2026-09-14T12:00:00+09:00"),
        make_article(id="a_old", source="a", published_at="2026-09-12T12:00:00+09:00"),
        make_article(id="b_cap", source="b", published_at="2026-09-13T12:00:00+09:00"),  # 3日以内
        make_article(id="b_far", source="b", published_at="2026-09-05T12:00:00+09:00"),  # 3日より前
        make_article(id="c_old", source="c", published_at="2026-09-15T12:00:00+09:00"),  # 全体の窓の外
    ]
    kept, dropped = organize.within_window(arts, since, per_source=per_source)
    assert [a.id for a in kept] == ["a_new", "b_cap"]
    assert dropped == 3


def test_期限切れの繰り越しは統合の前に落ちるので_新着は自分の話題として残る():
    old = make_story(id="s_old", title="Rust 1.90 の話題", category="lang",
                     keywords=["Rust", "1.90"], first_seen="2026-09-01",
                     last_updated="2026-09-01", status="carryover")
    live, expired = organize.expire([old], today="2026-09-16", carryover_days=7)
    assert live == [] and expired == 1
    new = make_article(id="n1", title="Rust 1.90 がリリース", category="lang",
                       published_at="2026-09-16T01:00:00+09:00")
    stories, merged = organize.build_stories([new], live, today="2026-09-16",
                                             overlap=0.5, stopwords=[], article_urls={})
    assert merged == 0 and len(stories) == 1 and stories[0].article_ids == ["n1"]


# --- 写真集の話題 ---

def test_個人投稿はカテゴリごとに1つの写真集にまとまり_新しい順に上限まで():
    items = [make_article(id=f"p{i}", kind="gallery", category="keyboard",
                          image_url=f"https://i.test/{i}.jpg",
                          published_at=f"2026-09-17T{i:02d}:00:00+09:00") for i in range(8)]
    items.append(make_article(id="noimg", kind="gallery", category="keyboard", image_url=""))
    stories, chosen = organize.build_galleries(items, today="2026-09-17",
                                               labels={"keyboard": "キーボード"}, per_gallery=6)
    assert len(stories) == 1
    s = stories[0]
    assert s.kind == "gallery" and s.id.startswith("g_") and s.title == "🖼 キーボードの写真集"
    assert s.article_ids == ["p7", "p6", "p5", "p4", "p3", "p2"]     # 新しい順に6件
    assert s.latest_url == "https://example.invalid/a" and len(chosen) == 6
    assert organize.build_galleries([], today="2026-09-17", labels={}) == ([], [])
