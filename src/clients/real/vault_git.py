"""Open Vault リポジトリへの commit と push（deploy key）。

[[AI_GUIDE]] の自動化ルールに従う。

- commitメッセージの先頭に `auto:` を付け、人手の変更と区別できるようにする
- **rebase、force push、履歴の書き換えはしない**
- pushが競合した場合は中止し、失敗した事実を通知先へ明示して次回実行へ持ち越す
- 書き込むのは宣言したパスだけ。範囲外は `vault.py` の関数が制限している

Actions のrunnerは毎回まっさらなので、実行のたびに浅いcloneを作る。
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from pathlib import Path

DEFAULT_REPO = "git@github.com:kage-goodworks/Open-Vault.git"
BRANCH = "main"

#: commit に載せる名義。runner はまっさらで git の user.name / user.email が
#: 無く、無いと commit 自体が「Author identity unknown」で落ちる。
COMMITTER = ("morning-paper[bot]", "morning-paper@users.noreply.github.com")
AUTHOR_NAME = "morning-paper[bot]"
AUTHOR_EMAIL = "morning-paper@users.noreply.github.com"

#: 書き込みを許す接頭辞。これ以外へは書かない（Overview「Vaultへ書き込む範囲」）。
ALLOWED_PREFIXES = ("50_Logs/News/", "40_Knowledge/News/",
                    "40_Knowledge/News-Review/",
                    # 記事化した中身を主題ごとに積む知識ノート（2026-10-15 追加）
                    "40_Knowledge/Tech/")


class VaultError(RuntimeError):
    pass


class RealNoteStore:
    """`NoteStore`。"""

    def __init__(self, repo: str | None = None, workdir: Path | None = None) -> None:
        self.repo = repo or os.environ.get("VAULT_REPO") or DEFAULT_REPO
        self._workdir = workdir
        self._root: Path | None = None
        self._key_file: Path | None = None
        self._dirty = False

    # -- 下回り -----------------------------------------------------------

    def _git(self, *args: str, cwd: Path | None = None) -> str:
        env = dict(os.environ)
        if self._key_file is not None:
            env["GIT_SSH_COMMAND"] = (
                f'ssh -i "{self._key_file}" -o IdentitiesOnly=yes '
                "-o StrictHostKeyChecking=accept-new")
        result = subprocess.run(
            ["git", *args], cwd=str(cwd or self._root), env=env,
            capture_output=True, text=True, encoding="utf-8", timeout=180)
        if result.returncode != 0:
            raise VaultError(
                f"git {' '.join(args[:2])}: {result.stderr.strip()[:200]}")
        return result.stdout

    def _write_key(self) -> None:
        """deploy key を一時ファイルへ置く。**値はログに出さない。**"""
        key = os.environ.get("VAULT_DEPLOY_KEY")
        if not key:
            raise VaultError("VAULT_DEPLOY_KEY が設定されていません")
        handle = tempfile.NamedTemporaryFile(
            "w", suffix=".key", delete=False, encoding="utf-8", newline="\n")
        handle.write(key if key.endswith("\n") else key + "\n")
        handle.close()
        path = Path(handle.name)
        os.chmod(path, 0o600)
        self._key_file = path

    def _ensure_clone(self) -> Path:
        if self._root is not None:
            return self._root
        self._write_key()
        base = self._workdir or Path(tempfile.mkdtemp(prefix="vault-"))
        base.mkdir(parents=True, exist_ok=True)
        root = base / "Open-Vault"
        # clone に成功するまで _root を確定させない。先に入れると、失敗後の
        # 再試行が clone を飛ばして「ノートなし」を返してしまう。
        if not root.exists():
            self._git("clone", "--depth", "1", "--branch", BRANCH,
                      self.repo, str(root), cwd=base)
        self._git("config", "user.name", AUTHOR_NAME, cwd=root)
        self._git("config", "user.email", AUTHOR_EMAIL, cwd=root)
        self._root = root
        return self._root

    @staticmethod
    def _check_path(rel_path: str) -> None:
        normalized = (rel_path or "").replace("\\", "/")
        if ".." in normalized.split("/") or normalized.startswith("/"):
            raise VaultError(f"許可されないパス: {rel_path}")
        if not normalized.startswith(ALLOWED_PREFIXES):
            raise VaultError(f"書き込み範囲の外: {rel_path}")

    # -- 読み書き ---------------------------------------------------------

    def write(self, rel_path: str, content: str) -> None:
        self._check_path(rel_path)
        root = self._ensure_clone()
        path = root / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        self._dirty = True

    def read(self, rel_path: str) -> str | None:
        self._check_path(rel_path)
        path = self._ensure_clone() / rel_path
        if not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def commit_push(self, message: str) -> None:
        if not self._dirty:
            return
        root = self._ensure_clone()

        if not message.startswith("auto:"):
            message = "auto: " + message

        # まだ無いディレクトリ（週次レビュー前の News-Review/ など）を渡すと
        # git add は「pathspec が一致しない」で落ちる。あるものだけ渡す。
        present = [prefix for prefix in ALLOWED_PREFIXES
                   if (root / prefix).exists()]
        if not present:
            self._dirty = False
            return
        self._git("add", "--", *present)
        status = self._git("status", "--porcelain")
        if not status.strip():
            self._dirty = False
            return

        self._git("-c", f"user.name={COMMITTER[0]}",
                  "-c", f"user.email={COMMITTER[1]}",
                  "commit", "-m", message)
        try:
            self._git("push", "origin", f"HEAD:{BRANCH}")
        except VaultError as exc:
            # **rebase も force push もしない。**commit だけ取り消して次回へ
            # 持ち越す。`--hard` にすると作業中の変更（書いたノート）まで消え、
            # 再試行が「変更なし」で正常終了してしまう（保存していないのに
            # 保存済み扱いになる）。`--soft` で変更は残す。
            self._git("reset", "--soft", "HEAD~1")
            raise VaultError(
                f"pushが競合したため中止しました（次回へ持ち越します）: {exc}") from exc
        self._dirty = False

    def cleanup(self) -> None:
        if self._key_file is not None and self._key_file.exists():
            self._key_file.unlink()
        self._key_file = None
        if self._workdir is None and self._root is not None:
            shutil.rmtree(self._root.parent, ignore_errors=True)
        self._root = None
