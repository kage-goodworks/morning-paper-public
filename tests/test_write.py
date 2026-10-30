"""write.py の単体テスト。

プロンプト組み立てと通知文は純粋関数。両モデルが落ちたときに**記事化を
行わない**という判断もここで固定する。
"""

from __future__ import annotations

from src import write
from tests.conftest import make_article, make_story


def target(**kwargs) -> write.WriteTarget:
    story = kwargs.pop("story", make_story(id="s_a1b2c3", title="話題のタイトル"))
    articles = kwargs.pop("articles", [
        make_article(id="a1", url="https://example.invalid/1", title="1本目",
                     published_at="2026-09-01T09:00:00+09:00",
                     body_md="1本目の本文。"),
        make_article(id="a2", url="https://example.invalid/2", title="2本目",
                     published_at="2026-09-02T09:00:00+09:00",
                     body_md="2本目の本文。"),
    ])
    return write.WriteTarget(story=story, articles=articles, **kwargs)


def prompt_for(*targets) -> str:
    return write.build_prompt(list(targets), char_range=[600, 900], hint_count=1)


def test_プロンプトに構成と字数が入る():
    text = prompt_for(target())
    assert "600〜900字" in text
    # 部品化した構成（LEAD / RECAP / POINTS / BODY / WHY / TERMS）
    for name in ("LEAD:", "RECAP:", "POINTS:", "BODY:", "WHY:", "TERMS:"):
        assert name in text
    assert "である調" in text
    assert "自分との関係" in text


def test_読み手のプロフィールが入り_自分との関係は具体性を求める():
    text = write.build_prompt([target()], char_range=[600, 900], hint_count=1,
                              profile="個人開発者。\n- Python と Slack を使う")
    assert "--- PROFILE（読み手） ---" in text
    assert "- Python と Slack を使う" in text
    # ありきたりな一般論を禁じ、開発への取り込み・将来・波及の3点を求める
    assert "一般論は書かない" in text
    assert "取り込める" in text and "将来" in text and "波及" in text
    # プロフィールが無ければ PROFILE の節は出ない
    assert "PROFILE（読み手）" not in prompt_for(target())


def test_複数記事は時系列が分かるよう公開日時も渡す():
    text = prompt_for(target())
    assert "2026-09-01T09:00:00+09:00" in text
    assert "2026-09-02T09:00:00+09:00" in text


def test_続報では扱い済みの記事に印が付く():
    """前回書いた文章は保存していないが、どの記事まで書いたかは分かる。"""
    text = prompt_for(target(already={"a1"}))

    assert "[扱い済み] [2026-09-01T09:00:00+09:00] 1本目" in text
    assert "[新規] [2026-09-02T09:00:00+09:00] 2本目" in text
    assert "STATUS: 続報" in text
    assert "その内容は RECAP にまとめ" in text


def test_初回は続報の印が付かない():
    text = prompt_for(target())
    assert "STATUS: 続報" not in text
    # 指示文には常に語が出るので、記事の一覧部分だけを見る
    listing = text.split("--- 話題 1 ---", 1)[1]
    assert "[扱い済み]" not in listing
    assert listing.count("[新規]") == 2


def test_反応の創作と個人の投稿を禁じる指示が入る():
    text = prompt_for(target())
    assert "反応を創作しないでください" in text
    assert "個人のアカウント名と投稿本文は書かないこと" in text


def test_記事数がCOUNTとして渡る():
    text = prompt_for(target(), target(story=make_story(id="s_2")))
    assert "COUNT: 2" in text


def test_両モデルが落ちたら記事を作らない():
    """短い記事でごまかさない。話題は消化されず、あとから押し直せる。"""
    outcome = write.WriteOutcome(ok=False, kind=write.KIND_OTHER)
    assert outcome.results == []


def test_クレジット切れの通知は他のエラーと区別される():
    credit = write.failure_notice(
        write.WriteOutcome(ok=False, kind=write.KIND_CREDIT), count=5)
    other = write.failure_notice(
        write.WriteOutcome(ok=False, kind=write.KIND_OTHER), count=5)

    assert "クレジットがなくなったことにより記事が作れませんでした" in credit
    assert "利用枠が尽きています" in credit
    assert "クレジット" not in other
    assert "AIが応答しませんでした" in other


def test_失敗の通知はチェックが残ることを伝える():
    text = write.failure_notice(
        write.WriteOutcome(ok=False, kind=write.KIND_OTHER), count=3)
    assert "対象の3件はチェックが付いたまま残しています" in text
    assert "もう一度ボタンを押せます" in text
    assert "元記事のURLは続けて投稿します" in text


def test_降格したことが1文で分かる():
    text = write.degraded_notice(
        write.WriteOutcome(ok=True, degraded=True, model="gemini"),
        primary="claude")
    assert text == "この回の記事はgeminiが書いています（claudeが応答しなかったため）"


def test_降格していなければ通知を出さない():
    text = write.degraded_notice(
        write.WriteOutcome(ok=True, degraded=False, model="claude"),
        primary="claude")
    assert text == ""


def test_出典URLは執筆の成否に関わらず取り出せる():
    t = target()
    assert t.sources == ["https://example.invalid/1", "https://example.invalid/2"]
    assert t.published_dates == ["2026-09-01", "2026-09-02"]
