"""実行の途中で Vault が進んでも保存できること（本物の git で確かめる）。"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from src.clients.real.vault_git import RealNoteStore, VaultError

DAILY = "50_Logs/News/2026-11-05.md"
TOPIC = "40_Knowledge/Tech/ai/生成AI.md"


def git(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True,
                          text=True, encoding="utf-8").stdout


def commit_as_human(clone: Path, rel: str, text: str) -> None:
    path = clone / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    git("add", "-A", cwd=clone)
    git("-c", "user.name=h", "-c", "user.email=h@example.com",
        "commit", "-m", "human", cwd=clone)
    git("push", "origin", "HEAD:main", cwd=clone)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """(本体の裸リポジトリ, 人の写し, 自動処理の保存先)。"""
    origin = tmp_path / "origin.git"
    git("init", "--bare", "-b", "main", str(origin), cwd=tmp_path)
    human = tmp_path / "human"
    git("clone", origin.as_uri(), str(human), cwd=tmp_path)
    git("checkout", "-b", "main", cwd=human)
    commit_as_human(human, TOPIC, "最初\n")
    store = RealNoteStore(repo=origin.as_uri(), workdir=tmp_path / "work")
    monkeypatch.setattr(store, "_write_key", lambda: None)
    return origin, human, store


def latest(human: Path) -> None:
    git("pull", "--ff-only", "origin", "main", cwd=human)


def test_別のファイルなら途中で進んでも保存でき_人の変更も残る(vault):
    _, human, store = vault
    store.read(TOPIC)                                 # 朝の開始時に clone
    store.write(DAILY, "朝刊\n")
    commit_as_human(human, "20_Projects/メモ.md", "人の変更\n")

    store.commit_push("auto: news 2026-11-05")

    latest(human)
    assert (human / DAILY).read_text(encoding="utf-8") == "朝刊\n"
    assert (human / "20_Projects/メモ.md").read_text(encoding="utf-8") == "人の変更\n"
    log = git("log", "--format=%s", cwd=human).splitlines()
    assert log[:2] == ["auto: news 2026-11-05", "human"]   # 履歴は積み上がるだけ


def test_同じファイルが更新されていたら上書きせず中止する(vault):
    _, human, store = vault
    store.read(TOPIC)
    store.write(TOPIC, "自動処理の内容\n")
    commit_as_human(human, TOPIC, "人の変更\n")      # 日本語のパスでも見分ける

    with pytest.raises(VaultError, match="同じノート"):
        store.commit_push("auto: knowledge 2026-11-05")

    latest(human)
    assert (human / TOPIC).read_text(encoding="utf-8") == "人の変更\n"
    assert git("log", "-1", "--format=%s", cwd=human).strip() == "human"


def test_最新を取ったあとにpushで負けたら中止し_再試行で保存できる(vault, monkeypatch):
    _, human, store = vault
    store.read(TOPIC)
    store.write(DAILY, "朝刊\n")
    real_git = store._git

    def lose_once(*args, **kwargs):
        if args[0] == "push" and not lost:
            lost.append(True)
            commit_as_human(human, "20_Projects/メモ.md", "直前の変更\n")
        return real_git(*args, **kwargs)

    lost: list[bool] = []
    monkeypatch.setattr(store, "_git", lose_once)
    with pytest.raises(VaultError, match="pushが競合"):
        store.commit_push("auto: news 2026-11-05")

    store.commit_push("auto: news 2026-11-05")
    latest(human)
    assert (human / DAILY).exists() and (human / "20_Projects/メモ.md").exists()


def test_保存のあとは取り直せて_続けて知識も積める(vault):
    _, human, store = vault
    store.read(TOPIC)
    store.write(DAILY, "朝刊\n")
    store.commit_push("auto: news 2026-11-05")
    commit_as_human_after = "人の追記\n"
    latest(human)
    commit_as_human(human, TOPIC, commit_as_human_after)

    store.refresh()
    assert store.read(TOPIC) == commit_as_human_after
    store.write(TOPIC, commit_as_human_after + "知識\n")
    store.commit_push("auto: knowledge 2026-11-05")

    latest(human)
    assert (human / TOPIC).read_text(encoding="utf-8").endswith("知識\n")
