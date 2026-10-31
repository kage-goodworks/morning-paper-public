"""記事化の自動やり直し（2026-10-24）。

Slack に送れない・AI が応答しない・途中で止まった話題を、本人が押し直さなくても
後の実行で作って送る。
"""

from __future__ import annotations

import json

from src import articles as articles_mod
from src import main as main_mod
from src.clients.factory import build_mock
from src.config import load as load_config
from src.pipeline import Run
from src.retry import NO_SLEEP
from src.state import State, load as load_state, save as save_state


def _run(tmp_path, **kw) -> Run:
    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path, **kw)
    return Run(config=cfg, clients=clients, date_str="2026-09-01",
               out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)


def _morning(tmp_path):
    state = State()
    result = main_mod.run_morning(_run(tmp_path), state)
    listed = [s.id for s in state.all_stories()
              if s.status == "listed" and s.kind != "gallery"][:3]
    return state, result, listed


def _files(tmp_path):
    return [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((tmp_path / "slack").glob("*-files.json"))]


def test_紙面を送れなくても同じ実行の中で待って送り直す(tmp_path, monkeypatch):
    state, result, listed = _morning(tmp_path)
    real = articles_mod.slack_io.post_completion
    calls = {"n": 0}

    def flaky(*args, **kwargs):
        calls["n"] += 1
        return None if calls["n"] == 1 else real(*args, **kwargs)

    monkeypatch.setattr(articles_mod.slack_io, "post_completion", flaky)
    run = _run(tmp_path)
    out = articles_mod.run_articles(run, state, story_ids=listed,
                                    message_ts=result["message_ts"])
    assert len(out["written"]) == 3 and calls["n"] == 2
    assert NO_SLEEP.waited[-1] == 60                         # 1分待ってから送り直した
    assert any("送り直して届きました" in line for line in run.status)
    assert state.redo == []


def test_送れないままなら待ち行列に入り_次の実行で作って送る(tmp_path):
    state, result, listed = _morning(tmp_path)

    run = _run(tmp_path, fail="slack")
    out = articles_mod.run_articles(run, state, story_ids=listed,
                                    message_ts=result["message_ts"])
    assert out["written"] == []
    assert len(state.redo) == 1
    item = state.redo[0]
    assert item["date"] == "2026-09-01" and set(item["story_ids"]) == set(listed)
    assert item["reason"] == "slack" and item["attempts"] == 0

    # 待ち行列は outbox に保存され、読み直せる
    save_state(state, tmp_path)
    state = load_state(tmp_path)
    assert len(state.redo) == 1

    # Slack が戻った（モックの失敗注入なし）。やり直しの実行で作って送る
    again = _run(tmp_path)
    done = articles_mod.process_redo(again, state)
    assert set(done["written"]) == set(listed)
    assert state.redo == [] and done["left"] == 0
    assert all(state.is_written(s) for s in listed)
    assert _files(tmp_path)[-1]["text"].startswith("✅ 2026-09-01（火）の記事 3件")


def test_完了通知は押し直しを求めず自動でやり直すと伝える():
    text = articles_mod.completion_text(2, 3, degraded=False, credit=False,
                                        auto_retry=True)
    assert "自動でやり直して送ります" in text and "押してください" not in text
    deferred = articles_mod.completion_text(5, 7, degraded=False, credit=False,
                                            deferred=7, auto_retry=True)
    assert "自動で続きから作ります" in deferred and "押すと" not in deferred
    abort = articles_mod.abort_text(4, auto_retry=True)
    assert "自動でやり直して送ります" in abort and "押してください" not in abort


def test_上限回数やり直しても駄目なら理由を1通出して外す(tmp_path, monkeypatch):
    state, result, listed = _morning(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("Chromium failed")

    # 紙面が作れない状態が続く（Slack は生きているので、諦めた知らせは届く）
    monkeypatch.setattr(articles_mod.render, "capture_article_pdf", broken)
    articles_mod.run_articles(_run(tmp_path), state, story_ids=listed,
                              message_ts=result["message_ts"])
    assert len(state.redo) == 1 and state.redo[0]["reason"] == "render"

    for n in range(1, 3):
        out = articles_mod.process_redo(_run(tmp_path), state, max_attempts=2)
        if n == 1:
            assert len(state.redo) == 1 and state.redo[0]["attempts"] == 1
    assert state.redo == [] and set(out["given_up"]) == set(listed)
    threads = [json.loads(p.read_text(encoding="utf-8"))
               for p in (tmp_path / "slack").glob("*-thread.json")]
    posts = [json.loads(p.read_text(encoding="utf-8"))
             for p in (tmp_path / "slack").glob("*-post.json")]
    notices = [m for m in threads + posts if "自動で2回やり直しましたが" in m.get("text", "")]
    assert notices, "諦めた知らせが出ていない"
    assert "紙面の画像を作れない" in notices[0]["text"]


def test_途中で止まった分も待ち行列に入る(tmp_path):
    state, result, listed = _morning(tmp_path)
    out = articles_mod.restore_after_abort(_run(tmp_path), state, story_ids=listed,
                                           message_ts=result["message_ts"])
    assert set(out["pending"]) == set(listed)
    assert state.redo and state.redo[0]["reason"] == "aborted"


def test_同じ一覧日の項目はまとまり_作成済みは入らない(tmp_path):
    state = State()
    state.mark_written("s_done", date_str="2026-09-01", thread_ts="", permalink="u")
    articles_mod.enqueue_redo(state, ["s_a", "s_done"], list_date="2026-09-01",
                              reason="other", now="t")
    articles_mod.enqueue_redo(state, ["s_b", "s_a"], list_date="2026-09-01",
                              reason="slack", now="t")
    articles_mod.enqueue_redo(state, ["s_c"], list_date="2026-09-02", reason="other", now="t")
    assert [i["story_ids"] for i in state.redo] == [["s_a", "s_b"], ["s_c"]]
    assert state.redo[0]["reason"] == "slack"


def test_設定で止めれば待ち行列は使わない(tmp_path):
    state, result, listed = _morning(tmp_path)
    run = _run(tmp_path, fail="slack")
    run.config.data.setdefault("auto_retry", {})["enabled"] = False
    try:
        articles_mod.run_articles(run, state, story_ids=listed,
                                  message_ts=result["message_ts"])
    finally:
        run.config.data["auto_retry"]["enabled"] = True
    assert state.redo == []


def test_送れなかった紙面は書き直さずに送り直す(tmp_path, monkeypatch):
    """やり直しのたびに全記事を書き直すと、1回1時間かかり利用時間を使い切る（2026-10-29）。"""
    import shutil
    state, result, listed = _morning(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    articles_mod.run_articles(_run(tmp_path, fail="slack"), state, story_ids=listed,
                              message_ts=result["message_ts"])
    item = state.redo[0]
    assert item["reason"] == "slack" and item["deliveries"][0]["run_id"] == "12345"
    files = item["deliveries"][0]["files"]
    assert files

    # articles.yml が前の実行の成果物を取ってくる場所へ置く
    dest = tmp_path / "redeliver" / "12345"
    dest.mkdir(parents=True)
    for name in files:
        shutil.copy(tmp_path / "paper" / name, dest / name)

    def no_rewrite(*args, **kwargs):
        raise AssertionError("書き直してはいけない")

    monkeypatch.setattr(articles_mod.write, "write_articles", no_rewrite)
    out = articles_mod.process_redo(_run(tmp_path), state)
    assert set(out["written"]) == set(listed)
    assert state.redo == []
    assert all(state.written[s]["permalink"] for s in listed)


import pytest


@pytest.mark.parametrize("final_only", [False, True])
def test_同じ1通だけが3回続けて送れなければ_届いた分を作成済みにして知らせる(tmp_path, monkeypatch, final_only):
    """脆弱性の記事を含む1通だけが Slack に共有されず、87件が延々やり直しに残った（2026-10-31）。"""
    import shutil
    state, result, listed = _morning(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    articles_mod.run_articles(_run(tmp_path, fail="slack"), state, story_ids=listed,
                              message_ts=result["message_ts"])
    files = state.redo[0]["deliveries"][0]["files"]
    dest = tmp_path / "redeliver" / "12345"
    dest.mkdir(parents=True)
    for name in files:
        shutil.copy(tmp_path / "paper" / name, dest / name)

    def partly(run, paths, text, *, channel=None, continued="", report=None):
        report.update({"failed": [3], "total": 18,
                       "first": {"ok": True, "ts": "1.1", "permalink": "https://x/p"}})
        return None

    notices = []
    monkeypatch.setattr(articles_mod.slack_io, "post_completion", partly)
    monkeypatch.setattr(articles_mod.slack_io, "post_channel_notice",
                        lambda run, text: notices.append(text))
    # 上限の回なら、3回を待たずに届いた分を作成済みにする（諦めの知らせにしない）
    if final_only:
        out = articles_mod.process_redo(_run(tmp_path), state, max_attempts=1)
        assert set(out["written"]) == set(listed) and out["given_up"] == []
        assert state.redo == []
        return
    for _ in range(2):
        articles_mod.process_redo(_run(tmp_path), state)
        assert state.redo and not any(state.is_written(s) for s in listed)
    out = articles_mod.process_redo(_run(tmp_path), state)
    assert set(out["written"]) == set(listed)
    assert state.redo == []
    assert any("3通目" in n and "18通" in n for n in notices)


def test_前の紙面が無ければ作り直しに任せる(tmp_path, monkeypatch):
    state, result, listed = _morning(tmp_path)
    monkeypatch.setenv("GITHUB_RUN_ID", "999")
    articles_mod.run_articles(_run(tmp_path, fail="slack"), state, story_ids=listed,
                              message_ts=result["message_ts"])
    out = articles_mod.process_redo(_run(tmp_path), state)   # 紙面は取ってきていない
    assert set(out["written"]) == set(listed)                 # 作り直して届いた
    assert state.redo == []


def test_並列で書いても全件が作られ_順序と記録が崩れない(tmp_path):
    state = State()
    result = main_mod.run_morning(_run(tmp_path), state)
    listed = [s.id for s in state.all_stories()
              if s.status == "listed" and s.kind != "gallery"][:12]
    run = _run(tmp_path)
    run.config.data["limits"]["write_parallel"] = 3
    out = articles_mod.run_articles(run, state, story_ids=listed,
                                    message_ts=result["message_ts"])
    assert set(out["written"]) == set(listed)
    assert out["pages"] == 3                                  # 5+5+2 のまとまりごとに1つ
    assert all(state.is_written(s) for s in listed)


def test_作り直しは1日の上限回数まで_送り直しは制限しない(tmp_path, monkeypatch):
    state, result, listed = _morning(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("Chromium failed")

    monkeypatch.setattr(articles_mod.render, "capture_article_pdf", broken)
    articles_mod.run_articles(_run(tmp_path), state, story_ids=listed,
                              message_ts=result["message_ts"])
    for _ in range(5):
        articles_mod.process_redo(_run(tmp_path), state)
    item = state.redo[0]
    assert item["regen"] == {"date": "2026-09-01", "count": 3}   # 同じ日は3回まで
    assert item["attempts"] == 3                                 # 見送った回は数えない


def test_今月の利用時間が上限に近ければ作り直さず_1度だけ知らせる(tmp_path, monkeypatch):
    state, result, listed = _morning(tmp_path)

    def broken(*args, **kwargs):
        raise RuntimeError("Chromium failed")

    monkeypatch.setattr(articles_mod.render, "capture_article_pdf", broken)
    articles_mod.run_articles(_run(tmp_path), state, story_ids=listed,
                              message_ts=result["message_ts"])
    run = _run(tmp_path)
    run.record_usage(state, 1700)
    assert any("実行時間" in r for r in run.reports)
    run.reports.clear()
    run.record_usage(state, 1)
    assert not run.reports                                        # 同じ月は1度だけ
    articles_mod.process_redo(run, state)
    assert state.redo[0].get("regen") is None                     # 作り直していない
    assert any("上限に近い" in line for line in run.status)



def test_週1回の点検は大事な部品が動かないときだけ直し方を添えて知らせる():
    from src import check
    ok = check.Result("Claude（執筆）", check.OK, "")
    assert check.notice_for([ok]) == ""
    bad = check.Result("Claude（執筆）", check.NG, "401 Unauthorized")
    minor = check.Result("xAI（X上の反応）", check.NG, "x")
    text = check.notice_for([bad, minor, ok])
    assert "Claude（執筆）: 401" in text and "setup-token" in text
    assert "xAI" not in text



def test_公開ログでは失敗理由から中身を落とす():
    from src.pipeline import log_reason
    reason = "claude -p の出力がJSONではない: 自分との関係: 個人開発者として"
    assert log_reason(reason, public=False) == reason
    assert log_reason(reason, public=True) == "claude -p の出力がJSONではない"
    assert "個人" not in log_reason("出力形式が不正（個人開発者）", public=True)
    # API のエラーコードは残す（原因が分かるように）。日本語の中身は落とす
    assert log_reason("Slack files.completeUploadExternal: not_in_channel",
                      public=True) == "Slack files.completeUploadExternal: not_in_channel"
    assert log_reason("Slack chat.postMessage: 記事の題名", public=True) == "Slack chat.postMessage"


def test_本人の像は非公開データのファイルから読む(tmp_path):
    from src.config import load as load_config
    from src.write import reader_profile
    cfg = load_config()
    cfg.root = tmp_path
    (tmp_path / "private").mkdir()
    assert reader_profile(cfg) == ""
    (tmp_path / "private" / "profile.md").write_text("個人開発者。\n", encoding="utf-8")
    assert reader_profile(cfg) == "個人開発者。"
