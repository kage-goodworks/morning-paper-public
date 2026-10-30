"""blocks.py の単体テスト。AC-3 / AC-4 / AC-5 / AC-6 / AC-26 / AC-27 / AC-28。"""

from __future__ import annotations

import json
from pathlib import Path

from src import blocks
from tests.conftest import make_story

LABELS = {"ai": "AI・機械学習", "lang": "言語・フレームワーク"}
FIXTURES = Path(__file__).parent / "fixtures"


def stories(count: int, category: str = "ai", articles: int = 1):
    return [make_story(id=f"s_{i:06d}", title=f"話題{i}", category=category,
                       status="listed",
                       article_ids=[f"a{j}" for j in range(articles)],
                       latest_url=f"https://example.invalid/{i}")
            for i in range(count)]


def checkbox_blocks(message):
    return [b for b in message
            if b["type"] == "actions" and b["block_id"].startswith("cat_")]


def flatten(messages):
    """複数メッセージを1つに畳む。中身の検証用。"""
    return [b for m in messages for b in m]


def test_ac3_話題25件が10件ごとに3要素へ分割される():
    messages = blocks.build_list(date_str="2026-09-02",
                                 shown={"ai": stories(25)},
                                 labels=LABELS, status=[])
    elements = checkbox_blocks(messages[0])

    assert len(elements) == 3
    assert [len(e["elements"][0]["options"]) for e in elements] == [10, 10, 5]


def test_ac5_記事1件の話題はn件が付かない():
    label = blocks.story_label(stories(1, articles=1)[0])
    assert label == "<https://example.invalid/0|話題0>"
    assert "件）" not in label


def test_ac6_記事3件の話題も最新記事URLだけでn件は付かない():
    """「（n件）」は不要（本人の要望、2026-10-01）。"""
    label = blocks.story_label(stories(1, articles=3)[0])
    assert label == "<https://example.invalid/0|話題0>"
    assert "件" not in label


def heading_of(flat, label_part: str):
    """カテゴリ見出し（header）の位置を返す。"""
    return next(i for i, b in enumerate(flat)
                if b["type"] == "header" and label_part in b["text"]["text"])


def links_of(flat, label_part: str):
    """カテゴリ見出しの直下にある番号リンクの section。"""
    return flat[heading_of(flat, label_part) + 1]


def test_カテゴリ見出しは大きな文字のheaderで_直下に番号リンクが並ぶ():
    messages = blocks.build_list(date_str="2026-09-02", shown={"ai": stories(3)},
                                 labels=LABELS, status=[])
    flat = flatten(messages)
    i = heading_of(flat, "AI・機械学習")
    assert flat[i]["type"] == "header" and flat[i]["block_id"] == "head_ai"
    links = flat[i + 1]
    assert links["type"] == "section" and links["block_id"] == "links_ai"
    assert links["text"]["text"].startswith("↗ ")
    assert flat[i + 2]["type"] == "actions"       # その下がチェックボックス


def test_番号は丸数字で題名と切り離される():
    label = blocks.option_label(stories(1)[0], 3)
    assert label == "③ 話題0"
    assert blocks.circled(12) == "⑫" and blocks.circled(50) == "㊿"
    assert blocks.circled(51) == "51."           # 丸数字が無ければ従来の形


def test_一括選択のボタンが付く():
    messages = blocks.build_list(date_str="2026-09-02",
                                 shown={"ai": stories(2), "lang": stories(2, "lang")},
                                 labels=LABELS, status=[])
    flat = flatten(messages)
    tail = next(b for b in flat if b.get("block_id") == "create")
    ids = [e["action_id"] for e in tail["elements"]]
    assert ids == ["create_articles", "select_all", "clear_all"]
    # 区分ごとの全選択は番号リンクの右端。value は区分のキー
    for category in ("ai", "lang"):
        links = links_of(flat, LABELS[category])
        assert links["accessory"]["action_id"] == "select_category"
        assert links["accessory"]["value"] == category


def test_全件が済みの区分には区分の全選択ボタンを付けない():
    update = flatten(blocks.build_update(
        date_str="2026-09-02", shown={"ai": stories(2)}, labels=LABELS,
        status=[], written={"s_000000": "u1", "s_000001": "u2"}))
    assert "accessory" not in links_of(update, "AI")


def test_チェックボックスのvalueはstory_id():
    messages = blocks.build_list(date_str="2026-09-02", shown={"ai": stories(3)},
                                 labels=LABELS, status=[])
    options = checkbox_blocks(messages[0])[0]["elements"][0]["options"]
    assert [o["value"] for o in options] == ["s_000000", "s_000001", "s_000002"]


def test_ac4_ブロック上限を超えたら2通目へ分割されボタンは最後だけに付く():
    # 仕様の「話題80件」ではこの構造上45ブロックに届かないため、
    # 分割機構そのものを上限を下げて検証する（docs/PROGRESS.md の Q7 を参照）。
    shown = {"ai": stories(30, "ai"), "lang": stories(30, "lang")}
    messages = blocks.build_list(date_str="2026-09-02", shown=shown,
                                 labels=LABELS, status=[],
                                 blocks_per_message=6)

    assert len(messages) > 1
    assert all(len(m) <= 6 for m in messages)

    has_button = [any(b.get("block_id") == "create" for b in m) for m in messages]
    assert has_button[-1] is True
    assert not any(has_button[:-1])


def test_ac4_話題80件は45ブロックに届かず1通に収まる():
    """仕様の入力そのものを記録として残す。分割は起きない。"""
    shown = {"ai": stories(40, "ai"), "lang": stories(40, "lang")}
    messages = blocks.build_list(date_str="2026-09-02", shown=shown,
                                 labels=LABELS, status=[],
                                 blocks_per_message=45)
    assert len(messages) == 1
    assert len(messages[0]) < 45


def test_ac28_記事化済みは元の位置で文字になり_押せない():
    listed = stories(4)
    written = {"s_000001": "https://slack.invalid/t2"}   # 2番だけ済み

    update = flatten(blocks.build_update(
        date_str="2026-09-02", shown={"ai": listed}, labels=LABELS,
        status=[], written=written))

    # 見出し → ① のチェックボックス → ✅ ② の文字 → ③④ のチェックボックス
    body = [b for b in update if b["type"] in ("actions", "context", "section")]
    links = next(i for i, b in enumerate(body)
                 if b["type"] == "section" and b.get("block_id") == "links_ai")
    seq = body[links + 1: links + 4]
    assert [b["type"] for b in seq] == ["actions", "context", "actions"]
    assert [o["value"] for o in seq[0]["elements"][0]["options"]] == ["s_000000"]
    assert seq[1]["elements"][0]["text"] == "✅ ② 話題1"
    assert [o["value"] for o in seq[2]["elements"][0]["options"]] == ["s_000002", "s_000003"]
    assert [o["text"]["text"] for o in seq[2]["elements"][0]["options"]] == ["③ 話題2", "④ 話題3"]

    # 済みはどのチェックボックスにも入っていない
    all_values = [o["value"] for b in checkbox_blocks(update)
                  for o in b["elements"][0]["options"]]
    assert "s_000001" not in all_values

    # 見出しの番号リンクに紙面（スレッド）へのリンクが添えられる
    text = body[links]["text"]["text"]
    assert "<https://example.invalid/1|②><https://slack.invalid/t2|📰>" in text
    assert "<https://example.invalid/2|③>" in text and "③><" not in text


def test_ac28_全話題が済みなら文字だけになりボタンも消える():
    listed = stories(2)
    written = {"s_000000": "u1", "s_000001": "u2"}
    update = flatten(blocks.build_update(
        date_str="2026-09-02", shown={"ai": listed}, labels=LABELS,
        status=[], written=written))
    assert checkbox_blocks(update) == []
    done = [b for b in update if b["type"] == "context" and "✅" in b["elements"][0]["text"]]
    assert len(done) == 1 and done[0]["elements"][0]["text"] == "✅ ① 話題0\n✅ ② 話題1"
    assert not any(b["type"] == "actions" for b in update)   # 「記事を作成」も無い


def test_記事化済みのpermalinkが無ければ壊れたSlackリンクを作らない():
    update = flatten(blocks.build_update(
        date_str="2026-09-02", shown={"ai": stories(1)}, labels=LABELS,
        status=[], written={"s_000000": ""}))
    text = links_of(update, "AI")["text"]["text"]
    assert "<https://example.invalid/0|①>" in text
    assert "<|" not in text and "📰" not in text


def test_ac26_保留になった話題はチェックが付いたまま復元される():
    listed = stories(4)
    update = flatten(blocks.build_update(
        date_str="2026-09-02", shown={"ai": listed}, labels=LABELS, status=[],
        written={"s_000000": "u1"}, pending={"s_000003"}))

    initial = [o["value"] for b in checkbox_blocks(update)
               for o in b["elements"][0].get("initial_options", [])]
    assert initial == ["s_000003"]


def test_ac26_ボタン押下ペイロードからチェック済みstory_idを取り出す():
    payload = json.loads(
        (FIXTURES / "slack" / "block_actions.json").read_text(encoding="utf-8"))
    ids = blocks.selected_story_ids(payload)
    assert len(ids) == 8
    assert ids[0] == "s_000001"


def test_選択が空のペイロードは空リストを返す():
    assert blocks.selected_story_ids({"state": {"values": {}}}) == []
    assert blocks.selected_story_ids({}) == []


def test_取得状況が冒頭のcontextへ出る():
    messages = blocks.build_list(date_str="2026-09-02", shown={"ai": stories(1)},
                                 labels=LABELS,
                                 status=["株価: 取得失敗", "AI: 18件中12件を表示"])
    context = messages[0][1]
    assert context["type"] == "context"
    assert "株価: 取得失敗" in context["elements"][0]["text"]


# --------------------------------------------------------------------------
# 分割の設計（Q7）
# --------------------------------------------------------------------------

def test_分割してもカテゴリ見出しとチェックボックスが離れない():
    """見出しだけが前のメッセージに残ると、どのカテゴリか分からなくなる。"""
    shown = {"ai": stories(30, "ai"), "lang": stories(30, "lang")}
    messages = blocks.build_list(date_str="2026-09-02", shown=shown,
                                 labels=LABELS, status=[],
                                 blocks_per_message=8)

    for message in messages:
        for index, block in enumerate(message):
            if block["type"] != "header" or not block.get("block_id", "").startswith("head_"):
                continue
            rest = message[index + 1:]
            assert len(rest) >= 2, "見出しの直後に中身が無い"
            assert rest[0]["type"] == "section"          # 番号リンク
            assert rest[1]["type"] == "actions", "見出しの直後に中身が無い"


def test_1カテゴリで上限を超える場合は見出しを繰り返す():
    shown = {"ai": stories(50, "ai")}
    # 冒頭3ブロック＋見出し2ブロック＋チェックボックス1つ＝6で割れる最小
    messages = blocks.build_list(date_str="2026-09-02", shown=shown,
                                 labels=LABELS, status=[],
                                 blocks_per_message=6)
    headings = [b["text"]["text"] for m in messages for b in m
                if b["type"] == "header" and b["text"]["text"].startswith("AI")]
    assert len(headings) > 1
    assert any("（続き）" in h for h in headings)
    # 繰り返した見出しの block_id は重複しない（Slack は1通の中で一意を求める）
    ids = [b["block_id"] for m in messages for b in m if "block_id" in b]
    for m in messages:
        own = [b["block_id"] for b in m if "block_id" in b]
        assert len(own) == len(set(own)), own
    assert ids


def test_chat_updateは切り捨てず分割する():
    """上限で切ると話題が黙って消える。"""
    shown = {"ai": stories(30, "ai"), "lang": stories(30, "lang")}
    messages = blocks.build_update(date_str="2026-09-02", shown=shown,
                                   labels=LABELS, status=[], written={},
                                   blocks_per_message=6)

    assert len(messages) > 1
    assert all(len(m) <= 6 for m in messages)

    values = [o["value"] for b in flatten(messages)
              if b["type"] == "actions" and b["block_id"].startswith("cat_")
              for o in b["elements"][0]["options"]]
    assert len(values) == 60


def test_更新後に不要になったメッセージの差し替えが作れる():
    payload = blocks.build_exhausted("2026-09-02", "この分の話題は記事化済みです。")
    assert payload[0]["type"] == "header"
    assert "記事化済み" in payload[1]["elements"][0]["text"]


# --- Slack の文字数上限（invalid_blocks の再発防止） ---

def _long_story(n: int = 0):
    from tests.conftest import make_story
    return make_story(
        id=f"s_long{n}", title="と" * 200, article_ids=["a", "b", "c"],
        latest_url="https://example.invalid/" + "p" * 150)


def test_チェックボックスの文字列は75文字に収まりリンクを含まない():
    label = blocks.option_label(_long_story(), 12)
    assert len(label) <= blocks.OPTION_TEXT_MAX
    assert label.startswith("⑫ ")
    assert "件" not in label
    assert "<" not in label


def test_番号リンクはカテゴリ見出しの横に1行で並ぶ():
    messages = blocks.build_list(date_str="2026-09-02", shown={"ai": stories(12)},
                                 labels=LABELS, status=[])
    flat = [b for m in messages for b in m]
    text = links_of(flat, "AI")["text"]["text"]
    assert "<https://example.invalid/0|①>" in text
    assert "<https://example.invalid/11|⑫>" in text       # 10件で区切らない
    # チェックボックスの合間にリンクの context は挟まらない
    actions = [i for i, b in enumerate(flat)
               if b["type"] == "actions" and b.get("block_id", "").startswith("cat_")]
    assert len(actions) == 2
    assert actions[1] == actions[0] + 1     # 2つの要素は隣り合う


def test_どのブロックもSlackの上限を超えない():
    long_status = ["x" * 300 for _ in range(20)]
    shown = {c: [_long_story(i) for i in range(12)] for c in LABELS}
    messages = blocks.build_list(date_str="2026-09-02", shown=shown,
                                 labels=LABELS, status=long_status)
    for m in messages:
        assert len(m) <= 45
        for b in m:
            if b["type"] == "header":
                assert len(b["text"]["text"]) <= blocks.HEADER_TEXT_MAX
            if b["type"] == "context":
                for e in b["elements"]:
                    assert len(e["text"]) <= blocks.CONTEXT_TEXT_MAX
            if b["type"] == "section" and "text" in b:
                assert len(b["text"]["text"]) <= blocks.SECTION_TEXT_MAX
            if b["type"] == "actions":
                for e in b["elements"]:
                    for o in e.get("options", []):
                        assert len(o["text"]["text"]) <= blocks.OPTION_TEXT_MAX
                        assert len(o["value"]) <= 75


def test_記事ごとの失敗は取得状況に載らず集計だけ載る(tmp_path):
    from src import extract
    from src.clients.factory import build_mock
    from src.config import load as load_config
    from src.pipeline import Run
    from src.retry import NO_SLEEP
    from tests.conftest import make_article

    cfg = load_config()
    clients = build_mock(scenario="normal", day=1, out_dir=tmp_path)
    run = Run(config=cfg, clients=clients, date_str="2026-09-01",
              out_dir=tmp_path, data_dir=tmp_path, sleep=NO_SLEEP)

    class Broken:
        name = "broken"

        def extract(self, article):
            raise RuntimeError("HTTP 403 Forbidden")

    run.clients.extractors[:] = [Broken()]
    extract.extract_all(run, [make_article(id=f"z{i}", url=f"https://e.test/{i}",
                                           title=str(i)) for i in range(5)])
    assert not any("403" in line for line in run.status)
    assert any("5件が4段とも失敗" in line for line in run.status)
