"""Slack。投稿・スレッド返信・画像・chat.update・失敗通知。

必要な権限は `chat:write` と `files:write`。落ちても止めず、送れなかった分は
`state.json` の `pending` へ積んで次回まとめて送る（呼び出し側の `slack.py`）。
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
import urllib.request
import uuid
from pathlib import Path

from . import http

API = "https://slack.com/api/{method}"

_SHORTCODE = re.compile(r":[a-z0-9_+\-]+:")


def text_key(text: str) -> str:
    """本文を比べるための形。絵文字と空白の違いを無視する。純粋関数。

    Slack は投稿された絵文字（✅ や 📎）を `:white_check_mark:` のような
    名前に直して返す。そのまま比べると**自分の投稿を見つけられず**、
    「投稿済みか」の確認が常に外れていた（2026-10-01 に同じ完了通知が2通出た）。
    絵文字そのものと `:名前:` の両方を落としてから比べる。
    """
    text = _SHORTCODE.sub("", text or "")
    text = "".join(ch for ch in text
                   if unicodedata.category(ch) not in ("So", "Cf", "Mn"))
    return " ".join(text.split())


class SlackError(RuntimeError):
    """Slack は失敗でも HTTP 200 を返す。`ok: false` をここで例外に変える。"""


class RealSlackClient:
    """`SlackClient`。"""

    def __init__(self, channel: str | None = None) -> None:
        self._channel = channel

    # -- 下回り -----------------------------------------------------------

    @property
    def token(self) -> str:
        token = os.environ.get("SLACK_BOT_TOKEN")
        if not token:
            raise RuntimeError("SLACK_BOT_TOKEN が設定されていません")
        return token

    @property
    def channel(self) -> str:
        channel = self._channel or os.environ.get("SLACK_CHANNEL")
        if not channel:
            raise RuntimeError("投稿先のチャンネルが設定されていません")
        return channel

    def _call(self, method: str, payload: dict, *, timeout: float = 30.0,
              form: bool = False) -> dict:
        """Slack API を1回呼ぶ。

        ほとんどのメソッドは JSON を受け取るが、`files.getUploadURLExternal`
        は **フォーム形式しか受け取らない**（JSON で送ると「filename と
        length が無い」と言われる）。`form=True` で切り替える。
        """
        headers = {"Authorization": f"Bearer {self.token}"}
        if form:
            result = http.post_form(API.format(method=method),
                                    {k: str(v) for k, v in payload.items()},
                                    headers=headers, timeout=timeout)
        else:
            result = http.post_json(API.format(method=method), payload,
                                    headers=headers, timeout=timeout)
        if not (result or {}).get("ok"):
            error = (result or {}).get("error", "unknown")
            # invalid_blocks などは response_metadata.messages に「どのブロックの
            # どの項目が」まで入る。無いと直しようがない。
            detail = ((result or {}).get("response_metadata") or {}).get("messages") or []
            if detail:
                error += " " + " | ".join(str(m) for m in detail)[:300]
            raise SlackError(f"Slack {method}: {error}")
        return result

    def _permalink(self, ts: str, channel: str | None = None) -> str:
        try:
            # GET 系のメソッドで JSON を受け取らない。フォーム形式で送る
            result = self._call("chat.getPermalink",
                                {"channel": channel or self.channel, "message_ts": ts},
                                form=True)
            return str(result.get("permalink") or "")
        except Exception:  # noqa: BLE001 - 取れなくても投稿は成功している
            return ""

    # -- 投稿 -------------------------------------------------------------

    def post_message(self, blocks: list[dict], text: str) -> dict:
        result = self._call("chat.postMessage", {
            "channel": self.channel, "text": text, "blocks": blocks,
            "unfurl_links": False, "unfurl_media": False,
        })
        ts = str(result.get("ts") or "")
        return {"ok": True, "ts": ts, "channel": self.channel,
                "permalink": self._permalink(ts)}

    def reply_in_thread(self, thread_ts: str, blocks: list[dict],
                        text: str) -> dict:
        payload = {"channel": self.channel, "text": text, "blocks": blocks,
                   "unfurl_links": False, "unfurl_media": False}
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = self._call("chat.postMessage", payload)
        ts = str(result.get("ts") or "")
        return {"ok": True, "ts": ts, "channel": self.channel,
                "permalink": self._permalink(ts)}

    def update_message(self, ts: str, blocks: list[dict], text: str) -> dict:
        self._call("chat.update", {"channel": self.channel, "ts": ts,
                                   "text": text, "blocks": blocks})
        return {"ok": True, "ts": ts, "channel": self.channel}

    def find_message(self, text: str, *, within_hours: float = 36.0,
                     channel: str | None = None) -> dict | None:
        """直近の履歴から、同じ本文の投稿を探す（channels:history / groups:history）。

        `channel` を省くと一覧のチャンネル。記事用のチャンネルを分けたときは
        そちらを渡す。

        本文は Slack が返す `text`（ブロック付き投稿では投稿時の fallback text、
        ファイル付き投稿では initial_comment）と比べる。権限が無ければ
        `missing_scope` の例外になり、呼び出し側は「無いもの」として扱う。
        """
        import time as _time
        target = channel or self.channel
        oldest = f"{_time.time() - within_hours * 3600:.6f}"
        cursor = ""
        for _ in range(5):                      # 200件 × 5 まで
            payload = {"channel": target, "oldest": oldest, "limit": "200"}
            if cursor:
                payload["cursor"] = cursor
            result = self._call("conversations.history", payload, form=True)
            for message in result.get("messages") or []:
                body = str(message.get("text") or "")
                if text_key(body) == text_key(text):
                    ts = str(message.get("ts") or "")
                    return {"ts": ts, "permalink": self._permalink(ts, target)}
            cursor = ((result.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                break
        return None

    def notify_failure(self, report: str) -> dict:
        return self.post_message(
            [{"type": "section", "text": {"type": "mrkdwn", "text": report}}],
            report[:150])

    # -- 画像 -------------------------------------------------------------

    def upload_image(self, thread_ts: str, path: str, title: str) -> dict:
        """1ファイルをスレッドへ。`upload_files` の薄い包み。"""
        return self.upload_files([path], "", thread_ts=thread_ts)

    #: 共有の確認で待つ秒数（合計 約45秒）。共有は非同期で、すぐには現れない。
    #: 18秒では、続けて送った3通目が現れないうちに失敗にしていた（2026-10-31）
    SHARE_WAITS = (2.0, 3.0, 5.0, 8.0, 12.0, 15.0)

    def _confirm_shared(self, text: str, channel: str) -> dict | None:
        """ファイル付きの投稿がチャンネルに現れたかを履歴で確かめる。

        `files.completeUploadExternal` は **共有に失敗しても ok を返す**。
        1通に14個以上の PDF を付けた回は、ファイルは上がったのに投稿が
        チャンネルに現れず、紙面が誰にも見えないまま「作成済み」になっていた
        （2026-09-26〜10-02 に5回）。現れたことを確かめてから成功にする。
        """
        import time as _time
        for wait in self.SHARE_WAITS:
            _time.sleep(wait)
            found = self.find_message(text, within_hours=1.0, channel=channel)
            if found:
                return found
        return None

    def upload_files(self, paths: list[str], text: str,
                     thread_ts: str = "", channel: str | None = None) -> dict:
        """複数ファイルを**1通**で投げる。`text` はその通の本文。

        Slackの外部アップロードは3手順になる。URLをもらう → そこへPUTする →
        完了を伝える。`files.upload` は廃止されているため。完了の1回で複数
        ファイルをまとめられるので、記事化の完了通知に紙面をまとめて添える。

        **1通に付けるのは10個まで**（呼び出し側の `slack.post_completion` が
        分ける）。それより多いと共有されないことがある。`channel` を渡すと
        一覧とは別のチャンネルへ送る（記事だけを集める場所）。
        """
        target = channel or self.channel
        entries: list[dict[str, str]] = []
        for path in paths:
            data = Path(path).read_bytes()
            name = Path(path).name
            ticket = self._call("files.getUploadURLExternal",
                                {"filename": name, "length": len(data)}, form=True)
            upload_url = str(ticket.get("upload_url") or "")
            file_id = str(ticket.get("file_id") or "")
            if not upload_url or not file_id:
                raise SlackError("Slack files.getUploadURLExternal: URLが返らない")
            request = urllib.request.Request(
                upload_url, data=data, method="POST",
                headers={"Content-Type": "application/octet-stream",
                         "User-Agent": http.USER_AGENT})
            with urllib.request.urlopen(request, timeout=120.0) as response:
                status = getattr(response, "status", 200)
                body = response.read(1024).decode("utf-8", "replace")
            # アップロード先は失敗しても例外にならないことがある。届かなかったファイルの
            # id で完了を呼ぶと file_not_found になる（2026-10-29 に87件分が届かなかった）。
            # ここで確かめ、どのファイルで止まったかを分かるようにする
            if status != 200 or (body and not body.startswith("OK")):
                raise SlackError(f"Slack のファイル受け取りに失敗（{name}: HTTP {status} {body[:60]}）")
            entries.append({"id": file_id})

        payload: dict = {"files": json.dumps(entries), "channel_id": target}
        if text:
            payload["initial_comment"] = text
        if thread_ts:
            payload["thread_ts"] = thread_ts
        result = self._complete_upload(payload, entries)

        files = result.get("files") or [{}]
        file_permalink = str(files[0].get("permalink") or "")
        first_id = entries[0]["id"] if entries else ""

        if text and not thread_ts:
            # チャンネルへの投稿は、現れたことを確かめる。確かめられなければ
            # 失敗にして、呼び出し側の再試行と「作成済みにしない」へ乗せる
            shared = self._confirm_shared(text, target)
            if shared is None:
                raise SlackError(
                    "Slack files.completeUploadExternal: not_shared（ファイルは上がったが"
                    "チャンネルに投稿が現れない。bot が未参加か、共有に失敗）")
            return {"ok": True, "ts": shared["ts"], "file_id": first_id,
                    "file_ids": [e["id"] for e in entries], "channel": target,
                    "permalink": shared["permalink"] or file_permalink}
        # 一覧の 📰 から飛ぶ先は「ファイルが共有されたメッセージ」にしたい。
        # 共有先メッセージの ts は files.info の shares に入る。取れなければ
        # ファイル自体の permalink（ファイル表示）で代える。
        return {"ok": True, "ts": thread_ts, "file_id": first_id,
                "file_ids": [e["id"] for e in entries],
                "permalink": (self._shared_message_permalink(first_id) if first_id else "")
                or file_permalink}

    #: file_not_found のとき、形式を変えて送り直すまでに待つ秒数
    COMPLETE_RETRY_WAIT = 3.0

    def _complete_upload(self, payload: dict, entries: list[dict]) -> dict:
        """`files.completeUploadExternal` を呼ぶ。

        **フォーム形式で送る**（`files` は JSON 文字列。公式の書き方）。JSON の本文に
        `files` を文字列で入れて送っていたところ、2026-10-29 から毎回 file_not_found に
        なり、10/29 の紙面が3日届かなかった（アップロード自体は OK を返していた）。
        それでも file_not_found なら、少し待って `files` を配列にした JSON で1回だけ送り直す。
        """
        try:
            return self._call("files.completeUploadExternal", payload, form=True)
        except SlackError as exc:
            if "file_not_found" not in str(exc):
                raise
        import time as _time
        _time.sleep(self.COMPLETE_RETRY_WAIT)
        return self._call("files.completeUploadExternal", {**payload, "files": entries})

    def _shared_message_permalink(self, file_id: str) -> str:
        try:
            info = self._call("files.info", {"file": file_id}, form=True)
            shares = (info.get("file") or {}).get("shares") or {}
            for scope in ("public", "private"):
                for channel, entries in (shares.get(scope) or {}).items():
                    if channel != self.channel or not entries:
                        continue
                    ts = str(entries[0].get("ts") or "")
                    if ts:
                        return self._permalink(ts)
        except Exception:  # noqa: BLE001 - 取れなくても投稿は成功している
            pass
        return ""

    # -- 受付の返信 -------------------------------------------------------

    def post_ephemeral(self, user: str, text: str) -> dict:
        self._call("chat.postEphemeral",
                   {"channel": self.channel, "user": user, "text": text})
        return {"ok": True}


def new_client_id() -> str:
    """再送のときに同じ投稿を二重に出さないための識別子。"""
    return uuid.uuid4().hex
