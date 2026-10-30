"""失敗注入ラッパーのテスト。

`--fail` の再現形が揃っていることを確認する。個々のモックには失敗の分岐を
書かないので、ここが壊れると失敗系が一切踏めなくなる。
"""

from __future__ import annotations

import pytest

from src.clients import failure as F
from src.clients.parsers import llm_out


class Dummy:
    name = "dummy"

    def __init__(self) -> None:
        self.calls = 0

    def fetch(self, value=None):
        self.calls += 1
        return ["a", "b", "c", "d"]

    def complete(self, prompt: str, purpose: str) -> str:
        return "<!-- ARTICLE 1 -->本文"


def proxy(mode: str, inner=None):
    return F.FailingProxy(inner or Dummy(), "dummy", mode)


def test_指定なしなら何も包まない():
    spec = F.FailureSpec(None)
    assert not spec
    assert spec.mode_for("stooq") is None


def test_名前だけの指定はalwaysになる():
    spec = F.FailureSpec("stooq,claude")
    assert spec.mode_for("stooq") == "always"
    assert spec.mode_for("claude") == "always"
    assert spec.mode_for("slack") is None


def test_コロンでモードを指定できる():
    spec = F.FailureSpec("slack:transient,grok:timeout")
    assert spec.mode_for("slack") == "transient"
    assert spec.mode_for("grok") == "timeout"


def test_未知のモードは弾く():
    with pytest.raises(ValueError, match="未知の失敗モード"):
        F.FailureSpec("slack:explode")


def test_always_は毎回失敗する():
    p = proxy("always")
    for _ in range(3):
        with pytest.raises(F.InjectedFailure):
            p.fetch()


def test_transient_は2回失敗して3回目に成功する():
    inner = Dummy()
    p = F.FailingProxy(inner, "dummy", "transient", transient_fails=2)

    for _ in range(2):
        with pytest.raises(F.InjectedFailure):
            p.fetch()
    assert p.fetch() == ["a", "b", "c", "d"]
    assert inner.calls == 1


def test_timeout_はTimeoutErrorを投げる():
    with pytest.raises(TimeoutError):
        proxy("timeout").fetch()


def test_empty_は例外を投げず同じ型の空を返す():
    assert proxy("empty").fetch() == []


def test_missing_は一部フィールドが欠けた値を返す():
    result = proxy("missing").fetch()
    assert len(result) < 4
    assert result


def test_badformat_はLLM出力の形式を壊す():
    text = proxy("badformat").complete("p", "write")
    split = llm_out.split_articles(text, expected=1)
    assert split.ok is False
    assert "区切り" in split.reason


def test_属性はそのまま通す():
    assert proxy("always").name == "dummy"


def test_3回失敗はリトライ回数と一致する():
    from src import retry
    p = proxy("always")
    captured = []
    result = retry.call(p.fetch, target="dummy", impact="",
                        attempts=3, sleep=lambda s: None,
                        on_failure=lambda f, r: captured.append(f),
                        default="縮退")
    assert result == "縮退"
    assert captured[0].kind == "server"


def test_transientはリトライの中で成功して縮退しない():
    from src import retry
    p = proxy("transient")
    captured = []
    result = retry.call(p.fetch, target="dummy", impact="",
                        attempts=3, sleep=lambda s: None,
                        on_failure=lambda f, r: captured.append(f),
                        default="縮退")
    assert result == ["a", "b", "c", "d"]
    assert captured == []


def test_LLM出力の検証():
    ok = llm_out.split_articles("前<!-- ARTICLE 1 -->A<!-- ARTICLE 2 -->B", 2)
    assert ok.ok is True
    assert ok.bodies == ["A", "B"]

    assert llm_out.split_articles("", 1).ok is False
    assert llm_out.split_articles("区切りなし", 1).ok is False
    assert llm_out.split_articles("<!-- ARTICLE 1 -->A", 2).ok is False


def test_LLM出力はARTICLE番号の順序と重複も検証する():
    reversed_ = llm_out.split_articles(
        "<!-- ARTICLE 2 -->B<!-- ARTICLE 1 -->A", 2)
    duplicated = llm_out.split_articles(
        "<!-- ARTICLE 1 -->A<!-- ARTICLE 1 -->B", 2)

    assert reversed_.ok is False
    assert duplicated.ok is False
    assert "番号" in reversed_.reason


def test_credit_はクレジット切れとして失敗する():
    """通知の文面を分けるため、他のエラーと区別できる必要がある。"""
    from src import retry
    p = proxy("credit")
    try:
        p.fetch()
        raise AssertionError("失敗していない")
    except F.InjectedFailure as exc:
        assert retry.classify(exc) == "credit"
        assert "credit balance is too low" in str(exc)


def test_credit_と_always_は別の分類になる():
    from src import retry
    for mode, expected in (("credit", "credit"), ("always", "server"),
                           ("timeout", "timeout")):
        try:
            proxy(mode).fetch()
        except Exception as exc:  # noqa: BLE001
            assert retry.classify(exc) == expected, mode
