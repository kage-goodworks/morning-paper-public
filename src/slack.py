"""Slackへの投稿（副作用あり）。

失敗しても止めない。投げられなかった分は `state.json` の `pending` へ積み、
次回実行でまとめて送る（Spec「外部連携」）。
"""

from __future__ import annotations

from typing import Any

from .pipeline import Run
from .state import State


def _find(run: Run, text: str, channel: str | None):
    """`find_message` を呼ぶ。`channel` は渡されたときだけ付ける。"""
    finder = getattr(run.clients.slack, "find_message", None)
    if finder is None:
        return None
    return finder(text, channel=channel) if channel else finder(text)


def already_posted(run: Run, text: str, channel: str | None = None) -> dict | None:
    """同じ本文の投稿が直近にあるか。問い合わせが失敗したら「無い」とみなす。

    投稿の直後に応答が届かなかった（timeout）／プロセスが死んだ、のあとの
    再実行で二重に投稿しないための確認。履歴を読む権限が無い場合も None。
    """
    if getattr(run.clients.slack, "find_message", None) is None:
        return None
    return run.guarded(lambda: _find(run, text, channel),
                       target="Slack（投稿済みの確認）",
                       impact="二重投稿の確認ができません", default=None, status="")


def idempotent(run: Run, text: str, do_post, channel: str | None = None):
    """`do_post` を「二度送らない」形に包む。

    1回目は普通に送る。失敗して再試行に入るときは、送る前に履歴を見て
    同じ本文があれば**それを成功として返す**。応答だけが落ちた投稿を
    再試行で二重に送らないため。`guarded` の再試行と組み合わせて使う。
    """
    finder = getattr(run.clients.slack, "find_message", None)
    attempted = {"n": 0}

    def fn():
        if attempted["n"] and finder is not None:
            try:
                existing = _find(run, text, channel)
            except Exception:  # noqa: BLE001 - 確認できなければ普通に送る
                existing = None
            if existing:
                run.note_status("Slack: 前回の投稿は届いていたため送り直していません")
                return {"ok": True, **existing}
        attempted["n"] += 1
        return do_post()

    return fn


def list_text(date_str: str, index: int, total: int) -> str:
    """一覧の fallback text。通ごとに違う文字列にして履歴で区別できるように。"""
    base = f"{date_str} のニュース"
    return base if total <= 1 else f"{base} ({index + 1}/{total})"


def post_list(run: Run, state: State, messages: list[list[dict[str, Any]]],
              text: str, *, list_date: str | None = None,
              start_index: int = 0, total_count: int | None = None) -> list[dict]:
    """一覧を投稿する。2通目以降も同じ経路で送る。

    通ごとに、同じ本文がすでにチャンネルにあれば投稿せず、その ts を記録する
    （前回の投稿の応答だけが落ちた場合の二重投稿を防ぐ）。
    """
    posted: list[dict] = []
    total = total_count if total_count is not None else start_index + len(messages)
    for offset, blocks in enumerate(messages):
        index = start_index + offset
        this_text = list_text(list_date, index, total) if list_date else text
        existing = already_posted(run, this_text)
        if existing:
            run.note_status("Slack: 同じ一覧がすでに投稿されていたため送り直していません")
            posted.append({"ok": True, **existing})
            _remember_list_message(state, list_date, existing)
            continue
        result = run.guarded(
            idempotent(run, this_text,
                       lambda blocks=blocks, t=this_text: run.clients.slack.post_message(blocks, t)),
            target="Slack（一覧の投稿）",
            impact="一覧が届きません。次回実行でまとめて送ります",
            default=None,
            status="Slack: 投稿に失敗したため次回へ持ち越します")
        if result is None:
            # 分割一覧は順序が重要。途中が失敗したら後続も送らず、残りを
            # まとめて保留する。成功した接頭辞 + 後日の保留分、の順を守る。
            for later, pending_blocks in enumerate(messages[offset:]):
                item = {"kind": "post",
                        "text": list_text(list_date, index + later, total) if list_date else text,
                        "blocks": pending_blocks}
                if list_date:
                    item["list_date"] = list_date
                state.pending.append(item)
            break
        posted.append(result)
        _remember_list_message(state, list_date, result)
    return posted


def _remember_list_message(state: State, list_date: str | None,
                           result: dict) -> None:
    """朝の一覧の ts を、即時投稿と保留再送の両方で記録する。"""
    ts = result.get("ts")
    if not list_date or not ts:
        return
    messages = state.listed_messages.setdefault(list_date, [])
    if ts not in messages:
        messages.append(ts)


def flush_pending(run: Run, state: State) -> int:
    """前回送れなかった分をまとめて送る。"""
    if not state.pending:
        return 0
    sent = 0
    remaining: list[dict] = []
    for index, item in enumerate(state.pending):
        existing = already_posted(run, item.get("text") or "")
        if existing:
            # 前回、投稿は届いていたのに応答だけ落ちていた。送り直さない
            result = {"ok": True, **existing}
        else:
            result = run.guarded(
                idempotent(run, item.get("text") or "",
                           lambda item=item: run.clients.slack.post_message(
                               item.get("blocks") or [], item.get("text") or "")),
                target="Slack（保留分の投稿）",
                impact="保留分の送信を次回へ持ち越します",
                default=None, status=None)
        if result is None:
            # ここでも追い越しを許さず、元のメッセージ順を保つ。
            remaining.extend(state.pending[index:])
            break
        else:
            sent += 1
            _remember_list_message(state, item.get("list_date"), result)
    state.pending = remaining
    if sent:
        run.note_status(f"Slack: 保留していた{sent}件を送信しました")
    return sent


def post_paper(run: Run, thread_ts: str, png_paths: list[str],
               title: str) -> list[dict]:
    """紙面PNGをスレッドへ投稿する。"""
    out: list[dict] = []
    for path in png_paths:
        result = run.guarded(
            lambda path=path: run.clients.slack.upload_image(
                thread_ts, path, title),
            target="Slack（紙面の投稿）",
            impact="紙面がスレッドに出ません",
            default=None)
        if result is not None:
            out.append(result)
    return out


def post_sources(run: Run, thread_ts: str, entries: list[tuple[str, list[str]]]
                 ) -> dict | None:
    """記事の元ページURLをテキストで出す。画像内のURLは押せないため（FR-5）。"""
    lines: list[str] = []
    for title, urls in entries:
        lines.append(f"*{title}*")
        lines.extend(f"• {u}" for u in urls)
    text = "\n".join(lines) or "出典はありません"
    return run.guarded(
        lambda: run.clients.slack.reply_in_thread(
            thread_ts, [{"type": "section",
                         "text": {"type": "mrkdwn", "text": text}}], text),
        target="Slack（出典の投稿）",
        impact="元ページのURLがスレッドに出ません",
        default=None)


def update_list(run: Run, message_ts: str, blocks: list[dict[str, Any]],
                text: str) -> dict | None:
    """一覧メッセージを書き換える（FR-5「記事化済みの固定表示」）。"""
    return run.guarded(
        lambda: run.clients.slack.update_message(message_ts, blocks, text),
        target="Slack（一覧の更新）",
        impact="記事化済みの固定表示が反映されません",
        default=None)


#: 1通に付ける紙面PDFの数。Slack の画面からは1通10個まで。API は数を
#: 断らないが、14個以上を付けた回は**投稿が現れなかった**（ok は返る）。
FILES_PER_MESSAGE = 10


def post_completion(run: Run, paths: list[str], text: str, *,
                    channel: str | None = None,
                    continued: str = "") -> dict | None:
    """記事化の完了通知。紙面PDFを添えて送る。

    一覧のスレッドには入れない。スレッドを開くたびに長い一覧を
    スクロールするのが手間なため（本人の要望、2026-09-17）。
    `channel` があればそこへ送る（記事だけを集めるチャンネル。本人の要望、
    2026-10-02）。無ければ一覧と同じチャンネル。

    PDF は1通 `FILES_PER_MESSAGE` 個まで。超える分は続きの通に分ける
    （本文は `continued` に「（続き 2/3）」を足したもの）。1通でも送れ
    なければ None を返し、呼び出し側は作成済みにしない。

    同じ本文の通知がすでにあれば送らない（応答が落ちた後の再実行で二重に
    ならないように。本文には話題の識別子が入っているので同じ内容にだけ一致する）。
    戻り値は最初の通。
    """
    per = max(1, int(run.config.get("delivery.files_per_message", FILES_PER_MESSAGE)
                      or FILES_PER_MESSAGE))
    chunks = [paths[i:i + per] for i in range(0, len(paths), per)] or [list(paths)]
    head = continued or ("📎 " + (text.splitlines()[0] if text else "").lstrip("✅ "))
    first: dict | None = None
    for index, chunk in enumerate(chunks):
        this_text = text if index == 0 else f"{head}（続き {index + 1}/{len(chunks)}）"
        existing = already_posted(run, this_text, channel)
        if existing:
            run.note_status("Slack: 同じ完了通知がすでに投稿されていたため送り直していません")
            result: dict | None = {"ok": True, **existing}
        else:
            result = run.guarded(
                idempotent(run, this_text,
                           lambda chunk=chunk, t=this_text:
                           run.clients.slack.upload_files(chunk, t, channel=channel),
                           channel),
                target="Slack（記事化の完了通知）",
                impact="紙面が届きません",
                default=None)
        if result is None:
            return None
        first = first or result
    return first


def post_channel_notice(run: Run, text: str) -> dict | None:
    """1文の通知をチャンネルへ出す（記事を作れなかった連絡）。"""
    return run.guarded(
        idempotent(run, text, lambda: run.clients.slack.post_message(
            [{"type": "section", "text": {"type": "mrkdwn", "text": text}}], text)),
        target="Slack（通知の投稿）",
        impact="通知が出ません",
        default=None, status=None)


def post_notice(run: Run, thread_ts: str, text: str) -> dict | None:
    """1文の通知をスレッドへ出す。降格の明示や、記事を作れなかった連絡に使う。"""
    return run.guarded(
        lambda: run.clients.slack.reply_in_thread(
            thread_ts, [{"type": "section",
                         "text": {"type": "mrkdwn", "text": text}}], text),
        target="Slack（通知の投稿）",
        impact="スレッドに通知が出ません",
        default=None, status=None)


def post_ephemeral(run: Run, count: int) -> dict | None:
    """「n件を記事にしています」。3秒以内に返す想定（Workers側が担当）。"""
    text = f"{count}件を記事にしています"
    return run.guarded(
        lambda: run.clients.slack.reply_in_thread(
            "", [{"type": "section",
                  "text": {"type": "mrkdwn", "text": text}}], text),
        target="Slack（受付の返信）",
        impact="受付の返信が出ません",
        default=None, status=None)
