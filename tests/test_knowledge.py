"""knowledge.py のテスト。記事化した中身を主題ごとの知識ノートへ積む。

純粋関数（主題名・項目・索引）と、記事化からの通し（モック Vault へ実ファイルを書く）。
"""

from __future__ import annotations

from src import articles as articles_mod
from src import knowledge
from src import main as main_mod
from src.clients.factory import build_mock
from src.config import load as load_config
from src.pipeline import Run
from src.retry import NO_SLEEP
from src.state import State

BASE = knowledge.DEFAULT_DIR
BODY = """LEAD: 定額プランを追加
POINTS:
- 提供地域が12に拡大
BODY:
本文。
WHY: 自分の朝刊の執筆に使えば、費用の見通しが立つ。
TOPIC: Claude Code
KNOW:
- 2026-10-14 に定額プランが追加された
- 提供地域が3から12に増えた
"""


def entry(**kw):
    base = dict(story_id="s_abc123", title="題", category="ai", body_md=BODY,
                sources=["https://example.com/a"],
                notes=["40_Knowledge/News/ai/2026/10/2026-10-14-aaaa1111.md"],
                date_str="2026-10-14")
    base.update(kw)
    return knowledge.entry_from(**base)


# --------------------------------------------------------------------------
# 純粋関数
# --------------------------------------------------------------------------

def test_主題名は比較のとき全角半角と大小と空白を無視する():
    assert knowledge.topic_key("Claude Code") == knowledge.topic_key("ｃｌａｕｄｅ　ｃｏｄｅ")
    assert knowledge.topic_key("GitHub Actions") != knowledge.topic_key("GitHub")


def test_主題名はファイル名に使えない文字を落とし_空なら未分類():
    assert knowledge.clean_topic('A/B:C*"D"') == "A B C D"
    assert knowledge.clean_topic("   ") == knowledge.UNSORTED
    assert len(knowledge.clean_topic("あ" * 200)) <= 60
    assert knowledge.note_path(BASE, "ai", "Claude Code") == f"{BASE}/ai/Claude Code.md"


def test_TOPICとKNOWから項目を作り_活かし方と出典を持つ():
    e = entry()
    assert e.topic == "Claude Code"
    assert e.facts == ["2026-10-14 に定額プランが追加された", "提供地域が3から12に増えた"]
    assert e.headline == "定額プランを追加"
    assert "費用の見通し" in e.use
    text = knowledge.render_entry(e)
    assert text.startswith("## 2026-10-14 定額プランを追加\n<!-- story:s_abc123 -->")
    assert "**活かし方**:" in text
    assert "[[40_Knowledge/News/ai/2026/10/2026-10-14-aaaa1111|元記事]]" in text
    assert "[example.com](https://example.com/a)" in text


def test_TOPICが無い出力は要点を知識にして未分類へ入れる():
    e = entry(body_md="LEAD: 一言\nPOINTS:\n- 要点1\n- 要点2\nBODY:\n本文。")
    assert e.topic == knowledge.UNSORTED
    assert e.facts == ["要点1", "要点2"]


def test_取得先にできないURLは出典に載せない():
    e = entry(sources=["javascript:alert(1)", "https://ok.example/x"])
    assert e.sources == ["https://ok.example/x"]


def test_新しい項目は一番上に入り_同じ話題は2度入らない():
    note = knowledge.new_note(topic="Claude Code", category="ai", label="AI・機械学習",
                              date_str="2026-10-13")
    first, changed = knowledge.add_entry(note, entry(story_id="s_old", date_str="2026-10-13"))
    assert changed
    second, changed = knowledge.add_entry(first, entry(story_id="s_new", date_str="2026-10-14"))
    assert changed
    assert second.index("<!-- story:s_new -->") < second.index("<!-- story:s_old -->")
    assert "updated: 2026-10-14" in second
    assert "created: 2026-10-13" in second
    again, changed = knowledge.add_entry(second, entry(story_id="s_new"))
    assert not changed and again == second


def test_索引は書いて読み直せる_最近の追加は日数で切る():
    index = knowledge.Index()
    index.topics[knowledge.topic_key("Claude Code")] = knowledge.Topic(
        name="Claude Code", path=f"{BASE}/ai/Claude Code.md", category="ai",
        updated="2026-10-14", count=3)
    index.topics[knowledge.topic_key("Rust")] = knowledge.Topic(
        name="Rust", path=f"{BASE}/lang/Rust.md", category="lang",
        updated="2026-09-01", count=1)
    index.recent = [
        knowledge.Recent("2026-10-14", f"{BASE}/ai/Claude Code.md", "Claude Code", "定額プラン"),
        knowledge.Recent("2026-08-01", f"{BASE}/lang/Rust.md", "Rust", "古い話"),
    ]
    text = knowledge.render_index(index, labels={"ai": "AI・機械学習", "lang": "言語"},
                                  order=["ai", "lang"], today="2026-10-15", recent_days=30)
    assert "- 2026-10-14 [[40_Knowledge/Tech/ai/Claude Code|Claude Code]] — 定額プラン" in text
    assert "古い話" not in text                                   # 30日より前は落とす
    assert "### AI・機械学習" in text and "（最終 2026-10-14・3件）" in text
    assert "AIへの案内" in text

    back = knowledge.parse_index(text, base=BASE)
    assert set(back.topics) == {knowledge.topic_key("Claude Code"), knowledge.topic_key("Rust")}
    assert back.find("claude code").category == "ai"
    assert back.find("Rust").count == 1
    assert [r.name for r in back.recent] == ["Claude Code"]
    assert knowledge.topic_names(back) == ["Claude Code", "Rust"]   # 新しく使われた順


def test_執筆プロンプトに主題の部品と既存の主題が入る():
    from src import write
    from tests.conftest import make_article, make_story
    target = write.WriteTarget(story=make_story(), articles=[make_article(id="a1")])
    text = write.build_prompt([target], char_range=[600, 900], hint_count=1,
                              topics=["Claude Code", "Rust"])
    assert "TOPIC:" in text and "KNOW:" in text
    assert "--- 既存の主題" in text and "Claude Code、Rust" in text
    plain = write.build_prompt([target], char_range=[600, 900], hint_count=1)
    assert "--- 既存の主題" not in plain


def test_主題と知識は紙面に出ない():
    from src import paginate, render
    from src.clients.parsers import article_sections
    sec = article_sections.parse(BODY)
    assert sec.topic == "Claude Code" and len(sec.knowledge) == 2
    page = render.build_article_sheet(
        paginate.PaperArticle(story_id="s_1", title="題", body_md=BODY, category="ai"),
        date_str="2026-10-14", labels={}, index=1, total=1)
    assert "提供地域が3から12に増えた" not in page
    assert "TOPIC" not in page and "KNOW" not in page


# --------------------------------------------------------------------------
# 記事化からの通し
# --------------------------------------------------------------------------

def _run(tmp_path, **kw) -> Run:
    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path, **kw)
    return Run(config=cfg, clients=clients, date_str="2026-09-01",
               out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)


def test_記事化すると主題ごとの知識ノートと索引がVaultにでき_作り直しで重複しない(tmp_path):
    state = State()
    morning = main_mod.run_morning(_run(tmp_path), state)
    listed = [s.id for s in state.all_stories()
              if s.status == "listed" and s.kind != "gallery"][:6]

    run = _run(tmp_path)
    out = articles_mod.run_articles(run, state, story_ids=listed,
                                    message_ts=morning["message_ts"])
    assert len(out["written"]) == 6

    vault = tmp_path / "vault" / BASE
    index = (vault / knowledge.INDEX_NAME).read_text(encoding="utf-8")
    notes = [p for p in vault.rglob("*.md") if p.name != knowledge.INDEX_NAME]
    assert notes, "知識ノートができていない"
    marks = sum(p.read_text(encoding="utf-8").count("<!-- story:") for p in notes)
    assert marks == 6                                         # 1話題1項目
    # 同じ主題（モックでは見出しの最初の語）は1つのノートにまとまる
    assert len(notes) < 6 or len({p.stem for p in notes}) == len(notes)
    assert "## 最近の追加" in index and "2026-09-01 [[40_Knowledge/Tech/" in index
    # 元の記事ノートへのリンクが付く
    assert "|元記事]]" in notes[0].read_text(encoding="utf-8")
    assert any("knowledge" in c for c in run.clients.notes.commits)
    assert any(line.startswith("知識: 6件") for line in run.status)

    # 作り直し（force）でも、同じ話題は追記しない
    again = _run(tmp_path)
    articles_mod.run_articles(again, state, story_ids=listed,
                              message_ts=morning["message_ts"], force=True)
    marks_after = sum(p.read_text(encoding="utf-8").count("<!-- story:")
                      for p in vault.rglob("*.md") if p.name != knowledge.INDEX_NAME)
    assert marks_after == 6

    # 2回目の執筆では、既存の主題名がプロンプトに渡る
    names = knowledge.topic_names(knowledge.parse_index(index, base=BASE))
    assert names


def test_設定で止めれば知識ノートは作らない(tmp_path):
    state = State()
    morning = main_mod.run_morning(_run(tmp_path), state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]
    run = _run(tmp_path)
    run.config.data.setdefault("knowledge", {})["enabled"] = False
    try:
        articles_mod.run_articles(run, state, story_ids=listed,
                                  message_ts=morning["message_ts"])
    finally:
        run.config.data["knowledge"]["enabled"] = True
    assert not (tmp_path / "vault" / BASE).exists()


def test_知識の保存に失敗しても記事化は成功のまま(tmp_path, monkeypatch):
    state = State()
    morning = main_mod.run_morning(_run(tmp_path), state)
    listed = [s.id for s in state.all_stories() if s.status == "listed"][:2]

    def broken(*args, **kwargs):
        raise RuntimeError("vault down")

    monkeypatch.setattr(articles_mod.knowledge, "record", broken)
    run = _run(tmp_path)
    out = articles_mod.run_articles(run, state, story_ids=listed,
                                    message_ts=morning["message_ts"])
    assert len(out["written"]) == 2 and out["failed"] is False
    assert any(line.startswith("知識: 保存に失敗") for line in run.status)
