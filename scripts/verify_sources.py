"""`config.yml` と `youtube.yml` の情報源が実際に取得できるかを確かめる。

**このスクリプトだけは外部へ接続する。**パイプラインの一部ではなく、
実キー投入の前に情報源リストを点検するための道具。

    .venv/Scripts/python.exe scripts/verify_sources.py
    .venv/Scripts/python.exe scripts/verify_sources.py --timeout 20

フィードのURLはサイトの改修で変わる。朝の処理は落ちたソースを飛ばして続ける
ので気づきにくく、黙って情報源が減っていく。ここで定期的に確かめる。

各URLについて次を見る。

- 取得できるか（HTTPのステータス）
- フィードとして解析できるか（`clients/parsers/feed.py` を通す）
- 記事が何件取れるか
- `fulltext: true` の指定が実態と合っているか

依存は標準ライブラリだけ。
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src import safety  # noqa: E402
from src.clients.parsers import feed as feed_parser  # noqa: E402
from src.config import load as load_config  # noqa: E402

USER_AGENT = "morning-paper/0.1 (feed verification)"
MAX_BYTES = 5 * 1024 * 1024

#: この長さを超えていれば全文配信とみなす。これ未満は抜粋。抜粋を本文として
#: 採用すると、記事の中身が薄いまま執筆へ回ってしまう。
FULLTEXT_CHARS = 1500


@dataclass
class Result:
    source_id: str
    category: str
    url: str
    ok: bool
    detail: str
    articles: int = 0
    fulltext_actual: bool = False
    fulltext_config: bool = False

    @property
    def mark(self) -> str:
        return "[OK]" if self.ok else "[NG]"


def fetch(url: str, timeout: float) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read(MAX_BYTES)


def check(source: dict, timeout: float) -> Result:
    source_id = str(source.get("id") or "?")
    category = str(source.get("category") or "?")
    url = str(source.get("url") or "")
    wanted_fulltext = bool(source.get("fulltext"))

    base = Result(source_id=source_id, category=category, url=url, ok=False,
                  detail="", fulltext_config=wanted_fulltext)

    if not safety.is_fetchable(url):
        base.detail = "取得先として許可されないURL"
        return base

    try:
        raw = fetch(url, timeout)
    except urllib.error.HTTPError as exc:
        base.detail = f"HTTP {exc.code}"
        return base
    except urllib.error.URLError as exc:
        base.detail = f"接続できない（{exc.reason}）"
        return base
    except Exception as exc:  # noqa: BLE001 - 点検なので全部受ける
        base.detail = f"{type(exc).__name__}: {str(exc)[:60]}"
        return base

    articles = feed_parser.parse(raw, source_id=source_id,
                                 default_category=category,
                                 fulltext=True)
    if not articles:
        base.detail = f"フィードとして解析できない（{len(raw)} bytes）"
        return base

    # 本文が「入っているか」だけで判定すると、抜粋しか載せないフィードまで
    # 全文配信とみなしてしまう。**長さの中央値**で見る。
    lengths = sorted(len(a.body_md.strip()) for a in articles if a.body_md.strip())
    median = lengths[len(lengths) // 2] if lengths else 0

    base.ok = True
    base.articles = len(articles)
    base.fulltext_actual = median >= FULLTEXT_CHARS
    base.detail = f"{len(articles)}件"
    if median:
        base.detail += f" / 本文の中央値 {median}字"
    if base.fulltext_actual != wanted_fulltext:
        base.detail += (f" / fulltext は {str(base.fulltext_actual).lower()} "
                        f"が実態（設定は {str(wanted_fulltext).lower()}）")
    return base


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="verify_sources.py")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--workers", type=int, default=8)
    args = parser.parse_args(argv)

    config = load_config()
    sources = list(config.get("sources", []) or [])
    for channel in config.get("youtube", []) or []:
        sources.append({**channel, "category": channel.get("category", "ai")})

    if not sources:
        print("情報源が設定されていません。")
        return 0

    print(f"{len(sources)}件を確認します（タイムアウト {args.timeout:.0f}秒）\n")
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(lambda s: check(s, args.timeout), sources))

    width = max(len(r.source_id) for r in results)
    current = ""
    for result in sorted(results, key=lambda r: (r.category, r.source_id)):
        if result.category != current:
            current = result.category
            print(f"\n--- {current} ---")
        print(f"  {result.mark} {result.source_id.ljust(width)}  {result.detail}")
        if not result.ok:
            print(f"       {result.url}")

    ng = [r for r in results if not r.ok]
    mismatched = [r for r in results
                  if r.ok and r.fulltext_actual != r.fulltext_config]

    print(f"\nOK {len(results) - len(ng)}件 / NG {len(ng)}件")
    if mismatched:
        print(f"fulltext の設定と実態が違うもの: "
              f"{', '.join(r.source_id for r in mismatched)}")
    if ng:
        print("\nNG のものは config.yml から外すか、正しいURLへ直してください。")
        print("朝の処理は落ちたソースを飛ばして続けるため、放置すると黙って")
        print("情報源が減っていきます。")
    return 1 if ng else 0


if __name__ == "__main__":
    raise SystemExit(main())
