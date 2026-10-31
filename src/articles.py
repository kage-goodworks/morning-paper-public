"""記事化のエントリポイント。

「記事を作成」ボタン → Workers → `repository_dispatch` → ここ。

**外部へは取りに行かない。**朝に保存した記事ノートを Vault から読んで、
執筆と紙面化を行うだけ（FR-5）。

**Claudeが書いた文章と紙面画像は保存しない。**元データ（記事ノート）があれば
作り直せるため。
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from dataclasses import dataclass, field

from typing import Any, Callable

from . import blocks, knowledge, paginate, redo_doctor, render
from . import slack as slack_io
from . import vault, write
from .models import Article, Story
from .pipeline import Run
from .state import State


def select_targets(state: State, story_ids: list[str], *, limit: int,
                   force: bool = False
                   ) -> tuple[list[str], list[str], list[str]]:
    """作成済みを除き、`limit` 件ずつのまとまりに分ける前の順序付き一覧。

    戻り値は (未作成の全件, 作成済み, 空)。3つ目は互換のため残している
    （以前は「上限を超えて保留した分」だった。今は全件を1回の起動で
    順に処理するので保留は出ない）。
    """
    if force:
        # 作り直し。作成済みも対象にする（紙面が届かなかった日の再送用）
        return list(story_ids), [], []
    already = [sid for sid in story_ids if state.is_written(sid)]
    remaining = [sid for sid in story_ids if not state.is_written(sid)]
    return remaining, already, []


def load_articles(run: Run, state: State, story: Story) -> list[Article]:
    """保存済みの記事ノートを Vault から読む。外部へは取りに行かない。"""
    out: list[Article] = []
    for rel in state.story_notes.get(story.id, []):
        article = vault.read_article(run, rel)
        if article is not None:
            out.append(article)
    return out


@dataclass
class BatchResult:
    """5件ずつのまとまり1つの結果。"""
    written: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    pdf: str = ""
    kind: str = ""           # 失敗の種類。credit なら以降のまとまりは回さない
    degraded: bool = False
    notice: str = ""
    #: 作成済みになった記事の執筆結果（写真集を除く）。知識ノートの材料
    records: list[write.WriteResult] = field(default_factory=list)


def _write_batch(run: Run, state: State, story_ids: list[str], *,
                 labels: dict[str, str], index: int,
                 topics: list[str] | None = None) -> BatchResult:
    """まとまり1つを執筆して紙面PDFにする。Slack へは送らない。"""
    primary = str(run.config.get("write.model", "claude"))
    result = BatchResult()

    write_targets: list[write.WriteTarget] = []
    for story_id in story_ids:
        story = state.get_story(story_id)
        if story is None:
            result.failed.append(story_id)
            continue
        articles = load_articles(run, state, story)
        # 比べる相手は「登録済みのノート」ではなく「話題の元記事すべて」。
        # 朝の保存に失敗した記事はノートのパス自体が登録されないため
        expected = len(state.story_articles.get(story_id) or story.article_ids)
        if not articles or len(articles) < expected:
            # 一部だけ読めた状態で書くと、欠けたまま「作成済み」になって
            # 復旧後にやり直せない。読めなかった分がある間は作らない
            run.note_status(
                f"記事化: {story.title} の記事ノートを読めませんでした"
                f"（{len(articles)}/{expected}件）。チェックを残しています")
            result.failed.append(story_id)
            continue
        write_targets.append(write.WriteTarget(
            story=story, articles=articles,
            already=set(state.written_articles.get(story_id, []))))
    if not write_targets:
        return result

    # 写真集は執筆しない。一言だけ書かせて、写真を並べる
    gallery_targets = [t for t in write_targets if t.story.kind == "gallery"]
    write_targets = [t for t in write_targets if t.story.kind != "gallery"]

    paper_articles: list[paginate.PaperArticle] = []
    written_targets: list[write.WriteTarget] = []
    results: list[write.WriteResult] = []

    if write_targets:
        outcome = write.write_articles(run, write_targets, topics=topics)
        if not outcome.ok:
            result.failed.extend(t.story.id for t in write_targets)
            result.kind = outcome.kind
            result.notice = write.failure_notice(outcome, count=len(write_targets))
            if not gallery_targets:
                return result
        else:
            result.degraded = outcome.degraded
            result.notice = write.degraded_notice(outcome, primary=primary)
            paper_articles.extend(
                paginate.PaperArticle(
                    story_id=r.story_id, title=r.title, body_md=r.body_md,
                    sources=r.sources, published_dates=sorted(set(r.published_dates)),
                    category=r.category,
                    # 原因を調べた対策で「画像なし」にした回は、元記事の画像を載せない
                    image_url="" if run.config.get("render.no_images") else r.image_url)
                for r in outcome.results)
            written_targets.extend(write_targets)
            results = list(outcome.results)

    for target in gallery_targets:
        notes = write.write_gallery_notes(run, target)
        items = [{"title": a.title, "url": a.url, "image_url": a.image_url,
                  "note": notes.get(i, "")}
                 for i, a in enumerate(target.articles, start=1)]
        paper_articles.append(paginate.PaperArticle(
            story_id=target.story.id, title=target.story.title, body_md="",
            sources=[a.url for a in target.articles], category=target.story.category,
            kind="gallery", gallery=items))
        written_targets.append(target)

    if not paper_articles:
        return result
    pages = paginate.one_per_page(paper_articles)
    captured = run.guarded(
        lambda: render.capture_article_pdf(
            pages, date_str=run.date_str, labels=labels,
            out_dir=run.out_dir / "paper",
            prefix=f"{run.date_str}-articles-{index}", notice=result.notice),
        target="紙面の撮影",
        impact="紙面が出ません",
        default=None)
    if not captured:
        result.failed.extend(t.story.id for t in written_targets)
        result.kind = "render"
        return result

    result.pdf = str(captured)
    for target in written_targets:
        state.written_articles[target.story.id] = [a.id for a in target.articles]
        state.mark_written(target.story.id, date_str=run.date_str, thread_ts="",
                           permalink="")
        result.written.append(target.story.id)
    result.records = [r for r in results if r.story_id in result.written]
    return result


def completion_tag(story_ids: list[str]) -> str:
    """完了通知に添える短い識別子。同じ話題の組なら同じ値。

    再実行時に「この通知はもう送ったか」を Slack の履歴で見分けるため。
    """
    import hashlib
    joined = ",".join(sorted(story_ids))
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()[:6]


def date_label(date_str: str) -> str:
    """`2026-10-02（金）`。完了通知に「どの日の一覧の記事か」を入れるため。"""
    weekday = blocks.weekday_label(date_str)
    return f"{date_str}（{weekday}）" if weekday else date_str


def completion_text(written: int, failed: int, *, degraded: bool,
                    credit: bool, tag: str = "", deferred: int = 0,
                    label: str = "", auto_retry: bool = False) -> str:
    """完了通知の文面。純粋関数。1通で全部伝える。

    `deferred` は時間の上限に達して次回へ回した件数（`failed` に含まれる）。
    作れなかったのではなく「まだ」なので、文面を分ける。
    `label` は一覧の日付（`date_label`）。記事だけのチャンネルで、どの日の
    記事かが本文から分かるようにする（投稿日と一覧の日は違うことがある）。
    """
    broken = max(0, failed - deferred)
    made = (f"{label}の記事 {written}件を生成しました" if label
            else f"{written}件の記事を生成しました")
    if written and not failed:
        text = f"✅ {made}"
    elif written and not broken:
        text = f"✅ {made}（残り{deferred}件は次回へ）"
    elif written:
        text = f"✅ {made}（{broken}件は作れませんでした）"
    elif deferred and not broken:
        text = f":warning: 時間の上限に達し、記事を作れませんでした（{deferred}件）"
    else:
        text = f":warning: 記事を作れませんでした（{failed}件）"
    if tag:
        text += f"  `#{tag}`"
    if credit:
        text += "\nAIの利用枠が尽きています。補充すると再び作れます。"
        if auto_retry:
            text += "（補充後、自動でやり直します）"
    elif broken:
        text += ("\n作れなかった分は、自動でやり直して送ります。操作は不要です。" if auto_retry
                 else "\n作れなかった分はチェックが付いたまま残しています。時間をおいてもう一度押してください。")
    if deferred and not credit:
        text += (f"\n時間の上限に達したため{deferred}件は次回に回しました。"
                 + ("残りは自動で続きから作ります。操作は不要です。" if auto_retry
                    else "チェックは付いたままです。もう一度「記事を作成」を押すと続きから作ります。"))
    if degraded:
        text += "\n（一部は Gemini で書きました）"
    return text


def abort_text(pending: int, *, tag: str = "", auto_retry: bool = False) -> str:
    """途中で止まったあとの後始末の文面。純粋関数。"""
    text = f":warning: 記事化が途中で止まりました（{pending}件が未作成）"
    if tag:
        text += f"  `#{tag}`"
    if auto_retry:
        text += "\n未作成の分は、自動でやり直して送ります。操作は不要です。"
    else:
        text += ("\nチェックは付いたままです。時間をおいてもう一度「記事を作成」を押してください。"
                 "\n続くようなら GitHub Actions の実行ログを確認してください（時間切れ・鍵の期限など）。")
    return text


# --------------------------------------------------------------------------
# 自動のやり直し（2026-10-24）
# --------------------------------------------------------------------------

#: 待ち行列の1項目を諦めるまでの回数（朝・12時・20時に回るので約3日強）。
#: 5回はそのまま、6回目からは原因を調べて対策を入れ、さらに駄目なら強い対策へ上げる
DEFAULT_REDO_MAX = 10
#: 紙面を送れなかったとき、同じ実行の中で送り直すまでの待ち（秒）
DEFAULT_DELIVERY_WAITS = [60, 180, 300]

REASON_TEXT = {
    "slack": "Slack に紙面を送れない（Slack 側の障害・ファイルの大きさ）",
    "credit": "AI の利用枠が尽きている",
    "render": "紙面の画像を作れない（Chromium）",
    "aborted": "記事化の実行が途中で止まる（時間切れ・GitHub 側の障害）",
    "other": "AI が応答しない（混雑）か、元記事のノートを読めない",
}


def setup_minutes(config) -> float:
    """Python の外の準備（依存・フォント・Chromium の導入など）に掛かる分の見積り。"""
    return float(config.get("budget.setup_minutes", 4) or 0)


def usage_minutes(state: State, month: str) -> float:
    return float((state.usage or {}).get(month, 0) or 0)


def usage_tight(config, state: State, today: str) -> bool:
    """今月の GitHub Actions の利用時間（見積り）が上限に近いか。"""
    soft = float(config.get("budget.monthly_minutes_soft", 1600) or 0)
    return bool(soft) and usage_minutes(state, today[:7]) >= soft


def auto_retry_enabled(config) -> bool:
    return bool(config.get("auto_retry.enabled", True))


def delivery_waits(config) -> list[float]:
    return [float(w) for w in (config.get("auto_retry.delivery_waits_seconds",
                                          DEFAULT_DELIVERY_WAITS) or [])]


def enqueue_redo(state: State, story_ids: list[str], *, list_date: str,
                 reason: str, now: str, errors: list[str] | None = None,
                 delivery: dict | None = None) -> None:
    """やり直しの待ち行列へ足す。同じ一覧日の項目にはまとめる。純粋（state だけ触る）。"""
    ids = [s for s in dict.fromkeys(story_ids) if s and not state.is_written(s)]
    if not ids:
        return
    for item in state.redo:
        if item.get("date") == list_date:
            item["story_ids"] = list(dict.fromkeys(list(item.get("story_ids") or []) + ids))
            item["reason"] = reason
            if errors:
                item["errors"] = list(errors)[-10:]
            if delivery:
                item.setdefault("deliveries", []).append(delivery)
            return
    state.redo.append({"date": list_date, "story_ids": ids, "attempts": 0,
                       "reason": reason, "since": now, "errors": list(errors or [])[-10:],
                       **({"deliveries": [delivery]} if delivery else {})})


#: やり直しの実行で、前の実行の紙面（Actions の成果物）を置く場所
REDELIVER_DIR = "redeliver"


def stuck_text(delivery: dict, stuck: list[int], total: int) -> str:
    """一部の通が Slack に受け付けられなかったときの知らせ。純粋関数。"""
    head = str(delivery.get("text") or "").splitlines()[0].lstrip("✅ ").strip() if delivery.get("text") else "紙面"
    nums = "・".join(f"{n}通目" for n in stuck)
    return (f":warning: {head} の紙面{total}通のうち {nums} は、何度送っても Slack に"
            "受け付けられませんでした（内容が Slack の検査で止められた可能性）。残りは届いています。")


def redeliver(run: Run, state: State, item: dict, *, final: bool = False) -> list[str]:
    """前の実行で作ったのに送れなかった紙面を、書き直さずに送り直す。

    紙面は articles.yml が前の実行の成果物から `out/redeliver/<run_id>/` へ取ってくる。
    揃っていなければ（成果物の期限切れなど）その分は諦め、通常の作り直しに任せる。
    送れた話題の id を返す。
    """
    sent: list[str] = []
    keep: list[dict] = []
    for delivery in item.get("deliveries") or []:
        folder = run.out_dir / REDELIVER_DIR / str(delivery.get("run_id"))
        paths = [folder / name for name in delivery.get("files") or []]
        ids = [sid for sid in (delivery.get("written") or {}) if not state.is_written(sid)]
        if not ids:
            continue
        if not paths or not all(p.exists() for p in paths):
            run.note_status("やり直し: 前の紙面が見つからないため作り直します")
            continue
        report: dict = {}
        posted = slack_io.post_completion(
            run, [str(p) for p in paths], str(delivery.get("text") or ""),
            channel=str(delivery.get("channel") or "") or None,
            continued=str(delivery.get("continued") or ""),
            report=report)
        stuck = report.get("failed") or []
        if posted is None and report.get("first") and stuck:
            # 一部の通だけが何度送っても共有されない（内容が Slack の検査で止められる
            # など。2026-10-31、脆弱性の記事を含む1通だけが18通中で止まり続けた）。
            # 同じ通で3回止まったら、届いた分を作成済みにし、止まった通だけを知らせる
            # 最後の回（このあと諦める）でも、届いた分は作成済みにする
            if final or (stuck == delivery.get("stuck")
                         and int(delivery.get("stuck_count") or 0) >= 2):
                posted = report["first"]
                slack_io.post_channel_notice(run, stuck_text(delivery, stuck, report.get("total", 0)))
            else:
                delivery["stuck_count"] = (int(delivery.get("stuck_count") or 0) + 1
                                           if stuck == delivery.get("stuck") else 1)
                delivery["stuck"] = stuck
        if posted is None:
            keep.append(delivery)
            continue
        for sid in ids:
            info = dict((delivery.get("written") or {}).get(sid) or {})
            info["permalink"] = posted.get("permalink", "")
            info["thread_ts"] = posted.get("ts", "")
            state.written[sid] = info
            state.written_articles[sid] = list((delivery.get("written_articles") or {}).get(sid, []))
        sent.extend(ids)
        run.note_status(f"やり直し: 前に作った紙面{len(paths)}件を送り直しました（{len(ids)}件の記事）")
        run.save_point("紙面の送り直し")
    if keep:
        item["deliveries"] = keep
    else:
        item.pop("deliveries", None)
    return sent


def failure_trace(run: Run) -> list[str]:
    """この実行の失敗の手がかり（種類・対象・理由）。原因を調べる材料。値（鍵）は含まない。"""
    out = [f"{f.kind}|{f.target}|{' '.join(str(f.reason).split())[:160]}" for f in run.failures]
    out += [line for line in run.status if "読めませんでした" in line]
    return list(dict.fromkeys(out))


def giveup_text(item: dict) -> str:
    """自動のやり直しを諦めたときの文面。純粋関数。"""
    diag = item.get("diagnosis") or {}
    lines = [f":warning: {item.get('date')} の記事 {len(item.get('story_ids') or [])}件は、"
             f"自動で{item.get('attempts')}回やり直しましたが作れませんでした。"]
    if diag.get("cause"):
        lines.append(f"調べた原因: {diag['cause']}")
        if diag.get("fix_text"):
            lines.append(f"入れた対策: {diag['fix_text']}（それでも通りませんでした）")
        if diag.get("needs_user"):
            lines.append(f"必要な操作: {diag['needs_user']}")
    else:
        reason = REASON_TEXT.get(str(item.get("reason")), REASON_TEXT["other"])
        lines.append(f"原因として多いもの: {reason}")
    lines.append("チェックは付いたままです。原因が解けたら「記事を作成」を押すと作れます。")
    return "\n".join(lines)


def process_redo(run: Run, state: State, *, max_attempts: int | None = None,
                 time_budget_seconds: float | None = None) -> dict[str, Any]:
    """待ち行列を順に作り直して送る。

    項目は処理の間も行列に残し（途中で止まっても失われない）、終わってから
    まだ作成済みでない話題だけを残す。上限回数に達したら、その一覧のスレッドへ
    理由を1通出して外す。
    """
    limit = int(max_attempts or run.config.get("auto_retry.max_attempts", DEFAULT_REDO_MAX))
    after = int(run.config.get("auto_retry.diagnose_after", redo_doctor.DEFAULT_DIAGNOSE_AFTER))
    done: list[str] = []
    given_up: list[str] = []
    for item in list(state.redo):
        ids = [s for s in item.get("story_ids") or [] if not state.is_written(s)]
        if not ids:
            state.redo.remove(item)
            continue
        item["attempts"] = int(item.get("attempts") or 0) + 1
        date_key = str(item.get("date"))

        # 5回を超えても駄目なら、原因を調べて対策を決める（1度だけ。結果は項目に残す）
        if item["attempts"] > after and not item.get("diagnosis"):
            diag = redo_doctor.diagnose(item, run.config)
            item["diagnosis"] = diag.to_dict()
            _thread_notices(run, state, date_key, None)
            slack_io.post_channel_notice(run, redo_doctor.notice_text(item, diag))
            run.note_status(f"やり直し: 原因を調べました（{diag.cause}）")
            if diag.give_up:
                state.redo.remove(item)
                given_up.extend(ids)
                continue
        fixes = redo_doctor.fixes_for(item, run.config, diagnose_after=after)
        # 送れなかっただけの紙面は、書き直さずに送り直す（対策もその間だけ入れる）
        if item.get("deliveries"):
            with redo_doctor.applied(run.config, fixes):
                sent = redeliver(run, state, item, final=item["attempts"] >= limit)
            done.extend(sent)
            ids = [s for s in ids if not state.is_written(s)]
            if sent:
                labels = dict(run.config.get("categories.labels", {}) or {})
                update_lists(run, state, labels=labels, pending=set(ids), message_ts=None,
                             date_str=date_key)
            if not ids:
                state.redo.remove(item)
                continue
            if item.get("deliveries"):
                # 紙面はあるのにまだ送れない。書き直しても同じなので、この回はここまで
                if item["attempts"] >= limit:
                    _thread_notices(run, state, date_key, None)
                    slack_io.post_channel_notice(run, giveup_text(item))
                    state.redo.remove(item)
                    given_up.extend(ids)
                else:
                    item["story_ids"] = ids
                continue
        # 書き直し（AI で作り直す）は重い。同じ項目は1日に決まった回数まで。今月の
        # 利用時間が上限に近いときは書き直さない（送り直しだけにする）
        today = run.date_str
        per_day = int(run.config.get("auto_retry.regenerations_per_day", 3) or 0)
        regen = item.get("regen") or {}
        count = int(regen.get("count") or 0) if regen.get("date") == today else 0
        if per_day and count >= per_day:
            run.note_status(f"やり直し: {date_key} の分は今日{count}回作り直したため、次の日に回します")
            item["attempts"] -= 1
            continue
        if usage_tight(run.config, state, today):
            run.note_status("やり直し: 今月の実行時間が上限に近いため、作り直しは来月に回します")
            item["attempts"] -= 1
            continue
        item["regen"] = {"date": today, "count": count + 1}
        per_run = int(fixes.get("auto_retry.per_run_limit") or 0)
        batch_ids = ids[:per_run] if per_run else ids
        run.save_point("やり直しの開始")
        listed = state.listed_messages.get(date_key, [])
        with redo_doctor.applied(run.config, fixes):
            result = run_articles(run, state, story_ids=batch_ids,
                                  message_ts=listed[-1] if listed else None,
                                  time_budget_seconds=time_budget_seconds)
        done.extend(result["written"])
        if item in state.redo and per_run and len(result["written"]) == len(batch_ids):
            # 少しずつ作る対策の回で、その分は全部通った。失敗回数に数えない
            item["attempts"] -= 1
        if item not in state.redo:
            continue
        left = [s for s in item.get("story_ids") or [] if not state.is_written(s)]
        if not left:
            state.redo.remove(item)
        elif item["attempts"] >= limit:
            item["story_ids"] = left
            _thread_notices(run, state, str(item.get("date")), None)
            slack_io.post_channel_notice(run, giveup_text(item))
            state.redo.remove(item)
            given_up.extend(left)
        else:
            item["story_ids"] = left
    run.flush_notifications()
    return {"written": done, "given_up": given_up, "left": len(state.redo)}


def run_articles(run: Run, state: State, *, story_ids: list[str],
                 message_ts: str | None = None,
                 thread_ts: str | None = None,
                 time_budget_seconds: float | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 force: bool = False) -> dict[str, Any]:
    """記事化を最後まで通す。

    選ばれた全件を `limits.articles_per_request` 件ずつ順に執筆し、最後に
    チャンネルへ**完了通知を1通**（紙面PDFをまとめて添付）出す。途中経過は
    通知しない（本人の要望、2026-09-17）。

    **時間の上限（`time_budget_seconds`）**: 次のまとまりに入る前に経過時間を
    見て、超えていれば残りを次回に回す。1日分（70〜80件＝16まとまり）を
    まとめて選ぶと Actions の 30 分を超え、切られると完了通知も記録も
    残らないまま「生成中」で止まっていた（2026-09-29・30）。切られる前に
    自分で止め、作れた分を送る。
    """
    config = run.config
    labels = dict(config.get("categories.labels", {}) or {})
    limit = max(1, int(config.get("limits.articles_per_request", 5)))
    list_date = _list_date(state, story_ids, fallback=run.date_str)
    articles_channel = str(config.get("slack.articles_channel", "") or "") or None
    _thread_notices(run, state, list_date, message_ts)

    targets, already, _ = select_targets(state, story_ids, limit=limit, force=force)
    if already:
        run.note_status(f"記事化: {len(already)}件は作成済みのため飛ばしました")

    written: list[str] = []
    failed: list[str] = []
    deferred: list[str] = []
    pdfs: list[str] = []
    records: list[write.WriteResult] = []
    degraded = False
    credit = False
    kind = ""
    use_knowledge = knowledge.enabled(config)
    topics = (knowledge.topic_names(knowledge.load_index(run))
              if use_knowledge and targets else [])
    started = clock()
    # 執筆は待ち時間がほとんど（AI の応答待ち）。まとまりを並べて書き、全体の時間を
    # 縮める。GitHub Actions の利用時間は実行時間で数えられるため（2026-10-30）
    parallel = max(1, int(config.get("limits.write_parallel", 2) or 1))
    batches = [targets[i:i + limit] for i in range(0, len(targets), limit)]
    position = 0
    while position < len(batches):
        if (time_budget_seconds is not None and position > 0
                and clock() - started >= time_budget_seconds):
            # 最初のまとまりは必ず回す（何も作らずに終わらない）
            deferred = [sid for batch in batches[position:] for sid in batch]
            failed.extend(deferred)
            minutes = int(time_budget_seconds // 60)
            run.note_status(
                f"記事化: 時間の上限（{minutes}分）に達したため、"
                f"残り{len(deferred)}件を次回に回しました")
            break
        group = batches[position:position + parallel]
        if len(group) == 1:
            results = [_write_batch(run, state, group[0], labels=labels,
                                    index=position + 1, topics=topics)]
        else:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=len(group)) as pool:
                results = list(pool.map(
                    lambda pair: _write_batch(run, state, pair[1], labels=labels,
                                              index=pair[0], topics=topics),
                    [(position + offset + 1, batch) for offset, batch in enumerate(group)]))
        position += len(group)
        for result in results:
            written.extend(result.written)
            records.extend(result.records)
            failed.extend(result.failed)
            degraded = degraded or result.degraded
            kind = result.kind or kind
            if result.pdf:
                pdfs.append(result.pdf)
            if result.kind == write.KIND_CREDIT:
                credit = True
        if credit:
            # 利用枠切れ。残りを回しても同じなので、残り全部を失敗にする
            failed.extend(sid for batch in batches[position:] for sid in batch)
            break

    pages = len(pdfs)
    if targets:
        tag = completion_tag(written or failed)
        label = date_label(list_date)
        auto = auto_retry_enabled(config)
        text = completion_text(len(written), len(failed), degraded=degraded,
                               credit=credit, tag=tag,
                               deferred=len(deferred), label=label, auto_retry=auto)
        if pdfs:
            posted = slack_io.post_completion(
                run, pdfs, text, channel=articles_channel,
                continued=f"📎 {label}の記事  `#{tag}`")
            # 送れなかったら、作った紙面のまま少し待って送り直す（Slack の一時的な
            # 504 など。2026-10-23 に89件分の紙面が届かなかった）
            for wait in ([] if posted is not None else delivery_waits(config)):
                run.note_status(f"記事化: Slackへ送れなかったため{int(wait)}秒後に送り直します")
                run.sleep(wait)
                posted = slack_io.post_completion(
                    run, pdfs, text, channel=articles_channel,
                    continued=f"📎 {label}の記事  `#{tag}`")
                if posted is not None:
                    run.note_status("記事化: 送り直して届きました")
                    break
        else:
            posted = slack_io.post_channel_notice(run, text)
        if posted is None and written:
            # 紙面を送れなかった。作成済みにせず、やり直しの待ち行列へ。**作った紙面は
            # 残し（Actions の成果物）、やり直しでは書き直さずに送り直すだけにする**。
            # 書き直すと1回に1時間かかり、Actions の利用時間を使い切る（2026-10-29）
            run.note_status("記事化: 紙面をSlackへ送れなかったため作成済みにしていません")
            if use_knowledge:
                # 中身はできている。知識は送れたかどうかに関わらず積む
                _save_knowledge(run, state, [r for r in records if r.story_id in written],
                                list_date)
            delivery = {
                "run_id": os.environ.get("GITHUB_RUN_ID", ""),
                "files": [Path(p).name for p in pdfs],
                "text": text, "continued": f"📎 {label}の記事  `#{tag}`",
                "channel": articles_channel or "",
                "written": {sid: dict(state.written[sid]) for sid in written
                            if sid in state.written},
                "written_articles": {sid: list(state.written_articles.get(sid, []))
                                     for sid in written},
            }
            for story_id in written:
                state.written.pop(story_id, None)
                state.written_articles.pop(story_id, None)
            failed.extend(written)
            if auto_retry_enabled(config) and delivery["run_id"] and delivery["files"]:
                enqueue_redo(state, written, list_date=list_date, reason="slack",
                             now=run.now_iso(), errors=failure_trace(run), delivery=delivery)
            written = []
            kind = "slack"
        else:
            permalink = (posted or {}).get("permalink", "")
            for story_id in written:
                state.written[story_id]["permalink"] = permalink
                state.written[story_id]["thread_ts"] = (posted or {}).get("ts", "")
            # 紙面は届いた。ここで死んでも「作成済み」が残るように書いておく
            run.save_point("記事化の完了通知")
            if use_knowledge:
                _save_knowledge(run, state, [r for r in records if r.story_id in written],
                                list_date)

    # 作れなかった・送れなかった分は、自動でやり直す待ち行列へ（本人は押し直さない）
    if failed and auto_retry_enabled(config):
        enqueue_redo(state, failed, list_date=list_date, reason=kind or "other",
                     now=run.now_iso(), errors=failure_trace(run))
        run.note_status(f"記事化: {len(set(failed))}件は自動でやり直します")

    # 一覧の chat.update（AC-28）。作れなかった分はチェックが付いたまま残す。
    update_lists(run, state, labels=labels, pending=set(failed),
                 message_ts=message_ts, date_str=list_date)
    run.flush_notifications()
    return {"written": written, "already": already, "pending": failed,
            "deferred": deferred,
            "pages": pages, "png": pdfs, "failed": bool(failed) and not written,
            "kind": "credit" if credit else kind, "degraded": degraded}


def _save_knowledge(run: Run, state: State, records: list[write.WriteResult],
                    list_date: str) -> None:
    """書いた記事の中身を、主題ごとの知識ノートとして Vault に積む。

    失敗しても記事化は失敗にしない（紙面は届いている）。取得状況に残す。
    """
    entries = [knowledge.entry_from(
        story_id=r.story_id, title=r.title, category=r.category, body_md=r.body_md,
        sources=r.sources, notes=list(state.story_notes.get(r.story_id, [])),
        date_str=list_date) for r in records]
    entries = [e for e in entries if e.facts]
    if not entries:
        return
    try:
        added = knowledge.record(run, entries)
    except Exception as exc:  # noqa: BLE001 - 知識の保存で記事化を落とさない
        run.note_status(f"知識: 保存に失敗しました（{type(exc).__name__}）")
        return
    if added:
        vault.commit(run, f"auto: knowledge {list_date}")


def restore_after_abort(run: Run, state: State, *, story_ids: list[str],
                        message_ts: str | None = None) -> dict[str, Any]:
    """ワークフローが途中で止まった（時間切れ・失敗）あとの後始末。

    `run_articles` が最後まで走れなかったとき、一覧は「⏳ 生成中」のまま
    残り、本人には何も届かない。ボタンを戻し、作れていない分はチェックが
    付いたまま残し、チャンネルへ1文だけ知らせる。**外部へは取りに行かず、
    執筆もしない。**

    作成済みの判定はディスク上の state（途中保存）を使う。完了通知まで
    届いていれば `written` に入っているので、二重には数えない。
    """
    labels = dict(run.config.get("categories.labels", {}) or {})
    list_date = _list_date(state, story_ids, fallback=run.date_str)
    _thread_notices(run, state, list_date, message_ts)
    pending = [sid for sid in story_ids if not state.is_written(sid)]
    update_lists(run, state, labels=labels, pending=set(pending),
                 message_ts=message_ts, date_str=list_date)
    if pending and auto_retry_enabled(run.config):
        enqueue_redo(state, pending, list_date=list_date, reason="aborted",
                     now=run.now_iso(), errors=failure_trace(run))
    if pending:
        slack_io.post_channel_notice(
            run, abort_text(len(pending), tag=completion_tag(pending),
                            auto_retry=auto_retry_enabled(run.config)))
    run.flush_notifications()
    return {"pending": pending}


def _thread_notices(run: Run, state: State, list_date: str,
                    message_ts: str | None) -> None:
    """失敗通知の行き先を、その日の一覧のスレッドにする。"""
    if not run.config.get("slack.errors_in_thread", True):
        return
    listed = state.listed_messages.get(list_date) or []
    run.notice_thread_ts = (listed[0] if listed else message_ts) or None


def listed_on_date(state: State, date_str: str, *,
                   include_written: bool = False) -> list[str]:
    """その日の一覧に出て、まだ記事化していない話題の id。純粋関数。

    `include_written` を立てると作成済みも含める（作り直し用）。

    ボタンが「生成中」のまま消えた一覧（2026-09-29・30）を、Actions の
    手動実行（`all_of_date`）から救うために使う。一覧と同じ並び
    （カテゴリ順 → 新しい順）にする。
    """
    from . import organize
    from .models import CATEGORIES
    listed = [s for s in state.all_stories()
              if s.status == "listed" and s.listed_on == date_str
              and (include_written or not state.is_written(s.id))]
    grouped = organize.group_by_category(listed, list(CATEGORIES))
    return [s.id for items in grouped.values()
            for s in organize.sort_newest_first(items)]


def _list_date(state: State, story_ids: list[str], *, fallback: str) -> str:
    """ボタンが置かれていた一覧日を状態から復元する。"""
    for story_id in story_ids:
        story = state.stories.get(story_id) or {}
        listed_on = story.get("listed_on")
        if listed_on:
            return str(listed_on)[:10]
        written = state.written.get(story_id) or {}
        written_on = written.get("listed_on") or written.get("date")
        if written_on:
            return str(written_on)[:10]
    return fallback


def update_lists(run: Run, state: State, *, labels: dict[str, str],
                 pending: set[str], message_ts: str | None,
                 date_str: str) -> None:
    """記事化済みを固定表示にした一覧へ書き換える。

    一覧が2通に分かれていた場合、ボタンは最後のメッセージにしか無いので、
    押されたときに分かるのは最後の ts だけになる。**朝に保存した全メッセージの
    ts** を使って、1通目も一緒に書き換える。
    """
    config = run.config
    targets = list(state.listed_messages.get(date_str, []))
    if not targets and message_ts:
        targets = [message_ts]
    if not targets:
        return

    shown = rebuild_shown(state, config, date_str=date_str)
    written_links = {sid: info.get("permalink", "")
                     for sid, info in state.written.items()}
    updates = blocks.build_update(
        date_str=date_str, shown=shown, labels=labels,
        status=run.status, written=written_links, pending=pending,
        options_max=int(config.get("slack.checkbox_options_max", 10)),
        blocks_per_message=int(config.get("slack.blocks_per_message", 45)))

    text = f"{date_str} のニュース"
    for index, ts in enumerate(targets):
        if index < len(updates):
            payload = updates[index]
        else:
            # 更新後は通数が減った。残った分は空にせず、その旨を残す。
            payload = blocks.build_exhausted(
                date_str, "この分の話題は記事化済みです。")
        slack_io.update_list(run, ts, payload, text=text)

    # 済みの行が増えてブロック数が上限を超えると、更新後のほうが通数が
    # 多くなることがある。増えた分を送らないと、その中の話題と
    # 「記事を作成」ボタンが黙って消える。新しい通として続けて投稿し、
    # 次回の更新対象に加える。
    if len(updates) > len(targets):
        # 通し番号を続けて付ける（同じ本文だと「投稿済み」と誤認される）
        slack_io.post_list(run, state, updates[len(targets):], text,
                           list_date=date_str, start_index=len(targets),
                           total_count=len(updates))


def rebuild_shown(state: State, config, *, date_str: str
                  ) -> dict[str, list[Story]]:
    """chat.update 用に、その日一覧へ出した話題を組み直す。"""
    from . import organize
    order = list(config.get("categories.order", []) or [])
    listed = [s for s in state.all_stories()
              if s.status == "listed" and s.listed_on == date_str]
    for story_id, info in state.written.items():
        listed_on = info.get("listed_on") or info.get("date")
        if str(listed_on or "")[:10] != date_str:
            continue
        if not any(s.id == story_id for s in listed):
            listed.append(Story(id=story_id, title=info.get("title", story_id),
                                category=info.get("category", "ai"),
                                status="written",
                                last_updated=str(listed_on or "")[:10]))
    # 朝の一覧と同じ並び（カテゴリ内は新しい順）。並びが変わると番号が
    # ずれ、✅ の行と番号リンクが別の話題を指す
    grouped = organize.group_by_category(listed, order)
    return {category: organize.sort_newest_first(items)
            for category, items in grouped.items()}


def main(argv: list[str] | None = None) -> int:
    """Actions のエントリポイント。値は環境変数から受け取る。

    `run:` の中で `${{ }}` を展開せず、シェルへ本文が渡る経路を作らない。
    """
    import os
    from datetime import datetime

    from .clients.factory import build
    from .config import load as load_config
    from .state import load as load_state, save as save_state

    cfg = load_config()
    began = time.monotonic()
    date_str = os.environ.get("TARGET_DATE") or datetime.now().strftime("%Y-%m-%d")
    story_ids = [s for s in (os.environ.get("STORY_IDS") or "").split() if s]
    out_dir = cfg.root / "out"
    state = load_state(cfg.root)
    force = bool(os.environ.get("FORCE"))
    if not story_ids and os.environ.get("ALL_OF_DATE"):
        # 手動実行: その日の一覧に出た未作成の全話題（一覧のボタンが消えたときの救済）。
        # FORCE を立てると作成済みも作り直す（紙面が届かなかった日の再送）
        story_ids = listed_on_date(state, date_str, include_written=force)
        kind = "全話題（作り直し）" if force else "未作成"
        print(f"対象: {date_str} の一覧の{kind} {len(story_ids)}件")

    clients = build(mock=False, out_dir=out_dir,
                    slack_channel=cfg.get("slack.channel"),
                    models=cfg.get("models"))
    run = Run(config=cfg, clients=clients, date_str=date_str, out_dir=out_dir)
    run.checkpoint = lambda: save_state(state, cfg.root, sections=("written", "outbox"))

    if os.environ.get("RESTORE_ONLY"):
        # 途中で止まった後始末だけ（articles.yml の failure/cancelled ステップ）
        result = restore_after_abort(
            run, state, story_ids=story_ids,
            message_ts=os.environ.get("MESSAGE_TS") or None)
        save_state(state, cfg.root, sections=("written", "outbox"))
        print(f"後始末: 未作成 {len(result['pending'])}件のチェックを戻しました")
        return 0

    budget_minutes = cfg.get("limits.articles_time_budget_minutes")
    budget = float(budget_minutes) * 60 if budget_minutes else None
    if os.environ.get("RETRY_QUEUE"):
        # 自動のやり直し（朝の処理のあと・12時・20時）。待ち行列だけを処理する
        result = process_redo(run, state, time_budget_seconds=budget)
        run.record_usage(state, setup_minutes(cfg) + (time.monotonic() - began) / 60)
        run.flush_notifications()
        save_state(state, cfg.root, sections=("written", "outbox"))
        print(f"やり直し: 作成 {len(result['written'])}件 / 諦め {len(result['given_up'])}件"
              f" / 残り {result['left']}項目")
        return 0
    result = run_articles(
        run, state, story_ids=story_ids,
        message_ts=os.environ.get("MESSAGE_TS") or None,
        thread_ts=os.environ.get("THREAD_TS") or None,
        time_budget_seconds=budget, force=force)
    run.record_usage(state, setup_minutes(cfg) + (time.monotonic() - began) / 60)
    run.flush_notifications()
    save_state(state, cfg.root, sections=("written", "outbox"))

    print(f"記事化 {len(result['written'])}件 / 作成済み {len(result['already'])}件 "
          f"/ 保留 {len(result['pending'])}件（うち次回へ {len(result['deferred'])}件）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
