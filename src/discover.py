"""記事が集まらないときの探索（FR-2）。

段階を順に試し、`fallback.trigger_count` に達した時点で止める。
**「記事なし」で終わらせない。**

段階1 Grokで前日の話題 → 一次ソースを確認 → 確認できたものだけ採用
段階2 検索連携で探索 → `discovered_sources.yml` へ候補を追記
段階3 株価とGitHub Releasesで成立させ、`取得状況`に明記
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .collect import collect_releases
from .models import Article
from .pipeline import Run

#: 一次ソースとみなすドメインの手がかり。Xの投稿そのものは記事にしない。
PRIMARY_HINTS = ("blog.", "news.", "releases", "github.com", "arxiv.org",
                 "/official", "example.invalid")


def _is_primary_source(url: str) -> bool:
    """一次ソース（公式発表、リリースノート、論文）かを見る。"""
    lowered = (url or "").lower()
    if "x.com" in lowered or "twitter.com" in lowered:
        return False
    return any(hint in lowered for hint in PRIMARY_HINTS)


def stage1(run: Run) -> list[Article]:
    """Grokで前日の話題から逆引きし、一次ソースで裏を取る。

    `x_enabled` が false でも実行する（記事が集まらないのは例外事態のため）。
    """
    found = run.guarded(
        lambda: run.clients.social.discover(run.date_str),
        target="xAI（探索 段階1）",
        impact="段階2の探索へ進みます",
        default=None)
    if found is None:
        run.note_status("探索: 段階1を実行できず、段階2へ進みました")
        return []

    verified = [a for a in found if _is_primary_source(a.url)]
    dropped = len(found) - len(verified)
    if verified:
        run.note_status(
            f"探索: 段階1でXから{len(found)}件を検出し、一次ソースを確認できた"
            f"{len(verified)}件を採用しました"
            + (f"（裏が取れなかった{dropped}件は不採用）" if dropped else ""))
    return verified


def stage2(run: Run) -> list[Article]:
    """検索連携で探索し、見つかったドメインを候補として記録する。"""
    found = run.guarded(
        lambda: run.clients.search.search("技術ニュース", run.date_str),
        target="検索連携（探索 段階2）",
        impact="段階3の探索へ進みます",
        default=None)
    if not found:
        run.note_status("探索: 段階2で記事が見つかりませんでした")
        return []

    promote = run.guarded(
        lambda: record_candidates(
            run.data_dir / "discovered_sources.yml",
            [a.url for a in found],
            promote_after=int(run.config.get("fallback.promote_after", 3))),
        target="探索候補の保存",
        impact="discovered_sources.yml の採用回数を更新できません",
        default=[]) or []

    run.note_status(f"探索: 段階2で{len(found)}件を採用しました")
    for domain, used in promote:
        run.note_status(
            f"新しい情報源の候補: {domain}（{used}回採用）。config.yml への追加を検討してください")
    return found


def stage3(run: Run) -> list[Article]:
    """必ずある領域で成立させる。"""
    releases = collect_releases(run)
    run.note_status("探索: 通常の情報源に更新がなかったため、"
                    "株価とGitHub Releasesで一覧を構成しました")
    return releases


def record_candidates(path: Path, urls: list[str], *, promote_after: int
                      ) -> list[tuple[str, int]]:
    """`discovered_sources.yml` へ候補を追記する。

    **`config.yml` は自動で書き換えない**（FR-2 段階2 / AC-24）。
    戻り値は、採用回数がしきい値に達したドメイン。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {"candidates": []}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            data = yaml.safe_load(f) or {"candidates": []}
    candidates = {c["domain"]: c for c in (data.get("candidates") or [])}

    before: dict[str, int] = {}
    for url in urls:
        domain = urlsplit(url).netloc.lower()
        if not domain:
            continue
        entry = candidates.setdefault(domain, {"domain": domain, "used": 0})
        before.setdefault(domain, int(entry.get("used", 0)))
        entry["used"] = int(entry.get("used", 0)) + 1

    threshold = max(1, promote_after)
    promoted = [(domain, int(candidates[domain]["used"]))
                for domain, old in before.items()
                if old < threshold <= int(candidates[domain]["used"])]

    data["candidates"] = sorted(candidates.values(),
                                key=lambda c: (-c["used"], c["domain"]))
    with path.open("w", encoding="utf-8") as f:
        f.write("# 探索で見つかった情報源の候補。discover.py が自動追記する。\n"
                "# config.yml は自動で書き換えない（FR-2 段階2）。\n")
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    return promoted


def run_discovery(run: Run, existing: list[Article]) -> list[Article]:
    """段階を順に試す。件数が足りた時点で止める（AC-23）。"""
    trigger = int(run.config.get("fallback.trigger_count", 5))
    found: list[Article] = []

    for stage in (stage1, stage2, stage3):
        found.extend(stage(run))
        if len(existing) + len(found) >= trigger:
            break

    return found
