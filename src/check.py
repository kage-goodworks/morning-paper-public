"""自己診断（FR-10）。

全外部依存を1回ずつ叩き、可否を一覧表示する。**✗があっても処理は止めず**、
該当機能を無効化して継続することを示す。

ネットワーク・git・Slack APIを呼ぶ確認はテストではなくここで行う
（Spec「テスト方針」）。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from .clients.factory import build
from .config import load as load_config
from .models import Article

OK = "OK"
NG = "NG"
SKIP = "SKIP"


@dataclass
class Result:
    name: str
    status: str
    detail: str = ""

    @property
    def mark(self) -> str:
        # Windows PowerShell の既定CP932でも診断自体が止まらない表記にする。
        return {OK: "[OK]", NG: "[NG]", SKIP: "[-]"}[self.status]


def _probe(name: str, fn, *, requires: str | tuple[str, ...] | None = None
           ) -> Result:
    """`requires` が複数なら「どれか1つあればよい」。"""
    needed = (requires,) if isinstance(requires, str) else (requires or ())
    if needed and not any(os.environ.get(k) for k in needed):
        return Result(name, SKIP, " / ".join(needed) + " が未設定")
    try:
        fn()
        return Result(name, OK)
    except NotImplementedError:
        return Result(name, SKIP, "未実装（第1段階）")
    except Exception as exc:  # noqa: BLE001 - 診断なので全部受ける
        return Result(name, NG, type(exc).__name__ + ": " + str(exc)[:120])


def _must(value, message: str):
    if value is None:
        raise RuntimeError(message)
    return value


#: 止まると朝の一覧や記事化が動かなくなる部品。週1回の点検でこれが NG なら知らせる
CRITICAL = ("RSS / フィード", "Claude（執筆）", "Gemini（翻訳・要約）",
            "Slack（履歴の読み取り・二重投稿の防止）", "Open Vault（deploy key）")

#: 部品ごとの直し方（本人向け）
HOW_TO_FIX = {
    "Claude（執筆）": "`claude setup-token` で鍵を作り直し、GitHub の Secrets の CLAUDE_CODE_OAUTH_TOKEN を差し替え",
    "Gemini（翻訳・要約）": "Google AI Studio で鍵を確認し、Secrets の GEMINI_API_KEY を差し替え（モデル名の引退なら config.yml の models.gemini）",
    "Slack（履歴の読み取り・二重投稿の防止）": "Slack アプリの鍵（SLACK_BOT_TOKEN）と権限（channels:history / groups:history）を確認",
    "Open Vault（deploy key）": "Open-Vault リポジトリの Deploy keys と Secrets の VAULT_DEPLOY_KEY を確認",
    "RSS / フィード": "config.yml の情報源の URL を確認（scripts/verify_sources.py）",
}


def notice_for(results: list[Result]) -> str:
    """点検で動かない大事な部品があったときの知らせ。無ければ空。純粋関数。"""
    bad = [r for r in results if r.status == NG and r.name in CRITICAL]
    if not bad:
        return ""
    lines = [":stethoscope: 週1回の点検で、動かない部品が見つかりました。朝の処理や記事化が止まる前に直してください。"]
    for r in bad:
        lines.append(f"• {r.name}: {r.detail[:120]}")
        if r.name in HOW_TO_FIX:
            lines.append(f"  直し方: {HOW_TO_FIX[r.name]}")
    return "\n".join(lines)


def run_check(*, mock: bool = False, quiet_slack: bool = False) -> list[Result]:
    cfg = load_config()
    clients = build(mock=mock, out_dir=cfg.root / "out",
                    slack_channel=cfg.get("slack.channel"),
                    models=cfg.get("models"))
    sources = list(cfg.get("sources", []) or [])
    # 本文取得は実在の**公開**ページで試す。架空のドメインだと名前解決で
    # 落ち、private なページだと 404 になって、取得層の可否が分からない。
    sample = Article(id="probe",
                     url="https://github.com/python/cpython",
                     title="probe", source="probe", category="ai",
                     published_at="2026-01-01T00:00:00+09:00")

    results = [
        _probe("RSS / フィード",
               lambda: clients.feed.fetch_articles(
                   sources[0] if sources else {"id": "none", "category": "ai"})),
        _probe("反応（HN・はてブ・Reddit）",
               lambda: clients.reactions.fetch(sample)),
        _probe("GitHub Releases",
               lambda: clients.releases.fetch(
                   (cfg.get("releases.repos", []) or ["rust-lang/rust"])[0])),
        _probe("Yahoo Finance（株価）",
               lambda: clients.quotes.fetch(
                   list(cfg.get("quotes.symbols", []) or []))),
    ]

    for stage in clients.extractors:
        results.append(_probe(f"本文取得 / {stage.name}",
                              lambda stage=stage: stage.extract(sample)))

    results.append(_probe(
        "Claude（執筆）",
        lambda: clients.llms["claude"].complete("TITLE: probe", "write"),
        requires=None if mock else ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")))
    gemini = _probe(
        "Gemini（翻訳・要約）",
        lambda: clients.llms["gemini"].complete("TITLE: probe", "translate"),
        requires=None if mock else "GEMINI_API_KEY")
    results.append(gemini)
    if not mock:
        yt = [c for c in (cfg.get("youtube", []) or []) if c.get("url")]
        if yt:
            results.append(_probe(
                "YouTube Data API（動画一覧）",
                lambda: _must(clients.feed._fetch_youtube_api({**yt[0], "type": "youtube"}),
                              "キーが無いか API が落ちて RSS へ落ちる状態"),
                requires="YOUTUBE_API_KEY"))
    # 動画の要約は文字の翻訳と別経路（fileData で動画URLを渡す）。別に確かめる
    results.append(_probe(
        "Gemini（YouTube動画の要約）",
        lambda: clients.llms["gemini"].complete(
            "URL: https://www.youtube.com/watch?v=9bZkp7q19f0\nTITLE: probe", "youtube"),
        requires=None if mock else "GEMINI_API_KEY"))
    if gemini.status == NG and hasattr(clients.llms["gemini"], "list_models"):
        # モデルが引退したときに、次に何を config.yml へ書けばよいかを出す。
        # 失敗ではなく参考情報なので SKIP 扱い。
        try:
            names = clients.llms["gemini"].list_models()
            flash = [n for n in names if "flash" in n and "image" not in n
                     and "tts" not in n and "live" not in n]
            results.append(Result("Gemini（呼べるモデル名）", SKIP,
                                  ", ".join(flash or names)[:110]))
        except Exception as exc:  # noqa: BLE001 - 参考情報が取れないだけ
            results.append(Result("Gemini（呼べるモデル名）", SKIP,
                                  f"一覧を取れず: {type(exc).__name__}"))

    if cfg.get("reactions.x_enabled", False):
        results.append(_probe("xAI（X上の反応）",
                              lambda: clients.social.topics("2026-01-01"),
                              requires=None if mock else "XAI_API_KEY"))
    else:
        results.append(Result("xAI（X上の反応）", SKIP,
                              "reactions.x_enabled が false"))

    results.append(_probe("検索連携（探索）",
                          lambda: clients.search.search("probe", "2026-01-01")))
    if not quiet_slack:
        # 手動の診断だけ。週1回の点検ではチャンネルに試しの投稿をしない
        results.append(_probe("Slack",
                              lambda: clients.slack.post_message([], "probe"),
                              requires=None if mock else "SLACK_BOT_TOKEN"))
    # 二重投稿を防ぐ履歴の読み取り（channels:history / groups:history）。
    # 権限が無いと missing_scope で落ちる＝二重投稿の防止が効かない状態
    results.append(_probe("Slack（履歴の読み取り・二重投稿の防止）",
                          lambda: clients.slack.find_message("probe-not-found"),
                          requires=None if mock else "SLACK_BOT_TOKEN"))
    results.append(_probe("Open Vault（deploy key）",
                          lambda: clients.notes.read("50_Logs/News/.keep"),
                          requires=None if mock else "VAULT_DEPLOY_KEY"))

    return results


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="python -m src.check")
    parser.add_argument("--mock", action="store_true",
                        help="モックに対して診断する（配線の確認用）")
    parser.add_argument("--notify", action="store_true",
                        help="週1回の点検用。試しの投稿をせず、動かない部品があれば Slack へ1通知らせる")
    args = parser.parse_args(argv)

    parser_args = args
    results = run_check(mock=args.mock, quiet_slack=parser_args.notify)
    width = max(len(r.name) for r in results)
    for result in results:
        line = f"  {result.mark} {result.name.ljust(width)}"
        if result.detail:
            line += f"  {result.detail}"
        print(line)

    ng = [r for r in results if r.status == NG]
    print()
    print(f"OK {sum(1 for r in results if r.status == OK)}件 / "
          f"NG {len(ng)}件 / SKIP {sum(1 for r in results if r.status == SKIP)}件")
    if ng:
        print("NG があっても処理は止めません。該当機能を無効化して継続します。")

    if args.notify:
        text = notice_for(results)
        if text:
            try:
                cfg = load_config()
                clients = build(mock=args.mock, out_dir=cfg.root / "out",
                                slack_channel=cfg.get("slack.channel"), models=cfg.get("models"))
                clients.slack.post_message(
                    [{"type": "section", "text": {"type": "mrkdwn", "text": text[:2900]}}], text[:150])
            except Exception as exc:  # noqa: BLE001 - Slack 自体が落ちていれば知らせようがない
                print(f"点検の知らせを送れませんでした: {type(exc).__name__}")

    # 診断そのものは常に成功で終える。止めないことが仕様のため。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
