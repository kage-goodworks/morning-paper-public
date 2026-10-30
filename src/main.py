"""朝のエントリポイント。

`state.json` を読む → 収集 → 本文取得 → 翻訳 → 反応取得 → 話題への統合
→ Vaultへ保存 → 一覧をSlackへ投稿 → `state.json` を更新。

**下ごしらえを全部ここで終える。**ボタンを押したあとは、ここで保存した記事
ノートを読んで書くだけにする。
"""

from __future__ import annotations

from time import sleep as real_sleep
from typing import Any

from . import blocks, collect, discover, extract, note, organize, reactions
from . import slack as slack_io
from . import vault
from .pipeline import Run
from .retry import NO_SLEEP
from .state import State, save


def run_morning(run: Run, state: State) -> dict[str, Any]:
    """朝の処理を最後まで通す。どこで失敗しても一覧は必ず出る。"""
    config = run.config
    order = list(config.get("categories.order", []) or [])
    labels = dict(config.get("categories.labels", {}) or {})

    # 0. 同じ日に2回走らない。主（Cloudflare の時計）と保険（GitHub の
    #    schedule）の両方から起動されるため。一覧をすでに出していれば終わる。
    held_today = any(item.get("list_date") == run.date_str for item in state.pending)
    if state.listed_messages.get(run.date_str) or held_today:
        # 1通目だけ送れて2通目が落ちた日、あるいは全通が送れずに保留になった日。
        # 一覧はもう作ってあるので作り直さず、送れていない分だけ送る
        # （作り直すと、保留分と新しい一覧の両方が届いて同じ日に2つ並ぶ）
        sent = slack_io.flush_pending(run, state)
        if sent:
            run.save_point("保留分の投稿")
        run.note_status("朝の処理: 今日の一覧はすでに作成済みのため"
                        + (f"、送れていなかった{sent}通だけ送りました" if sent
                           else "何もしていません"))
        run.flush_notifications()
        listed = state.listed_messages.get(run.date_str) or []
        return {"articles": 0, "stories": 0, "listed": 0, "unpublished": 0,
                "messages": sent, "message_ts": listed[0] if listed else None,
                "skipped": True}

    # 1. 収集。開始時刻を「今回の成功時点」として控える。04:40 に集めて
    #    05:00 と記録すると、04:40〜05:00 に出た記事が次回「対象期間外」に
    #    なって永久に落ちる。
    collected_at = run.now_iso()
    articles = collect.collect(run)
    articles.extend(collect.collect_releases(run))

    # 2. 対象期間に絞る（Requirements「前回成功時点以降。初回は直近24時間」）
    #    フィードは過去分を千件単位で載せることがあり、ここで絞らないと
    #    本文取得が15分の枠に収まらない。
    since = state.last_success_at or organize.default_window_start(run.date_str)
    articles, outside = organize.within_window(
        articles, since, per_source=state.source_success_at)
    if outside:
        run.note_status(f"対象期間外: {outside}件を除外しました（{since} より前）")

    # 3. 重複排除と seen の除外（AC-1 / AC-12）
    articles, dropped = organize.dedupe(
        articles, seen=state.seen, title_norm=state.title_norm,
        similarity=float(config.get("dedupe.title_similarity", 0.85)))
    if dropped:
        run.note_status(f"重複排除: {dropped}件を除外しました")

    # 4. 件数が足りなければ探索（FR-2 / AC-23）
    trigger = int(config.get("fallback.trigger_count", 5))
    if len(articles) < trigger:
        run.note_status(f"探索: 収集が{len(articles)}件と{trigger}件未満のため探索しました")
        found = discover.run_discovery(run, articles)
        # seen だけでなく、今朝すでに集めた記事とも突き合わせる。同じURLが
        # 両方から返ると、同じ話題が一覧に2件並ぶ
        articles, _ = organize.dedupe(
            articles + found, seen=state.seen, title_norm=state.title_norm,
            similarity=float(config.get("dedupe.title_similarity", 0.85)))

    # 個人投稿（写真集）はここで分ける。本文取得・翻訳・反応は通さず、
    # 話題への統合もしない（1件ずつ一覧に流さず、写真集として1行にまとめる）
    gallery_items = [a for a in articles if a.kind == "gallery"]
    articles = [a for a in articles if a.kind != "gallery"]

    # 5. 本文取得（4段のフォールバック）
    articles = extract.extract_all(run, articles)

    # 6. カテゴリの上書き
    keyword_map = dict(config.get("categories.keywords", {}) or {})
    for article in articles:
        article.category = organize.categorize(article, keyword_map)

    # 7. タイトルの翻訳（AC-25）
    articles = extract.translate_titles(run, articles)

    # 8. 反応（無料 → X）
    articles = reactions.fetch_free(run, articles)
    x_reactions = reactions.fetch_x(run, articles)
    x_topics = reactions.fetch_x_topics(run)
    for article in articles:
        result = x_reactions.get(article.url)
        if result is not None:
            article.reactions.x_summary = reactions.summarize(result)
            article.reactions.x_citations = result.citations

    # 9. 話題への統合（AC-7〜AC-10）
    #    期限切れの繰り越しは**統合の前に**落とす。統合の後に落とすと、
    #    期限切れの話題へまとめられた今日の新着まで一緒に消える。
    carryover, expired = organize.expire(
        state.carryover_stories(), today=run.date_str,
        carryover_days=int(config.get("limits.carryover_days", 7)))
    if expired:
        run.note_status(f"繰り越し: 期限切れの{expired}件を候補から外しました")

    stories, merged = organize.build_stories(
        articles, carryover, today=run.date_str,
        overlap=float(config.get("story.keyword_overlap", 0.5)),
        stopwords=list(config.get("story.stopwords", []) or []),
        article_urls=state.story_urls)
    if merged:
        run.note_status(f"統合: {merged}件を既存の話題にまとめました")

    # 写真集の話題（カテゴリごとに1つ）。採用した投稿だけノートに残す
    galleries, gallery_chosen = organize.build_galleries(
        gallery_items, today=run.date_str, labels=labels,
        per_gallery=int(config.get("limits.gallery_items", 6)))
    if galleries:
        stories.extend(galleries)
        articles.extend(gallery_chosen)
        run.note_status(f"写真集: {len(gallery_chosen)}件の投稿を{len(galleries)}件の写真集にまとめました")

    # 10. カテゴリごとの上限（FR-3「バランスと繰り越し」）
    shown, counts = organize.apply_limits(
        stories, order=order,
        per_category=int(config.get("limits.per_category", 12)))
    shown_ids = {s.id for group in shown.values() for s in group}
    unpublished = [s for s in stories
                   if s.status == "carryover" and s.id not in shown_ids]
    for category, (total, kept) in counts.items():
        if total > kept:
            run.note_status(
                f"{labels.get(category, category)}: {total}件中{kept}件を表示"
                f"（{total - kept}件は未発行）")

    # 11. 株価
    quotes = collect.collect_quotes(run)

    # 12. Vaultへ保存
    #     記事ノート（1記事1ノート）→ デイリーノート → 未発行のリンク集
    articles_by_id = {a.id: a for a in articles}
    story_of = {a: s.id for s in stories for a in s.article_ids}

    note_writes = vault.write_articles(run, articles, story_of, run.date_str)
    written_notes = note_writes.created
    if written_notes:
        run.note_status(f"保存: 記事ノートを{written_notes}件作成しました")

    # 繰り越された話題は前日以前の記事を束ねている。デイリーノートのリンクを
    # 作るために、保存済みのノートから読み直して補う。
    for story in stories:
        for article_id in story.article_ids:
            if article_id in articles_by_id:
                continue
            for rel in state.story_notes.get(story.id, []):
                if rel.endswith(f"-{article_id}.md"):
                    saved = vault.read_article(run, rel)
                    if saved is not None:
                        articles_by_id[article_id] = saved
                    break

    daily = note.build_daily(
        date_str=run.date_str, shown=shown, labels=labels,
        articles_by_id=articles_by_id, status=run.status,
        pending_count=len(unpublished), quotes=quotes, x_topics=x_topics,
        agents=["gemini"] if any(a.title_original for a in articles) else [])
    vault.write_note(run, vault.daily_path(run.date_str), daily)

    pending_note = note.build_pending(
        date_str=run.date_str, stories=unpublished, labels=labels,
        articles_by_id=articles_by_id)
    vault.write_note(run, vault.pending_path(), pending_note)

    vault_committed = vault.commit(run, f"auto: news {run.date_str}")

    # 13. Slackへ投稿
    slack_io.flush_pending(run, state)
    messages = blocks.build_list(
        date_str=run.date_str, shown=shown, labels=labels, status=run.status,
        quotes=quotes, x_topics=x_topics,
        options_max=int(config.get("slack.checkbox_options_max", 10)),
        blocks_per_message=int(config.get("slack.blocks_per_message", 45)))
    posted = slack_io.post_list(run, state, messages,
                                text=slack_io.list_text(run.date_str, 0, 1),
                                list_date=run.date_str)
    # 投稿は取り消せない。ここで死んでも「投稿済み」が残るように書いておく
    run.save_point("一覧の投稿")
    # 以降の失敗通知は一覧のスレッドへ（一覧のチャンネルを一覧だけに保つ）
    if posted and config.get("slack.errors_in_thread", True):
        run.notice_thread_ts = posted[0].get("ts") or None

    # 14. state の更新
    listed_ids = [s.id for group in shown.values() for s in group]
    state.put_stories(stories)
    state.mark_listed(listed_ids, run.date_str)
    for story in stories:
        state.story_urls.setdefault(story.id, [])
        state.story_articles[story.id] = list(story.article_ids)
        notes: list[str] = list(state.story_notes.get(story.id, []))
        for article_id in story.article_ids:
            article = articles_by_id.get(article_id)
            if article is None or article.id not in note_writes.available:
                continue
            if article.url not in state.story_urls[story.id]:
                state.story_urls[story.id].append(article.url)
            rel = note.article_path(article)
            if rel not in notes:
                notes.append(rel)
        state.story_notes[story.id] = notes

    # ノートを書けず、または commit/push できなかった記事は seen にしない。
    # 次回の朝処理で再取得し、Vault 保存をやり直せる状態にしておく。
    persisted = note_writes.available if vault_committed else set()
    for article in articles:
        if article.id not in persisted:
            continue
        state.seen[article.url_hash] = run.date_str
        norm = organize.normalize_title(article.title)
        if norm:
            state.title_norm[norm] = run.date_str

    state.seen = organize.prune_seen(state.seen, today=run.date_str, days=30)
    state.title_norm = organize.prune_seen(
        state.title_norm, today=run.date_str,
        days=int(config.get("dedupe.title_memory_days", 7)))

    # Slackの履歴は90日。過ぎるとボタンが押せないので候補から外す。
    # 記事そのものはVaultの記事ノートに残る。
    culled = state.prune_listed(today=run.date_str,
                               days=int(config.get("limits.listed_days", 90)))
    if culled:
        run.note_status(f"整理: 90日を過ぎた話題{culled}件を候補から外しました")

    # 前回成功時点の更新。Vault に保存できなかった記事は seen に無いので、
    # 時点を進めると次回「対象期間外」になり二度と拾えない。
    # - 全部保存できた → 収集開始時刻へ進める
    # - 一部だけ失敗 → 失敗した記事のうち最も古い公開時刻まで**しか**進めない
    #   （その記事だけ次回もう一度拾える。保存済みは seen が弾く）
    # - commit/push 自体が失敗 → 進めない
    unsaved = [a for a in articles if a.id not in persisted]
    next_point = organize.success_point(collected_at, unsaved)
    if not vault_committed:
        run.note_status("対象期間: Vault保存に失敗したため前回成功時点を進めていません"
                        "（次回、今回の分も取り直します）")
    elif unsaved and not next_point:
        run.note_status(f"対象期間: {len(unsaved)}件の記事ノートを保存できず公開時刻も"
                        "読めないため、前回成功時点を進めていません")
    elif unsaved:
        state.last_success_at = next_point
        run.note_status(f"対象期間: {len(unsaved)}件の記事ノートを保存できなかったため、"
                        f"前回成功時点を {next_point} に留めています（次回取り直します）")
    else:
        state.last_success_at = collected_at

    # ソースごとの成功時点。今回落ちたソースは進めない（復旧後にその間の
    # 記事も拾えるように）。成功したソースは全体と同じ時点へ
    if vault_committed:
        failed = set(run.failed_sources)
        for source in _all_source_ids(config):
            if source not in failed:
                state.source_success_at[source] = state.last_success_at or collected_at
            elif source not in state.source_success_at:
                # 一度も成功していないソースが落ちた。記録が無いままだと復旧時に
                # 全体の新しい時点が使われ、障害中の記事が落ちる。今回の窓の
                # 始まりを「このソースの前回成功時点」として残す
                state.source_success_at[source] = since
    run.flush_notifications()

    return {
        "articles": len(articles),
        "stories": len(stories),
        "listed": len(listed_ids),
        "unpublished": len(unpublished),
        "notes": written_notes,
        "messages": len(messages),
        "message_ts": posted[-1]["ts"] if posted else None,
        "status": list(run.status),
        "shown": shown,
        "quotes": quotes,
        "x_topics": x_topics,
    }


def _all_source_ids(config) -> list[str]:
    ids = [str(s.get("id")) for s in (config.get("sources", []) or []) if s.get("id")]
    ids += [str(c.get("id")) for c in (config.get("youtube", []) or []) if c.get("id")]
    if config.get("releases.repos", []):
        ids.append(collect.RELEASES_SOURCE)
    return ids


def main(argv: list[str] | None = None) -> int:
    """本番のエントリポイント。実キー投入の段階で `--mock` を外して使う。"""
    import argparse
    from datetime import datetime

    from .clients.factory import build
    from .config import load as load_config
    from .state import load as load_state

    parser = argparse.ArgumentParser(prog="python -m src.main")
    parser.add_argument("--mock", action="store_true", help="外部へ接続しない")
    parser.add_argument("--offline", action="store_true", help="--mock と同義")
    parser.add_argument("--dry-run", action="store_true",
                        help="実際に取得するが、Slackへ投稿せずVaultへpushしない")
    parser.add_argument("--fail", default=None, help="失敗を注入する対象")
    parser.add_argument("--date", default=None)
    args = parser.parse_args(argv)

    mock = args.mock or args.offline
    cfg = load_config()
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    out_dir = cfg.root / "out"
    clients = build(mock=mock, dry_run=args.dry_run,
                    slack_channel=cfg.get("slack.channel"),
                    models=cfg.get("models"),
                    fail=args.fail, out_dir=out_dir)

    data_dir = out_dir if mock or args.dry_run else cfg.root
    run = Run(config=cfg, clients=clients, date_str=date_str, out_dir=out_dir,
              data_dir=data_dir, sleep=NO_SLEEP if mock else real_sleep)
    state = load_state(data_dir)
    run.checkpoint = lambda: save(state, data_dir, sections=("collect", "stories", "outbox"))

    import time as _time
    began = _time.monotonic()
    result = run_morning(run, state)
    if not mock and not args.dry_run:
        setup = float(cfg.get("budget.setup_minutes", 4) or 0)
        run.record_usage(state, setup + (_time.monotonic() - began) / 60)
        run.flush_notifications()
    save(state, data_dir, sections=("collect", "stories", "outbox"))
    print(f"話題 {result['stories']}件 / 表示 {result['listed']}件 "
          f"/ 未発行 {result['unpublished']}件 / 記事 {result['articles']}件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
