#!/usr/bin/env bash
# state を origin/main の最新に載せて push する（Actions の「状態のcommit」）。
#
#   scripts/push_state.sh "<commit message>" <file>...
#
# rebase はしない。`written.json` のような「辞書の末尾に足す」ファイルは
# 前の実行の commit と必ず競合し、3回とも失敗して記録が消えた（2026-10-01）。
# 代わりに、自分が書いたファイルを退避 → origin/main に合わせる → JSON として
# 三方向で併合（scripts/merge_state.py）→ commit → push。公開済みの履歴は
# 書き換えない。
set -euo pipefail

# 公開化（2026-10-31）で、state は非公開の morning-paper-data を checkout した
# private/ で commit する。呼び出しは `cd private && ../scripts/push_state.sh ...`
here=$(cd "$(dirname "$0")" && pwd)

message="$1"; shift
files=("$@")

base=$(git rev-parse HEAD)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
mkdir -p "$work/ours" "$work/base"

for f in "${files[@]}"; do
  [ -f "$f" ] || continue
  mkdir -p "$work/ours/$(dirname "$f")" "$work/base/$(dirname "$f")"
  cp "$f" "$work/ours/$f"
  # チェックアウト時点の中身（無ければ新規ファイル）
  git show "$base:$f" > "$work/base/$f" 2>/dev/null || rm -f "$work/base/$f"
done

git config user.name "morning-paper[bot]"
git config user.email "morning-paper@users.noreply.github.com"

for attempt in 1 2 3; do
  git fetch origin main
  git reset -q --hard origin/main
  python "$here/merge_state.py" "$work/base" "$work/ours" .
  git add -A -- "${files[@]}" 2>/dev/null || git add -A -- $(for f in "${files[@]}"; do [ -f "$f" ] && echo "$f"; done)
  if git diff --cached --quiet; then
    echo "state に変更はありません"
    exit 0
  fi
  git commit -q -m "$message"
  if git push origin HEAD:main; then
    echo "push しました（${attempt}回目）"
    exit 0
  fi
  sleep 5
done

# 3回とも push できなければ失敗にする。0 で終わると、投稿済み・記事化済みの
# 記録が残らないまま「成功」に見えて、次回に重複する
echo "::error::state の push に3回失敗しました。次回の実行で記録が引き継がれません"
exit 1
