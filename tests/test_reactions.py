"""反応まわりの純粋関数のテスト。AC-17 / AC-18 / AC-19。"""

from __future__ import annotations

from src import reactions
from src.clients.parsers import social
from src.models import Reactions, SocialResult
from tests.conftest import make_article

CATEGORIES = ["ai", "lang", "devtools", "infra", "industry", "security"]
THRESHOLDS = {"hn_points": 100, "hatena": 50, "reddit": 50}


def test_ac17_citationsが空なら反応を創作せず定型文になる():
    result = social.parse({"summary": "賛否が割れている", "citations": []})

    assert result.citations == []
    assert result.summary is None
    assert social.summary_text(result) == "目立った反応は見つからなかった"
    assert reactions.summarize(result) == "目立った反応は見つからなかった"


def test_ac17_citationsがあれば要約をそのまま使う():
    result = social.parse({"summary": "価格設定に議論が集まっている",
                           "citations": ["https://x.invalid/1"]})
    assert reactions.summarize(result) == "価格設定に議論が集まっている"


def test_ac17_応答が無い場合も定型文になる():
    assert reactions.summarize(None) == "目立った反応は見つからなかった"
    assert social.summary_text(social.parse(None)) == "目立った反応は見つからなかった"


def test_ac18_正規化結果に投稿本文とアカウント名を持たない():
    payload = {"summary": "傾向の要約", "citations": ["https://x.invalid/1"],
               "posts": [{"author": "@someone", "text": "個人の投稿本文"}]}
    result = social.parse(payload)

    assert not hasattr(result, "posts")
    assert "@someone" not in (result.summary or "")
    assert "個人の投稿本文" not in (result.summary or "")
    assert set(vars(result)) == {"summary", "citations", "topics"}


def test_ac19_x無効のとき無料の反応だけで要約が成立する():
    article = make_article(reactions=Reactions(hn_points=512, hn_comments=210,
                                               hatena=340, reddit=88))
    text = reactions.free_summary(article)

    assert "Hacker Newsで512点・210コメント" in text
    assert "はてなブックマーク340件" in text
    assert "Redditスコア88" in text


def test_ac19_反応が全く無ければ定型文になる():
    article = make_article(reactions=Reactions())
    assert reactions.free_summary(article) == "目立った反応は見つからなかった"


def test_x対象の絞り込みはカテゴリとしきい値の両方を見る():
    high = Reactions(hn_points=150)
    low = Reactions(hn_points=10, hatena=10, reddit=10)

    assert reactions.is_x_target(
        make_article(category="ai", reactions=high),
        categories=CATEGORIES, thresholds=THRESHOLDS) is True

    # しきい値は超えるがカテゴリが対象外
    assert reactions.is_x_target(
        make_article(category="gadget", reactions=high),
        categories=CATEGORIES, thresholds=THRESHOLDS) is False

    # カテゴリは対象だがしきい値未満
    assert reactions.is_x_target(
        make_article(category="ai", reactions=low),
        categories=CATEGORIES, thresholds=THRESHOLDS) is False


def test_しきい値はいずれか1つを超えればよい():
    for field, value in (("hn_points", 100), ("hatena", 50), ("reddit", 50)):
        article = make_article(category="ai",
                               reactions=Reactions(**{field: value}))
        assert reactions.is_x_target(article, categories=CATEGORIES,
                                     thresholds=THRESHOLDS) is True


def test_バッチ分割はbatch_size件ずつに割る():
    items = [make_article(url=f"https://example.invalid/{i}") for i in range(12)]
    chunks = reactions.batches(items, 5)

    assert [len(c) for c in chunks] == [5, 5, 2]
    assert sum(len(c) for c in chunks) == 12


def test_バッチ分割は0でも1件ずつに落ちる():
    items = [make_article(url=f"https://example.invalid/{i}") for i in range(3)]
    assert [len(c) for c in reactions.batches(items, 0)] == [1, 1, 1]


def test_無料の反応の正規化():
    from src.clients.parsers import reactions as parser
    merged = parser.merge(
        {"hits": [{"points": 512, "num_comments": 210}]},
        340,
        {"data": {"children": [{"data": {"score": 88}}]}})
    assert (merged.hn_points, merged.hn_comments, merged.hatena,
            merged.reddit) == (512, 210, 340, 88)


def test_無料の反応は欠損しても0で成立する():
    from src.clients.parsers import reactions as parser
    merged = parser.merge(None, None, None)
    assert (merged.hn_points, merged.hatena, merged.reddit) == (0, 0, 0)
