"""リトライと失敗レポート。

全外部呼び出しをここで包む。**リトライそのものはモックしない。**モック実行でも
本番と同じこの関数を通り、3回失敗すれば同じ経路で縮退へ落ちる。

待ち時間だけは注入できるようにしてある。モックでは実際に待たない
（`NO_SLEEP`）。待つかどうかは検証したい対象ではなく、待つと `simulate full`
が3日分×全ソースで無意味に遅くなるため。

レポート生成（`build_report`）は外部I/Oを持たない純粋関数で、AC-21 の対象。
"""

from __future__ import annotations

from dataclasses import dataclass
from time import sleep as real_sleep
from typing import Any, Callable, TypeVar

from .models import Failure

T = TypeVar("T")

#: モック実行で使う「待たない」スリープ。呼ばれた秒数だけ記録する。
class _NoSleep:
    def __init__(self) -> None:
        self.waited: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.waited.append(seconds)


NO_SLEEP = _NoSleep()


@dataclass
class Attempt:
    """1回分の試行結果。レポートに載せる。"""
    index: int
    error: str


def classify(exc: BaseException) -> str:
    """原因を分類する。純粋関数（定型文の選択に使う）。"""
    if isinstance(exc, TimeoutError):
        return "timeout"
    text = str(exc).lower()
    status = None
    for code in (401, 403, 404, 429, 500, 502, 503, 504):
        if str(code) in text:
            status = code
            break
    if status in (401, 403):
        return "auth"
    if status == 429:
        return "rate_limit"
    if status == 404:
        return "gone"
    if status in (500, 502, 503, 504):
        return "server"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    if "credit" in text or "quota" in text or "残高" in text:
        return "credit"
    return "unknown"


#: 原因ごとの「直し方の案」。`retry.py` が持つ定型文（Spec の分類表）。
ADVICE: dict[str, list[str]] = {
    "auth": [
        "キーの期限切れかスコープ不足の可能性があります",
        "GitHub Secrets のキーをローテーションして差し替えてください",
        "トークンに必要なスコープが付いているか確認してください",
    ],
    "rate_limit": [
        "レート制限に当たっています",
        "実行時刻を数分ずらしてください",
        "リクエスト間隔を広げるか、対象件数を減らしてください",
    ],
    "gone": [
        "対象が消えているか、URLが変わっています",
        "config.yml のURL・銘柄コード・リポジトリ名を確認してください",
        "上場廃止やリポジトリの改名が起きていないか確認してください",
    ],
    "server": [
        "一時的な障害の可能性が高いです。明日も同じなら以下を確認してください",
        "対象のサービスがブラウザで開けるか",
        "config.yml の設定値が有効か",
        "レート制限の場合は実行時刻を数分ずらす",
    ],
    "timeout": [
        "応答がありません",
        "タイムアウト値の見直しを検討してください",
        "代替の情報源への切り替えを検討してください",
    ],
    "credit": [
        "クレジットが不足しています",
        "該当機能を無効化して継続しています",
        "クレジットを補充するか、config.yml で該当機能を切ってください",
    ],
    "unknown": [
        "原因を特定できませんでした",
        "実行ログで対象の処理を確認してください",
        "翌日も続く場合は対象サービスの状態を確認してください",
    ],
}


class NonRetryable(Exception):
    """再試行しても結果が変わらない失敗の印。

    HTTP 404 や 403 のような**決定的な**失敗は、3回繰り返しても同じ応答が
    返るだけで、バックオフ（1+4+16秒）ぶん朝のジョブを遅らせる。この印を
    持つ例外は1回で打ち切り、そのまま縮退（`default`）へ進む。通知や
    `取得状況` への記録は通常の失敗と同じ。
    """


def build_report(failure: Failure, attempts: list[Attempt]) -> str:
    """失敗通知の本文を組み立てる。純粋関数（AC-21）。

    対象・理由・影響・直し方の案を必ず含める。値そのもの（キーなど）は載せない。
    """
    reasons = [a.error for a in attempts]
    same = len(set(reasons)) == 1 and len(reasons) > 1
    reason_line = reasons[-1] if reasons else "不明"
    if same:
        reason_line += "（" + str(len(reasons)) + "回とも同じ）"

    lines = [
        ":warning: " + failure.target + "の処理に失敗しました",
        "",
        "対象: " + failure.target,
        "理由: " + reason_line,
        "影響: " + failure.impact,
        "",
        "直し方の案:",
    ]
    lines.extend("・" + a for a in ADVICE.get(failure.kind, ADVICE["unknown"]))
    return "\n".join(lines)


def call(fn: Callable[[], T], *, target: str, impact: str,
         attempts: int = 3, backoff: list[float] | None = None,
         sleep: Callable[[float], None] = real_sleep,
         on_failure: Callable[[Failure, str], None] | None = None,
         default: Any = None) -> T | Any:
    """外部呼び出しを包む。3回まで再試行し、全部失敗したら縮退する。

    **例外を上へ投げない。**フロー全体が止まる条件を作らないため、失敗しても
    `default` を返して処理を続ける。何が欠けたかは `on_failure` で通知する。
    """
    backoff = backoff if backoff is not None else [1, 4, 16]
    tried: list[Attempt] = []

    for i in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 - 縮退のため全て受ける
            tried.append(Attempt(index=i + 1, error=str(exc)))
            last = exc
            if isinstance(exc, NonRetryable):
                break  # 繰り返しても同じ。すぐ縮退へ
            if i < attempts - 1:
                sleep(backoff[i] if i < len(backoff) else backoff[-1])

    failure = Failure(target=target, reason=str(last), impact=impact,
                      kind=classify(last))
    if on_failure is not None:
        on_failure(failure, build_report(failure, tried))
    return default
