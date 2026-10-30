"""実行の文脈。

`取得状況` に載せる行と、送るべき失敗通知をここへ集める。原則は縮退で、
フロー全体が止まる条件は設けない（基本方針）。
"""

from __future__ import annotations

import os
import re
import sys
import traceback

from dataclasses import dataclass, field
from pathlib import Path
from time import sleep as real_sleep
from typing import Any, Callable

from .clients.base import Clients
from .config import Config
from .models import Failure
from .retry import call


#: 公開リポジトリの Actions で立てる。実行ログは誰でも読めるため、失敗理由から
#: AI の出力の断片などの中身を落とす（詳しい理由は非公開の Slack スレッドへ出る）
PUBLIC_LOG = bool(os.environ.get("MORNING_PAPER_PUBLIC_LOG"))


def log_reason(reason: str, *, public: bool | None = None) -> str:
    """実行ログに書く失敗理由。公開ログでは見出しだけにする。純粋関数。

    `claude -p の出力がJSONではない: <出力の末尾>` → `claude -p の出力がJSONではない`
    """
    if not (PUBLIC_LOG if public is None else public):
        return reason
    head = re.split(r"[:：（(]", reason, maxsplit=1)[0].strip()
    return head[:60] or "（理由は Slack のスレッドを参照）"


def _public_excepthook(exc_type, exc, tb) -> None:
    """落ちたときも、例外の中身（応答の断片など）は出さず場所だけ出す。"""
    frames = traceback.extract_tb(tb)
    print("Traceback（公開ログのため例外の中身は省略）:", file=sys.stderr)
    for frame in frames[-6:]:
        print(f"  {frame.filename}:{frame.lineno} in {frame.name}", file=sys.stderr)
    print(f"{exc_type.__name__}", file=sys.stderr, flush=True)


if PUBLIC_LOG:
    sys.excepthook = _public_excepthook


@dataclass
class Run:
    config: Config
    clients: Clients
    date_str: str
    out_dir: Path
    #: リポジトリ側の可変データ（`state.json`、`discovered_sources.yml`）の置き場所。
    #: **モック実行ではリポジトリを書き換えない。**`out/` を指す。
    #: 本番ではリポジトリ直下を指し、Actions がcommitして持ち回す。
    data_dir: Path | None = None
    status: list[str] = field(default_factory=list)
    #: 今回取得できなかったソースのid（collect が入れる）
    failed_sources: list[str] = field(default_factory=list)
    #: 外への書き込み（Slack投稿・Vault保存）が成功した直後に state をディスクへ
    #: 書くための関数。処理がそこで死んでも、ワークフローの最後の push が
    #: ディスク上の state を拾うので「書いたのに記録が無い」を作らない。
    #: 無ければ何もしない（純粋な単体テストなど）
    checkpoint: Callable[[], None] | None = None
    #: 失敗通知を入れるスレッド（その日の一覧の ts）。入っていれば、通知は
    #: チャンネルではなく一覧のスレッドへ出す。一覧のチャンネルを一覧だけに
    #: 保つため（本人の要望、2026-10-02。通知そのものは残す）
    notice_thread_ts: str | None = None

    def save_point(self, label: str) -> None:
        """外への書き込みの直後に呼ぶ。失敗しても処理は止めない。"""
        if self.checkpoint is None:
            return
        try:
            self.checkpoint()
        except Exception as exc:  # noqa: BLE001 - 記録できない旨だけ残す
            print(f"[失敗] state の途中保存（{label}）: {log_reason(str(exc))}",
                  file=sys.stderr, flush=True)
    reports: list[str] = field(default_factory=list)
    failures: list[Failure] = field(default_factory=list)
    sleep: Callable[[float], None] = real_sleep

    def __post_init__(self) -> None:
        if self.data_dir is None:
            self.data_dir = self.config.root

    def now_iso(self) -> str:
        """今の時刻を JST の ISO 8601 で。`--date` 指定時はその日の実時刻。

        シミュレーションでは日付を固定して連続実行するため、日付部分は
        `date_str` に合わせ、時刻だけ実時刻を使う。
        """
        from datetime import datetime, timedelta, timezone
        jst = timezone(timedelta(hours=9))
        now = datetime.now(jst)
        return f"{self.date_str}T{now.strftime('%H:%M:%S')}+09:00"

    def note_status(self, line: str) -> None:
        if line and line not in self.status:
            self.status.append(line)

    def guarded(self, fn: Callable[[], Any], *, target: str, impact: str,
                default: Any = None, status: str | None = None,
                notify: bool = True) -> Any:
        """外部呼び出しを本番と同じ `retry.call` で包む。

        3回とも失敗したら通知を積み、`取得状況` へ理由を書いて処理を続ける。
        `notify=False` は Slack へ通知しない（実行ログには残す）。記事ごとの
        本文取得のように、失敗しても次の手段へ自動で進み、集計側が別に
        まとめるものに使う。1記事1通で通知すると洪水になる。
        """
        def on_failure(failure: Failure, report: str) -> None:
            self.failures.append(failure)
            if notify:
                self.reports.append(report)
            # status=None なら既定の1行、"" なら取得状況には書かない
            # （記事ごとの失敗など、集計側が別に書くもの）
            if status is None:
                self.note_status(f"{target}: 取得失敗（{failure.reason}）")
            elif status:
                self.note_status(status)
            # 通知先の Slack 自体が落ちていると理由がどこにも残らない。
            # 実行ログ（stderr）を最後の記録にする。値（キーなど）は含まない。
            print(f"[失敗] {target}: {log_reason(failure.reason)}", file=sys.stderr, flush=True)

        return call(
            fn, target=target, impact=impact,
            attempts=int(self.config.get("retry.attempts", 3)),
            backoff=list(self.config.get("retry.backoff_seconds", [1, 4, 16])),
            sleep=self.sleep, on_failure=on_failure, default=default,
        )

    def record_usage(self, state, minutes: float) -> None:
        """この実行の利用時間（見積り）を今月分に足し、上限に近ければ1度だけ知らせる。"""
        month = self.date_str[:7]
        state.usage[month] = round(float(state.usage.get(month, 0) or 0) + minutes, 1)
        # 古い月は3か月分だけ残す
        for key in sorted(state.usage)[:-3]:
            state.usage.pop(key, None)
        soft = float(self.config.get("budget.monthly_minutes_soft", 1600) or 0)
        limit = float(self.config.get("budget.monthly_minutes_limit", 2000) or 0)
        if soft and state.usage[month] >= soft and state.usage_warned.get(month) != "soft":
            state.usage_warned[month] = "soft"
            self.reports.append(
                f":hourglass: 今月の GitHub Actions の実行時間が約{int(state.usage[month])}分になりました"
                f"（無料枠 {int(limit)}分）。\n"
                "枠を使い切ると朝の一覧も出なくなるため、今月は記事の自動の作り直しを止め、"
                "送り直しだけにします。ボタンからの記事化はこれまでどおり動きます。")

    def flush_notifications(self) -> None:
        """溜めた失敗通知をSlackへ送る。通知先は一覧と同じチャンネル（FR-9）。

        同じ対象の通知が複数あれば1通にまとめる。何かが壊れた朝に同じ内容が
        何十通も並ぶと、肝心の一覧が埋もれる。
        """
        for report in collapse_reports(self.reports):
            try:
                self._notify(report)
            except Exception:  # noqa: BLE001 - 通知の失敗で止めない
                pass
        self.reports.clear()

    def _notify(self, report: str) -> None:
        """失敗通知を1通出す。スレッドが決まっていればそこへ、無ければチャンネルへ。"""
        if self.notice_thread_ts:
            try:
                self.clients.slack.reply_in_thread(
                    self.notice_thread_ts,
                    [{"type": "section", "text": {"type": "mrkdwn", "text": report}}],
                    report[:150])
                return
            except Exception:  # noqa: BLE001 - スレッドに出せなければチャンネルへ
                pass
        self.clients.slack.notify_failure(report)


def collapse_reports(reports: list[str]) -> list[str]:
    """同じ対象（1行目が同じ）の通知を1通にまとめる。純粋関数。

    先頭の1通を残し、残りの件数を末尾に添える。
    """
    grouped: dict[str, list[str]] = {}
    order: list[str] = []
    for report in reports:
        key = report.splitlines()[0] if report else ""
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(report)
    out: list[str] = []
    for key in order:
        same = grouped[key]
        first = same[0]
        if len(same) > 1:
            first += f"\n\n（同じ対象の失敗が他に{len(same) - 1}件）"
        out.append(first)
    return out
