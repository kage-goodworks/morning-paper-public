"""retry.py の単体テスト。AC-21。

レポート生成は純粋関数。`call` の待ち時間はテストでも注入して実際に待たない。
"""

from __future__ import annotations

from time import sleep as real_sleep

from src import retry
from src.models import Failure
from src.pipeline import Run


def report_for(kind: str, errors: list[str]) -> str:
    failure = Failure(target="Stooq（株価の取得）",
                      reason=errors[-1],
                      impact="今日の一覧に株価が載りません", kind=kind)
    attempts = [retry.Attempt(index=i + 1, error=e) for i, e in enumerate(errors)]
    return retry.build_report(failure, attempts)


def test_ac21_通知に対象と理由と直し方の案が含まれる():
    text = report_for("server", ["HTTP 503 Service Unavailable"] * 3)

    assert "対象: Stooq（株価の取得）" in text
    assert "理由: HTTP 503 Service Unavailable（3回とも同じ）" in text
    assert "影響: 今日の一覧に株価が載りません" in text
    assert "直し方の案:" in text
    assert "一時的な障害の可能性が高いです" in text


def test_ac21_原因ごとに案が変わる():
    assert "ローテーション" in report_for("auth", ["HTTP 401"])
    assert "実行時刻" in report_for("rate_limit", ["HTTP 429"])
    assert "上場廃止" in report_for("gone", ["HTTP 404"])
    assert "タイムアウト値" in report_for("timeout", ["timed out"])
    assert "クレジット" in report_for("credit", ["insufficient credit"])


def test_ac21_理由が回ごとに違えば最後のものを出す():
    text = report_for("server", ["HTTP 500", "HTTP 502", "HTTP 503"])
    assert "理由: HTTP 503" in text
    assert "3回とも同じ" not in text


def test_原因の分類():
    assert retry.classify(RuntimeError("HTTP 401 Unauthorized")) == "auth"
    assert retry.classify(RuntimeError("HTTP 403 Forbidden")) == "auth"
    assert retry.classify(RuntimeError("HTTP 429 Too Many Requests")) == "rate_limit"
    assert retry.classify(RuntimeError("HTTP 404 Not Found")) == "gone"
    assert retry.classify(RuntimeError("HTTP 503")) == "server"
    assert retry.classify(TimeoutError("no response")) == "timeout"
    assert retry.classify(RuntimeError("insufficient quota")) == "credit"
    assert retry.classify(RuntimeError("何かが壊れた")) == "unknown"


def test_3回リトライして全部失敗したら既定値を返し例外を投げない():
    calls = {"n": 0}
    waited: list[float] = []

    def boom():
        calls["n"] += 1
        raise RuntimeError("HTTP 503")

    captured: list[tuple] = []
    result = retry.call(boom, target="Stooq", impact="株価が載りません",
                        attempts=3, backoff=[1, 4, 16],
                        sleep=waited.append,
                        on_failure=lambda f, r: captured.append((f, r)),
                        default="縮退")

    assert result == "縮退"
    assert calls["n"] == 3
    assert waited == [1, 4]          # 最後の失敗のあとは待たない
    assert len(captured) == 1
    assert captured[0][0].kind == "server"
    assert "直し方の案:" in captured[0][1]


def test_途中で成功したら通知しない():
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("HTTP 503")
        return "ok"

    captured: list[tuple] = []
    result = retry.call(flaky, target="X", impact="", sleep=lambda s: None,
                        on_failure=lambda f, r: captured.append((f, r)))

    assert result == "ok"
    assert captured == []


def test_モック実行の既定スリープは実際に待たない():
    assert retry.NO_SLEEP(3.5) is None


def test_本番の既定リトライは実際のsleepを使う():
    assert retry.call.__kwdefaults__["sleep"] is real_sleep
    assert Run.__dataclass_fields__["sleep"].default is real_sleep


def test_再試行しても無駄な失敗は1回で打ち切り_通知は出す():
    calls = {"n": 0}
    waited: list[float] = []

    class Gone(retry.NonRetryable):
        pass

    def gone():
        calls["n"] += 1
        raise Gone("HTTP 404 Not Found")

    captured: list[tuple] = []
    result = retry.call(gone, target="trafilatura（本文取得）", impact="次の手段へ",
                        attempts=3, backoff=[1, 4, 16], sleep=waited.append,
                        on_failure=lambda f, r: captured.append((f, r)),
                        default=None)

    assert result is None
    assert calls["n"] == 1               # 2回目以降は呼ばない
    assert waited == []                  # バックオフで待たない
    assert len(captured) == 1            # 失敗としては記録する
    assert "404" in captured[0][0].reason


def test_httpの4xxは再試行せず_408と429と5xxは再試行する():
    import urllib.error
    from unittest.mock import patch

    from src.clients.real import http

    def raising(code):
        def fake(request, timeout):
            raise urllib.error.HTTPError(request.full_url, code, "x", {}, None)
        return fake

    for code, expected in ((404, http.ClientError), (403, http.ClientError),
                           (408, http.FetchError), (429, http.FetchError),
                           (503, http.FetchError)):
        with patch("urllib.request.urlopen", raising(code)):
            try:
                http.fetch_bytes("https://example.test/x")
            except http.FetchError as exc:
                assert type(exc) is expected, code
                assert isinstance(exc, retry.NonRetryable) is (expected is http.ClientError)
            else:
                raise AssertionError(code)


def test_gzip圧縮されたエラー本文は解いてから文字にし_制御文字は残さない():
    import gzip
    import io
    import urllib.error
    from email.message import Message
    from unittest.mock import patch

    from src.clients.real import http

    body = gzip.compress("<title>Stooq</title>ページが見つかりません".encode("utf-8"))
    headers = Message()
    headers["Content-Encoding"] = "gzip"

    def fake(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 404, "Not Found",
                                     headers, io.BytesIO(body))

    with patch("urllib.request.urlopen", fake):
        try:
            http.fetch_bytes("https://example.test/x")
        except http.FetchError as exc:
            text = str(exc)
        else:
            raise AssertionError("例外が出ない")

    assert "HTTP 404" in text
    assert "ページが見つかりません" in text
    assert all(ch.isprintable() or ch.isspace() for ch in text)


def test_同じ対象の失敗通知は1通にまとまる():
    from src.pipeline import collapse_reports
    reports = [":warning: trafilatura（本文取得）の処理に失敗しました\n\n対象: a",
               ":warning: trafilatura（本文取得）の処理に失敗しました\n\n対象: b",
               ":warning: Stooqの処理に失敗しました\n\n対象: c",
               ":warning: trafilatura（本文取得）の処理に失敗しました\n\n対象: d"]
    out = collapse_reports(reports)
    assert len(out) == 2
    assert out[0].endswith("（同じ対象の失敗が他に2件）")
    assert "Stooq" in out[1] and "他に" not in out[1]


def test_記事ごとの本文取得の失敗はSlackへ通知しない(tmp_path):
    from src import extract
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.pipeline import Run
    from src.retry import NO_SLEEP
    from tests.conftest import make_article

    cfg = load_config()
    run = Run(config=cfg, clients=build_mock(scenario="normal", day=1, out_dir=tmp_path),
              date_str="2026-09-01", out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)

    class Broken:
        name = "broken"
        def extract(self, article):
            raise RuntimeError("HTTP 403 Forbidden")

    run.clients.extractors[:] = [Broken()]
    extract.extract_all(run, [make_article(id=f"n{i}", url=f"https://e.test/{i}",
                                           title=str(i)) for i in range(4)])
    assert run.reports == []          # 通知は積まない
    assert len(run.failures) == 4     # 記録は残る


def test_htmlのエラー本文はタグとcssを落として理由にする():
    import io
    import urllib.error
    from email.message import Message
    from unittest.mock import patch
    from src.clients.real import http

    body = b"<body class=x><style>.a{--rem:1rem}</style><h1>Blocked</h1><p>Access denied</p></body>"
    def fake(request, timeout):
        raise urllib.error.HTTPError(request.full_url, 403, "Blocked", Message(), io.BytesIO(body))
    with patch("urllib.request.urlopen", fake):
        try:
            http.fetch_bytes("https://example.test/x")
        except http.FetchError as exc:
            text = str(exc)
    assert "--rem" not in text and "<" not in text
    assert "Blocked" in text and "Access denied" in text
