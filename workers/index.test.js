// Workers の純粋関数のテスト。`node --test workers/` で走る（Node 22 の組み込み）。
// ネットワークも Slack も触らない。

import { test } from "node:test";
import assert from "node:assert/strict";

import { applySelection, eventForCron, generatingText, selectedStoryIds } from "./index.js";

function option(value) {
  return { text: { type: "mrkdwn", text: `① ${value}` }, value };
}

function checkboxBlock(category, part, values, initial = []) {
  const element = {
    type: "checkboxes",
    action_id: `stories_${category}_${part}`,
    options: values.map(option),
  };
  if (initial.length) element.initial_options = initial.map(option);
  return { type: "actions", block_id: `cat_${category}_${part}`, elements: [element] };
}

function stateValues(selectedByBlock) {
  const values = {};
  for (const [blockId, selected] of Object.entries(selectedByBlock)) {
    const [, category, part] = blockId.split("_");
    values[blockId] = {
      [`stories_${category}_${part}`]: {
        type: "checkboxes",
        selected_options: selected.map((v) => ({ value: v })),
      },
    };
  }
  return values;
}

const BLOCKS = [
  { type: "header", block_id: "head_ai", text: { type: "plain_text", text: "AI" } },
  { type: "section", block_id: "links_ai", text: { type: "mrkdwn", text: "① ②" } },
  checkboxBlock("ai", 0, ["s_a1", "s_a2"]),
  { type: "header", block_id: "head_lang", text: { type: "plain_text", text: "言語" } },
  checkboxBlock("lang", 0, ["s_l1", "s_l2", "s_l3"], ["s_l1"]),
  { type: "actions", block_id: "create", elements: [{ type: "button", action_id: "create_articles" }] },
];

function initialOf(blocks, blockId) {
  const block = blocks.find((b) => b.block_id === blockId);
  return (block.elements[0].initial_options || []).map((o) => o.value);
}

test("すべて選択: 全チェックボックスの全項目が initial_options になる", () => {
  const out = applySelection(BLOCKS, stateValues({}), { action_id: "select_all" });
  assert.deepEqual(initialOf(out, "cat_ai_0"), ["s_a1", "s_a2"]);
  assert.deepEqual(initialOf(out, "cat_lang_0"), ["s_l1", "s_l2", "s_l3"]);
  // チェックボックス以外のブロックはそのまま
  assert.deepEqual(out[0], BLOCKS[0]);
  assert.deepEqual(out[5], BLOCKS[5]);
});

test("選択を解除: initial_options が全て消える", () => {
  const values = stateValues({ cat_ai_0: ["s_a1"], cat_lang_0: ["s_l1", "s_l2"] });
  const out = applySelection(BLOCKS, values, { action_id: "clear_all" });
  assert.deepEqual(initialOf(out, "cat_ai_0"), []);
  assert.deepEqual(initialOf(out, "cat_lang_0"), []);
  assert.equal("initial_options" in out[4].elements[0], false);
});

test("区分の全選択: その区分だけ全部付き、他の区分は今の選択を保つ", () => {
  const values = stateValues({ cat_ai_0: [], cat_lang_0: ["s_l2"] });
  const out = applySelection(BLOCKS, values, { action_id: "select_category", value: "ai" });
  assert.deepEqual(initialOf(out, "cat_ai_0"), ["s_a1", "s_a2"]);
  assert.deepEqual(initialOf(out, "cat_lang_0"), ["s_l2"]);
});

test("区分の全選択: すでに全部選ばれていれば外す（押し直しで戻せる）", () => {
  const values = stateValues({ cat_ai_0: ["s_a1", "s_a2"], cat_lang_0: ["s_l3"] });
  const out = applySelection(BLOCKS, values, { action_id: "select_category", value: "ai" });
  assert.deepEqual(initialOf(out, "cat_ai_0"), []);
  assert.deepEqual(initialOf(out, "cat_lang_0"), ["s_l3"]);
});

test("区分の全選択: 区分名が前方一致で他の区分を巻き込まない", () => {
  const blocks = [checkboxBlock("ai", 0, ["s_a1"]), checkboxBlock("aiops", 0, ["s_x1"])];
  const out = applySelection(blocks, stateValues({}), { action_id: "select_category", value: "ai" });
  assert.deepEqual(initialOf(out, "cat_ai_0"), ["s_a1"]);
  assert.deepEqual(initialOf(out, "cat_aiops_0"), []);
});

test("initial_options は options のオブジェクトをそのまま使う", () => {
  const out = applySelection(BLOCKS, stateValues({}), { action_id: "select_all" });
  const element = out[2].elements[0];
  assert.deepEqual(element.initial_options, element.options);
});

test("入力のブロックを書き換えない", () => {
  const before = JSON.stringify(BLOCKS);
  applySelection(BLOCKS, stateValues({ cat_ai_0: ["s_a1"] }), { action_id: "select_all" });
  applySelection(BLOCKS, stateValues({}), { action_id: "clear_all" });
  assert.equal(JSON.stringify(BLOCKS), before);
});

test("生成中の文面に件数と目安の分数が入る", () => {
  assert.equal(generatingText(5), "⏳ 生成中（5件・目安 約3分）… 終わったらチャンネルに紙面が届きます");
  assert.match(generatingText(78), /78件・目安 約40分/);
});

test("チェック済みの story_id を state.values から取り出す", () => {
  const values = stateValues({ cat_ai_0: ["s_a2", "s_a1"], cat_lang_0: ["s_a1"] });
  assert.deepEqual(selectedStoryIds({ state: { values } }), ["s_a2", "s_a1"]);
  assert.deepEqual(selectedStoryIds({}), []);
});

test("朝の Cron は朝の処理、それ以外は記事化の自動やり直しを起動する", () => {
  assert.equal(eventForCron("40 19 * * *"), "morning");
  assert.equal(eventForCron("0 21,22,0 * * *"), "morning");
  assert.equal(eventForCron("0 3,11 * * *"), "articles-retry");
});
