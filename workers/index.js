/**
 * Cloudflare Workers —— Slack からのボタン押下を受けて GitHub Actions を起動する中継。
 *
 * 状態を持たない。受けて、検証して、渡すだけ（Requirements「対象外」）。
 *
 * 秘密は Workers の環境変数にのみ置く（NFR-2）:
 *   SLACK_SIGNING_SECRET  Slack からのリクエスト検証
 *   GH_DISPATCH_TOKEN     repository_dispatch（repo スコープのみ）
 *   GH_REPO               "owner/morning-paper"
 *
 * Slack は3秒以内の応答を求めるため、検証と ack だけを同期で行い、
 * dispatch は waitUntil で後ろへ回す。
 *
 * あわせて**朝の処理の時計**も担う（scheduled）。GitHub Actions の schedule は
 * 混雑時間帯に数時間遅れることがあり（2026-09-16 は 04:40 の予定が 07:25）、
 * 「05:00 に届く」を守れない。Cloudflare の Cron Trigger は分単位で正確なので、
 * ここから repository_dispatch で起動する。GitHub 側の schedule は 05:30 の
 * 保険として残し、その日の一覧がすでに出ていれば何もしない。
 *
 * **一括選択**（2026-10-01）: 「すべて選択」「選択を解除」「この区分を全選択」の
 * ボタンもここで受ける。Slack のチェックボックスは外から状態を変えられない
 * ため、同じ一覧を `initial_options` を付け替えて書き戻す（replace_original）。
 * GitHub は起動しない。組み立ては `applySelection`（純粋関数）。
 */

const ACTION_ID = "create_articles";
const SELECT_ALL = "select_all";
const CLEAR_ALL = "clear_all";
const SELECT_CATEGORY = "select_category";
const SELECTION_ACTIONS = [SELECT_ALL, CLEAR_ALL, SELECT_CATEGORY];
const MAX_SKEW_SECONDS = 60 * 5;

//: 5件あたりの執筆時間の目安（分）。「生成中」の表示に添える
const MINUTES_PER_BATCH = 2.5;
const BATCH_SIZE = 5;

//: 朝の処理の時計（UTC）。19:40 = 4:40。21:00 / 22:00 / 0:00 = 6:00 / 7:00 / 9:00（Asia/Tokyo）は
//: 見張り（4:40 が何かで動かなかった日の保険）。今日の一覧がもうあれば
//: Actions 側の gate が数秒で終わる
// Cloudflare の無料枠は Cron 5本まで。時刻をまとめた式にして本数を抑える
const MORNING_CRONS = ["40 19 * * *", "0 21,22,0 * * *"];

/** Cron の式から起動するイベントを決める。純粋関数。 */
export function eventForCron(cron) {
  return MORNING_CRONS.includes(cron) ? "morning" : "articles-retry";
}

export default {
  async scheduled(event, env, ctx) {
    ctx.waitUntil(dispatchEvent(env, eventForCron(event.cron),
                                { trigger: "cloudflare-cron", cron: event.cron }));
  },

  async fetch(request, env, ctx) {
    if (request.method !== "POST") {
      return new Response("method not allowed", { status: 405 });
    }

    const raw = await request.text();
    const timestamp = request.headers.get("x-slack-request-timestamp") || "";
    const signature = request.headers.get("x-slack-signature") || "";

    // 秘密は登録時に末尾へ改行が混ざることがある（シェルのパイプ経由など）。
    // 1文字違えば署名は別物になるので、前後の空白と改行を落としてから使う。
    const signingSecret = (env.SLACK_SIGNING_SECRET || "").trim();

    if (!(await verifySignature(signingSecret, timestamp, signature, raw))) {
      // 検証に落ちた理由は返さない。ログには**形だけ**残す（値は出さない）。
      // 秘密が束ねられていない・時計のずれ・本文の欠落を切り分けるため。
      console.log(JSON.stringify({
        event: "signature_rejected",
        hasSecret: Boolean(signingSecret),
        secretLength: signingSecret.length,
        ageSeconds: Math.abs(Math.floor(Date.now() / 1000) - Number(timestamp)),
        signaturePrefix: signature.slice(0, 3),
        signatureLength: signature.length,
        bodyLength: raw.length,
        contentType: request.headers.get("content-type") || "",
      }));
      return new Response("unauthorized", { status: 401 });
    }

    const payload = parsePayload(raw);
    if (!payload) {
      return new Response("bad request", { status: 400 });
    }

    const action = (payload.actions || [])[0] || {};

    // 一括選択。一覧を書き戻すだけで、GitHub は起動しない
    if (SELECTION_ACTIONS.includes(action.action_id)) {
      ctx.waitUntil(reselect(payload, action));
      return new Response("", { status: 200 });
    }

    // チェックのたびに block_actions が飛ぶ。ボタン以外は 200 を返して無視する。
    if (action.action_id !== ACTION_ID) {
      return new Response("", { status: 200 });
    }

    // ボタン押下（block_actions）への HTTP 応答本文は Slack に表示されない。
    // 本人にだけ見えるメッセージは response_url へ別途送る。
    const storyIds = selectedStoryIds(payload);
    if (storyIds.length === 0) {
      ctx.waitUntil(ephemeral(payload,
        "記事にする話題が選ばれていません。チェックを付けてから押してください。"));
      return new Response("", { status: 200 });
    }

    ctx.waitUntil(dispatchAndAck(env, payload, storyIds));
    return new Response("", { status: 200 });
  },
};

// --------------------------------------------------------------------------
// 一括選択
// --------------------------------------------------------------------------

async function reselect(payload, action) {
  const blocks = (payload.message || {}).blocks;
  if (!Array.isArray(blocks)) return;
  const values = ((payload || {}).state || {}).values || {};
  try {
    await replaceOriginal(payload, applySelection(blocks, values, action));
  } catch (err) {
    console.log(JSON.stringify({ event: "reselect_failed", action: action.action_id,
                                 error: (err && err.name) || "error" }));
  }
}

/**
 * 一覧のブロックに選択状態を書き込む。純粋関数。
 *
 * - `select_all`      全チェックボックスの全項目を選択
 * - `clear_all`       全項目の選択を外す
 * - `select_category` その区分（block_id が `cat_<区分>_n`）の全項目を選択。
 *                     すでに全部選ばれていれば外す（押し直しで戻せる）。
 *                     他の区分は、いま画面で選ばれている状態を保つ
 *
 * Slack はメッセージを書き換えるとチェックボックスを `initial_options` の
 * 状態に戻す。そこで、望む選択を `initial_options` として組み立てる。
 * 項目のオブジェクトは `options` のものをそのまま使う（一致しないと
 * Slack が invalid_blocks を返す）。
 */
export function applySelection(blocks, stateValues, action) {
  const kind = (action || {}).action_id;
  const selected = new Set(selectedStoryIds({ state: { values: stateValues || {} } }));

  if (kind === SELECT_CATEGORY) {
    const category = String((action || {}).value || "");
    const inCategory = blocks
      .filter((block) => isCategoryCheckbox(block, category))
      .flatMap(optionsOf);
    const allChosen = inCategory.length > 0 &&
      inCategory.every((option) => selected.has(option.value));
    for (const option of inCategory) {
      if (allChosen) selected.delete(option.value);
      else selected.add(option.value);
    }
  }

  return blocks.map((block) => {
    if (!isCheckboxBlock(block)) return block;
    const elements = block.elements.map((element) => {
      if (!element || element.type !== "checkboxes") return element;
      const options = element.options || [];
      let chosen;
      if (kind === SELECT_ALL) chosen = options;
      else if (kind === CLEAR_ALL) chosen = [];
      else chosen = options.filter((option) => selected.has(option.value));
      const next = { ...element };
      delete next.initial_options;
      if (chosen.length > 0) next.initial_options = chosen;
      return next;
    });
    return { ...block, elements };
  });
}

function isCheckboxBlock(block) {
  return Boolean(block) && block.type === "actions" &&
    Array.isArray(block.elements) &&
    block.elements.some((el) => el && el.type === "checkboxes");
}

function isCategoryCheckbox(block, category) {
  return isCheckboxBlock(block) && category !== "" &&
    String(block.block_id || "").startsWith(`cat_${category}_`);
}

function optionsOf(block) {
  return (block.elements || [])
    .filter((el) => el && el.type === "checkboxes")
    .flatMap((el) => el.options || []);
}

// --------------------------------------------------------------------------
// 記事化の起動
// --------------------------------------------------------------------------

async function dispatchAndAck(env, payload, storyIds) {
  // 押した瞬間に「記事を作成」ボタンを「⏳ 生成中」に置き換える。
  // 進捗の通知は出さない（本人の要望）。ボタンが消えるので二度押しも
  // できない。記事化の終わりに一覧が書き換えられ、ボタンは戻る。
  // 表示の切り替えは失敗しても構わない（本命は GitHub の起動）。ここで
  // 例外が出ても起動へ進めるよう、独立に囲む
  try {
    await markGenerating(payload, storyIds.length);
  } catch (err) {
    console.log(JSON.stringify({ event: "mark_generating_failed",
                                 error: (err && err.name) || "error" }));
  }

  // 通信そのものが例外で落ちる（DNS・タイムアウト）ことがある。その場合も
  // 「生成中」のまま放置せず、ボタンを戻して本人に知らせる
  let status = 0;
  let detail = "";
  try {
    status = await dispatch(env, payload, storyIds);
  } catch (err) {
    detail = (err && err.name) ? err.name : "network error";
  }
  if (status < 200 || status >= 300) {
    // 起動そのものに失敗したときだけ、本人に知らせる。値（トークン）は出さない。
    const why = status ? `GitHub が ${status} を返しました` : `GitHub に届きませんでした（${detail}）`;
    // ボタンを戻すのが先。通知の通信が落ちても「生成中」のまま残さない
    try {
      await restoreButton(payload);
    } catch (err) {
      console.log(JSON.stringify({ event: "restore_button_failed",
                                   error: (err && err.name) || "error" }));
    }
    try {
      await ephemeral(payload,
        `記事化を起動できませんでした（${why}）。` +
        "時間をおいてもう一度押してください。続くようなら GH_DISPATCH_TOKEN の期限を確認してください。");
    } catch (err) {
      console.log(JSON.stringify({ event: "notify_failed",
                                   error: (err && err.name) || "error" }));
    }
  }
}

/** 「生成中」の文面。件数と、終わるまでの目安を添える。純粋関数。 */
export function generatingText(count) {
  const minutes = Math.ceil(Math.ceil(count / BATCH_SIZE) * MINUTES_PER_BATCH);
  return `⏳ 生成中（${count}件・目安 約${minutes}分）… 終わったらチャンネルに紙面が届きます`;
}

/** ボタンのブロックを「⏳ 生成中（N件）」の文字に置き換えて、元の通を書き換える。 */
async function markGenerating(payload, count) {
  const blocks = (payload.message || {}).blocks;
  if (!Array.isArray(blocks)) return;
  const replaced = blocks.map((block) => (isCreateButton(block)
    ? { type: "context", block_id: block.block_id,
        elements: [{ type: "mrkdwn", text: generatingText(count) }] }
    : block));
  await replaceOriginal(payload, replaced);
}

async function restoreButton(payload) {
  const blocks = (payload.message || {}).blocks;
  if (Array.isArray(blocks)) await replaceOriginal(payload, blocks);
}

function isCreateButton(block) {
  return block && block.type === "actions" &&
    (block.elements || []).some((el) => el && el.action_id === ACTION_ID);
}

async function replaceOriginal(payload, blocks) {
  const url = payload.response_url;
  if (!url || !url.startsWith("https://hooks.slack.com/")) return;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ replace_original: true, blocks,
                           text: (payload.message || {}).text || "ニュース一覧" }),
  });
}

async function ephemeral(payload, text) {
  const url = payload.response_url;
  if (!url || !url.startsWith("https://hooks.slack.com/")) return;
  await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ response_type: "ephemeral", replace_original: false, text }),
  });
}

/** `state.values` からチェックされた全 story_id を取り出す。 */
export function selectedStoryIds(payload) {
  const out = [];
  const values = ((payload || {}).state || {}).values || {};
  for (const block of Object.values(values)) {
    for (const element of Object.values(block || {})) {
      if (!element || element.type !== "checkboxes") continue;
      for (const option of element.selected_options || []) {
        if (option.value && !out.includes(option.value)) out.push(option.value);
      }
    }
  }
  return out;
}

function parsePayload(raw) {
  try {
    const params = new URLSearchParams(raw);
    const encoded = params.get("payload");
    return encoded ? JSON.parse(encoded) : null;
  } catch (_) {
    return null;
  }
}

/** Slack の署名検証。タイムスタンプのずれも見る。 */
export async function verifySignature(secret, timestamp, signature, body) {
  if (!secret || !timestamp || !signature) return false;

  const age = Math.abs(Math.floor(Date.now() / 1000) - Number(timestamp));
  if (!Number.isFinite(age) || age > MAX_SKEW_SECONDS) return false;

  const key = await crypto.subtle.importKey(
    "raw",
    new TextEncoder().encode(secret),
    { name: "HMAC", hash: "SHA-256" },
    false,
    ["sign"],
  );
  const mac = await crypto.subtle.sign(
    "HMAC",
    key,
    new TextEncoder().encode(`v0:${timestamp}:${body}`),
  );
  const expected = "v0=" + [...new Uint8Array(mac)]
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");

  return timingSafeEqual(expected, signature);
}

function timingSafeEqual(a, b) {
  if (a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

async function dispatch(env, payload, storyIds) {
  return dispatchEvent(env, "articles", {
    story_ids: storyIds,
    date: (payload.actions || [])[0]?.value || "",
    channel: (payload.channel || {}).id || "",
    message_ts: (payload.message || {}).ts || "",
    thread_ts: (payload.message || {}).thread_ts || (payload.message || {}).ts || "",
  });
}

async function dispatchEvent(env, eventType, clientPayload) {
  const body = { event_type: eventType, client_payload: clientPayload };
  const storyIds = clientPayload.story_ids || [];

  const response = await fetch(`https://api.github.com/repos/${env.GH_REPO}/dispatches`, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${(env.GH_DISPATCH_TOKEN || "").trim()}`,
      Accept: "application/vnd.github+json",
      "Content-Type": "application/json",
      "User-Agent": "morning-paper-worker",
    },
    body: JSON.stringify(body),
  });
  console.log(JSON.stringify({ event: "dispatch", type: eventType,
                               status: response.status, stories: storyIds.length }));
  return response.status;
}
