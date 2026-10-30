"""note.py の単体テスト。AC-16 / AC-20。

Vaultに残すのは「取得した記事そのもの」だけで、AIが加工したもの（Claudeの
解説、紙面画像）は保存しない。その境界もここで固定する。
"""

from __future__ import annotations

import re

from src import note
from src.models import Quote, Reactions
from tests.conftest import make_article, make_story

LABELS = {"ai": "AI・機械学習", "lang": "言語・フレームワーク"}

#: [[AI_GUIDE]] の必須キー。Hermes v1 のフロントマター検査が見る（AC-16）。
REQUIRED_KEYS = ["id", "type", "status", "created", "updated", "summary"]
VALID_TYPES = {"note", "project", "decision", "task", "log", "transcript",
               "reference", "person"}
VALID_STATUS = {"inbox", "proposed", "active", "waiting", "done", "archived"}


def frontmatter_of(text: str) -> str:
    return text.split("---")[1]


def assert_hermes_ok(text: str) -> None:
    """Hermes v1 のフロントマター検査に通る形かを確かめる。"""
    front = frontmatter_of(text)
    for key in REQUIRED_KEYS:
        match = re.search(rf"^{key}:\s*(.*)$", front, re.M)
        assert match, f"{key} が無い"
        assert match.group(1).strip() not in ("", "[]"), f"{key} が空"
    assert "vault: open" in front
    assert re.search(r"^type: (\S+)", front, re.M).group(1) in VALID_TYPES
    assert re.search(r"^status: (\S+)", front, re.M).group(1) in VALID_STATUS


# --------------------------------------------------------------------------
# 記事ノート
# --------------------------------------------------------------------------

def build_article_note(**kwargs) -> str:
    article = kwargs.pop("article", make_article(
        id="a1b2c3d4", url="https://example.invalid/1",
        title="OpenAI が新モデルを発表", source="techcrunch", category="ai",
        published_at="2026-09-01T09:00:00+09:00",
        body_md="# 見出し\n\n本文の1段落目。\n\n本文の2段落目。",
        extracted_by="trafilatura",
        reactions=Reactions(hn_points=512, hn_comments=210, hatena=340,
                            reddit=88)))
    base = dict(story_id="s_a1b2c3", date_str="2026-09-01")
    base.update(kwargs)
    return note.build_article(article, **base)


def test_ac16_記事ノートが必須キーと_vault_open_を持つ():
    assert_hermes_ok(build_article_note())


def test_記事ノートがURLとstory_idを持つ():
    text = build_article_note()
    assert "url: https://example.invalid/1" in text
    assert "story_id: s_a1b2c3" in text
    assert "category: ai" in text
    assert "publisher: techcrunch" in text
    assert "extracted_by: trafilatura" in text


def test_story_idを持つのは話題のまとまりをVault側に残すため():
    """`state.json` から登録が消えても、どの記事が同じ話題かを辿れる。"""
    text = build_article_note()
    assert note.story_id_of(text) == "s_a1b2c3"


def test_記事ノートに整形済みの本文全文が入る():
    text = build_article_note()
    assert "本文の1段落目。" in text
    assert "本文の2段落目。" in text


def test_本文の見出しは2段下げてノートの構造を壊さない():
    text = build_article_note()
    body = text.split("## 本文")[1].split("## 反応")[0]
    assert "### 見出し" in body
    assert "\n# 見出し" not in body


def test_反応の数値が本文と別に持たれる():
    text = build_article_note()
    assert "  hn_points: 512" in text
    assert "- Hacker News 512点 / 210コメント" in text
    assert "- はてなブックマーク 340件" in text


def test_本文が取れなかった記事も見出しとURLで残る():
    text = build_article_note(article=make_article(
        id="x", url="https://example.invalid/x", title="取れなかった記事",
        body_md="", extracted_by="none"))
    assert_hermes_ok(text)
    assert "（本文を取得できませんでした）" in text
    assert "url: https://example.invalid/x" in text


def test_記事ノートを読み戻せる():
    """記事化はこのノートを読んで書く。外部へは取りに行かない（FR-5）。"""
    text = build_article_note()
    article = note.parse_article(text)

    assert article is not None
    assert article.url == "https://example.invalid/1"
    assert article.title == "OpenAI が新モデルを発表"
    assert article.category == "ai"
    assert article.reactions.hn_points == 512
    assert "本文の1段落目。" in article.body_md


def test_本文が無いノートを読むと空の本文になる():
    text = build_article_note(article=make_article(
        id="x", url="https://example.invalid/x", title="t", body_md=""))
    article = note.parse_article(text)
    assert article is not None
    assert article.body_md == ""


def test_壊れたノートはNoneを返し例外にしない():
    assert note.parse_article("") is None
    assert note.parse_article("フロントマターが無い") is None
    assert note.parse_article("---\ntype: reference\n---\n") is None


def test_記事ノートのパスはカテゴリと年月で分かれる():
    article = make_article(id="a1b2c3d4", category="ai",
                           published_at="2026-09-01T09:00:00+09:00")
    assert note.article_path(article) == \
        "40_Knowledge/News/ai/2026/09/2026-09-01-a1b2c3d4.md"


def test_タグの列挙値を勝手に増やさない():
    """AI_GUIDE「機械処理ではプロパティ名と列挙値を変更しない」。"""
    allowed = {"topic/ai", "topic/development", "topic/finance", "topic/health"}
    for category in ("ai", "lang", "keyboard", "career"):
        assert note.CATEGORY_TOPIC[category] in allowed


# --------------------------------------------------------------------------
# デイリーノート
# --------------------------------------------------------------------------

def build_daily(**kwargs) -> str:
    story = kwargs.pop("story", make_story(id="s_a1b2c3", status="listed",
                                           article_ids=["a1", "a2"]))
    articles = kwargs.pop("articles", {
        "a1": make_article(id="a1", url="https://example.invalid/1",
                           title="TechCrunch の見出し", category="ai",
                           published_at="2026-09-01T09:00:00+09:00"),
        "a2": make_article(id="a2", url="https://example.invalid/2",
                           title="ITmedia の見出し", category="ai",
                           published_at="2026-09-01T10:00:00+09:00"),
    })
    base = dict(date_str="2026-09-01", shown={"ai": [story]}, labels=LABELS,
                articles_by_id=articles, status=[])
    base.update(kwargs)
    return note.build_daily(**base)


def test_ac16_デイリーノートが必須キーと_vault_open_を持つ():
    assert_hermes_ok(build_daily())


def test_デイリーノートは題名とURLの一覧である():
    text = build_daily()
    assert "#### 話題 ^s_a1b2c3" in text
    assert "[TechCrunch の見出し](https://example.invalid/1)" in text
    assert "[ITmedia の見出し](https://example.invalid/2)" in text


def test_デイリーノートから記事ノートへリンクする():
    text = build_daily()
    assert "[[40_Knowledge/News/ai/2026/09/2026-09-01-a1|TechCrunch の見出し]]" in text


def test_反応の数値はデイリーに持たない():
    """記事ノート側にあるので、ここへ置くと同じ情報が2か所になる。"""
    articles = {
        "a1": make_article(id="a1", url="https://example.invalid/1",
                           title="見出し",
                           reactions=Reactions(hn_points=512, hatena=340)),
    }
    text = build_daily(story=make_story(id="s_x", article_ids=["a1"]),
                       articles=articles)
    assert "512" not in text
    assert "はてブ" not in text


def test_ac20_収集で3件失敗した入力は取得状況に理由付きで載る():
    status = [
        "収集: 3件のソースを取得できませんでした（stooq, hn, rss）",
        "株価: 取得失敗（Stooqが503、3回リトライ後）",
        "統合: 3件を1つの話題にまとめました",
    ]
    section = build_daily(status=status).split("## 取得状況")[1]
    for line in status:
        assert f"- {line}" in section


def test_取得状況が空でも節は必ず出る():
    text = build_daily(status=[])
    assert "## 取得状況" in text
    assert "- すべて取得できました" in text


def test_未発行があればリンク集への案内が出る():
    text = build_daily(pending_count=6)
    assert "## 未発行" in text
    assert "6件" in text
    assert "[[50_Logs/News/_pending|未発行の一覧]]" in text


def test_株価とX上の話題が節として出る():
    text = build_daily(quotes=[Quote("NVDA", 183.78, 2.1)], x_topics=["価格改定"])
    assert "| NVDA | 183.78 | +2.10% |" in text
    assert "## X上の話題" in text


def test_Claudeが書いた記事はデイリーに保存しない():
    """元データがあれば作り直せるため保存しない。"""
    text = build_daily()
    assert "## 記事" not in text


# --------------------------------------------------------------------------
# 未発行
# --------------------------------------------------------------------------

def test_未発行は上限を超えた話題のリンク集になる():
    stories = [make_story(id="s_1", title="繰り越しの話題", category="ai",
                          article_ids=["a1"], first_seen="2026-09-01")]
    articles = {"a1": make_article(id="a1", title="記事の見出し", category="ai",
                                   published_at="2026-09-01T09:00:00+09:00")}
    text = note.build_pending(date_str="2026-09-02", stories=stories,
                              labels=LABELS, articles_by_id=articles)

    assert "繰り越しの話題" in text
    assert "2026-09-01 から繰り越し" in text
    assert "[[40_Knowledge/News/ai/2026/09/2026-09-01-a1|記事の見出し]]" in text


def test_未発行が0件でも壊れない():
    text = note.build_pending(date_str="2026-09-02", stories=[],
                              labels=LABELS, articles_by_id={})
    assert "未発行の話題はありません" in text


# --------------------------------------------------------------------------
# 週次レビュー
# --------------------------------------------------------------------------

def test_週次レビューのノートも必須キーを持つ():
    text = note.build_weekly(week_str="2026-W36", entries=[
        {"title": "話題A", "body": "反応が変化した。",
         "daily_link": "50_Logs/News/2026-09-01"}])
    assert_hermes_ok(text)
    assert "[[50_Logs/News/2026-09-01]]" in text


def test_Xの要約はノートを往復しても残る():
    from src import note
    from src.models import Reactions
    from tests.conftest import make_article
    a = make_article(id="x1", reactions=Reactions(hn_points=10, x_summary="賛否が割れている"))
    text = note.build_article(a, story_id="s_1", date_str="2026-09-18")
    assert "- X: 賛否が割れている" in text
    back = note.parse_article(text)
    assert back is not None and back.reactions.x_summary == "賛否が割れている"
    assert back.reactions.hn_points == 10
    plain = note.parse_article(note.build_article(make_article(id="x2"), story_id="s_2", date_str="2026-09-18"))
    assert plain.reactions.x_summary is None


def test_見出しの引用符やバックスラッシュはノートを往復しても壊れない():
    from src import note, safety
    from tests.conftest import make_article
    title = 'Release: "v2" — path C:' + chr(92) + "tmp"
    text = note.build_article(make_article(id="q1", title=title), story_id="s_1", date_str="2026-09-18")
    back = note.parse_article(text)
    assert back is not None and back.title == title
    assert safety.yaml_unscalar(safety.yaml_scalar('- "x"')) == '- "x"'
    assert safety.yaml_unscalar("plain") == "plain"
