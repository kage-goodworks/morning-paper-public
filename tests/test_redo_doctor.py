"""やり直しが続けて失敗したときの「原因を調べて、直してから再挑戦」。"""

from __future__ import annotations

import json

from src import articles as articles_mod
from src import main as main_mod
from src import redo_doctor as doctor
from src.clients.factory import build_mock
from src.config import load as load_config
from src.pipeline import Run
from src.retry import NO_SLEEP
from src.state import State

CFG = load_config()


def item(**kw):
    base = {"date": "2026-10-23", "story_ids": ["s_a", "s_b"], "attempts": 6,
            "reason": "other", "errors": []}
    base.update(kw)
    return base


# --------------------------------------------------------------------------
# 原因の判定（純粋関数）
# --------------------------------------------------------------------------

def test_Slackの送信が時間切れなら画像を外して1通1ファイルにする():
    d = doctor.diagnose(item(reason="slack",
                             errors=["server|Slack（記事化の完了通知）|HTTP Error 504"]), CFG)
    assert "時間切れ" in d.cause
    assert d.fixes == {"delivery.files_per_message": 1, "render.no_images": True}
    assert not d.needs_user and not d.give_up


def test_Slackの鍵が無効なら本人に差し替えを頼む():
    d = doctor.diagnose(item(reason="slack", errors=["auth|Slack|invalid_auth"]), CFG)
    assert "SLACK_BOT_TOKEN" in d.cause and "差し替え" in d.needs_user
    assert d.fixes == {}


def test_記事用チャンネルに入れないなら一覧のチャンネルへ送り_招待を頼む():
    d = doctor.diagnose(item(reason="slack", errors=["unknown|Slack|not_in_channel"]), CFG)
    assert d.fixes == {"slack.articles_channel": ""}
    assert "/invite" in d.needs_user


def test_Claudeの利用枠切れならGeminiで書く():
    d = doctor.diagnose(item(reason="credit"), CFG)
    assert d.fixes == {"write.model": "gemini", "write.fallback_model": "claude"}
    assert "補充" in d.needs_user


def test_AIの出力が崩れるなら本数を減らしAIの順番を入れ替える():
    d = doctor.diagnose(item(errors=["unknown|claude（記事の執筆）|出力形式が不正（ARTICLE番号が不正）"]), CFG)
    assert "崩れ" in d.cause
    assert d.fixes["limits.articles_per_request"] == 2 and d.fixes["write.model"] == "gemini"


def test_紙面が作れないなら画像を外す_時間切れなら少量ずつ():
    assert doctor.diagnose(item(reason="render"), CFG).fixes == {"render.no_images": True}
    aborted = doctor.diagnose(item(reason="aborted"), CFG)
    assert aborted.fixes["auto_retry.per_run_limit"] == 15


def test_元記事のノートが無いなら直しようがないので外す():
    d = doctor.diagnose(item(errors=["記事化: X の記事ノートを読めませんでした（0/2件）"]), CFG)
    assert d.give_up and "Vault" in d.cause


def test_対策は回数で段階が上がる():
    found = {"cause": "c", "fixes": {"render.no_images": True}}
    assert doctor.fixes_for(item(attempts=5, diagnosis=found), CFG) == {}
    assert doctor.fixes_for(item(attempts=6, diagnosis=found), CFG) == {"render.no_images": True}
    strong = doctor.fixes_for(item(attempts=8, diagnosis=found), CFG)
    assert strong["limits.articles_per_request"] == 1 and strong["auto_retry.per_run_limit"] == 5
    assert strong["render.no_images"] is True


def test_設定の上書きはその間だけで_元に戻る():
    cfg = load_config()
    before = cfg.get("limits.articles_per_request")
    with doctor.applied(cfg, {"limits.articles_per_request": 1, "delivery.files_per_message": 1}):
        assert cfg.get("limits.articles_per_request") == 1
        assert cfg.get("delivery.files_per_message") == 1
    assert cfg.get("limits.articles_per_request") == before
    assert cfg.get("delivery.files_per_message") is None


def test_知らせには原因と対策とお願いが入る():
    d = doctor.Diagnosis(cause="原因X", fix_text="対策Y", needs_user="操作Z")
    text = doctor.notice_text(item(attempts=6), d)
    assert "5回やり直しても" in text
    assert "原因: 原因X" in text and "対策: 対策Y" in text and "お願い: 操作Z" in text


# --------------------------------------------------------------------------
# 通し: 5回失敗 → 原因を調べる → 対策を入れて通る
# --------------------------------------------------------------------------

def _run(tmp_path, **kw) -> Run:
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path, **kw)
    cfg = load_config()
    # 同じ日に何度も回して段階を確かめるため、1日の作り直し回数の上限を外す
    cfg.data.setdefault("auto_retry", {})["regenerations_per_day"] = 0
    return Run(config=cfg, clients=clients, date_str="2026-09-01",
               out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)


def test_紙面が作れない状態が続くと原因を調べ_画像を外して作り直して届ける(tmp_path, monkeypatch):
    state = State()
    morning = main_mod.run_morning(_run(tmp_path), state)
    listed = [s.id for s in state.all_stories()
              if s.status == "listed" and s.kind != "gallery"][:3]
    original = articles_mod.render.capture_article_pdf
    real_applied = doctor.applied
    current: dict = {}

    def recording(config, fixes):
        current.clear()
        current.update(fixes)
        return real_applied(config, fixes)

    def needs_no_images(pages, **kwargs):
        # 画像を外す対策が入っている回だけ通る紙面
        if not current.get("render.no_images"):
            raise RuntimeError("Chromium failed")
        return original(pages, **kwargs)

    monkeypatch.setattr(articles_mod.redo_doctor, "applied", recording)
    monkeypatch.setattr(articles_mod.render, "capture_article_pdf", needs_no_images)
    run = _run(tmp_path)
    articles_mod.run_articles(run, state, story_ids=listed, message_ts=morning["message_ts"])
    assert state.redo and state.redo[0]["reason"] == "render"

    # 1〜5回目: そのままやり直す（画像ありのまま落ち続ける）
    for _ in range(5):
        articles_mod.process_redo(_run(tmp_path), state)
    assert state.redo and state.redo[0]["attempts"] == 5
    assert "diagnosis" not in state.redo[0]

    # 6回目: 原因を調べ、画像を外して作り直す → 届く
    out = articles_mod.process_redo(_run(tmp_path), state)
    assert set(out["written"]) == set(listed)
    assert state.redo == []
    posts = [json.loads(p.read_text(encoding="utf-8"))
             for p in (tmp_path / "slack").glob("*.json") if not p.name.startswith("_")]
    notice = [m for m in posts if "原因を調べました" in m.get("text", "")]
    assert notice and "画像を外して紙面を作ります" in notice[0]["text"]
    # 対策は実行の間だけ。終わったら設定は元に戻っている
    assert load_config().get("render.no_images") is None
