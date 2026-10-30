"""`real/` の単体テスト。

**外部へは一切接続しない。**確かめるのは、実装がインタフェースを満たしているか、
取得先の検査が効いているか、書き込み範囲の制限が効いているか、そして共通の
パーサーを通しているか。実サービスとのやり取りは実キー投入の段階で確認する。
"""

from __future__ import annotations

import pytest

from src.clients import base
from src.clients.factory import build_real
from src.clients.real import http as real_http
from pathlib import Path as pathlib_Path

from src.clients.real.vault_git import RealNoteStore, VaultError


# --------------------------------------------------------------------------
# インタフェース
# --------------------------------------------------------------------------

def test_real側もMockと同じProtocolを満たす():
    """差し替えても呼び出し側のコードが1行も変わらないこと。"""
    clients = build_real()

    assert isinstance(clients.feed, base.FeedClient)
    assert isinstance(clients.reactions, base.ReactionClient)
    assert isinstance(clients.releases, base.ReleaseClient)
    assert isinstance(clients.quotes, base.QuoteClient)
    assert isinstance(clients.social, base.SocialClient)
    assert isinstance(clients.search, base.SearchClient)
    assert isinstance(clients.slack, base.SlackClient)
    assert isinstance(clients.notes, base.NoteStore)
    for stage in clients.extractors:
        assert isinstance(stage, base.BodyExtractor)
    for llm in clients.llms.values():
        assert isinstance(llm, base.LLMClient)


def test_本文取得の4段が仕様どおりの順で並ぶ():
    assert [s.name for s in build_real().extractors] == \
        ["rss", "trafilatura", "jina", "firecrawl"]


def test_MockとRealで段の名前が一致する():
    """`extracted_by` に入る値がずれると、ノートの記録が食い違う。"""
    from src.clients.mock import extract as mock_extract
    from src.clients.mock.context import MockContext

    real_names = [s.name for s in build_real().extractors]
    mock_names = [s.name for s in mock_extract.build(MockContext())]
    assert real_names == mock_names


def test_MockとRealで執筆モデルの名前が一致する():
    from src.clients.factory import build_mock
    assert sorted(build_real().llms) == sorted(build_mock().llms)


# --------------------------------------------------------------------------
# 取得先の検査（SSRF）
# --------------------------------------------------------------------------

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/feed.xml",
    "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.5/x",
    "file:///etc/passwd",
    "javascript:alert(1)",
])
def test_許可されない取得先は接続する前に弾く(url):
    """フィードやGrokが返したURLをそのまま取りに行かない。"""
    with pytest.raises(real_http.FetchError, match="許可されていない"):
        real_http.fetch_bytes(url)


def test_RSS取得も検査を通る():
    from src.clients.real.rss import RealFeedClient
    with pytest.raises(real_http.FetchError):
        RealFeedClient().fetch_articles(
            {"id": "x", "url": "http://localhost/feed.xml", "category": "ai"})


def test_URLが無いソースは空を返す():
    from src.clients.real.rss import RealFeedClient
    assert RealFeedClient().fetch_articles({"id": "x", "category": "ai"}) == []


# --------------------------------------------------------------------------
# Vaultの書き込み範囲
# --------------------------------------------------------------------------

@pytest.mark.parametrize("rel_path", [
    "50_Logs/News/2026-09-11.md",
    "50_Logs/News/_pending.md",
    "40_Knowledge/News/ai/2026/09/2026-09-11-a1b2c3d4.md",
    "40_Knowledge/News-Review/2026-W37.md",
    "40_Knowledge/Tech/ai/Claude Code.md",
    "40_Knowledge/Tech/_INDEX.md",
])
def test_宣言したパスへは書ける(rel_path):
    RealNoteStore._check_path(rel_path)


@pytest.mark.parametrize("rel_path", [
    "AI_GUIDE.md",
    "20_Projects/Active/Hermes/Spec.md",
    "50_Logs/Automation/x.md",
    "../etc/passwd",
    "50_Logs/News/../../AI_GUIDE.md",
    "/etc/passwd",
])
def test_宣言していないパスへは書かない(rel_path):
    """[[AI_GUIDE]]「宣言したパス以外へ書き込まない」。"""
    with pytest.raises(VaultError):
        RealNoteStore._check_path(rel_path)


def test_deploy_keyが無ければcloneしない():
    import os
    store = RealNoteStore()
    saved = os.environ.pop("VAULT_DEPLOY_KEY", None)
    try:
        with pytest.raises(VaultError, match="VAULT_DEPLOY_KEY"):
            store.read("50_Logs/News/2026-09-11.md")
    finally:
        if saved is not None:
            os.environ["VAULT_DEPLOY_KEY"] = saved


def test_commitは自動化の約束を守る(monkeypatch, tmp_path):
    """[[AI_GUIDE]]「auto: を付ける」「rebase・force push・履歴の書き換えをしない」。"""
    calls: list[tuple[str, ...]] = []

    store = RealNoteStore(workdir=None)
    store._root = tmp_path
    (tmp_path / "50_Logs" / "News").mkdir(parents=True)   # 存在する範囲だけ add する
    store._dirty = True
    monkeypatch.setattr(store, "_ensure_clone", lambda: store._root)
    monkeypatch.setattr(
        store, "_git",
        lambda *args, **kwargs: (calls.append(args),
                                 " M 50_Logs/News/x.md" if args[0] == "status"
                                 else "")[1])

    store.commit_push("news 2026-09-11")

    commit = next(c for c in calls if "commit" in c)
    assert commit[commit.index("-m") + 1].startswith("auto: ")
    # runner には git の名義が無いので、commit 側で名乗る
    assert any(a.startswith("user.name=") for a in commit)
    assert any(a.startswith("user.email=") for a in commit)

    flat = " ".join(" ".join(c) for c in calls)
    assert "--force" not in flat
    assert "rebase" not in flat
    assert "push origin HEAD:main" in flat
    # まだ無いディレクトリは add に渡さない（pathspec エラーで落ちるため）
    add = next(c for c in calls if c[0] == "add")
    assert "50_Logs/News/" in add and "40_Knowledge/News-Review/" not in add


# --------------------------------------------------------------------------
# キーが無いときの振る舞い
# --------------------------------------------------------------------------

def test_キーが無ければ例外を投げて縮退へ流す(monkeypatch):
    """黙って空を返すと、欠けたことが取得状況に載らない。"""
    from src.clients.real import grok, search, slack
    from src.models import Article

    for name in ("XAI_API_KEY", "FIRECRAWL_API_KEY", "SLACK_BOT_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    article = Article(id="a", url="https://example.com/x", title="t",
                      source="s", category="ai", published_at="2026-09-11")

    with pytest.raises(RuntimeError, match="XAI_API_KEY"):
        grok.RealSocialClient().reactions([article])
    with pytest.raises(RuntimeError, match="FIRECRAWL_API_KEY"):
        search.RealSearchClient().search("q", "2026-09-11")
    with pytest.raises(RuntimeError, match="SLACK_BOT_TOKEN"):
        slack.RealSlackClient(channel="C1").post_message([], "t")


def test_Firecrawlはキーが無ければ次の段へ渡す(monkeypatch):
    """本文取得は4段のフォールバック。ここだけは例外ではなく ok=False。"""
    from src.clients.real.extract import RealFirecrawl
    from src.models import Article

    monkeypatch.delenv("FIRECRAWL_API_KEY", raising=False)
    article = Article(id="a", url="https://example.com/x", title="t",
                      source="s", category="ai", published_at="2026-09-11")
    assert RealFirecrawl().extract(article).ok is False


def test_RSS全文の段は取りに行かない():
    """1段目は取得済みの本文を使うだけ。相手に負荷をかけない。"""
    from src.clients.real.extract import RealRssFulltext
    from src.models import Article

    with_body = Article(id="a", url="https://example.com/x", title="t",
                        source="s", category="ai", published_at="2026-09-11",
                        body_md="本文", extracted_by="rss")
    result = RealRssFulltext().extract(with_body)
    assert result.ok is True
    assert result.extracted_by == "rss"

    without = Article(id="b", url="https://example.com/y", title="t",
                      source="s", category="ai", published_at="2026-09-11")
    assert RealRssFulltext().extract(without).ok is False


# --------------------------------------------------------------------------
# 設定
# --------------------------------------------------------------------------

def test_執筆に使うモデルが最新世代である():
    from src.clients.real.llm import RealClaude
    assert RealClaude().model == "claude-opus-5"


def test_取得の上限とタイムアウトが決まっている():
    assert real_http.MAX_BYTES <= 10 * 1024 * 1024
    assert 5 <= real_http.DEFAULT_TIMEOUT <= 60


def test_UserAgentを名乗る():
    assert "morning-paper" in real_http.USER_AGENT


def test_投稿先は設定から渡せる():
    """チャンネルIDは秘密ではないので config.yml に置く（NFR-7）。"""
    clients = build_real(slack_channel="C0C1FCDE0EP")
    assert clients.slack.channel == "C0C1FCDE0EP"


def test_設定が空なら環境変数へ落ちる(monkeypatch):
    monkeypatch.setenv("SLACK_CHANNEL", "C_FROM_ENV")
    assert build_real(slack_channel="").slack.channel == "C_FROM_ENV"


def test_どちらも無ければ例外を投げる(monkeypatch):
    monkeypatch.delenv("SLACK_CHANNEL", raising=False)
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="チャンネル"):
        _ = build_real(slack_channel="").slack.channel


# --- Claude Code の非対話モード経由 ---

def test_claude_pの応答から本文を取り出す():
    from src.clients.real import llm
    assert llm._cli_result({"type": "result", "subtype": "success",
                            "is_error": False, "stop_reason": "end_turn",
                            "result": "  本文  "}) == "本文"


def test_claude_pのエラーと拒否は例外にして降格へ乗せる():
    import pytest as _pytest
    from src.clients.real import llm
    with _pytest.raises(RuntimeError, match="エラー"):
        llm._cli_result({"is_error": True, "subtype": "error_during_execution",
                         "result": "boom"})
    with _pytest.raises(RuntimeError, match="拒否"):
        llm._cli_result({"is_error": False, "stop_reason": "refusal", "result": ""})
    with _pytest.raises(RuntimeError):
        llm._cli_result("not a dict")


def test_認証の種類で経路が決まる(monkeypatch):
    from src.clients.real import llm
    c = llm.RealClaude()
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "x")
    assert c._via_cli() is True
    monkeypatch.setenv("ANTHROPIC_API_KEY", "y")      # API キーがあれば SDK
    assert c._via_cli() is False
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    assert c._via_cli() is False


def test_claude_pの呼び出しはトークンをコマンドラインに載せない(monkeypatch):
    """秘密は環境変数のまま渡す。引数に出るとプロセス一覧やログに残る。"""
    import subprocess
    from src.clients.real import llm

    seen = {}

    def fake_run(command, **kwargs):
        seen["command"] = command
        seen["input"] = kwargs.get("input")
        class P:
            returncode = 0
            stdout = '{"is_error": false, "stop_reason": "end_turn", "result": "ok"}'
            stderr = ""
        return P()

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-SECRET")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = llm.RealClaude(cli_path="claude")
    assert c.complete("TITLE: t", "write") == "ok"
    assert "SECRET" not in " ".join(seen["command"])
    assert "--tools" in seen["command"] and "-p" in seen["command"]
    assert seen["input"] == "TITLE: t"


def test_geminiは本命が混雑なら候補へ渡り歩き_それ以外の失敗はそのまま投げる(monkeypatch):
    from src.clients.real import http, llm
    tried = []

    def fake(model, prompt, purpose):
        tried.append(model)
        if model == "a":
            raise http.FetchError("HTTP 503 Service Unavailable")
        if model == "b":
            raise http.ClientError("HTTP 404 Not Found")
        return "ok from " + model

    g = llm.RealGemini(model="a", fallbacks=["b", "c"])
    monkeypatch.setattr(g, "_complete_with", fake)
    assert g.complete("x", "translate") == "ok from c"
    assert tried == ["a", "b", "c"]

    tried.clear()
    def bad(model, prompt, purpose):
        tried.append(model); raise http.ClientError("HTTP 400 Bad Request")
    monkeypatch.setattr(g, "_complete_with", bad)
    import pytest as _pytest
    with _pytest.raises(http.ClientError):
        g.complete("x", "translate")
    assert tried == ["a"]   # 400 は候補を変えても直らないので渡り歩かない


def test_アップロードURLの取得はフォーム形式で送る(monkeypatch, tmp_path):
    """files.getUploadURLExternal は JSON を受け取らない（本番で invalid_arguments）。"""
    from src.clients.real import http, slack

    calls = []
    monkeypatch.setattr(http, "post_form", lambda url, fields, **kw: (
        calls.append(("form", url, fields)) or {"ok": True, "upload_url": "https://up.test/x", "file_id": "F1"}))
    monkeypatch.setattr(http, "post_json", lambda url, payload, **kw: (
        calls.append(("json", url, payload)) or {"ok": True, "files": [{"id": "F1"}]}))

    class R:
        def __enter__(self): return self
        def __exit__(self, *a): return False
        def read(self, n=-1): return b""
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=0: R())
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")

    png = tmp_path / "p.png"; png.write_bytes(b"\x89PNG....")
    client = slack.RealSlackClient(channel="C1")
    client.upload_image("1.2", str(png), "紙面")

    kinds = [(k, url.rsplit("/", 1)[-1]) for k, url, _ in calls]
    assert ("form", "files.getUploadURLExternal") in kinds
    assert ("json", "files.completeUploadExternal") in kinds
    form_fields = next(f for k, u, f in calls if k == "form")
    assert form_fields["filename"] == "p.png" and form_fields["length"] == str(png.stat().st_size)


def test_permalinkの取得はフォーム形式で送る(monkeypatch):
    from src.clients.real import http, slack
    calls = []
    monkeypatch.setattr(http, "post_form", lambda url, fields, **kw: (
        calls.append(url.rsplit("/", 1)[-1]) or {"ok": True, "permalink": "https://x.slack.com/p1"}))
    monkeypatch.setattr(http, "post_json", lambda url, payload, **kw: (
        calls.append("json:" + url.rsplit("/", 1)[-1]) or {"ok": True}))
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-test")
    client = slack.RealSlackClient(channel="C1")
    assert client._permalink("1.2") == "https://x.slack.com/p1"
    assert calls == ["chat.getPermalink"]


# --- 点検で見つかった3件（2026-09-16 その2） ---

def test_pushに失敗しても書いたノートは残り_再試行でもう一度commitする(monkeypatch, tmp_path):
    """reset --hard だと変更ごと消え、再試行が「変更なし」で成功扱いになる。"""
    calls: list[tuple[str, ...]] = []
    pushes = {"n": 0}

    store = RealNoteStore(workdir=None)
    store._root = tmp_path
    (tmp_path / "50_Logs" / "News").mkdir(parents=True)
    store._dirty = True
    monkeypatch.setattr(store, "_ensure_clone", lambda: store._root)

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args[0] == "status":
            return " M 50_Logs/News/x.md"          # 変更は残っている
        if args[0] == "push":
            pushes["n"] += 1
            if pushes["n"] == 1:
                raise VaultError("push rejected")
        return ""

    monkeypatch.setattr(store, "_git", fake_git)

    import pytest as _pytest
    with _pytest.raises(VaultError):
        store.commit_push("news 2026-09-16")
    assert ("reset", "--soft", "HEAD~1") in calls
    assert not any("--hard" in c for c in calls)

    store.commit_push("news 2026-09-16")          # 再試行
    assert pushes["n"] == 2
    assert sum(1 for c in calls if "commit" in c) == 2


def test_反応は一部の取得先が落ちても前回の値を保ち_全部落ちたら失敗にする(monkeypatch):
    from src.clients.real import hatena, hn, reactions, reddit
    from src.models import Reactions
    from tests.conftest import make_article

    article = make_article(id="x1", reactions=Reactions(hn_points=120, hn_comments=40,
                                                          hatena=30, reddit=7))
    monkeypatch.setattr(hn, "fetch", lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    monkeypatch.setattr(hatena, "fetch", lambda url: 55)
    monkeypatch.setattr(reddit, "fetch", lambda url: {"data": {"children": []}})

    got = reactions.RealReactionClient().fetch(article)
    assert (got.hn_points, got.hn_comments) == (120, 40)   # 落ちた HN は前回の値
    assert got.hatena == 55                                 # 取れたものは新しい値

    for mod in (hatena, reddit):
        monkeypatch.setattr(mod, "fetch", lambda url: (_ for _ in ()).throw(RuntimeError("down")))
    import pytest as _pytest
    with _pytest.raises(RuntimeError, match="3つとも失敗"):
        reactions.RealReactionClient().fetch(article)


def test_cloneに失敗したら取得先を確定させず_再試行でもう一度cloneする(monkeypatch, tmp_path):
    calls = []
    store = RealNoteStore(workdir=tmp_path)
    monkeypatch.setattr(store, "_write_key", lambda: None)
    attempts = {"n": 0}

    def fake_git(*args, cwd=None):
        calls.append(args[0])
        if args[0] == "clone":
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise VaultError("clone failed")
            (tmp_path / "Open-Vault").mkdir()
        return ""

    monkeypatch.setattr(store, "_git", fake_git)
    import pytest as _pytest
    with _pytest.raises(VaultError):
        store._ensure_clone()
    assert store._root is None                 # 失敗したら確定しない
    root = store._ensure_clone()               # 再試行で clone し直す
    assert calls.count("clone") == 2 and root == tmp_path / "Open-Vault"


# --- YouTube Data API ---

def test_youtube_apiの応答を記事にし_キーが無ければRSSへ落ちる(monkeypatch):
    from src.clients.parsers import youtube_api
    from src.clients.real import http, rss

    assert youtube_api.channel_id_of("https://www.youtube.com/feeds/videos.xml?channel_id=UCu8Jtlcv4Q2P4VeGwwiPnTg") == "UCu8Jtlcv4Q2P4VeGwwiPnTg"
    assert youtube_api.uploads_playlist_id("UCu8Jtlcv4Q2P4VeGwwiPnTg") == "UUu8Jtlcv4Q2P4VeGwwiPnTg"
    assert youtube_api.uploads_playlist_id("bad") == ""

    payload = {"items": [
        {"snippet": {"title": "新型キーボード紹介", "publishedAt": "2026-09-17T08:00:00Z",
                     "resourceId": {"videoId": "abc123"},
                     "thumbnails": {"high": {"url": "https://i.ytimg.com/vi/abc123/hqdefault.jpg"}}}},
        {"snippet": {"title": "Private video", "resourceId": {"videoId": "zzz"}}},
    ]}
    arts = youtube_api.parse_playlist_items(payload, source_id="yt_x", default_category="gadget")
    assert len(arts) == 1
    a = arts[0]
    assert a.url == "https://www.youtube.com/watch?v=abc123"
    assert a.published_at == "2026-09-17T17:00:00+09:00"
    assert a.image_url.startswith("https://i.ytimg.com/")

    source = {"id": "yt_x", "type": "youtube", "category": "gadget",
              "url": "https://www.youtube.com/feeds/videos.xml?channel_id=UCu8Jtlcv4Q2P4VeGwwiPnTg"}
    client = rss.RealFeedClient()
    monkeypatch.delenv("YOUTUBE_API_KEY", raising=False)
    assert client._fetch_youtube_api(source) is None                 # キーが無い → RSS へ

    monkeypatch.setenv("YOUTUBE_API_KEY", "k")
    seen = {}
    monkeypatch.setattr(http, "fetch_json", lambda url, **kw: (seen.setdefault("url", url), payload)[1])
    got = client.fetch_articles(source)
    assert [x.url for x in got] == ["https://www.youtube.com/watch?v=abc123"]
    assert "playlistId=UUu8Jtlcv4Q2P4VeGwwiPnTg" in seen["url"] and "key=k" in seen["url"]

    # API が落ちたら RSS へ落ちる
    monkeypatch.setattr(http, "fetch_json", lambda url, **kw: (_ for _ in ()).throw(http.FetchError("HTTP 403")))
    monkeypatch.setattr(http, "fetch_bytes", lambda url, **kw: b'<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom" xmlns:yt="http://www.youtube.com/xml/schemas/2015"><entry><title>rss video</title><yt:videoId>r1</yt:videoId><published>2026-09-17T00:00:00Z</published></entry></feed>')
    got = client.fetch_articles(source)
    assert [x.title for x in got] == ["rss video"]


def test_Slackが絵文字を名前に直して返しても同じ本文とみなす():
    """✅ は `:white_check_mark:` で返る。そのまま比べると自分の投稿を見失う。"""
    from src.clients.real.slack import text_key
    sent = "✅ 2026-10-02（金）の記事 102件を生成しました  `#abc123`"
    back = ":white_check_mark: 2026-10-02（金）の記事 102件を生成しました  `#abc123`"
    assert text_key(sent) == text_key(back)
    assert text_key("📎 続き（続き 2/3）") == text_key(":paperclip: 続き（続き 2/3）")
    # 中身が違えば別物
    assert text_key(sent) != text_key(back.replace("102件", "101件"))
    assert text_key("2026-10-02 のニュース") == "2026-10-02 のニュース"


def test_共有の確認は履歴に現れるまで待ち_現れなければ失敗にする(monkeypatch):
    from src.clients.real import slack as real_slack
    client = real_slack.RealSlackClient(channel="C_MAIN")
    monkeypatch.setattr(real_slack.RealSlackClient, "SHARE_WAITS", (0.0, 0.0, 0.0))
    seen = {"n": 0, "channels": []}

    def fake_find(text, *, within_hours=36.0, channel=None):
        seen["n"] += 1
        seen["channels"].append(channel)
        return {"ts": "1.2", "permalink": "https://slack.invalid/p"} if seen["n"] >= 2 else None

    monkeypatch.setattr(client, "find_message", fake_find)
    assert client._confirm_shared("✅ x", "C_ARTICLES") == {"ts": "1.2", "permalink": "https://slack.invalid/p"}
    assert seen["channels"] == ["C_ARTICLES", "C_ARTICLES"]

    monkeypatch.setattr(client, "find_message", lambda *a, **k: None)
    assert client._confirm_shared("✅ x", "C_ARTICLES") is None

