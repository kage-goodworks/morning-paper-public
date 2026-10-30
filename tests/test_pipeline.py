"""通信なしの結合テスト。

純粋関数だけでなく、**実装後の形に近い状態**で動くことを確かめる。
モック層は通すが、判断・整形・保存の処理は本番と同じコードを通る。

ネットワークもgitも触らない。APIキーが1つも無い状態で全経路が通ることも
ここで固定する。
"""

from __future__ import annotations

import os
import json

import pytest

from src import articles as articles_mod
from src import main as main_mod
from src import discover, note, slack as slack_mod, weekly as weekly_mod
from src.clients.factory import build_mock
from src.config import load as load_config
from src.pipeline import Run
from src.retry import NO_SLEEP
from src.state import State, load as load_state, save as save_state
from tests.conftest import make_article

SECRET_ENV = ("SLACK_BOT_TOKEN", "SLACK_SIGNING_SECRET", "ANTHROPIC_API_KEY",
              "CLAUDE_CODE_OAUTH_TOKEN", "GEMINI_API_KEY", "XAI_API_KEY",
              "FIRECRAWL_API_KEY", "GH_DISPATCH_TOKEN", "VAULT_DEPLOY_KEY")


@pytest.fixture(autouse=True)
def no_api_keys(monkeypatch):
    """**APIキーが1つも無い状態**を作る。実キー投入前と同じ条件。"""
    for name in SECRET_ENV:
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def workspace(tmp_path):
    """実行のたびに使い捨てのout/を用意する。"""
    def make(day: int = 1, date_str: str = "2026-09-01",
             fail: str | None = None, scenario: str = "normal",
             phase: str = "morning") -> Run:
        cfg = load_config()
        clients = build_mock(scenario=scenario, day=day, out_dir=tmp_path,
                            phase=phase, fail=fail)
        return Run(config=cfg, clients=clients, date_str=date_str,
                   out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)
    make.root = tmp_path
    return make


# --------------------------------------------------------------------------
# 朝の処理
# --------------------------------------------------------------------------

def test_朝の処理が最後まで通り一覧とノートが出る(workspace):
    run = workspace()
    state = State()
    result = main_mod.run_morning(run, state)

    assert result["articles"] > 0
    assert result["listed"] > 0
    assert result["messages"] >= 1

    vault = workspace.root / "vault"
    assert (vault / "50_Logs/News/2026-09-01.md").exists()
    assert (vault / "50_Logs/News/_pending.md").exists()
    assert list((vault / "40_Knowledge/News").rglob("*.md"))

    slack = sorted((workspace.root / "slack").glob("*-post.json"))
    assert slack, "一覧が投稿されていない"


def test_APIキーが無くても朝の処理は完走する(workspace):
    for name in SECRET_ENV:
        assert os.environ.get(name) is None
    run = workspace()
    result = main_mod.run_morning(run, State())
    assert result["listed"] > 0


def test_上限を超えた話題が未発行へ回る(workspace):
    run = workspace()
    state = State()
    result = main_mod.run_morning(run, state)

    assert result["unpublished"] > 0
    pending = (workspace.root / "vault/50_Logs/News/_pending.md").read_text(
        encoding="utf-8")
    assert "繰り越し" in pending
    carried = [s for s in state.all_stories() if s.status == "carryover"]
    assert len(carried) == result["unpublished"]


def test_記事ノートは1記事1ファイルでstory_idを持つ(workspace):
    run = workspace()
    state = State()
    main_mod.run_morning(run, state)

    notes = list((workspace.root / "vault/40_Knowledge/News").rglob("*.md"))
    assert len(notes) >= 10
    for path in notes[:5]:
        text = path.read_text(encoding="utf-8")
        assert note.story_id_of(text), f"{path.name} に story_id が無い"
        assert note.parse_article(text) is not None


def test_一覧に載った話題は消化済みで未発行に入らない(workspace):
    run = workspace()
    state = State()
    main_mod.run_morning(run, state)

    listed = {s.id for s in state.all_stories() if s.status == "listed"}
    pending = (workspace.root / "vault/50_Logs/News/_pending.md").read_text(
        encoding="utf-8")
    for story_id in list(listed)[:5]:
        assert story_id not in pending


# --------------------------------------------------------------------------
# 縮退と探索
# --------------------------------------------------------------------------

def test_株価が落ちても一覧は出る(workspace):
    run = workspace(fail="stooq")
    result = main_mod.run_morning(run, State())

    assert result["listed"] > 0
    assert any("株価" in line for line in result["status"])
    # 失敗通知は残す。ただしチャンネルではなく、その日の一覧のスレッドへ出す
    # （一覧のチャンネルを一覧だけに保つ。本人の要望、2026-10-02）
    threads = [json.loads(p.read_text(encoding="utf-8"))
               for p in (workspace.root / "slack").glob("*-thread.json")]
    assert threads, "失敗通知が出ていない"
    assert all(t["thread_ts"] == result["message_ts"] for t in threads)
    assert not list((workspace.root / "slack").glob("*-failure.json"))


def test_失敗通知はスレッドに出せなければチャンネルへ出す(workspace):
    run = workspace(fail="stooq")
    run.config.data.setdefault("slack", {})["errors_in_thread"] = False
    try:
        main_mod.run_morning(run, State())
    finally:
        run.config.data["slack"]["errors_in_thread"] = True
    assert list((workspace.root / "slack").glob("*-failure.json"))


def test_収集0件なら探索が走る(workspace):
    run = workspace(scenario="empty")
    result = main_mod.run_morning(run, State())

    assert result["listed"] > 0
    assert any("探索" in line for line in result["status"])


def test_フィードが一時的に落ちてもリトライで成功する(workspace):
    run = workspace(fail="rss:transient")
    result = main_mod.run_morning(run, State())
    assert result["articles"] > 0


def test_Vaultが落ちてもSlackへは投稿する(workspace):
    state = State()
    run = workspace(fail="vault")
    result = main_mod.run_morning(run, state)

    assert result["listed"] > 0
    assert any("Vault" in line or "翌日" in line for line in result["status"])
    assert state.seen == {}, "保存できなかった記事は翌日再試行できる必要がある"

    retry_result = main_mod.run_morning(
        workspace(day=1, date_str="2026-09-02"), state)
    assert retry_result["notes"] > 0


# --------------------------------------------------------------------------
# 記事化
# --------------------------------------------------------------------------

def _morning(workspace, state, **kwargs):
    return main_mod.run_morning(workspace(**kwargs), state)


def test_記事化が保存済みノートから書ける(workspace, monkeypatch):
    monkeypatch.chdir(workspace.root)
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:3]

    run = workspace()
    out = articles_mod.run_articles(
        run, state, story_ids=listed, message_ts=result["message_ts"],
        thread_ts=result["message_ts"])

    assert out["failed"] is False
    assert len(out["written"]) == 3
    assert out["pages"] >= 1
    assert out["png"], "紙面PNGが生成されていない"
    assert all(state.written[sid]["permalink"].startswith("https://slack.invalid/")
               for sid in out["written"])
    assert not (workspace.root / "debug.log").exists()


def test_紙面をSlackへ送れなければ作成済みにせず再試行できる(workspace):
    state = State()
    result = _morning(workspace, state)
    selected = [s.id for s in state.all_stories() if s.status == "listed"][:1]

    out = articles_mod.run_articles(
        workspace(fail="slack"), state, story_ids=selected,
        message_ts=result["message_ts"], thread_ts=result["message_ts"])

    assert out["failed"] is True
    assert out["kind"] == "slack"
    assert out["written"] == []
    assert not state.is_written(selected[0])


def test_紙面を生成できなければ作成済みにしない(workspace, monkeypatch):
    state = State()
    result = _morning(workspace, state)
    selected = [s.id for s in state.all_stories() if s.status == "listed"][:1]

    def broken_capture(*args, **kwargs):
        raise RuntimeError("Chromium failed")

    monkeypatch.setattr(articles_mod.render, "capture_article_pdf", broken_capture)
    out = articles_mod.run_articles(
        workspace(), state, story_ids=selected,
        message_ts=result["message_ts"], thread_ts=result["message_ts"])

    assert out["failed"] is True
    assert out["kind"] == "render"
    assert not state.is_written(selected[0])


def test_両モデルが落ちたら記事化せず通知だけ出す(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]

    run = workspace(fail="claude,gemini")
    out = articles_mod.run_articles(
        run, state, story_ids=listed, message_ts=result["message_ts"],
        thread_ts=result["message_ts"])

    assert out["failed"] is True
    assert out["written"] == []
    assert out["pages"] == 0
    # 話題は消化されない。あとから押し直せる。
    for story_id in listed:
        assert not state.is_written(story_id)


def test_クレジット切れは通知の文面が変わる(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:1]

    run = workspace(fail="claude:credit,gemini:credit")
    out = articles_mod.run_articles(
        run, state, story_ids=listed, message_ts=result["message_ts"],
        thread_ts=result["message_ts"])

    assert out["failed"] is True
    assert out["kind"] == "credit"

    texts = [p.read_text(encoding="utf-8")
             for p in (workspace.root / "slack").glob("*-post.json")]
    assert any("利用枠が尽きています" in t for t in texts)   # チャンネルへ1通


def test_claudeが落ちたらgeminiへ降格して書く(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]

    run = workspace(fail="claude")
    out = articles_mod.run_articles(
        run, state, story_ids=listed, message_ts=result["message_ts"],
        thread_ts=result["message_ts"])

    assert out["failed"] is False
    assert out["degraded"] is True
    assert any("降格" in line for line in run.status)


def test_ac26_上限を超えた分も同じ起動で順に作られ_完了通知は1通(workspace):
    """8件選ぶ → 5件＋3件の2まとまりで執筆し、紙面PDF2つを1通にまとめて送る。"""
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:8]

    run = workspace()
    out = articles_mod.run_articles(
        run, state, story_ids=listed, message_ts=result["message_ts"],
        thread_ts=result["message_ts"])

    assert len(out["written"]) == 8
    assert out["pending"] == []
    assert out["pages"] == 2                       # まとまりごとに PDF 1つ
    files = list((workspace.root / "slack").glob("*-files.json"))
    assert len(files) == 1                          # 完了通知は1通
    import json
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["text"].startswith("✅ 2026-09-01（火）の記事 8件を生成しました")
    assert len(payload["files"]) == 2 and payload["thread_ts"] == ""   # スレッドではなくチャンネル
    assert all(state.written[sid]["permalink"] for sid in out["written"])


def test_時間の上限に達したら残りを次回に回し_作れた分は送る(workspace):
    """16まとまり（80件）が Actions の30分で切られ、何も届かなかった件の再発防止。"""
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:12]

    ticks = iter([0.0, 200.0, 400.0])     # 開始, 2まとまり目の前, 3まとまり目の前
    run = workspace()
    run.config.data["limits"]["write_parallel"] = 1   # 1まとまりずつ書く前提の確認
    out = articles_mod.run_articles(
        run, state, story_ids=listed, message_ts=result["message_ts"],
        thread_ts=result["message_ts"],
        time_budget_seconds=150.0, clock=lambda: next(ticks))

    assert len(out["written"]) == 5                    # 1まとまり目は必ず回す
    assert out["deferred"] == listed[5:] and out["pending"] == listed[5:]
    assert out["failed"] is False
    assert any("時間の上限" in line for line in run.status)
    files = list((workspace.root / "slack").glob("*-files.json"))
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["text"].startswith(
        "✅ 2026-09-01（火）の記事 5件を生成しました（残り7件は次回へ）")
    assert "続きから作ります" in payload["text"]
    # 回した分はチェックが付いたまま一覧に戻る
    updates = [json.loads(p.read_text(encoding="utf-8"))
               for p in (workspace.root / "slack").glob("*-update.json")]
    initial = [o["value"] for u in updates for b in u["blocks"]
               if b["type"] == "actions" and b["block_id"].startswith("cat_")
               for o in b["elements"][0].get("initial_options", [])]
    assert set(initial) == set(listed[5:])
    # 最初のまとまりの前では止めない（何も作らずに終わらない）
    ticks2 = iter([0.0, 999.0, 999.0])
    out2 = articles_mod.run_articles(
        workspace(), state, story_ids=listed[5:8], message_ts=result["message_ts"],
        time_budget_seconds=1.0, clock=lambda: next(ticks2))
    assert len(out2["written"]) == 3


def test_紙面PDFは1通10個までに分けて送り_記事用のチャンネルへ届く(workspace, monkeypatch):
    """14個以上を1通に付けた回は投稿が現れず、紙面が誰にも見えなかった。"""
    run = workspace()
    paths = [f"/tmp/p{i}.pdf" for i in range(23)]
    posted = slack_mod.post_completion(
        run, paths, "✅ 2026-09-01（火）の記事 115件を生成しました  `#abc123`",
        channel="C_ARTICLES", continued="📎 2026-09-01（火）の記事  `#abc123`")

    files = sorted((workspace.root / "slack").glob("*-files.json"))
    payloads = [json.loads(p.read_text(encoding="utf-8")) for p in files]
    assert [len(p["files"]) for p in payloads] == [10, 10, 3]
    assert all(p["channel"] == "C_ARTICLES" for p in payloads)
    assert payloads[0]["text"].startswith("✅ 2026-09-01（火）の記事 115件")
    assert payloads[1]["text"] == "📎 2026-09-01（火）の記事  `#abc123`（続き 2/3）"
    assert payloads[2]["text"].endswith("（続き 3/3）")
    assert posted["ts"]                              # 戻り値は最初の通

    # 途中の通が送れなければ None（呼び出し側は作成済みにしない）
    run2 = workspace(fail="slack")
    assert slack_mod.post_completion(run2, paths, "✅ x", channel="C_ARTICLES") is None


def test_記事用のチャンネルが設定されていれば完了通知はそこへ送る(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]
    run = workspace()
    run.config.data.setdefault("slack", {})["articles_channel"] = "C_ARTICLES"
    try:
        articles_mod.run_articles(run, state, story_ids=listed,
                                  message_ts=result["message_ts"])
    finally:
        run.config.data["slack"]["articles_channel"] = ""
    payload = json.loads(next((workspace.root / "slack").glob("*-files.json"))
                         .read_text(encoding="utf-8"))
    assert payload["channel"] == "C_ARTICLES"
    assert "2026-09-01（火）の記事 2件" in payload["text"]


def test_作り直しは作成済みも対象にして紙面を送り直す(workspace):
    """紙面が届かなかった日（共有に失敗した日）の再送。"""
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:3]
    articles_mod.run_articles(workspace(), state, story_ids=listed,
                              message_ts=result["message_ts"])
    assert all(state.is_written(s) for s in listed)

    # 普通に押し直すと何も作らない
    again = articles_mod.run_articles(workspace(), state, story_ids=listed,
                                      message_ts=result["message_ts"])
    assert again["written"] == [] and len(again["already"]) == 3
    # force なら作り直す
    forced = articles_mod.run_articles(workspace(), state, story_ids=listed,
                                       message_ts=result["message_ts"], force=True)
    assert len(forced["written"]) == 3 and forced["already"] == []
    # 日付だけで選ぶときも、作成済みを含められる
    ids = articles_mod.listed_on_date(state, "2026-09-01", include_written=True)
    assert set(listed) <= set(ids)
    assert not set(listed) & set(articles_mod.listed_on_date(state, "2026-09-01"))


def test_その日の一覧の未作成の全話題を選べる(workspace):
    """ボタンが「生成中」のまま消えた一覧を、手動実行（all_of_date）で救うため。"""
    state = State()
    _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"]
    state.mark_written(listed[0], date_str="2026-09-01", thread_ts="", permalink="u")

    ids = articles_mod.listed_on_date(state, "2026-09-01")
    assert set(ids) == set(listed[1:])
    assert articles_mod.listed_on_date(state, "2026-08-31") == []


def test_途中で止まった後始末はボタンを戻しチェックを残して1文だけ知らせる(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:6]
    state.mark_written(listed[0], date_str="2026-09-01", thread_ts="", permalink="u")

    run = workspace()
    out = articles_mod.restore_after_abort(
        run, state, story_ids=listed, message_ts=result["message_ts"])

    assert out["pending"] == listed[1:]
    assert not state.is_written(listed[1])
    posts = [json.loads(p.read_text(encoding="utf-8"))
             for p in (workspace.root / "slack").glob("*-post.json")]
    notices = [p for p in posts if "途中で止まりました" in p["text"]]
    assert len(notices) == 1 and "5件が未作成" in notices[0]["text"]
    updates = [json.loads(p.read_text(encoding="utf-8"))
               for p in (workspace.root / "slack").glob("*-update.json")]
    assert updates, "一覧が書き戻されていない"
    initial = [o["value"] for u in updates for b in u["blocks"]
               if b["type"] == "actions" and b["block_id"].startswith("cat_")
               for o in b["elements"][0].get("initial_options", [])]
    assert set(initial) == set(listed[1:])
    assert any(b.get("block_id") == "create" for u in updates for b in u["blocks"])
    # 執筆は走らない（LLM の出力ファイルが無い）
    assert not list((workspace.root / "slack").glob("*-files.json"))


def test_ac27_記事化済みは再生成せずリンクを返す(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]

    articles_mod.run_articles(workspace(), state, story_ids=listed,
                              message_ts=result["message_ts"],
                              thread_ts=result["message_ts"])
    out = articles_mod.run_articles(workspace(), state, story_ids=listed,
                                    message_ts=result["message_ts"],
                                    thread_ts=result["message_ts"])

    assert out["written"] == []
    assert sorted(out["already"]) == sorted(listed)


def test_Claudeが書いた文章はVaultへ保存しない(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]
    articles_mod.run_articles(workspace(), state, story_ids=listed,
                              message_ts=result["message_ts"],
                              thread_ts=result["message_ts"])

    daily = (workspace.root / "vault/50_Logs/News/2026-09-01.md").read_text(
        encoding="utf-8")
    assert "## 記事" not in daily


def test_執筆に使った記事を記録して続報で繰り返さない(workspace):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:1]
    articles_mod.run_articles(workspace(), state, story_ids=listed,
                              message_ts=result["message_ts"],
                              thread_ts=result["message_ts"])

    assert state.written_articles[listed[0]]


# --------------------------------------------------------------------------
# 3日分の状態変化
# --------------------------------------------------------------------------

def test_一覧に出た話題は翌日の朝処理後も期限内なら残る(workspace):
    state = State()
    main_mod.run_morning(
        workspace(day=1, date_str="2026-09-01", scenario="written_excluded"),
        state)
    previous = {s.id for s in state.all_stories()
                if s.status == "listed" and s.listed_on == "2026-09-01"}

    main_mod.run_morning(
        workspace(day=2, date_str="2026-09-02", scenario="written_excluded"),
        state)

    assert previous
    assert previous <= set(state.stories)


def test_chat_updateは対象日の話題だけを元カテゴリで再構築する(workspace):
    state = State()
    state.stories = {
        "s_today": {"id": "s_today", "title": "今日", "category": "lang",
                    "status": "listed", "listed_on": "2026-09-02"},
        "s_old": {"id": "s_old", "title": "昨日", "category": "security",
                  "status": "listed", "listed_on": "2026-09-01"},
    }
    state.written = {
        "s_done": {"date": "2026-09-02", "title": "作成済み",
                   "category": "devtools", "permalink": "u"},
        "s_old_done": {"date": "2026-09-01", "title": "昨日の作成済み",
                       "category": "ai", "permalink": "u2"},
    }

    shown = articles_mod.rebuild_shown(
        state, workspace().config, date_str="2026-09-02")

    assert {s.id for group in shown.values() for s in group} == {"s_today", "s_done"}
    assert [s.id for s in shown["lang"]] == ["s_today"]
    assert [s.id for s in shown["devtools"]] == ["s_done"]


def test_過去日のボタンは元の一覧だけを更新する(workspace):
    state = State()
    morning = main_mod.run_morning(
        workspace(day=1, date_str="2026-09-01", scenario="written_excluded"),
        state)
    story_id = next(
        s.id for s in state.all_stories()
        if s.status == "listed" and s.listed_on == "2026-09-01")
    old_messages = list(state.listed_messages["2026-09-01"])

    result = articles_mod.run_articles(
        workspace(day=2, date_str="2026-09-02"), state,
        story_ids=[story_id], message_ts=morning["message_ts"],
        thread_ts=morning["message_ts"])

    assert result["written"] == [story_id]
    assert state.written[story_id]["listed_on"] == "2026-09-01"
    update_payloads = sorted((workspace.root / "slack").glob("*-update.json"))
    assert update_payloads
    updated_ts = {
        json.loads(path.read_text(encoding="utf-8"))["ts"]
        for path in update_payloads
    }
    assert set(old_messages) <= updated_ts


def test_保留した朝一覧の再送tsを対象日に記録する(workspace):
    state = State()
    failed = workspace(fail="slack")
    slack_mod.post_list(failed, state, [[]], "一覧", list_date="2026-09-01")

    assert state.pending[0]["list_date"] == "2026-09-01"
    assert "2026-09-01" not in state.listed_messages

    sent = slack_mod.flush_pending(workspace(), state)

    assert sent == 1
    assert state.pending == []
    assert len(state.listed_messages["2026-09-01"]) == 1


def test_分割一覧は途中の投稿失敗後に後続を追い越さない(workspace):
    state = State()
    slack_mod.post_list(
        workspace(fail="slack"), state, [[{"part": 1}], [{"part": 2}]],
        "一覧", list_date="2026-09-01")

    assert [item["blocks"][0]["part"] for item in state.pending] == [1, 2]
    assert "2026-09-01" not in state.listed_messages


def test_dry_runは取得clientを保ちSlackとVaultだけoutへ逃がす(
        workspace, monkeypatch):
    from src.clients import factory
    from src.clients.mock.slack import MockSlackClient
    from src.clients.mock.vault_git import MockNoteStore

    real_bundle = workspace().clients
    original_feed = real_bundle.feed
    monkeypatch.setattr(factory, "build_real",
                        lambda **kwargs: real_bundle)

    clients = factory.build(mock=False, dry_run=True, out_dir=workspace.root)

    assert clients.feed is original_feed
    assert isinstance(clients.slack, MockSlackClient)
    assert isinstance(clients.notes, MockNoteStore)


def test_情報源候補はしきい値到達時に一度だけ提案する(workspace):
    path = workspace.root / "discovered_sources.yml"
    urls = [f"https://new.example.invalid/{i}" for i in range(4)]

    promoted = discover.record_candidates(path, urls, promote_after=3)
    repeated = discover.record_candidates(
        path, ["https://new.example.invalid/again"], promote_after=3)

    assert promoted == [("new.example.invalid", 4)]
    assert repeated == []


def test_探索候補の保存失敗だけでは探索結果を失わない(workspace, monkeypatch):
    def broken_record(*args, **kwargs):
        raise OSError("write failed")

    monkeypatch.setattr(discover, "record_candidates", broken_record)
    run = workspace(scenario="empty")

    found = discover.stage2(run)

    assert found
    assert any("探索候補の保存" in line for line in run.status)


def test_stateは不正なJSON構造でも空から復旧し保存は一時ファイルを残さない(
        workspace):
    root = workspace.root
    (root / "state").mkdir(parents=True, exist_ok=True)
    (root / "state/collect.json").write_text(
        json.dumps(["objectではない"]), encoding="utf-8")

    state = load_state(root)
    state.last_success_at = "2026-09-01T05:00:00+09:00"
    save_state(state, root)

    assert load_state(root).last_success_at == state.last_success_at
    assert not list((root / "state").glob("*.tmp"))


def test_記録は書き手ごとに別のファイルへ分かれる(workspace):
    """3つのワークフローが同時に動いても潰し合わないようにするため。"""
    root = workspace.root
    state = State()
    state.last_success_at = "2026-09-01T05:00:00+09:00"
    state.stories = {"s_1": {"id": "s_1", "status": "listed",
                             "listed_on": "2026-09-01"}}
    state.written = {"s_1": {"date": "2026-09-01"}}
    state.pending = [{"kind": "post"}]
    save_state(state, root)

    names = sorted(p.name for p in (root / "state").glob("*.json"))
    assert names == ["collect.json", "outbox.json", "stories.json",
                     "written.json"]


def test_記事化は朝のファイルへ書かない(workspace):
    """記事化が stories.json を触ると、朝の実行と衝突する。"""
    root = workspace.root
    state = State()
    state.stories = {"s_1": {"id": "s_1", "status": "listed",
                             "listed_on": "2026-09-01"}}
    save_state(state, root)
    before = (root / "state/stories.json").read_text(encoding="utf-8")

    state.mark_written("s_1", date_str="2026-09-02", thread_ts="t")
    save_state(state, root, sections=("written", "outbox"))

    assert (root / "state/stories.json").read_text(encoding="utf-8") == before
    assert state.is_written("s_1")
    # stories からは消さない。90日の刈り取りに任せる。
    assert "s_1" in state.stories


def test_旧来の1ファイルからも読める(workspace):
    """移行のため。次の保存で state/ 側へ移り、旧ファイルは消える。"""
    root = workspace.root
    (root / "state.json").write_text(
        json.dumps({"last_success_at": "2026-09-01T05:00:00+09:00",
                    "seen": {"abc": "2026-09-01"}}),
        encoding="utf-8")

    state = load_state(root)
    assert state.last_success_at == "2026-09-01T05:00:00+09:00"
    assert state.seen == {"abc": "2026-09-01"}

    save_state(state, root)
    assert not (root / "state.json").exists()
    assert load_state(root).seen == {"abc": "2026-09-01"}

def test_繰り越しと統合が日をまたいで効く(workspace):
    state = State()
    day1 = main_mod.run_morning(
        workspace(day=1, date_str="2026-09-01", scenario="written_excluded"),
        state)
    assert day1["unpublished"] > 0

    run2 = workspace(day=2, date_str="2026-09-02", scenario="written_excluded")
    day2 = main_mod.run_morning(run2, state)

    assert any("統合" in line for line in day2["status"])
    assert day2["listed"] > 0


def test_90日を過ぎた話題は候補から外れる():
    state = State()
    state.stories = {
        "s_old": {"id": "s_old", "status": "listed", "listed_on": "2026-01-01"},
        "s_new": {"id": "s_new", "status": "listed", "listed_on": "2026-09-01"},
    }
    state.listed_messages = {
        "2026-01-01": ["old-ts"], "2026-09-01": ["new-ts"]}
    removed = state.prune_listed(today="2026-09-10", days=90)

    assert removed == 1
    assert "s_old" not in state.stories
    assert "s_new" in state.stories
    assert "2026-01-01" not in state.listed_messages
    assert state.listed_messages["2026-09-01"] == ["new-ts"]


# --------------------------------------------------------------------------
# 週次レビュー
# --------------------------------------------------------------------------

def test_週次レビューが反応の変化から作られる(workspace, monkeypatch):
    state = State()
    main_mod.run_morning(workspace(day=1, date_str="2026-09-01"), state)

    sizes = []
    original = weekly_mod.write.write_articles

    def record_batch(run, targets):
        sizes.append(len(targets))
        return original(run, targets)

    monkeypatch.setattr(weekly_mod.write, "write_articles", record_batch)
    run = workspace(day=1, date_str="2026-09-03", phase="weekly")
    result = weekly_mod.run_weekly(run, state)

    assert result["changes"] > 0
    assert result["posted"] is True
    assert len(sizes) > 1
    assert max(sizes) <= 5
    assert (workspace.root / "vault/40_Knowledge/News-Review").exists()


def test_変化が0件なら週次レビューを作らない(workspace):
    run = workspace(day=1, date_str="2026-09-03", phase="weekly")
    result = weekly_mod.run_weekly(run, State())

    assert result["changes"] == 0
    assert result["posted"] is False


def test_週次レビューのSlack投稿失敗を成功扱いしない(workspace):
    state = State()
    main_mod.run_morning(workspace(day=1, date_str="2026-09-01"), state)

    result = weekly_mod.run_weekly(
        workspace(day=1, date_str="2026-09-03", phase="weekly", fail="slack"),
        state)

    assert result["changes"] > 0
    assert result["posted"] is False


# --------------------------------------------------------------------------
# 未実装の検出
# --------------------------------------------------------------------------

def test_real側はキーが無ければ黙って成功せず例外を投げる(monkeypatch):
    """「何もしないが成功」に見えると、欠けたことが取得状況に載らない。"""
    from src.clients.real import llm, slack, vault_git

    for name in ("SLACK_BOT_TOKEN", "VAULT_DEPLOY_KEY", "GEMINI_API_KEY"):
        monkeypatch.delenv(name, raising=False)

    for call in (
        lambda: slack.RealSlackClient(channel="C1").post_message([], "t"),
        lambda: vault_git.RealNoteStore().read("50_Logs/News/x.md"),
        lambda: llm.RealGemini().complete("p", "translate"),
    ):
        with pytest.raises(Exception):
            call()


def test_自己診断の記号はWindows既定cp932へ出力できる():
    from src.check import NG, OK, SKIP, Result

    for status in (OK, NG, SKIP):
        Result("probe", status).mark.encode("cp932")


def test_モック実行はリポジトリを書き換えない(workspace):
    """`discovered_sources.yml` は data_dir 側へ書く。

    探索の段階2まで落ちるとここへ追記される。モック実行でリポジトリが汚れると、
    差分がデバッグのノイズになる。
    """
    from src.config import load as _load

    # 公開リポジトリでは discovered_sources.yml を持たない（非公開データ側。2026-10-31）
    repo_file = _load().root / "discovered_sources.yml"
    before = repo_file.read_text(encoding="utf-8") if repo_file.exists() else None

    run = workspace(scenario="empty", fail="grok")
    main_mod.run_morning(run, State())

    after = repo_file.read_text(encoding="utf-8") if repo_file.exists() else None
    assert after == before, "リポジトリの discovered_sources.yml が書き換わった"
    assert (workspace.root / "discovered_sources.yml").exists(), \
        "out/ 側へ書かれていない"


def test_YouTubeの動画はGeminiの要約を本文にする(workspace):
    """`youtube.yml` は未選定で空なので simulate からは踏めない経路。

    チャンネルを設定したときに動くことをここで固定する。
    """
    from src import extract

    run = workspace()
    article = make_article(
        id="ytaaaa01", url="https://www.youtube.com/watch?v=vid0001",
        title="週刊AIニュース解説", category="ai")
    filled = extract.extract_one(run, article)

    assert filled.extracted_by == "gemini_youtube"
    assert filled.body_md.strip()


def test_YouTubeの要約が落ちても見出しとURLで残る(workspace):
    from src import extract

    run = workspace(fail="gemini")
    article = make_article(
        id="ytaaaa02", url="https://www.youtube.com/watch?v=vid0002",
        title="動画", category="ai")
    filled = extract.extract_one(run, article)

    assert filled.body_md == ""
    assert "youtube_summary_failed" in filled.errors


# --------------------------------------------------------------------------
# 点検で見つかった3件（2026-09-16）
# --------------------------------------------------------------------------

def test_一覧の更新で通数が増えたら増えた分を新しく投稿する(workspace, monkeypatch):
    """済みの行が増えて2通になったとき、2通目（ボタンを含む）が消えない。"""
    from src import blocks
    state = State()
    run = workspace()
    result = main_mod.run_morning(run, state)
    date_str = run.date_str
    assert len(state.listed_messages[date_str]) == 1

    two = [[{"type": "section", "text": {"type": "mrkdwn", "text": "1通目"}}],
           [{"type": "section", "text": {"type": "mrkdwn", "text": "2通目"}},
            blocks._button(date_str)]]
    monkeypatch.setattr(blocks, "build_update", lambda **kw: two)

    run2 = workspace(phase="click")
    articles_mod.update_lists(run2, state, labels={}, pending=set(),
                              message_ts=result["message_ts"], date_str=date_str)
    assert len(state.listed_messages[date_str]) == 2   # 2通目が投稿され記録される


def test_前回成功時点は収集開始の実時刻で_Vault保存に失敗したら進めない(workspace):
    state = State()
    run = workspace()
    main_mod.run_morning(run, state)
    assert state.last_success_at.startswith(run.date_str + "T")
    assert not state.last_success_at.startswith(run.date_str + "T05:00:00")  # 固定値ではない

    before = state.last_success_at
    failed = State()
    failed.last_success_at = before
    run_failed = workspace(day=2, date_str="2026-09-02", fail="vault")
    main_mod.run_morning(run_failed, failed)
    assert failed.last_success_at == before          # 進めない
    assert any("前回成功時点を進めていません" in line for line in run_failed.status)


def test_反応が執筆プロンプトへ渡る():
    from src import write
    from src.models import Reactions
    from tests.conftest import make_article
    a = make_article(id="r1", reactions=Reactions(hn_points=120, hn_comments=45, hatena=30,
                                                   x_summary="  賛否が割れて  いる "))
    line = write.reaction_line(a)
    assert "Hacker News 120点・コメント45件" in line
    assert "はてなブックマーク 30件" in line
    assert "X上の傾向: 賛否が割れて いる" in line
    assert write.reaction_line(make_article(id="r2")) == ""

    target = write.WriteTarget(story=__import__("tests.conftest", fromlist=["make_story"]).make_story(article_ids=["r1"]),
                               articles=[a])
    prompt = write.build_prompt([target], char_range=[600, 900], hint_count=1)
    assert "REACTIONS: Hacker News 120点" in prompt


def test_記事化が増やした一覧IDは保存され_次回も読める(tmp_path):
    from src.state import State as _State, load as load_state, save as save_state
    state = _State()
    state.listed_messages = {"2026-09-16": ["1.0", "2.0"]}
    save_state(state, tmp_path, sections=("written", "outbox"))   # 記事化が保存する範囲
    again = load_state(tmp_path)
    assert again.listed_messages == {"2026-09-16": ["1.0", "2.0"]}


def test_一部の記事ノートを保存できなければ_その記事が次回の対象期間に入る(workspace, monkeypatch):
    from src import vault
    state = State()
    run = workspace()
    original = vault.write_articles

    def drop_one(run_, articles, story_of, date_str):
        result = original(run_, articles, story_of, date_str)
        victim = sorted(articles, key=lambda a: a.published_at)[0]
        result.available.discard(victim.id)        # 1件だけ保存できなかったことにする
        run_.victim = victim
        return result

    monkeypatch.setattr(vault, "write_articles", drop_one)
    main_mod.run_morning(run, state)
    victim = run.victim
    assert victim.url_hash not in state.seen
    assert state.last_success_at <= victim.published_at   # 次回の窓に入る
    assert any("留めています" in line for line in run.status)


def test_記事化後の一覧も朝と同じ新しい順で並ぶ(workspace):
    state = State()
    run = workspace()
    main_mod.run_morning(run, state)

    shown = articles_mod.rebuild_shown(state, run.config, date_str=run.date_str)
    for category, stories in shown.items():
        stamps = [s.last_updated for s in stories]
        assert stamps == sorted(stamps, reverse=True), category


def test_探索で見つけた記事は当日の収集分と重複しない(workspace, monkeypatch):
    from src import discover
    state = State()
    run = workspace(scenario="empty")           # 収集が少なく探索が走る場面
    captured = {}
    original = discover.run_discovery

    def duplicate_first(run_, articles):
        found = original(run_, articles)
        if articles:
            dup = make_article(id="", url=articles[0].url, title=articles[0].title,
                               source="discovered", category=articles[0].category,
                               published_at=articles[0].published_at)
            found = [dup] + list(found)
        captured["found"] = found
        return found

    monkeypatch.setattr(discover, "run_discovery", duplicate_first)
    result = main_mod.run_morning(run, state)
    if captured.get("found"):
        ids = [sid for group in articles_mod.rebuild_shown(
            state, run.config, date_str=run.date_str).values() for sid in (s.id for s in group)]
        assert len(ids) == len(set(ids))          # 同じ話題が2件並ばない


def test_落ちたソースの成功時点は進まず_他のソースは進む(workspace, monkeypatch):
    state = State()
    run = workspace()
    original = run.clients.feed.fetch_articles

    def flaky(source):
        if source.get("id") == "hf_blog":
            raise RuntimeError("HTTP 503")
        return original(source)

    monkeypatch.setattr(run.clients.feed, "fetch_articles", flaky)
    main_mod.run_morning(run, state)
    assert "hf_blog" in run.failed_sources
    # 落ちたソースは進まない。初めての失敗なら今回の窓の始まりが残る
    assert state.source_success_at["hf_blog"] < state.last_success_at
    assert state.source_success_at.get("techcrunch") == state.last_success_at


def test_同じ日に2回起動されても一覧は1回しか出ない(workspace):
    """主（Cloudflare の時計）と保険（GitHub の schedule）の両方から起動される。"""
    state = State()
    first = main_mod.run_morning(workspace(), state)
    assert first["messages"] >= 1
    posted = list(state.listed_messages[first["message_ts"] and "2026-09-01"])

    run2 = workspace()
    second = main_mod.run_morning(run2, state)
    assert second.get("skipped") is True
    assert state.listed_messages["2026-09-01"] == posted      # 増えていない
    assert any("すでに作成済み" in line for line in run2.status)


def test_完了通知の文面():
    from src.articles import completion_text
    assert completion_text(23, 0, degraded=False, credit=False) == "✅ 23件の記事を生成しました"
    partial = completion_text(5, 2, degraded=True, credit=False)
    assert partial.startswith("✅ 5件の記事を生成しました（2件は作れませんでした）")
    assert "チェックが付いたまま" in partial and "Gemini" in partial
    none = completion_text(0, 3, degraded=False, credit=True)
    assert none.startswith(":warning: 記事を作れませんでした（3件）") and "利用枠" in none


def test_途中のまとまりが失敗しても残りは続き_失敗分だけチェックが残る(workspace, monkeypatch):
    state = State()
    result = _morning(workspace, state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:7]

    calls = {"n": 0}
    original = articles_mod.render.capture_article_pdf

    def flaky(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] <= 3:                       # 1まとまり目は3回とも失敗
            raise RuntimeError("Chromium failed")
        return original(*args, **kwargs)

    monkeypatch.setattr(articles_mod.render, "capture_article_pdf", flaky)
    run = workspace()
    run.config.data["limits"]["write_parallel"] = 1   # 1まとまりずつ書く前提の確認
    out = articles_mod.run_articles(run, state, story_ids=listed,
                                    message_ts=result["message_ts"])
    assert len(out["written"]) == 2 and len(out["pending"]) == 5
    assert out["failed"] is False                    # 一部は作れた
    assert all(not state.is_written(sid) for sid in out["pending"])
    import json
    payload = json.loads(next((workspace.root / "slack").glob("*-files.json")).read_text(encoding="utf-8"))
    assert "の記事 2件を生成しました（5件は作れませんでした）" in payload["text"]


def test_写真集は一覧に1行だけ出て_記事化すると写真のページになる(workspace):
    state = State()
    run = workspace()
    result = main_mod.run_morning(run, state)
    galleries = [s for s in state.all_stories() if s.kind == "gallery"]
    assert len(galleries) == 1 and galleries[0].status == "listed"
    assert any("写真集" in line for line in run.status)
    # 個人投稿は1件ずつの話題になっていない
    assert not any(s.kind != "gallery" and "Seal x OG" in s.title for s in state.all_stories())

    out = articles_mod.run_articles(workspace(phase="click"), state,
                                    story_ids=[galleries[0].id],
                                    message_ts=result["message_ts"])
    assert out["written"] == [galleries[0].id] and out["pages"] == 1
    html = next((workspace.root / "paper").glob("*-articles-1.html")).read_text(encoding="utf-8")
    assert "gallery-sheet" in html and html.count('<figure class="cell">') == 6


def test_投稿済みの日でも送れていなかった残りは保険の起動で送る(workspace):
    state = State()
    first = main_mod.run_morning(workspace(), state)
    date_str = "2026-09-01"
    # 2通目が落ちた状態を作る
    state.pending.append({"kind": "post", "text": "残り", "list_date": date_str,
                          "blocks": [{"type": "section", "text": {"type": "mrkdwn", "text": "2通目"}}]})
    run2 = workspace()
    second = main_mod.run_morning(run2, state)
    assert second.get("skipped") is True
    assert state.pending == []                                   # 再送された
    posts = [f.read_text(encoding="utf-8") for f in (workspace.root / "slack").glob("*-post.json")]
    assert any("2通目" in t for t in posts)
    assert any("送れていなかった1通" in line for line in run2.status)


def test_一度も成功していないソースが落ちたら_今回の窓の始まりが記録される(workspace, monkeypatch):
    state = State()
    run = workspace()
    original = run.clients.feed.fetch_articles

    def flaky(source):
        if source.get("id") == "hf_blog":
            raise RuntimeError("HTTP 503")
        return original(source)

    monkeypatch.setattr(run.clients.feed, "fetch_articles", flaky)
    main_mod.run_morning(run, state)
    # 初回の窓の始まり（前日05:00）が hf_blog の前回成功時点として残る
    assert state.source_success_at["hf_blog"] == "2026-08-31T05:00:00+09:00"
    assert state.source_success_at["techcrunch"] == state.last_success_at


def test_全通が保留になった日の再実行は_保留分を送るだけで一覧を作り直さない(workspace):
    state = State()
    run = workspace(fail="slack")                  # 投稿が全部落ちる
    main_mod.run_morning(run, state)
    assert state.listed_messages.get("2026-09-01") is None
    held = [p for p in state.pending if p.get("list_date") == "2026-09-01"]
    assert held                                     # 一覧は保留に入っている

    run2 = workspace()                              # 保険の起動（Slack は復旧）
    second = main_mod.run_morning(run2, state)
    assert second.get("skipped") is True
    assert state.pending == []
    posts = [f.read_text(encoding="utf-8") for f in (workspace.root / "slack").glob("*-post.json")]
    lists = [t for t in posts if "のニュース" in t and "話題" in t]
    assert len(lists) == 1                          # 同じ日の一覧が2つ並ばない


def test_元記事の一部が読めない話題は作らず_チェックを残す(workspace, monkeypatch):
    from src import vault
    state = State()
    result = _morning(workspace, state)
    # 記事が2件以上ある話題を選ぶ（無ければ1件の話題に2つ目のノートを足す）
    target = next((s for s in state.all_stories() if s.status == "listed"
                   and len(state.story_notes.get(s.id, [])) >= 2), None)
    if target is None:
        target = next(s for s in state.all_stories() if s.status == "listed")
        state.story_notes[target.id].append("40_Knowledge/News/ai/2026/09/2026-09-01-missing.md")

    original = vault.read_article
    calls = {"n": 0}

    def flaky(run_, rel):
        calls["n"] += 1
        return None if calls["n"] == 1 else original(run_, rel)   # 1件目だけ読めない

    monkeypatch.setattr(vault, "read_article", flaky)
    out = articles_mod.run_articles(workspace(phase="click"), state, story_ids=[target.id],
                                    message_ts=result["message_ts"])
    assert out["written"] == [] and out["pending"] == [target.id]
    assert not state.is_written(target.id)


def test_GitHub_Releasesが落ちたら_そのソースの成功時点は進まない(workspace, monkeypatch):
    from src import collect
    state = State()
    run = workspace()
    monkeypatch.setattr(run.clients.releases, "fetch", lambda repo: (_ for _ in ()).throw(RuntimeError("HTTP 503")))
    main_mod.run_morning(run, state)
    assert collect.RELEASES_SOURCE in run.failed_sources
    assert state.source_success_at[collect.RELEASES_SOURCE] < state.last_success_at

    ok = State()
    run_ok = workspace()
    main_mod.run_morning(run_ok, ok)
    assert ok.source_success_at[collect.RELEASES_SOURCE] == ok.last_success_at


def test_朝に保存できなかった元記事がある話題は_記事化しない(workspace):
    """ノートのパスが登録されていない記事は「読めた数」には出てこない。"""
    state = State()
    result = _morning(workspace, state)
    target = next(s for s in state.all_stories() if s.status == "listed")
    # 朝の保存に失敗した記事が1件ある状態を作る（話題の記事は2件、ノートは1件）
    state.story_articles[target.id] = list(state.story_articles[target.id]) + ["missing1"]
    assert len(state.story_notes[target.id]) < len(state.story_articles[target.id])

    out = articles_mod.run_articles(workspace(phase="click"), state, story_ids=[target.id],
                                    message_ts=result["message_ts"])
    assert out["written"] == [] and out["pending"] == [target.id]
    assert not state.is_written(target.id)


def test_週次レビューは見出しと紙面を1通で送り_落ちたら成功扱いにしない(workspace, monkeypatch):
    import json
    from src import weekly as weekly_mod
    state = State()
    for day, date_str in ((1, "2026-09-01"), (2, "2026-09-02"), (3, "2026-09-03")):
        main_mod.run_morning(workspace(day=day, date_str=date_str), state)

    run = workspace(day=3, date_str="2026-09-08", phase="weekly")
    out = weekly_mod.run_weekly(run, state)
    if out["changes"]:
        files = [json.loads(f.read_text(encoding="utf-8"))
                 for f in (workspace.root / "slack").glob("*-files.json")]
        weekly = [f for f in files if "反応の変化" in f["text"]]
        assert len(weekly) == 1 and weekly[0]["files"]       # 見出し＋PDF が1通
        assert out["posted"] is True

        state.weekly_posted.clear()                        # 別の状態で送信失敗を再現
        run_fail = workspace(day=3, date_str="2026-09-08", phase="weekly", fail="slack")
        out2 = weekly_mod.run_weekly(run_fail, state)
        assert not state.weekly_posted                     # 送れなかった週は記録しない
        assert out2["posted"] is False
        assert any("送れませんでした" in line for line in run_fail.status)


def test_同じ週の週次レビューは二度送らない(workspace):
    from src import weekly as weekly_mod
    state = State()
    for day, date_str in ((1, "2026-09-01"), (2, "2026-09-02"), (3, "2026-09-03")):
        main_mod.run_morning(workspace(day=day, date_str=date_str), state)

    first = weekly_mod.run_weekly(workspace(day=3, date_str="2026-09-08", phase="weekly"), state)
    if not first["changes"]:
        return
    assert first["posted"] is True and state.weekly_posted.get(first["week"])

    run2 = workspace(day=3, date_str="2026-09-08", phase="weekly")
    second = weekly_mod.run_weekly(run2, state)
    assert second.get("skipped") is True and second["posted"] is False
    assert any("投稿済み" in line for line in run2.status)

    # 記録は週次が保存する範囲（outbox）に入っている
    from src.state import load as load_state, save as save_state
    save_state(state, workspace.root, sections=("outbox",))
    assert load_state(workspace.root).weekly_posted == state.weekly_posted


def test_週次レビューのVault保存だけ失敗した週は_次回に保存だけやり直す(workspace, monkeypatch):
    from src import vault, weekly as weekly_mod
    state = State()
    for day, date_str in ((1, "2026-09-01"), (2, "2026-09-02"), (3, "2026-09-03")):
        main_mod.run_morning(workspace(day=day, date_str=date_str), state)

    # 読み出しは通し、commit/push だけ落とす
    monkeypatch.setattr(vault, "commit", lambda run_, message: False)
    run_fail = workspace(day=3, date_str="2026-09-08", phase="weekly")
    first = weekly_mod.run_weekly(run_fail, state)
    assert first["changes"] > 0
    week = first["week"]
    assert first["posted"] is True and state.weekly_posted.get(week)   # Slack は送れた
    assert state.weekly_unsaved.get(week)                                # 本文は控えてある
    assert any("次回やり直します" in line for line in run_fail.status)

    slack_before = len(list((workspace.root / "slack").glob("*.json")))
    monkeypatch.undo()                                                    # Vault 復旧
    run2 = workspace(day=3, date_str="2026-09-08", phase="weekly")
    second = weekly_mod.run_weekly(run2, state)
    assert second.get("skipped") is True and second.get("vault_saved") is True
    assert week not in state.weekly_unsaved
    assert (workspace.root / "vault" / "40_Knowledge/News-Review" / f"{week}.md").exists()
    assert len(list((workspace.root / "slack").glob("*.json"))) == slack_before   # Slack へは送らない
