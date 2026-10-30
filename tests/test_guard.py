"""guard.py の単体テスト。AC-22。

**値そのものが漏れないこと**を中心に確認する。
"""

from __future__ import annotations

from src import guard

SECRET = "sk-abcdefghijklmnopqrstuvwxyz012345"


def test_ac22_skで始まる文字列が伏せ字に置換される():
    result = guard.scan(f"# 一覧\n\nキーは {SECRET} です。\n")

    assert SECRET not in result.text
    assert "[REDACTED: openai_key]" in result.text
    assert len(result.findings) == 1


def test_ac22_伏せ字にした位置が報告される():
    result = guard.scan(f"# 一覧\n\nキーは {SECRET} です。\n")
    finding = result.findings[0]

    assert finding.pattern == "openai_key"
    assert finding.line == 3
    assert finding.column > 0
    assert finding.section == "一覧"


def test_ac22_通知に値そのものが含まれない():
    result = guard.scan(f"# 記事\n{SECRET}\n")
    report = guard.build_report("50_Logs/News/2026-09-01.md", result.findings)

    assert SECRET not in report
    assert "abcdefghijklmnop" not in report
    assert "openai_key" in report
    assert "50_Logs/News/2026-09-01.md" in report
    assert "ローテーション" in report


def test_ac22_処理は止めず伏せ字のまま本文が返る():
    result = guard.scan(f"前{SECRET}後")
    assert result.text.startswith("前")
    assert result.text.endswith("後")
    assert not result.clean


def test_取得状況の1行にも値が出ない():
    result = guard.scan(f"# 一覧\n{SECRET}\n")
    line = guard.status_line(result.findings)
    assert SECRET not in line
    assert "伏せ字: 1件" in line
    assert "openai_key" in line


def test_複数パターンを同じ行から拾う():
    text = "xoxb-1234567890-abcdefghijkl と ghp_abcdefghijklmnopqrstuvwxyz012345"
    result = guard.scan(text)
    names = sorted(f.pattern for f in result.findings)
    assert names == ["github_token", "slack_bot_token"]
    assert "xoxb-" not in result.text
    assert "ghp_" not in result.text


def test_pemヘッダも検出する():
    result = guard.scan("-----BEGIN OPENSSH PRIVATE KEY-----")
    assert result.findings[0].pattern == "pem_private_key"


def test_環境変数の値そのものも伏せ字になる():
    value = "not-a-known-pattern-but-secret-value"
    result = guard.scan(f"本文に {value} が混ざった", env_values=[value])

    assert value not in result.text
    assert result.findings[0].pattern == "env_value"
    assert value not in guard.build_report("x.md", result.findings)


def test_秘密が無ければcleanで通知も空():
    result = guard.scan("# 一覧\n普通の本文です。\n")
    assert result.clean
    assert guard.build_report("x.md", result.findings) == ""
    assert guard.status_line(result.findings) == ""


def test_日本語の直後に置かれたキーも検出する():
    """`\b` に頼ると「キーはsk-...です」を取りこぼす。回帰防止。"""
    result = guard.scan(f"キーは{SECRET}です")
    assert SECRET not in result.text
    assert not result.clean
    assert result.findings[0].pattern == "openai_key"


def test_anthropicのキーはopenaiより先に判定される():
    key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz"
    result = guard.scan(f"値は{key}")
    assert key not in result.text
    assert result.findings[0].pattern == "anthropic_key"
