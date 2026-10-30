"""safety.py の単体テスト。

**フィードの中身は信用できない。**タイトル・URL・日時・カテゴリは外部が決める値で、
そのまま使うと次の4つが起きる。ここはその回帰防止。

1. ファイルの置き場所をVaultの外へずらされる
2. ノートのフロントマターへ任意のキーを差し込まれる
3. Slackへ任意のリンクを出される
4. 内部ネットワークへの取得を踏ませられる
"""

from __future__ import annotations

import yaml

from src import blocks, note, safety
from tests.conftest import make_article, make_story

CATEGORIES = set(note.CATEGORY_TOPIC)


# --------------------------------------------------------------------------
# パスの組み立て
# --------------------------------------------------------------------------

def test_日時にパス区切りが混ざってもディレクトリを抜けられない():
    for evil in ("../../../../etc/passwd", "..", "/", "2026/09/01",
                 "....//....//x", ""):
        article = make_article(id="aaaa1111", category="ai", published_at=evil)
        path = note.article_path(article)
        assert ".." not in path
        assert path.startswith("40_Knowledge/News/ai/")
        assert path.count("/") == 5, path


def test_カテゴリにパス区切りが混ざっても既知の区分へ落ちる():
    """カテゴリは探索（Grokや検索連携の応答）由来にもなる。"""
    for evil in ("../../../../evil", "ai/../..", "AI", "存在しない", ""):
        article = make_article(id="bbbb2222", category=evil,
                               published_at="2026-09-01T09:00:00+09:00")
        path = note.article_path(article)
        assert ".." not in path
        segment = path.split("/")[2]
        assert segment in CATEGORIES or segment == safety.UNKNOWN_SEGMENT


def test_記事idは16進しか受け付けない():
    assert safety.safe_id("a1b2c3d4") == "a1b2c3d4"
    for evil in ("../../x", "a1b2/c3d4", "zzzz", "", "a" * 64):
        assert safety.safe_id(evil) == safety.UNKNOWN_SEGMENT


def test_正常な値はそのまま通る():
    article = make_article(id="a1b2c3d4", category="ai",
                           published_at="2026-09-01T09:00:00+09:00")
    assert note.article_path(article) == \
        "40_Knowledge/News/ai/2026/09/2026-09-01-a1b2c3d4.md"


def test_存在しない日付は弾く():
    assert safety.safe_date("2026-13-01") == safety.UNKNOWN_DATE
    assert safety.safe_date("2026-09-32") == safety.UNKNOWN_DATE
    assert safety.safe_date("2026-09-01T09:00:00+09:00") == "2026-09-01"


# --------------------------------------------------------------------------
# フロントマター
# --------------------------------------------------------------------------

def test_URLの改行でフロントマターへキーを差し込めない():
    """`vault: private` を差し込まれると Open Vault の対象から外れて見えなくなる。"""
    evil_url = "https://e.test/x\nstatus: active\nvault: private\nmalicious: yes"
    article = make_article(id="cccc3333", url=evil_url, category="ai",
                           published_at="2026-09-01T09:00:00+09:00")

    text = note.build_article(article, story_id="s_x", date_str="2026-09-01")
    front = yaml.safe_load(text.split("---")[1])

    assert "malicious" not in front
    assert front["status"] == "done"
    assert front["vault"] == "open"
    assert front["type"] == "reference"


def test_タイトルと媒体名とstory_idからも差し込めない():
    article = make_article(id="dddd4444", title="見出し\nvault: private",
                           source="s\ntype: task", category="ai",
                           published_at="2026-09-01T09:00:00+09:00")
    text = note.build_article(article, story_id="s_x\nstatus: inbox",
                              date_str="2026-09-01")
    front = yaml.safe_load(text.split("---")[1])

    assert front["status"] == "done"
    assert front["type"] == "reference"
    assert front["vault"] == "open"


def test_yaml_scalarは改行と制御文字を落とす():
    assert "\n" not in safety.yaml_scalar("a\nb")
    assert "\x00" not in safety.yaml_scalar("a\x00b")
    assert safety.yaml_scalar("") == '""'
    assert safety.yaml_scalar("true").startswith('"')   # 真偽値に化けさせない


def test_壊れたノートを書いても読み戻せる():
    article = make_article(id="eeee5555", url="https://e.test/x",
                           title="タイトル: コロン入り", category="ai",
                           published_at="2026-09-01T09:00:00+09:00")
    text = note.build_article(article, story_id="s_x", date_str="2026-09-01")
    assert yaml.safe_load(text.split("---")[1]) is not None
    assert note.parse_article(text) is not None


# --------------------------------------------------------------------------
# Slack
# --------------------------------------------------------------------------

def test_タイトルからSlackへ任意のリンクを差し込めない():
    """`>` を素通しすると、そこでリンクが終わり別のリンクが描画される。"""
    story = make_story(id="s_1", title="正常>|<https://evil.test|ここを押せ",
                       article_ids=["a"], latest_url="https://ok.test/1")
    label = blocks.story_label(story)

    # エスケープ後も文字列は残るが、Slackはリンクとして描画しない。
    # 生の `<` `>` が1組だけ＝本物のリンク1つだけが形成される。
    assert label.count("<") == 1
    assert label.count(">") == 1
    assert label.startswith("<https://ok.test/1|")
    assert "&lt;https://evil.test" in label


def test_記事化済みのラベルもエスケープされる():
    done = make_story(id="s_1", title="済み>|<https://evil.test|偽",
                      article_ids=["a"], latest_url="https://ok.test/1")
    messages = blocks.build_update(
        date_str="2026-09-02", shown={"ai": [done]}, labels={"ai": "AI"},
        status=[], written={"s_1": "https://slack.invalid/t1"})
    label = next(b["elements"][0]["text"] for m in messages for b in m
                 if b["type"] == "context" and "✅" in b["elements"][0]["text"])
    assert label.startswith("✅ ① ")
    assert "<" not in label and "&lt;https://evil.test" in label
    heading = next(b["text"]["text"] for m in messages for b in m
                   if b["type"] == "section" and b.get("block_id") == "links_ai")
    # 見出しのリンクは本物の2つ（元記事と紙面）だけ
    assert heading.count("<") == 2 and heading.count(">") == 2


def test_slack_escapeは3文字だけを実体参照にする():
    assert safety.slack_escape("a<b>c&d") == "a&lt;b&gt;c&amp;d"
    assert safety.slack_escape("日本語はそのまま") == "日本語はそのまま"


def test_取得先にできないURLはリンクにしない():
    story = make_story(id="s_1", title="題名", article_ids=["a"],
                       latest_url="javascript:alert(1)")
    assert blocks.story_label(story) == "題名"


# --------------------------------------------------------------------------
# Markdownリンク
# --------------------------------------------------------------------------

def test_markdownリンクは角括弧と丸括弧で壊れない():
    link = safety.markdown_link("題[名]です", "https://e.test/a(b)c")
    assert link.count("[") == 1
    assert link.count("]") == 1
    assert "(b)" not in link
    assert link.startswith("[題［名］です](")


# --------------------------------------------------------------------------
# 取得先のURL（SSRF）
# --------------------------------------------------------------------------

def test_内部アドレスへは取りに行かない():
    for blocked in (
        "http://127.0.0.1/x", "http://localhost/x", "https://[::1]/x",
        "http://169.254.169.254/latest/meta-data/",   # クラウドのメタデータ
        "http://10.0.0.5/x", "http://192.168.1.1/x", "http://172.16.0.1/x",
        "http://0.0.0.0/x", "http://metadata.google.internal/x",
        "http://api.internal/x", "http://foo.localhost/x",
    ):
        assert safety.is_fetchable(blocked) is False, blocked


def test_httpとhttps以外は取りに行かない():
    for blocked in ("file:///etc/passwd", "ftp://e.test/x", "gopher://e.test",
                    "javascript:alert(1)", "data:text/html,x", "", "not a url"):
        assert safety.is_fetchable(blocked) is False, blocked


def test_普通のURLは通る():
    for allowed in ("https://example.com/a", "http://news.example.co.jp/b",
                    "https://8.8.8.8/c"):
        assert safety.is_fetchable(allowed) is True, allowed
