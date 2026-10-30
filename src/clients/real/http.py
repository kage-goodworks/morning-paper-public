"""実接続の共通部分。

`real/` のクライアントはここを通して外へ出る。**取得先の検査・タイムアウト・
サイズ上限を1か所に集める**ため。リトライはここでは行わない。3回の再試行と
縮退は `src/retry.py` が担当する（モックでも本番でも同じ経路を通す）。

依存は標準ライブラリだけ。
"""

from __future__ import annotations

import gzip
import json
import re
import os
import socket
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from ... import safety
from ...retry import NonRetryable

USER_AGENT = "morning-paper/0.1 (+https://github.com/kage-goodworks/morning-paper)"

#: 1回の取得で受け取る上限。XML爆弾や巨大ページで詰まらせないため。
MAX_BYTES = 5 * 1024 * 1024

#: ソース単位のタイムアウト（Spec「朝の処理」）。
DEFAULT_TIMEOUT = 20.0


class FetchError(RuntimeError):
    """取得に失敗した。`retry.classify` が分類できる文言にする。"""


class ClientError(FetchError, NonRetryable):
    """4xx。相手が「この要求は受けない」と言っており、繰り返しても同じ。

    408（タイムアウト）と 429（レート制限）は時間を置けば通るので除く。
    """


#: 4xx のうち再試行に意味があるもの
_RETRYABLE_4XX = {408, 429}

#: 手元の端末で IPv6 が壊れている（アドレスはあるが外へ出られない）場合、
#: 接続ごとに IPv6 の失敗を待ってから IPv4 へ落ちるため、1接続に丸々
#: タイムアウト分かかる。`MORNING_PAPER_IPV4_ONLY=1` で名前解決を IPv4 に
#: 絞る。本番（GitHub Actions）では不要で、既定では何もしない。
if os.environ.get("MORNING_PAPER_IPV4_ONLY") == "1":
    _getaddrinfo = socket.getaddrinfo

    def _ipv4_only(*args, **kwargs):  # noqa: ANN001 - socket と同じ形
        results = _getaddrinfo(*args, **kwargs)
        v4 = [r for r in results if r[0] == socket.AF_INET]
        return v4 or results

    socket.getaddrinfo = _ipv4_only


def _check_target(url: str) -> None:
    """取りに行ってよい先かを確かめる。

    フィードやGrokが返したURLをそのまま取りに行くと、内部ネットワークへの
    踏み台にされる（SSRF）。**名前解決の結果までは見られない**ので、
    社内DNSが内部アドレスを返す環境では別途ネットワーク側で塞ぐこと。
    """
    if not safety.is_fetchable(url):
        raise FetchError(f"取得先として許可されていないURL: {url[:80]}")


def fetch_bytes(url: str, *, timeout: float = DEFAULT_TIMEOUT,
                headers: dict[str, str] | None = None,
                data: bytes | None = None,
                method: str | None = None) -> bytes:
    """1回だけ取りに行く。失敗は `FetchError` にそろえる。"""
    _check_target(url)

    merged = {"User-Agent": USER_AGENT, "Accept-Encoding": "gzip"}
    merged.update(headers or {})
    request = urllib.request.Request(url, headers=merged, data=data,
                                     method=method)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_BYTES + 1)
            if response.headers.get("Content-Encoding") == "gzip":
                raw = gzip.decompress(raw)
    except urllib.error.HTTPError as exc:
        body = _error_body(exc)
        message = f"HTTP {exc.code} {exc.reason} {_trim(body)}"
        if 400 <= exc.code < 500 and exc.code not in _RETRYABLE_4XX:
            raise ClientError(message) from exc
        raise FetchError(message) from exc
    except urllib.error.URLError as exc:
        reason = str(exc.reason)
        if "timed out" in reason.lower():
            raise TimeoutError(f"timeout: {url[:80]}") from exc
        raise FetchError(f"接続できない: {reason}") from exc
    except TimeoutError:
        raise
    except OSError as exc:
        raise FetchError(f"{type(exc).__name__}: {exc}") from exc

    if len(raw) > MAX_BYTES:
        raise FetchError(f"応答が大きすぎる（{MAX_BYTES}バイト超）: {url[:80]}")
    return raw


def fetch_text(url: str, **kwargs: Any) -> str:
    return fetch_bytes(url, **kwargs).decode("utf-8", "replace")


def fetch_json(url: str, **kwargs: Any) -> Any:
    text = fetch_text(url, **kwargs)
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise FetchError(f"JSONとして読めない応答: {_trim(text)}") from exc


def post_json(url: str, payload: dict[str, Any], *,
              headers: dict[str, str] | None = None,
              timeout: float = 60.0) -> Any:
    """JSONを送ってJSONを受け取る。"""
    merged = {"Content-Type": "application/json"}
    merged.update(headers or {})
    return fetch_json(url, data=json.dumps(payload).encode("utf-8"),
                      headers=merged, timeout=timeout, method="POST")


def post_form(url: str, fields: dict[str, str], *,
              headers: dict[str, str] | None = None,
              timeout: float = 30.0) -> Any:
    merged = {"Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    merged.update(headers or {})
    body = urllib.parse.urlencode(fields).encode("utf-8")
    return fetch_json(url, data=body, headers=merged, timeout=timeout,
                      method="POST")


def _error_body(exc: urllib.error.HTTPError) -> str:
    """エラー応答の本文を人が読める形で少しだけ取る。

    `Accept-Encoding: gzip` を送っているので、エラー応答も圧縮されて
    くることがある。そのまま文字にすると `取得状況` や失敗通知に
    バイナリのゴミが載る。解いてから文字にし、読めなければ捨てる。
    """
    try:
        raw = exc.read(4096)
    except Exception:  # noqa: BLE001 - 本文が読めなくても続ける
        return ""
    if exc.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
        try:
            raw = gzip.decompress(raw)
        except Exception:  # noqa: BLE001 - 途中で切れた圧縮データなど
            return ""
    text = raw.decode("utf-8", "replace")
    # HTML のエラーページは CSS やタグの切れ端しか残らず読めない。
    # style / script の中身とタグを落として本文だけにする。
    if "<" in text and ">" in text:
        text = re.sub(r"(?is)<(style|script)[^>]*>.*?</\1>", " ", text)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    return text


def _trim(text: str, limit: int = 200) -> str:
    """1行に潰し、表示できない文字を落として先頭だけ残す。"""
    cleaned = "".join(ch if ch.isprintable() or ch.isspace() else " "
                      for ch in (text or ""))
    return " ".join(cleaned.split())[:limit]
