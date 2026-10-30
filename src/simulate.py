"""シナリオ実行。

外部へ一切接続しない状態で、朝の処理から記事化・週次レビューまでを通す。

    python -m src.simulate morning
    python -m src.simulate click s_a1b2c3 s_d4e5f6
    python -m src.simulate weekly
    python -m src.simulate full                     3日分を連続実行

    python -m src.simulate morning --fail stooq,claude
    python -m src.simulate morning --empty

出力はすべて `out/` に残る。
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from . import articles as articles_mod
from . import blocks, main as main_mod, weekly as weekly_mod
from .clients.factory import build_mock
from .config import load as load_config
from .pipeline import Run
from .retry import NO_SLEEP
from .state import load as load_state, save as save_state

#: 3日分の日付。1日目に溢れた話題が2日目へ繰り越され、3日目に統合される。
DAYS = ["2026-09-01", "2026-09-02", "2026-09-03"]

#: 本番と同じ区画分けでシミュレーションする。朝は自分の持ち場だけ書く。
MORNING_SECTIONS = ("collect", "stories", "outbox")
ARTICLE_SECTIONS = ("written", "outbox")


def _make_run(cfg, *, date_str: str, day: int, out_dir: Path,
              scenario: str, fail: str | None, phase: str = "morning") -> Run:
    clients = build_mock(scenario=scenario, day=day, out_dir=out_dir,
                         phase=phase, fail=fail)
    return Run(config=cfg, clients=clients, date_str=date_str, out_dir=out_dir,
               data_dir=out_dir, sleep=NO_SLEEP)


def cmd_morning(args) -> int:
    cfg = load_config()
    out_dir = cfg.root / "out"
    if args.reset:
        shutil.rmtree(out_dir, ignore_errors=True)

    scenario = "empty" if args.empty else args.scenario
    run = _make_run(cfg, date_str=args.date or DAYS[0], day=args.day,
                    out_dir=out_dir, scenario=scenario, fail=args.fail)
    state = load_state(out_dir)

    result = main_mod.run_morning(run, state)
    save_state(state, out_dir, sections=MORNING_SECTIONS)

    print(f"[morning {run.date_str}] 記事 {result['articles']}件 / "
          f"話題 {result['stories']}件 / 表示 {result['listed']}件 / "
          f"未発行 {result['unpublished']}件 / ノート {result['notes']}件 / "
          f"メッセージ {result['messages']}通")
    for line in result["status"]:
        print("  取得状況:", line)
    return 0


def cmd_click(args) -> int:
    cfg = load_config()
    out_dir = cfg.root / "out"
    run = _make_run(cfg, date_str=args.date or DAYS[0], day=args.day,
                    out_dir=out_dir, scenario=args.scenario, fail=args.fail)
    state = load_state(out_dir)

    story_ids = list(args.story_ids)
    if not story_ids:
        story_ids = _from_payload(cfg, state, run.date_str)

    result = articles_mod.run_articles(
        run, state, story_ids=story_ids,
        message_ts=args.message_ts, thread_ts=args.thread_ts)
    save_state(state, out_dir, sections=ARTICLE_SECTIONS)

    state_line = "失敗（記事化せず）" if result.get("failed") else "成功"
    print(f"[click {run.date_str}] {state_line} / 記事化 {len(result['written'])}件 / "
          f"作成済み {len(result['already'])}件 / 保留 {len(result['pending'])}件 / "
          f"紙面 {result['pages']}ページ")
    for line in run.status:
        print("  取得状況:", line)
    return 0


def _from_payload(cfg, state, date_str: str) -> list[str]:
    """フィクスチャのボタン押下ペイロードから story_id を取り出す。

    ペイロードのidは固定なので、実際に一覧へ出た話題へ読み替える。
    """
    path = cfg.root / "tests" / "fixtures" / "slack" / "block_actions.json"
    if path.exists():
        payload = json.loads(path.read_text(encoding="utf-8"))
        ids = blocks.selected_story_ids(payload)
        known = [
            i for i in ids
            if i in state.stories
            and state.stories[i].get("status") == "listed"
            and state.stories[i].get("listed_on") == date_str
        ]
        if known:
            return known
    listed = [s.id for s in state.all_stories()
              if s.status == "listed" and s.listed_on == date_str]
    return listed[:8]


def cmd_weekly(args) -> int:
    cfg = load_config()
    out_dir = cfg.root / "out"
    run = _make_run(cfg, date_str=args.date or DAYS[-1], day=args.day,
                    out_dir=out_dir, scenario=args.scenario, fail=args.fail,
                    phase="weekly")
    state = load_state(out_dir)

    result = weekly_mod.run_weekly(run, state)
    save_state(state, out_dir, sections=("outbox",))
    print(f"[weekly] 変化 {result['changes']}件 / 投稿 {result['posted']}")
    for line in run.status:
        print("  取得状況:", line)
    return 0


def cmd_full(args) -> int:
    """3日分を連続実行する。

    1日目に溢れた話題が2日目へ繰り越され、3日目に統合される ―― この一連は
    1日分では確認できない。2日目に記事化を挟み、3日目に「記事化済みの除外」
    が効くことも見る。
    """
    cfg = load_config()
    out_dir = cfg.root / "out"
    shutil.rmtree(out_dir, ignore_errors=True)

    summary: list[str] = []

    for index, date_str in enumerate(DAYS, start=1):
        run = _make_run(cfg, date_str=date_str, day=index, out_dir=out_dir,
                        scenario="written_excluded", fail=args.fail)
        state = load_state(out_dir)
        result = main_mod.run_morning(run, state)
        save_state(state, out_dir, sections=MORNING_SECTIONS)

        line = (f"[{date_str}] 記事 {result['articles']}件 / "
                f"話題 {result['stories']}件 / 表示 {result['listed']}件 / "
                f"未発行 {result['unpublished']}件 / ノート {result['notes']}件")
        print(line)
        for status in result["status"]:
            print("   ", status)
        summary.append(line)

        # 2日目にボタンを押す。3日目で「記事化済みの除外」が効くことを見る。
        if index == 2:
            run2 = _make_run(cfg, date_str=date_str, day=index, out_dir=out_dir,
                             scenario="written_excluded", fail=args.fail)
            state = load_state(out_dir)
            listed = [s.id for s in state.all_stories()
                      if s.status == "listed" and s.listed_on == date_str]
            clicked = listed[:8]
            res = articles_mod.run_articles(
                run2, state, story_ids=clicked,
                message_ts=result.get("message_ts"),
                thread_ts=result.get("message_ts"))
            save_state(state, out_dir, sections=ARTICLE_SECTIONS)
            line = (f"[{date_str}] 記事化 {len(res['written'])}件 / "
                    f"保留 {len(res['pending'])}件 / 紙面 {res['pages']}ページ"
                    + ("（両モデル失敗のため記事化せず）" if res.get("failed") else ""))
            print(line)
            for status in run2.status:
                print("   ", status)
            summary.append(line)

            # 同じ話題をもう一度押す（AC-27）
            run3 = _make_run(cfg, date_str=date_str, day=index, out_dir=out_dir,
                             scenario="written_excluded", fail=args.fail)
            state = load_state(out_dir)
            res2 = articles_mod.run_articles(
                run3, state, story_ids=clicked[:2],
                message_ts=result.get("message_ts"),
                thread_ts=result.get("message_ts"))
            save_state(state, out_dir, sections=ARTICLE_SECTIONS)
            line = (f"[{date_str}] 再押下: 作成済み {len(res2['already'])}件 / "
                    f"新規 {len(res2['written'])}件")
            print(line)
            summary.append(line)

    # 週次レビュー
    run = _make_run(cfg, date_str=DAYS[-1], day=3, out_dir=out_dir,
                    scenario="written_excluded", fail=args.fail,
                    phase="weekly")
    state = load_state(out_dir)
    result = weekly_mod.run_weekly(run, state)
    save_state(state, out_dir, sections=("outbox",))
    line = f"[weekly] 変化 {result['changes']}件 / 投稿 {result['posted']}"
    print(line)
    for status in run.status:
        print("   ", status)
    summary.append(line)

    print("\n--- out/ ---")
    for sub in ("slack", "vault", "paper"):
        target = out_dir / sub
        count = len(list(target.rglob("*"))) if target.exists() else 0
        print(f"  {sub}/: {count} files")
    files = sorted((out_dir / "state").glob("*.json"))
    print(f"  state/: {', '.join(f.name for f in files) if files else 'なし'}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    # 共通オプションはサブコマンドの前後どちらにも書けるようにする。
    # 検証で使うのは `morning --fail stooq,claude` の形。
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--fail", default=None,
                        help="失敗を注入する対象。例: stooq,claude / slack:transient")
    common.add_argument("--empty", action="store_true",
                        help="収集0件にして探索を走らせる")
    common.add_argument("--offline", action="store_true",
                        help="--mock と同義（モックは常に有効）")
    common.add_argument("--scenario", default="normal")
    common.add_argument("--day", type=int, default=1)
    common.add_argument("--date", default=None)
    common.add_argument("--reset", action="store_true", help="out/ を消してから実行")

    parser = argparse.ArgumentParser(prog="python -m src.simulate",
                                     parents=[common])
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("morning", parents=[common]).set_defaults(func=cmd_morning)

    click = sub.add_parser("click", parents=[common])
    click.add_argument("story_ids", nargs="*")
    click.add_argument("--message-ts", default="1756000000.000100")
    click.add_argument("--thread-ts", default=None)
    click.set_defaults(func=cmd_click)

    sub.add_parser("weekly", parents=[common]).set_defaults(func=cmd_weekly)
    sub.add_parser("full", parents=[common]).set_defaults(func=cmd_full)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
