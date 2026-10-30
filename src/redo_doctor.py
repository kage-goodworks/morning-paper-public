"""やり直しが続けて失敗したときの「原因を調べて、直してから再挑戦」（2026-10-24）。

本人の要望: ただ同じことを繰り返すのではなく、5回を超えても駄目なら原因を探し、
直せるものは直してからやり直す。

流れ（`articles.process_redo` から呼ぶ）:

1. 1〜5回目は、そのままやり直す（多くは一時的な障害で、時間をおけば通る）
2. 6回目の前に `diagnose` で原因を判定する。材料は、待ち行列の項目に残した
   直近の失敗（対象・種類・理由）と、失敗の分類（slack / credit / render …）
3. 原因ごとの対策（設定の一時的な上書き）を入れて再挑戦する。調べた原因・入れた
   対策・本人にしか直せないことを、その一覧のスレッドへ1通で知らせる
4. 対策を入れても2回続けて駄目なら、さらに強い対策（1本ずつ・少量ずつ・画像なし）
   に上げる
5. 上限に達したら、判定した原因を添えて知らせて外す

対策は**その実行の間だけ**設定を上書きする（`config.yml` は書き換えない）。
判定は純粋関数。Slack へ問い合わせたりはせず、記録した失敗から決める。
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Iterator

#: そのままやり直す回数。これを超えたら原因を調べる
DEFAULT_DIAGNOSE_AFTER = 5
#: 対策を入れてから、さらに強い対策へ上げるまでの失敗回数
ESCALATE_AFTER = 2

#: 強い対策。どの原因でも、最後はこれで小さく確実に通す
STRONG_FIXES: dict[str, Any] = {
    "limits.articles_per_request": 1,
    "delivery.files_per_message": 1,
    "render.no_images": True,
    "auto_retry.per_run_limit": 5,
}


@dataclass
class Diagnosis:
    cause: str                       # 本人向けの一文
    fixes: dict[str, Any] = field(default_factory=dict)
    fix_text: str = ""               # 入れた対策の一文（無ければ空）
    needs_user: str = ""             # 本人にしか直せないこと（無ければ空）
    give_up: bool = False            # 直しようがない（やり直しても無駄）

    def to_dict(self) -> dict[str, Any]:
        return {"cause": self.cause, "fixes": dict(self.fixes), "fix_text": self.fix_text,
                "needs_user": self.needs_user, "give_up": self.give_up}


def _has(errors: list[str], *words: str) -> bool:
    text = "\n".join(errors).lower()
    return any(w.lower() in text for w in words)


def _swap_models(config) -> dict[str, Any]:
    primary = str(config.get("write.model", "claude"))
    fallback = str(config.get("write.fallback_model", "gemini"))
    return {"write.model": fallback, "write.fallback_model": primary}


def diagnose(item: dict, config) -> Diagnosis:
    """待ち行列の1項目について、続けて失敗している原因と対策を決める。純粋関数。

    上から順に当てはめる。より確かな手がかり（認証・チャンネル）を先に見る。
    """
    reason = str(item.get("reason") or "other")
    errors = [str(e) for e in item.get("errors") or []]

    # --- 直しようがないもの ------------------------------------------------
    if _has(errors, "記事ノートを読めませんでした"):
        return Diagnosis(
            cause="元記事のノートが Vault に無い（朝の保存に失敗したまま期限が過ぎた）",
            needs_user="元記事が無いため、この話題は作れません",
            give_up=True)

    # --- Slack ---------------------------------------------------------------
    if _has(errors, "invalid_auth", "token_revoked", "not_authed", "account_inactive"):
        return Diagnosis(
            cause="Slack の bot の鍵（SLACK_BOT_TOKEN）が無効になっている",
            needs_user=("Slack アプリを再インストールし、GitHub の Secrets の SLACK_BOT_TOKEN を"
                        "新しい鍵に差し替えてください。差し替えた後は自動でやり直します"))
    if _has(errors, "not_in_channel", "channel_not_found", "is_archived"):
        return Diagnosis(
            cause="記事用のチャンネルに bot が入っていない（または消えた）",
            fixes={"slack.articles_channel": ""},
            fix_text="記事を一覧と同じチャンネルへ送ります",
            needs_user="記事用のチャンネルで `/invite @news` を実行すると、次から元の場所へ戻ります")
    if reason == "slack" and _has(errors, "file_not_found", "ファイル受け取りに失敗"):
        return Diagnosis(
            cause="Slack がファイルを受け取れていない（アップロードの途中で失敗している）",
            fixes={"delivery.files_per_message": 1, "render.no_images": True},
            fix_text="紙面から画像を外して軽くし、1通に1ファイルずつ送ります")
    if reason == "slack":
        return Diagnosis(
            cause="紙面のファイルが大きく、Slack への送信が時間切れになっている（504 など）",
            fixes={"delivery.files_per_message": 1, "render.no_images": True},
            fix_text="紙面から画像を外して軽くし、1通に1ファイルずつ送ります")

    # --- AI ------------------------------------------------------------------
    if reason == "credit" or _has(errors, "credit", "quota", "利用枠"):
        return Diagnosis(
            cause="Claude の利用枠が尽きている",
            fixes=_swap_models(config),
            fix_text="Gemini で書きます",
            needs_user="利用枠が補充されれば、以降は自動で Claude に戻ります")
    if _has(errors, "auth|claude", "oauth"):
        return Diagnosis(
            cause="Claude の鍵（CLAUDE_CODE_OAUTH_TOKEN）が切れている可能性",
            fixes=_swap_models(config),
            fix_text="Gemini で書きます",
            needs_user=("`claude setup-token` で新しい鍵を作り、GitHub の Secrets の "
                        "CLAUDE_CODE_OAUTH_TOKEN を差し替えてください"))

    # --- 紙面 ----------------------------------------------------------------
    if reason == "render":
        return Diagnosis(
            cause="紙面の生成（Chromium）が失敗している。元記事の画像が読めないことが多い",
            fixes={"render.no_images": True},
            fix_text="画像を外して紙面を作ります")

    # --- 時間切れ・中断 ------------------------------------------------------
    if reason == "aborted":
        return Diagnosis(
            cause="記事化の実行が時間内に終わらず止まっている",
            fixes={"auto_retry.per_run_limit": 15, "limits.articles_per_request": 3},
            fix_text="1回に作る本数を15本までに絞り、残りは次の回に回します")

    # --- それ以外（AI が応答しない・出力が崩れる） ---------------------------
    if _has(errors, "出力形式が不正", "invalid", "記事数が一致しない"):
        cause = "AI の出力が崩れている（まとめて書かせる本数が多い）"
    elif _has(errors, "503", "429", "timeout", "time out", "overloaded", "rate"):
        cause = "AI が混雑していて応答しない"
    else:
        cause = "AI が応答しない（原因の手がかりは記録に残っていない）"
    return Diagnosis(
        cause=cause,
        fixes={"limits.articles_per_request": 2, **_swap_models(config)},
        fix_text="1回に書かせる本数を2本に減らし、使う AI の順番を入れ替えます")


def fixes_for(item: dict, config, *, diagnose_after: int = DEFAULT_DIAGNOSE_AFTER
              ) -> dict[str, Any]:
    """この回に入れる対策。回数に応じて段階を上げる。純粋関数。

    - `diagnose_after` 回まで: 対策なし
    - それ以降: 判定した原因の対策
    - さらに `ESCALATE_AFTER` 回失敗したら: 強い対策を重ねる
    """
    attempts = int(item.get("attempts") or 0)
    if attempts <= diagnose_after:
        return {}
    found = dict((item.get("diagnosis") or {}).get("fixes") or {})
    if attempts > diagnose_after + ESCALATE_AFTER:
        found.update({k: v for k, v in STRONG_FIXES.items()
                      if k not in found or k == "limits.articles_per_request"})
    return found


@contextmanager
def applied(config, fixes: dict[str, Any]) -> Iterator[None]:
    """設定をその実行の間だけ上書きする。`a.b` の形のキーを受ける。"""
    data = config.data
    saved: list[tuple[dict, str, Any, bool]] = []
    try:
        for dotted, value in fixes.items():
            parts = dotted.split(".")
            node = data
            for part in parts[:-1]:
                node = node.setdefault(part, {})
            last = parts[-1]
            saved.append((node, last, node.get(last), last in node))
            node[last] = value
        yield
    finally:
        for node, last, old, existed in reversed(saved):
            if existed:
                node[last] = old
            else:
                node.pop(last, None)


def notice_text(item: dict, diag: Diagnosis) -> str:
    """原因を調べた結果の知らせ。純粋関数。"""
    lines = [f":mag: {item.get('date')} の記事 {len(item.get('story_ids') or [])}件は、"
             f"{item.get('attempts', 0) - 1}回やり直しても作れなかったため原因を調べました",
             f"原因: {diag.cause}"]
    if diag.give_up:
        lines.append(f"対応: {diag.needs_user}。この話題のやり直しはやめます")
        return "\n".join(lines)
    if diag.fix_text:
        lines.append(f"対策: {diag.fix_text}。このあと自動で再挑戦します")
    if diag.needs_user:
        lines.append(f"お願い: {diag.needs_user}")
    return "\n".join(lines)
