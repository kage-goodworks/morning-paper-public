"""週次レビュー（反応の変化）。

新規ニュースの要約ではない。過去に収集した記事の**反応がどう変わったか**を
まとめる。**変化が確認できたものだけ書く。**対象が0件の週はレビューを作らず、
Slackにも投稿しない（FR-11）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from . import note, paginate, render
from . import slack as slack_io
from . import vault, write
from .models import Article, Reactions
from .pipeline import Run
from .state import State

#: 反応量がこの割合以上変われば「変化した」とみなす。
CHANGE_RATIO = 0.2
CHANGE_MIN = 10


@dataclass
class Change:
    story_id: str
    title: str
    before: Reactions
    after: Reactions
    note_link: str = ""


def week_string(date_str: str) -> str:
    """`YYYY-Www` 形式。"""
    try:
        d = date.fromisoformat(date_str)
    except ValueError:
        return date_str
    year, week, _ = d.isocalendar()
    return f"{year}-W{week:02d}"


def has_changed(before: Reactions, after: Reactions) -> bool:
    """反応が変わったかを判定する。純粋関数。"""
    for field_name in ("hn_points", "hn_comments", "hatena", "reddit"):
        old = getattr(before, field_name, 0)
        new = getattr(after, field_name, 0)
        if abs(new - old) >= CHANGE_MIN and (old == 0 or
                                             abs(new - old) / max(old, 1) >= CHANGE_RATIO):
            return True
    if (before.x_summary or "") != (after.x_summary or ""):
        return True
    return False


def collect_changes(run: Run, state: State) -> list[Change]:
    """直近7日の対象記事について、保存済みと現在の反応を比べる。"""
    categories = list(run.config.get("reactions.weekly_review_categories", [])
                      or [])
    try:
        today = date.fromisoformat(run.date_str)
    except ValueError:
        return []
    since = today - timedelta(days=7)

    changes: list[Change] = []
    seen_stories: set[str] = set()

    for story_id, info in list(state.written.items()) + [
            (s.id, {"date": s.last_updated, "title": s.title})
            for s in state.all_stories()]:
        if story_id in seen_stories:
            continue
        seen_stories.add(story_id)

        try:
            when = date.fromisoformat(str(info.get("date") or "")[:10])
        except ValueError:
            continue
        if when < since:
            continue

        articles = [a for a in (vault.read_article(run, rel)
                                for rel in state.story_notes.get(story_id, []))
                    if a]
        if not articles:
            continue
        if articles[0].category not in categories:
            continue

        before = articles[-1].reactions
        after = run.guarded(
            lambda a=articles[-1]: run.clients.reactions.fetch(a),
            target="反応の再取得（週次）",
            impact="その話題の変化を確認できません",
            default=None, status=None)
        if after is None:
            continue

        if run.config.get("reactions.x_enabled", False):
            social = run.guarded(
                lambda a=articles[-1]: run.clients.social.reactions([a]),
                target="xAI（週次の反応）",
                impact="X上の変化が載りません",
                default={})
            result = (social or {}).get(articles[-1].url)
            if result is not None:
                from . import reactions as reactions_mod
                after.x_summary = reactions_mod.summarize(result)

        if has_changed(before, after):
            changes.append(Change(
                story_id=story_id, title=info.get("title") or story_id,
                before=before, after=after,
                note_link=f"50_Logs/News/{when.isoformat()}"))

    return changes


def _prompt_targets(changes: list[Change]) -> list[write.WriteTarget]:
    from .models import Story
    targets: list[write.WriteTarget] = []
    for change in changes:
        story = Story(id=change.story_id, title=change.title, category="ai")
        article = Article(
            id=change.story_id, url="", title=change.title, source="weekly",
            category="ai", published_at="",
            body_md=(
                f"収集当時: HN {change.before.hn_points}点 / "
                f"はてブ {change.before.hatena} / Reddit {change.before.reddit}\n"
                f"現在: HN {change.after.hn_points}点 / "
                f"はてブ {change.after.hatena} / Reddit {change.after.reddit}\n"
                f"X（当時）: {change.before.x_summary or 'なし'}\n"
                f"X（現在）: {change.after.x_summary or 'なし'}"))
        targets.append(write.WriteTarget(story=story, articles=[article]))
    return targets


def run_weekly(run: Run, state: State) -> dict[str, Any]:
    """週次レビューを最後まで通す。同じ週を二度送らない。"""
    week = week_string(run.date_str)
    if state.weekly_posted.get(week):
        # Slack には送った週。Vault の保存だけ失敗していれば、控えてある
        # 本文で保存だけやり直す（Slack へは送らない、Claude も呼ばない）
        saved = _retry_unsaved(run, state, week)
        if saved is None:
            run.note_status(f"週次レビュー: {week} はすでに投稿済みのため何もしていません")
        run.flush_notifications()
        return {"changes": 0, "posted": False, "pages": 0, "png": [],
                "week": week, "skipped": True, "vault_retried": saved is not None,
                "vault_saved": bool(saved)}
    return _run_weekly(run, state)


def _save_weekly_note(run: Run, state: State, week: str, note_text: str) -> bool:
    """週次ノートを Vault へ。失敗したら本文を控えて次回やり直す。"""
    ok = vault.write_note(run, vault.weekly_path(week), note_text)
    ok = ok and vault.commit(run, f"auto: news-review {week}")
    if ok:
        state.weekly_unsaved.pop(week, None)
    else:
        state.weekly_unsaved[week] = note_text
        run.note_status(f"週次レビュー: {week} の Vault 保存に失敗したため次回やり直します")
    run.save_point("週次ノートの保存")
    return bool(ok)


def _retry_unsaved(run: Run, state: State, week: str) -> bool | None:
    """保存だけ失敗した週があればやり直す。無ければ None。"""
    note_text = state.weekly_unsaved.get(week)
    if not note_text:
        return None
    ok = _save_weekly_note(run, state, week, note_text)
    if ok:
        run.note_status(f"週次レビュー: {week} の Vault 保存をやり直して保存しました")
    return ok


def _run_weekly(run: Run, state: State) -> dict[str, Any]:
    """週次レビューを最後まで通す。"""
    changes = collect_changes(run, state)

    if not changes:
        run.note_status("週次レビュー: 変化が確認できた話題が0件のため作成していません")
        run.flush_notifications()
        return {"changes": 0, "posted": False}

    targets = _prompt_targets(changes)
    batch_size = max(1, int(run.config.get("limits.weekly_batch_size", 5)))
    results: list[write.WriteResult] = []
    for index in range(0, len(targets), batch_size):
        outcome = write.write_articles(run, targets[index:index + batch_size])
        if not outcome.ok:
            run.note_status("週次レビュー: 執筆に失敗したため作成していません")
            run.flush_notifications()
            return {"changes": len(changes), "posted": False}
        results.extend(outcome.results)

    paper_articles = [
        paginate.PaperArticle(story_id=r.story_id, title=r.title,
                              body_md=r.body_md, sources=r.sources)
        for r in results
    ]
    pages = paginate.paginate(paper_articles)
    week = week_string(run.date_str)

    captured = run.guarded(
        lambda: render.capture_pdf(pages, title="朝刊 週次レビュー",
                               date_str=run.date_str,
                               out_dir=run.out_dir / "paper",
                               prefix=f"{week}-review"),
        target="紙面の撮影（週次）",
        impact="週次レビューの紙面が出ません",
        default=None)
    png_paths = [str(captured)] if captured else []   # PDF 1つ（名前は歴史的）

    # 見出しと紙面PDFは**1通**で送る。別々に送ると「見出しだけ届く」
    # 「PDFだけ落ちて再送もされない」が起きる（紙面は runner にしか無く、
    # 保留に入れても後から送れない）。3回とも落ちたら成功扱いにしない
    text = f"*{week} 反応の変化* {len(changes)}件"
    if png_paths:
        posted = slack_io.post_completion(run, png_paths, text)     # 送る前に履歴を確認する
    else:
        notice = text + "\n（紙面を生成できなかったため、内容は Vault の週次ノートを参照）"
        posted = slack_io.already_posted(run, notice) or slack_io.post_channel_notice(run, notice)
    if posted is None:
        run.note_status("週次レビュー: Slack へ送れませんでした（内容は Vault の週次ノートに残しています）")
    else:
        state.weekly_posted[week] = str(posted.get("ts") or posted.get("file_id") or "sent")
        run.save_point("週次レビューの投稿")

    entries = [{"title": r.title, "body": r.body_md,
                "daily_link": next((c.note_link for c in changes
                                    if c.story_id == r.story_id), "")}
               for r in results]
    note_text = note.build_weekly(week_str=week, entries=entries,
                                  status=run.status)
    _save_weekly_note(run, state, week, note_text)

    run.flush_notifications()
    return {"changes": len(changes), "posted": bool(posted), "pages": len(pages),
            "png": png_paths, "week": week}


def main(argv: list[str] | None = None) -> int:
    """Actions のエントリポイント。"""
    from datetime import datetime

    from .clients.factory import build
    from .config import load as load_config
    from .state import load as load_state, save as save_state

    cfg = load_config()
    date_str = datetime.now().strftime("%Y-%m-%d")
    out_dir = cfg.root / "out"

    clients = build(mock=False, out_dir=out_dir,
                    slack_channel=cfg.get("slack.channel"),
                    models=cfg.get("models"))
    run = Run(config=cfg, clients=clients, date_str=date_str, out_dir=out_dir)
    state = load_state(cfg.root)
    run.checkpoint = lambda: save_state(state, cfg.root, sections=("outbox",))

    result = run_weekly(run, state)
    save_state(state, cfg.root, sections=("outbox",))
    print(f"変化 {result['changes']}件 / 投稿 {result['posted']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
