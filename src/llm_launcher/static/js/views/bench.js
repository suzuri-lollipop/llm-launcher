/* ===================================================================
   view: bench — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.bench = `
  <section id="view-bench" class="view hidden">
    <h2 class="view-title">ベンチマーク <span class="muted">作成済みプロファイルのサーバー (OpenAI 互換 API) を直接計測</span></h2>
    <div class="bm-tabs" role="tablist">
      <button class="bm-tab" id="bmt-run" role="tab" aria-selected="false" onclick="bmSetTab('run')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 実行<span class="run-dot hidden" id="bmt-run-dot" title="ベンチマーク実行中"></span></button>
      <button class="bm-tab" id="bmt-history" role="tab" aria-selected="false" onclick="bmSetTab('history')"><svg class=ic aria-hidden=true><use href=#i-history /></svg> 履歴・管理 <span class="n" id="bmt-hn">0</span></button>
      <button class="bm-tab" id="bmt-compare" role="tab" aria-selected="false" onclick="bmSetTab('compare')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> 結果・比較 <span class="n hidden" id="bmt-cn">0</span></button>
    </div>
    <div id="bmt-p-run">
    <div id="bm-active"></div>
    <div class="card">
      <div class="row" style="gap:8px">
        <select id="bm-profile" class="grow" style="min-width:180px" onchange="bmPersist()"></select>
        <span class="badge" id="bm-prof-state"><span class="dot"></span>-</span>
      </div>
      <div class="muted" id="bm-prof-note" style="margin-top:6px"></div>
      <div class="row" style="gap:8px;flex-wrap:wrap;margin-top:4px;align-items:flex-end">
        <div class="grow" style="min-width:220px"><label class="f" for="bm-bp">ベンチプロファイル <span class="muted">(設定一式を保存・再利用)</span></label>
          <select id="bm-bp" class="grow" onchange="bmProfileLoad()" style="min-width:200px"><option value="">（未選択）</option></select></div>
        <div style="min-width:180px"><label class="f" for="bm-bp-name">名前</label><input type="text" id="bm-bp-name" placeholder="例: Qwen NVFP4 c4 長デコード" maxlength="80" style="width:100%"></div>
        <button class="ghost" onclick="bmProfileSave()">保存</button>
        <button class="ghost" onclick="bmProfileDelete()">削除</button>
        <span class="muted" id="bm-bp-note" style="font-size:11px"></span>
      </div>
      <div class="row" style="gap:12px;flex-wrap:wrap;margin-top:4px">
        <div><label class="f" for="bm-mode">送信方式</label><select id="bm-mode" onchange="bmModeUI()" style="width:240px"><option value="loop">ループ (N 並列・空いた分を即送信)</option><option value="stagger">インタリーブ (デコード中に次を送信)</option></select></div>
        <div id="bm-gap-wrap" class="hidden"><label class="f" for="bm-gap">送信間隔 (秒)</label><input type="number" id="bm-gap" min="0.1" step="0.1" value="2" style="width:90px"></div>
        <div id="bm-conc-wrap"><label class="f" for="bm-conc">並列度 (カンマ区切り)</label><input type="text" id="bm-conc" value="1,8" style="width:110px"></div>
        <div><label class="f" for="bm-num">リクエスト数/段</label><input type="number" id="bm-num" min="1" value="24" style="width:96px"></div>
        <div><label class="f" for="bm-warm">warmup</label><input type="number" id="bm-warm" min="0" value="1" style="width:76px"></div>
        <div><label class="f" for="bm-inlen">prompt 語数≈</label><input type="number" id="bm-inlen" min="8" value="128" style="width:90px"></div>
        <div><label class="f" for="bm-decode">デコード長</label><select id="bm-decode" onchange="bmDecodeUI()" style="width:150px"><option value="custom">カスタム</option><option value="shortest">最短 (8 tok)</option><option value="longest">最長 (8192 tok)</option></select></div>
        <div><label class="f" for="bm-outlen">max_tokens</label><input type="number" id="bm-outlen" min="1" max="32768" value="128" style="width:96px"></div>
        <div><label class="f" for="bm-mintok">min_tokens</label><input type="number" id="bm-mintok" min="0" max="32768" value="0" style="width:96px"></div>
        <label class="switch" style="align-self:flex-end"><input type="checkbox" id="bm-ignoreeos"><span class="track"></span><span class="lab" style="font-size:12.5px">EOS 無視</span></label>
        <div><label class="f" for="bm-temp">temperature</label><input type="number" id="bm-temp" min="0" max="2" step="0.05" value="0" style="width:88px"></div>
        <div><label class="f" for="bm-endpoint">エンドポイント</label><select id="bm-endpoint" style="width:170px"><option value="chat">/v1/chat/completions</option><option value="completions">/v1/completions</option></select></div>
        <label class="switch" style="align-self:flex-end"><input type="checkbox" id="bm-stream" checked><span class="track"></span><span class="lab" style="font-size:12.5px">stream (TTFT計測)</span></label>
      </div>
      <div class="row" style="gap:18px;flex-wrap:wrap;margin-top:2px">
        <label class="switch"><input type="checkbox" id="bm-autostart" checked><span class="track"></span><span class="lab" style="font-size:12.5px">未起動ならサーバーを自動起動</span></label>
        <label class="switch"><input type="checkbox" id="bm-stopafter"><span class="track"></span><span class="lab" style="font-size:12.5px">完了後、自動起動したサーバーを停止</span></label>
      </div>
      <label class="f" for="bm-model">モデル名 <span class="muted">(空 = /v1/models の先頭 id を自動使用)</span></label>
      <input type="text" id="bm-model" class="mono" placeholder="(自動)">
      <div class="muted" style="font-size:11.5px;margin-top:6px">プロンプトは launcher が生成するランダム文章 (≈語数。リクエスト毎に重複なしで prefix cache を抑制)。トークン数は API の usage 優先、未取得時は文字数推定 (≈)。同一機上で複数実行すると結果が相互に狂うため同時実走は 1 件です。</div>
      <div class="muted" id="bm-mode-note" style="font-size:11.5px;margin-top:4px"></div>
      <div class="muted" id="bm-decode-note" style="font-size:11.5px;margin-top:2px"></div>
      <div class="row" style="margin-top:10px">
        <button class="primary need-bench" id="bm-start" onclick="bmStart()"><svg class=ic aria-hidden=true><use href=#i-play /></svg> ベンチ開始</button>
        <span class="grow"></span>
        <span class="muted" id="bm-note"></span>
      </div>
    </div>
    </div>
    <div id="bmt-p-history" class="hidden"><div id="bm-history"></div></div>
    <div id="bmt-p-compare" class="hidden"><div id="bm-compare"></div></div>
  </section>
`;
/* ============================================================= benchmarks */
/* 作成済みプロファイルのサーバーを launcher 内蔵ランナーで計測する。
   実行はバックエンド (POST /api/benchmarks) が裏で走らせ、ここでは 2.5 秒ポーリングで
   progress / 履歴を描画する。並列度リスト (例 "1,8,32") の各値が 1 ステージ。
   ページ内は 3 タブに機能分離する (ハンバーガーメニューをこれ以上増やさない):
     実行 (run)      … 実行フォーム + ライブカード
     履歴・管理 (…)  … 結果の一覧 / 再実行 / 保存 / 削除 / クリア
     結果・比較 (…) … 実行を選んでの詳細表示 + 複数件のグラフ・表比較 */
const BM_KEY = "llm-laun…h.v1";
const BM_TABS = ["run", "history", "compare"];
const BM_INPUTS = ["bm-profile","bm-conc","bm-num","bm-warm","bm-inlen","bm-outlen",
                   "bm-mintok","bm-ignoreeos","bm-decode","bm-temp","bm-endpoint",
                   "bm-stream","bm-autostart","bm-stopafter","bm-model","bm-mode","bm-gap"];
function bmInit() {
  let d = {};
  try { d = JSON.parse(localStorage.getItem(BM_KEY) || "{}") || {}; } catch { d = {}; }
  // cmp: 比較に選んだ run id (選択順 = 比較表の基準順), cmpMetric / cmpConc: 比較パネルの選択
  // tab: ページ内タブ (実行 / 履歴・管理 / 結果・比較) — 選択を localStorage で保つ
  S.bench = { runs: [], active: null, busy: false, _pSig: "", _bh: "", _bc: "", _recv: 0,
              cmp: [], cmpMetric: "decode", cmpConc: null, moreCols: false,
              profiles: [], bpId: "",
              tab: BM_TABS.includes(d.tab) ? d.tab : "run" };
  const set = (id, v) => { const el = $(id); if (el && v != null && v !== "") {
    if (el.type === "checkbox") el.checked = !!v; else el.value = v; } };
  set("#bm-profile", d.profile); set("#bm-conc", d.conc); set("#bm-num", d.num);
  set("#bm-warm", d.warm); set("#bm-inlen", d.inlen); set("#bm-outlen", d.outlen);
  set("#bm-temp", d.temp); set("#bm-endpoint", d.endpoint);
  set("#bm-model", d.model); set("#bm-mode", d.mode); set("#bm-gap", d.gap);
  set("#bm-mintok", d.mintok); set("#bm-decode", d.decode);
  if (d.ignoreeos === true) $("#bm-ignoreeos").checked = true;
  if (d.stream === false) $("#bm-stream").checked = false;
  if (d.autostart === false) $("#bm-autostart").checked = false;
  if (d.stopafter === true) $("#bm-stopafter").checked = true;
  BM_INPUTS.forEach(id => { const el = $(`#${id}`); if (el) el.addEventListener("change", bmPersist); });
  bmModeUI();
  bmDecodeUI();
  bmProfileList();
}
/* インタリーブ模式的な UI 切り替え: 間隔入力を出し、並列度入力は意味を失うので隠す
   (バックエンドは mode=stagger で concurrency=[本数] に正規化する) */
function bmModeUI() {
  const el = $("#bm-mode"); if (!el) return;
  const st = el.value === "stagger";
  $("#bm-gap-wrap").classList.toggle("hidden", !st);
  $("#bm-conc-wrap").classList.toggle("hidden", st);
  $("#bm-mode-note").textContent = st
    ? "インタリーブ: リクエスト数 = 同時ストリーム数。前の本がデコード中でも送信間隔秒ごとに次のプロンプトを送り、干渉 (後着の待ち・走行中の停止) を段として測ります。スイムレーンに停止区間 (赤)、比較バーに停止セグメントが出ます。"
    : "";
}
function bmPersist() {
  if (!S.bench) return;
  const v = id => $(id) ? $(id).value : "";
  try {
    localStorage.setItem(BM_KEY, JSON.stringify({
      profile: v("#bm-profile"), conc: v("#bm-conc"), num: v("#bm-num"), warm: v("#bm-warm"),
      inlen: v("#bm-inlen"), outlen: v("#bm-outlen"), mintok: v("#bm-mintok"),
      ignoreeos: $("#bm-ignoreeos").checked, decode: v("#bm-decode"), temp: v("#bm-temp"),
      bp: S.bench.bpId, bpName: v("#bm-bp-name"),
      endpoint: v("#bm-endpoint"), model: v("#bm-model"),
      stream: $("#bm-stream").checked, autostart: $("#bm-autostart").checked,
      stopafter: $("#bm-stopafter").checked, tab: S.bench.tab,
      mode: v("#bm-mode"), gap: v("#bm-gap"),
    }));
  } catch { /* 容量超過等は無視 */ }
}
/* ページ内タブの切替 (実行 / 履歴・管理 / 結果・比較)。ハンバーガーメニューの
   「ベンチマーク」1 項目のまま、ページ内で機能を分ける */
function bmSetTab(t) {
  if (!BM_TABS.includes(t)) return;
  S.bench.tab = t; bmPersist(); renderBench();
}
function openBench(pid) {
  if (!S.bench) bmInit();
  S.bench.tab = "run";                        // 各所の「ベンチ」ボタンは実行の意図で飛ぶ
  if (pid) { $("#bm-profile").value = pid; bmPersist(); }
  switchTab("bench");
  bmProfileList();
  bmPoll(true);
}
function bmFormBody() {
  const v = id => ($(id).value || "").trim();
  const num = (id, d) => { const x = parseInt(v(id), 10); return isNaN(x) ? d : x; };
  const conc = [...new Set(v("#bm-conc").split(/[,，\s]+/)
    .map(x => parseInt(x, 10)).filter(x => x >= 1 && x <= 256))].sort((a, b) => a - b);
  // 段数上限 8: 超えたら最小と最大を保ったまま等間隔に間引く (サーバー側と同じ規則)
  if (conc.length > 8) {
    const n = conc.length;
    const keep = new Set([...Array(8).keys()].map(i => conc[Math.round(i * (n - 1) / 7)]));
    conc.length = 0; conc.push(...[...keep].sort((a, b) => a - b));
  }
  const temp = parseFloat(v("#bm-temp"));
  const body = {
    profile_id: v("#bm-profile"),
    model: v("#bm-model"),
    endpoint: v("#bm-endpoint"),
    stream: $("#bm-stream").checked,
    mode: v("#bm-mode") || "loop",
    gap_s: parseFloat(v("#bm-gap")) || 2,
    concurrency: conc.length ? conc : [8],
    num_prompts: num("#bm-num", 24),
    input_len: num("#bm-inlen", 128),
    output_len: num("#bm-outlen", 128),
    min_tokens: num("#bm-mintok", 0),
    ignore_eos: $("#bm-ignoreeos").checked,
    warmup: num("#bm-warm", 1),
    auto_start: $("#bm-autostart").checked,
    stop_after: $("#bm-stopafter").checked,
  };
  if (!isNaN(temp)) body.temperature = temp; else body.temperature = null;
  return body;
}
async function bmStart() {
  const d = S.bench;
  if (d.busy || d.active) return;
  const body = bmFormBody();
  if (!body.profile_id) return toast("プロファイルを選択してください");
  d.busy = true; renderBench(); bmPersist();
  try {
    const run = await api("/api/benchmarks", {method: "POST", body});
    toast(`ベンチ開始: ${run.profile_name} (並列 ${body.concurrency.join("/")} · ${body.num_prompts} req/段)`);
    d.active = run.id;
    bmPoll(true);
  } catch (e) { toast("ベンチ開始失敗: " + e.message); }
  finally { d.busy = false; bmPoll(true); }
}
async function bmPoll(force) {
  let j;
  try { j = await api("/api/benchmarks"); }
  catch { return; }                               // 取得失敗は次のポーリングで追いつく (静默)
  S.bench.runs = j.runs || []; S.bench.active = j.active; S.bench._recv = Date.now() / 1000;
  bmBadge();
  try { if (S.tab === "bench" || force) renderBench(); }
  catch (e) { console.error("render error:", e); }  // 描画例外を握り潰すと「ページが沈む」
}
function bmBadge() {
  const nb = $("#nb-bench"); if (!nb) return;
  const on = !!S.bench.active;
  nb.classList.toggle("hidden", !on);
  nb.classList.toggle("run", on);
  nb.innerHTML = on ? IC("play") : "";
}
async function bmCancel(id) {
  if (!confirm("ベンチマークを中断しますか? 実行中のリクエストは完了まで待たれます")) return;
  try { await api(`/api/benchmarks/${id}/cancel`, {method: "POST"}); toast("中断要求を送信しました"); bmPoll(true); }
  catch (e) { toast("失敗: " + e.message); }
}
async function bmDelete(id) {
  if (!confirm("このベンチ履歴を削除しますか?")) return;
  try {
    await api(`/api/benchmarks/${id}`, {method: "DELETE"});
    S.bench.cmp = (S.bench.cmp || []).filter(x => x !== id);   // 消えた id の残選択を掃除
    toast("削除しました"); bmPoll(true);
  }
  catch (e) { toast("失敗: " + e.message); }
}
async function bmClearAll() {
  const done = (S.bench.runs || []).filter(r => r.id !== S.bench.active);
  if (!done.length) return toast("消せる履歴がありません");
  if (!confirm(`履歴 ${done.length} 件をまとめて消します。実行中のベンチは残ります。よろしいですか?`)) return;
  try {
    const j = await api("/api/benchmarks", {method: "DELETE"});
    S.bench.cmp = [];
    toast(`履歴を ${j.removed} 件消しました`);
    bmPoll(true);
  } catch (e) { toast("失敗: " + e.message); }
}
function bmExport(id) {
  const r = S.bench.runs.find(x => x.id === id);
  if (r) downloadJson(r, `llm-bench_${r.id}.json`);
}
function bmRerun(id) {
  const r = S.bench.runs.find(x => x.id === id); if (!r) return;
  const p = r.params || {};
  const set = (sel, v2) => { const el = $(sel); if (el && v2 != null) {
    if (el.type === "checkbox") el.checked = !!v2; else el.value = v2; } };
  set("#bm-profile", r.profile_id); set("#bm-conc", (p.concurrency || []).join(","));
  set("#bm-num", p.num_prompts); set("#bm-warm", p.warmup); set("#bm-inlen", p.input_len);
  set("#bm-outlen", p.output_len); set("#bm-mintok", p.min_tokens || 0);
  if (p.ignore_eos === true) $("#bm-ignoreeos").checked = true; else $("#bm-ignoreeos").checked = false;
  set("#bm-decode", p.ignore_eos && Number(p.min_tokens || 0) >= 8192 ? "longest"
    : (Number(p.output_len) <= 8 && !Number(p.min_tokens || 0) ? "shortest" : "custom"));
  bmDecodeUI();
  set("#bm-temp", p.temperature); set("#bm-endpoint", p.endpoint);
  set("#bm-stream", p.stream); set("#bm-autostart", p.auto_start); set("#bm-stopafter", p.stop_after);
  set("#bm-model", p.model || ""); set("#bm-mode", p.mode || "loop"); set("#bm-gap", p.gap_s);
  S.bench.tab = "run";                      // フォームは実行タブ: そのまま見せる
  bmModeUI();
  bmPersist(); renderBench();
  toast("設定を実行タブに読み込みました (<svg class=ic aria-hidden=true><use href=#i-play /></svg> ベンチ開始で実行)");
}
/* 履歴行の「結果」: 比較・表示タブへ飛び、その実行だけ選択して詳細を出す */
function bmViewRun(id) {
  if (!S.bench) return;
  S.bench.cmp = [id]; S.bench.tab = "compare"; bmPersist(); renderBench();
}
const bmF1 = x => x == null ? "-" : (+x).toFixed(1);
const bmF2 = x => x == null ? "-" : (+x).toFixed(2);
const bmTps = x => x == null ? "-" : (+x >= 100 ? String(Math.round(x)) : (+x).toFixed(1));
/* decode tok/s Σ: 全並列ストリームのデコード速度を合算した実測
   (トークン合計 ÷ 最初→最後のトークン到着区間)。毎秒チャート (全並列合算) と同じ軸。
   新規記録は decode_tps_sum を使い、古い記録は完全な reqs (a/d,n) から同じ式で算出、
  reqs が間引かれていれば待ち込みの出力 tok/s で代替する (黙って空にしない) */
function bmDecSum(st) {
  if (!st) return null;
  if (st.decode_tps_sum != null) return st.decode_tps_sum;
  const rs = (st.reqs || []).filter(r => r && r.a != null && r.d != null && r.d > r.a);
  if (rs.length && !st.reqs_truncated) {
    const tok = rs.reduce((s, r) => s + (r.n || 0), 0);
    const span = Math.max(...rs.map(r => r.d)) - Math.min(...rs.map(r => r.a));
    if (tok && span > 0.05) return tok / span;
  }
  return st.out_tps != null ? st.out_tps : null;
}
function bmStatusBadge(r) {
  const map = {queued: ["info", "待機"], running: ["warn", "実行中"], completed: ["ok", "完了"],
               failed: ["err", "失敗"], cancelled: ["", "中断"], lost: ["purple", "切断記録"]};
  const [cls, label] = map[r.status] || ["", r.status];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}
function bmLenSummary(p) {
  const out = `out≤${p.output_len}t`;
  const min = Number(p.min_tokens || 0);
  return out + (min > 0 ? ` · min≥${min}t` : "") + (p.ignore_eos ? " · EOS無視" : "");
}
function bmDecodeUI() {
  const el = $("#bm-decode"); if (!el) return;
  const mode = el.value || "custom";
  if (mode === "shortest") {
    $("#bm-outlen").value = 8; $("#bm-mintok").value = 0; $("#bm-ignoreeos").checked = false;
  } else if (mode === "longest") {
    $("#bm-outlen").value = 8192; $("#bm-mintok").value = 8192; $("#bm-ignoreeos").checked = true;
  }
  $("#bm-decode-note").textContent = mode === "shortest"
    ? "最短: max_tokens=8。デコードの立ち上がり/TTFT 寄りの計測になります (トークン数が少ないと decode tok/s は不安定)。"
    : mode === "longest"
      ? "最長: max_tokens=8192 + min_tokens=8192 + EOS 無視。min_tokens / ignore_eos を解釈しないサーバーでは 400 になることがあります (その場合はカスタムで min_tokens=0 に)。"
      : "カスタム: max_tokens / min_tokens / EOS 無視を個別に指定します。min_tokens は max_tokens 以下にしてください。";
  bmPersist();
}
/* ---- benchmark profiles (data/bench_profiles.json): what to measure, separate from the launch profile ---- */
async function bmProfileList() {
  try {
    const j = await api("/api/bench-profiles");
    S.bench.profiles = j.profiles || [];
  } catch { S.bench.profiles = []; }
  bmProfileRender();
}
function bmProfileRender() {
  const sel = $("#bm-bp"); if (!sel) return;
  const cur = S.bench.bpId || sel.value;
  sel.innerHTML = `<option value="">（未選択）</option>` + (S.bench.profiles || [])
    .map(p => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  sel.value = (S.bench.profiles || []).some(p => p.id === cur) ? cur : "";
}
function bmProfileLoad() {
  const sel = $("#bm-bp"); if (!sel) return;
  const id = sel.value; S.bench.bpId = id;
  const p = (S.bench.profiles || []).find(x => x.id === id);
  $("#bm-bp-note").textContent = "";
  if (!p) { $("#bm-bp-name").value = ""; bmPersist(); return; }
  $("#bm-bp-name").value = p.name;
  bmApplyParams(p.params || {});
  const target = $("#bm-profile");
  if (p.params && p.params.profile_id && target &&
      ![...target.options].some(o => o.value === p.params.profile_id))
    $("#bm-bp-note").textContent = "対象の起動プロファイルが見つかりません";
  bmPersist();
}
function bmApplyParams(p) {
  const set = (id, v) => { const el = $(id); if (el && v != null) el.value = v; };
  set("#bm-profile", p.profile_id); set("#bm-mode", p.mode || "loop");
  set("#bm-gap", p.gap_s || 2); set("#bm-conc", (p.concurrency || []).join(","));
  set("#bm-num", p.num_prompts); set("#bm-warm", p.warmup); set("#bm-inlen", p.input_len);
  set("#bm-outlen", p.output_len); set("#bm-mintok", p.min_tokens || 0);
  set("#bm-temp", p.temperature); set("#bm-endpoint", p.endpoint); set("#bm-model", p.model || "");
  if (p.stream === false) $("#bm-stream").checked = false; else $("#bm-stream").checked = true;
  if (p.auto_start === false) $("#bm-autostart").checked = false; else $("#bm-autostart").checked = true;
  $("#bm-stopafter").checked = p.stop_after === true;
  $("#bm-ignoreeos").checked = p.ignore_eos === true;
  set("#bm-decode", p.ignore_eos && Number(p.min_tokens || 0) >= 8192 ? "longest"
    : (Number(p.output_len) <= 8 && !Number(p.min_tokens || 0) ? "shortest" : "custom"));
  bmDecodeUI();
  bmModeUI();
}
async function bmProfileSave() {
  const name = ($("#bm-bp-name").value || "").trim();
  if (!name) return toast("ベンチプロファイル名を入力してください");
  if (!$("#bm-profile").value) return toast("対象の起動プロファイルを選択してください");
  const params = bmFormBody();
  const id = S.bench.bpId;
  try {
    const p = id
      ? await api(`/api/bench-profiles/${id}`, {method: "PUT", body: {name, params}})
      : await api("/api/bench-profiles", {method: "POST", body: {name, params}});
    S.bench.bpId = p.id;
    await bmProfileList();
    $("#bm-bp").value = p.id;
    $("#bm-bp-name").value = p.name;
    $("#bm-bp-note").textContent = id ? "更新しました" : "保存しました";
    toast(id ? "ベンチプロファイルを更新しました" : "ベンチプロファイルを保存しました");
    bmPersist();
  } catch (e) { toast("保存失敗: " + e.message); }
}
async function bmProfileDelete() {
  const id = S.bench.bpId;
  if (!id) return toast("削除するベンチプロファイルを選択してください");
  if (!confirm("このベンチプロファイルを削除しますか? (実行履歴は残ります)")) return;
  try {
    await api(`/api/bench-profiles/${id}`, {method: "DELETE"});
    S.bench.bpId = ""; $("#bm-bp-name").value = ""; $("#bm-bp-note").textContent = "削除しました";
    await bmProfileList();
    toast("ベンチプロファイルを削除しました");
    bmPersist();
  } catch (e) { toast("削除失敗: " + e.message); }
}
function bmParamsSummary(p) {
  const ep = `${p.endpoint === "chat" ? "chat" : "completions"}${p.stream ? "+stream" : ""}` +
    (p.temperature ? ` · temp${p.temperature}` : " · greedy");
  if (p.mode === "stagger") {
    return `インタリーブ ${p.num_prompts} 本 · ${(p.gap_s || 2)}s 間隔で送信 · in≈${p.input_len}語 · ${bmLenSummary(p)} · ${ep}`;
  }
  const c = (p.concurrency || []).join("/") || "-";
  return `${p.num_prompts}req × ${c}並列 · in≈${p.input_len}語 · ${bmLenSummary(p)} · ${ep}`;
}
/* 代表値 (履歴行・比較ピッカー共通): decode 優先、無い = 非 stream なので出力 peak */
function bmPeakOf(r) {
  const hasDec = (r.stages || []).some(s => s.decode_tps && s.decode_tps.avg != null);
  const v = Math.max(0, ...(r.stages || []).map(s => {
    const x = hasDec ? bmDecSum(s) : s.out_tps;
    return x || 0;
  }));
  return {v: v || null, dec: hasDec};
}
function bmStallCell(st) {
  // 最長停止 = 生成開始後のチャンク間ギャップのワースト。目標は「人間が関知
  // できない」= 閾値 (stall_threshold_s, 既定 0.5s) 以下なので緑/黄/赤で色分け。
  // 計測追加前の古い履歴にはフィールドが無い。
  const w = st.stall_worst_s;
  if (w == null) return `<td title="stall 計測前の記録">-</td>`;
  const th = st.stall_threshold_s || 0.5;
  const color = w <= th ? "var(--ok)" : (w <= 2.0 ? "var(--warn)" : "var(--err)");
  return `<td><b style="color:${color}">${bmF2(w)}s</b></td>`;
}
/* ---------------- 比較 / チャート (追加依存なしの SVG だけ) ---------------- */
const BM_COLORS = ["var(--acc)", "var(--warn)", "var(--ok)", "var(--err)",
                   "var(--purple)", "var(--maya)", "var(--strata)", "var(--sub)"];
const BM_CMP_MAX = 6;   // 重ね描きが多すぎると判読不能になるので上限
const BM_METRICS = {    // 比較表の指標: up = 大きいほど良い (先頭 = 既定)
  decode:{lab: "decode tok/s Σ", get: s => bmDecSum(s), fmt: bmTps, up: true},
  out:   {lab: "出力 tok/s", get: s => s.out_tps, fmt: bmTps, up: true},
  tot:   {lab: "合計 tok/s", get: s => s.total_tps, fmt: bmTps, up: true},
  rps:   {lab: "req/s", get: s => s.rps, fmt: bmF2, up: true},
  ttft:  {lab: "TTFT avg", get: s => s.ttft && s.ttft.avg, fmt: v => bmF2(v) + "s", up: false},
  latp:  {lab: "遅延 p99", get: s => s.latency && s.latency.p99, fmt: v => bmF2(v) + "s", up: false},
  stall: {lab: "最大停止", get: s => s.stall_worst_s, fmt: v => bmF2(v) + "s", up: false},
};
function bmWhen(r) {
  const t = r.finished_at || r.created_at;
  if (!t) return "-";
  const d = new Date(t * 1000), p = n => String(n).padStart(2, "0");
  return `${d.getMonth() + 1}/${d.getDate()} ${p(d.getHours())}:${p(d.getMinutes())}`;
}
function bmRunShort(r) { return `${(r.profile_name || "?").slice(0, 16)} ${bmWhen(r)}`; }
function bmSeriesPts(st) {
  // series → [[t, 生成 tok/s]]。新記録の agg は「その 1 秒に実際に生成されたトークン
  // 合計」(ストリーム到着を刻み計測 → 1 秒差分)。記録更新前の古いデータは完了確定
  // ベースの win (こちらも全並列合算のレート) にフォールバック
  const s = st.series || [];
  const step = Math.max(1, Math.ceil(s.length / 240));
  const y = p => p.agg != null ? p.agg
    : p.win != null ? p.win
      : p.dec != null ? p.dec : (p.avg != null ? p.avg : 0);
  const pts = [];
  for (let i = 0; i < s.length; i += step) pts.push([s[i].t, y(s[i])]);
  const last = s[s.length - 1];
  if (last && pts.length && (s.length - 1) % step) pts.push([last.t, y(last)]);
  return pts;
}
/* 折れ線チャートを SVG 文字列で生成 (viewBox スケールで幅可変)。
   ser = [{pts:[[x,y]...], color, label, wide?}] / opts = {ylab, h, xNum} */
function bmSvgChart(ser, opts) {
  opts = opts || {};
  const W = 640, H = opts.h || 170, L = 46, R = 10, T = 12, B = 20;
  const all = [];
  ser.forEach(s => (s.pts || []).forEach(p => { if (p && isFinite(p[0]) && isFinite(p[1])) all.push(p); }));
  if (all.length < 2) return "";
  const x0 = opts.xNum ? Math.min(...all.map(p => p[0])) : 0;
  const x1 = Math.max(x0 + 1, Math.max(...all.map(p => p[0])));
  let y1 = Math.max(...all.map(p => p[1]));
  if (!(y1 > 0)) y1 = 1;
  const pw = Math.pow(10, Math.floor(Math.log10(y1)));
  const nice = [1, 1.5, 2, 2.5, 5, 10].find(k => k * pw >= y1) * pw;   // 目盛りは 1/2/5 系に丸める
  const X = x => L + (x - x0) / (x1 - x0) * (W - L - R);
  const Y = y => H - B - Math.min(y / nice, 1) * (H - T - B);
  const fmtV = v => v >= 100 ? String(Math.round(v)) : (+v).toFixed(v >= 10 ? 0 : 1);
  const g = [];
  [0, 0.25, 0.5, 0.75, 1].forEach(f => {
    const y = +Y(nice * f).toFixed(1);
    g.push(`<line x1="${L}" x2="${W - R}" y1="${y}" y2="${y}" style="stroke:var(--line)" stroke-width="1"${f ? ' stroke-dasharray="2 4"' : ""}/>`);
    if (f !== 0.25 && f !== 0.75) g.push(`<text x="${L - 4}" y="${y + 3}" text-anchor="end" font-size="9" style="fill:var(--sub)">${fmtV(nice * f)}</text>`);
  });
  let xt;
  if (opts.xNum) {
    const u = [...new Set(all.map(p => p[0]))].sort((a, b) => a - b);
    xt = u.filter((v, i) => u.length <= 8 || i % Math.ceil(u.length / 8) === 0 || i === u.length - 1);
  } else xt = [0, (x0 + x1) / 2, x1];
  xt.forEach(v => g.push(`<text x="${X(v).toFixed(1)}" y="${H - 6}" text-anchor="middle" font-size="9" style="fill:var(--sub)">${opts.xNum ? v + "p" : Math.round(v) + "s"}</text>`));
  if (opts.ylab) g.push(`<text x="4" y="9" font-size="9" style="fill:var(--sub)">${esc(opts.ylab)}</text>`);
  const paths = ser.map(s => {
    const pts = (s.pts || []).filter(p => p && isFinite(p[0]) && isFinite(p[1]));
    if (pts.length < 2) return "";
    const d = pts.map((p, i) => `${i ? "L" : "M"}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
    const e = pts[pts.length - 1];
    return `<path d="${d}" fill="none" style="stroke:${s.color}" stroke-width="${s.wide ? 2.3 : 1.6}" stroke-linejoin="round" stroke-linecap="round"><title>${esc(s.label || "")}</title></path>` +
      `<circle cx="${X(e[0]).toFixed(1)}" cy="${Y(e[1]).toFixed(1)}" r="2.6" style="fill:${s.color}"/>`;
  }).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img">${g.join("")}${paths}</svg>`;
}
function bmLegend(ser) {
  return `<div class="bm-lgd">${ser.map(s => `<span><i style="background:${s.color}"></i>${esc(s.label || "")}</span>`).join("")}</div>`;
}
/* 停止区間 (r.g = [開始, 終了] の組、段開始からの秒。チャンク間が検出閾値以上だった
   区間をバックエンドが最大 100 個記録) をデコード行に赤で重畳する。ライブでは
   まだ伸びるので until 時刻でクリップ。行ホバーのツールチップにも合計 (r.st) を出す */
function bmStallRects(r, rect, until) {
  const out = [];
  for (const g of (Array.isArray(r.g) ? r.g : [])) {
    if (!Array.isArray(g)) continue;
    const g0 = Math.max(typeof g[0] === "number" ? g[0] : -1, r.a ?? -1);
    const g1 = Math.min(typeof g[1] === "number" ? g[1] : until, until);
    if (g1 - g0 > 0.005) out.push(rect(g0, g1, "var(--err)", `停止 ${(g1 - g0).toFixed(2)}s (チャンクの空白・検出閾値以上)`, ".9"));
  }
  return out;
}
/* リクエスト単位のフェーズ・スイムレーン (ガント)。
   reqs = [{s,a,d,e,n}] — s=送信 a=最初のトークン d=最後のトークン e=確定 n=トークン数
   (段開始からの秒。None=未達)。行の重なりが同時実走、琥珀の長さが待ち (送信→最初の
   トークン = サーバー側キュー+プレフィル込み)、緑がデコード、灰色は確定処理。
   nowSec 指定 (ライブ) では未確定の行を現在まで延ばす。pct=true で横軸を
   段時間の 0〜100% に正規化 (絶対秒の違う実行の形比較用。ツールチップは秒のまま)。 */
function bmPhaseSvg(reqs, nowSec, pct) {
  const rows = (reqs || []).filter(r => r && r.s != null).sort((x, y) => x.s - y.s).slice(0, 60);
  if (!rows.length) return "";
  const endOf = r => r.e != null ? r.e : (r.d != null ? r.d : (r.a != null ? r.a : r.s));
  const maxT = Math.max(nowSec || 0, ...rows.map(endOf)) + 0.4;
  const W = 640, L = 8, R = 8, T = 4, rowH = 12, gp = 3, B = 16;
  const H = T + rows.length * (rowH + gp) + B;
  const X = t => L + Math.max(0, Math.min(t, maxT)) / maxT * (W - L - R);
  const f2 = v => (Math.round(v * 100) / 100).toFixed(2);
  const body = rows.map((r, i) => {
    const y = T + i * (rowH + gp);
    const rect = (t0, t1, col, title, op) =>
      t1 - t0 > 0.005 ? `<rect x="${X(t0).toFixed(1)}" y="${y}" width="${Math.max(0.6, X(t1) - X(t0)).toFixed(1)}" height="${rowH}" rx="2" style="fill:${col}"${op ? ` opacity="${op}"` : ""}><title>${esc(title)}</title></rect>` : "";
    const live = r.e == null, till = live ? Math.max(nowSec || r.s, endOf(r)) : r.e;
    let tip = "";
    const segs = [];
    if (r.a == null) {
      if (!live) segs.push(rect(r.s, till, "#94a3b8", "一括応答 (stream なし / 最初のトークン不明)"));
      else segs.push(rect(r.s, till, "var(--warn)", "待ち (送信→最初のトークン)…", ".8"));
      tip = live ? "待ち (送信→最初のトークン)…" : "一括応答";
    } else {
      if (r.arr != null && (r.q != null || r.p != null)) {
        // サーバーが内訳を公開 (strata 系 /metrics): 送信→受付 / キュー待ち / プレフィル / 配信
        const a0 = Math.max(r.arr, r.s), q1 = a0 + (r.q || 0), p1 = q1 + (r.p || 0);
        if (a0 - r.s > 0.005) segs.push(rect(r.s, a0, "var(--acc)", `送信 → サーバー受付 ${f2(a0 - r.s)}s`));
        if ((r.q || 0) > 0.01) segs.push(rect(a0, q1, "var(--purple)", `キュー待ち ${f2(r.q)}s (サーバー報告: 他の生成に押されて待った時間)`));
        segs.push(rect(q1, Math.min(p1, r.a), "var(--warn)", `プレフィル ${f2(r.p)}s (サーバー報告: プロンプト読込・初期計算)`));
        if (r.a - p1 > 0.05) segs.push(rect(p1, r.a, "#b9c6d4", `最初のトークン配信 ${f2(r.a - p1)}s`));
        tip = `キュー ${f2(r.q || 0)}s · プレフィル ${f2(r.p)}s`;
      } else {
        // 内訳非公開のサーバー: 実測できる TTFT 区間を正直に「待ち」として一枚で出す
        segs.push(rect(r.s, r.a, "var(--warn)", "待ち = 送信→最初のトークン (サーバーのキュー積み込み + プレフィル + 配送込み)"));
        tip = `待ち ${f2(r.a - r.s)}s`;
      }
      if (r.d != null) {
        const ds = Math.max(r.d - r.a, 0.05), rate = r.n / ds;
        segs.push(rect(r.a, r.d, "var(--ok)", `デコード ${f2(ds)}s ${r.n}tok (${f2(rate)} tok/s)`));
        segs.push(...bmStallRects(r, rect, r.d));
        if (r.e != null && r.e - r.d > 0.01) segs.push(rect(r.d, r.e, "#94a3b8", `確定 (クローズ) ${f2(r.e - r.d)}s`));
        tip += ` · デコード ${f2(ds)}s ${r.n}tok (${f2(rate)} tok/s)` + (r.st ? ` · 停止計 ${f2(r.st)}s` : "");
      } else {
        segs.push(rect(r.a, till, "var(--ok)", "デコード中…", ".8"));
        segs.push(...bmStallRects(r, rect, till));
        tip += ` · デコード中 ${r.n}tok` + (r.st ? ` · 停止計 ${f2(r.st)}s` : "");
      }
    }
    return `<g><title>#${i + 1} ${esc(tip)}</title>` +
      rect(0, maxT, "var(--line)", `#${i + 1} ${tip}`, ".25") + segs.join("") + `</g>`;
  }).join("");
  const axl = v => pct ? Math.round(v / maxT * 100) + "%" : Math.round(v) + "s";
  const axis = [0, maxT / 2, maxT].map(v =>
    `<text x="${X(v).toFixed(1)}" y="${H - 4}" text-anchor="middle" font-size="9" style="fill:var(--sub)">${axl(v)}</text>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" role="img">${body}${axis}</svg>`;
}
/* 凡例 (スイムレーン本体と「比較」パネルで共通)。サーバー内訳が付き始めた reqs が
   あれば 5 色分割版、無ければ 待ち/デコード/確定。停止区間は実データがある時だけ追加 */
function bmPhaseLegend(reqs) {
  const pm = reqs.some(r => r && r.arr != null);   // サーバー内訳が付き始めたか
  const anyStall = reqs.some(r => r && ((Array.isArray(r.g) && r.g.length) || r.st));
  let legend = pm
    ? `<span class="bm-lgd"><span><i style="background:var(--acc)"></i>送信</span>` +
      `<span><i style="background:var(--purple)"></i>キュー待ち</span>` +
      `<span><i style="background:var(--warn)"></i>プレフィル</span>` +
      `<span><i style="background:var(--ok)"></i>デコード</span>` +
      `<span><i style="background:#94a3b8"></i>確定</span>`
    : `<span class="bm-lgd"><span><i style="background:var(--warn)"></i>待ち</span>` +
      `<span><i style="background:var(--ok)"></i>デコード</span>` +
      `<span><i style="background:#94a3b8"></i>確定</span>`;
  if (anyStall) legend += `<span><i style="background:var(--err)"></i>停止</span>`;
  return legend + `</span>`;
}
function bmPhaseBlock(title, reqs, nowSec, truncated) {
  if (!(reqs || []).length) return "";
  const pm = reqs.some(r => r && r.arr != null);   // サーバー内訳が付き始めたか
  const legend = bmPhaseLegend(reqs);
  return `<div class="row" style="gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap">
      <b style="font-size:13px">${esc(title)}</b>${legend}</div>` +
    `<div class="bm-chart" style="margin-top:4px">${bmPhaseSvg(reqs, nowSec)}</div>` +
    `<div class="muted" style="font-size:11px">各行 = リクエスト 1 本 (上から送信順)。行の重なり = 同時に走っていた区間。行にホバーで内訳${truncated ? " · 多いため等間隔に間引いて表示" : ""}。デコード行の赤 = 停止区間 (チャンクが届かなかった空白)。<br>` +
    (pm
      ? `<b>キュー待ち</b> = サーバーが他の生成を捌いている間待たされた時間 (サーバー側 TTFT − プレフィル)。<b>プレフィル</b> = プロンプトの読み込み・初期計算 (サーバー報告の prompt_ms)。薄い灰の残り幅は最初のトークンの配信。いずれもサーバー (/metrics) が報告した値で、実測の「送信→最初のトークン」枠の中に配置しています`
      : `<b>待ち</b> = 送信 → 最初のトークン (サーバーのキュー積み込み + プレフィル + 配送をまとめた実測値。このサーバーは内訳を公開しないため分割できません。strata 系では完了後に自動で分割表示へ変わります)`) + `</div>`;
}
/* 実行中カードのライブチャート: 完了段の series に実行中段を重ねる (強調描画) */
function bmLiveChart(run) {
  const pr = run.progress || {};
  const act = run.status === "running" || run.status === "queued";
  const done = (run.stages || []).filter(s => (s.series || []).length >= 2);
  const ser = done.map((s, i) => ({color: BM_COLORS[i % BM_COLORS.length], label: `${s.concurrency}p 完了`, pts: bmSeriesPts(s)}));
  if (act && (pr.series || []).length >= 2)
    ser.push({color: BM_COLORS[(pr.stage || 0) % BM_COLORS.length],
              label: `${(pr.stages || [])[pr.stage] || "?"}p 計測中`, pts: bmSeriesPts({series: pr.series}), wide: true});
  if (!ser.length) {
    return (run.stages || []).length && !act
      ? `<div class="muted" style="font-size:11px;margin-top:6px">※ この履歴には経時記録がありません (記録機能の追加前の実行です)</div>` : "";
  }
  return `<div class="bm-chart">${bmSvgChart(ser, {ylab: "tok/s", h: 150})}${bmLegend(ser)}` +
    `<div class="muted" style="font-size:11px">経過秒に対する生成トークン/s (全並列リクエスト合算。ストリーム到着を 1 秒刻みで実測)。強調 = 実行中の段 · 記録更新前の古い履歴も全並列合算です (完了確定ベース)</div></div>`;
}
function bmStageOf(r, conc) { return (r.stages || []).find(s => s.concurrency === conc) || null; }
function bmDeltaCell(v, b, up) {
  if (v == null || b == null || !b) return "";
  const d = (v - b) / Math.abs(b) * 100;
  if (!isFinite(d) || Math.abs(d) < 0.05) return "";
  const good = d > 0 ? up : !up;
  return ` <span style="color:var(--${good ? "ok" : "err"});font-size:10.5px">${d > 0 ? "+" : ""}${Math.abs(d) >= 10 ? Math.round(d) : d.toFixed(1)}%</span>`;
}
/* 比較パネル: 選択中の実行を重ねる。折れ線 (並列度 vs 出力) + 経時オーバーレイ +
   フェーズ比較バー + 指標表 */
/* フェーズ比較バーのセグメント定義 (色はスイムレーンと同一。内訳非公開のサーバーは
   wait が q/p/res の枠をまとめて担う) */
const BM_PHASE_SEGS = [
  ["send", "var(--acc)", "送信"],
  ["q",    "var(--purple)", "キュー待ち"],
  ["p",    "var(--warn)", "プレフィル"],
  ["res",  "#b9c6d4", "トークン配信"],
  ["wait", "var(--warn)", "待ち (キュー+プレフィル+配送)"],
  ["dec",  "var(--ok)", "デコード"],
  ["stall", "var(--err)", "停止 (デコード内)"],
  ["cl",   "#94a3b8", "確定"],
];
/* 1 段のリクエスト平均フェーズ秒。サーバー内訳あり (strata 系) は 送信/キュー/プレフィル/配信
   に分け、なしは「待ち」にまとめる。無い項目は null のまま (0 と区別する) */
function bmPhaseAgg(st) {
  const rows = (st.reqs || []).filter(r => r && r.s != null);
  if (!rows.length) return null;
  const avg = a => a.length ? a.reduce((x, y) => x + y, 0) / a.length : null;
  const g = {send: [], q: [], p: [], res: [], wait: [], dec: [], cl: [], tok: [], st: []};
  for (const r of rows) {
    if (typeof r.st === "number") g.st.push(r.st);   // リクエスト単位の停止合計秒 (閾値以上)
    if (r.arr != null && (r.q != null || r.p != null)) {
      const a0 = Math.max(r.arr, r.s);
      if (a0 - r.s > 0.005) g.send.push(a0 - r.s);
      if (r.q != null) g.q.push(Math.max(r.q, 0));
      if (r.p != null) g.p.push(Math.max(r.p, 0));
      const res = r.a != null ? r.a - a0 - (r.q || 0) - (r.p || 0) : null;
      if (res != null && res > 0.005) g.res.push(res);
    } else if (r.a != null && r.arr == null) {
      g.wait.push(Math.max(r.a - r.s, 0));
    }
    if (r.a != null && r.d != null) g.dec.push(Math.max(r.d - r.a, 0));
    if (r.d != null && r.e != null) g.cl.push(Math.max(r.e - r.d, 0));
    if (r.n) g.tok.push(r.n);
  }
  return {pm: rows.some(r => r.arr != null), send: avg(g.send), q: avg(g.q), p: avg(g.p),
          res: avg(g.res), wait: avg(g.wait), dec: avg(g.dec), cl: avg(g.cl),
          stall: avg(g.st), tok: avg(g.tok), n: rows.length};
}
/* フェーズ比較バー: 同じ並列度段のリクエスト平均を実行ごとに積み上げ、共通の時間スケールで
   比較する横棒 (スイムレーンは実行ごとに別々で比較できなかった対策)。行末に decode tok/s と
   合計平均秒、凡例はそのデータに実際に存在するセグメントだけを出す */
function bmPhaseCmpBlock(entries) {
  const has = entries.filter(e => e && e.agg);
  if (!has.length) return "";
  const allPm = has.every(e => e.agg.pm), allFb = has.every(e => !e.agg.pm);
  const hasStall = has.some(e => e.agg.stall != null);
  // セグメント順: 待ち系 → デコード (停止があれば切り出して直後に赤) → 確定。
  // 混在 (内訳あり/なしの実行) は全枠を出し、各行は値のある枠だけ描画する
  let keys = allPm ? ["send", "q", "p", "res"] : allFb ? ["wait"] : ["send", "q", "p", "res", "wait"];
  keys.push("dec");
  if (hasStall) keys.push("stall");
  keys.push("cl");
  const segMap = Object.fromEntries(BM_PHASE_SEGS.map(s => [s[0], s]));
  const defs = keys.map(k => segMap[k]);
  // 停止はデコード区間のサブセット。切り出すときは緑を純生成分に寄せて棒の合計は不変
  const segVal = (e, k) => k === "dec" && e.agg.stall != null
    ? Math.max(0, (e.agg.dec || 0) - (e.agg.stall || 0)) : e.agg[k];
  const tot = e => keys.reduce((s, k) => s + (segVal(e, k) || 0), 0);
  const maxT = Math.max(0.05, ...has.map(tot));
  const W = 640, L = 160, R = 128, T = 2, rowH = 20, gp = 8, B = 14;
  const H = T + has.length * (rowH + gp) + B;
  const X = t => L + t / maxT * (W - L - R);
  const f2 = v => (Math.round(v * 100) / 100).toFixed(2) + "s";
  const rowsHtml = has.map((e, i) => {
    const y = T + i * (rowH + gp);
    let t0 = 0;
    const segHtml = defs.map(([k, c, lab]) => {
      const v = segVal(e, k);
      if (!v || v <= 0.004) return "";
      const x0 = X(t0); t0 += v; const w = X(t0) - x0;
      const tx = w > 44 ? `<text x="${(x0 + w / 2).toFixed(1)}" y="${y + rowH - 6}" text-anchor="middle" font-size="9" fill="#fff" style="paint-order:stroke;stroke:rgba(0,0,0,.35);stroke-width:2px">${f2(v)}</text>` : "";
      return `<rect x="${x0.toFixed(1)}" y="${y}" width="${Math.max(1, w).toFixed(1)}" height="${rowH}" rx="3" style="fill:${c}"><title>${esc(lab)} 平均 ${f2(v)} (${e.agg.n} リクエスト平均)</title></rect>` + tx;
    }).join("");
    const end = `<text x="${(X(tot(e)) + 6).toFixed(1)}" y="${y + rowH - 6}" font-size="9.5" style="fill:var(--sub)">${e.decTps != null ? "decode Σ " + bmTps(e.decTps) + " t/s · " : ""}計 ${f2(tot(e))}${e.agg.stall ? ` (停止 ${f2(e.agg.stall)})` : ""}</text>`;
    return `<text x="2" y="${y + rowH - 6}" font-size="10" style="fill:var(--text)">${esc(String(e.label).slice(0, 22))}</text>` + segHtml + end;
  }).join("");
  const axis = [0, maxT / 2, maxT].map(v =>
    `<text x="${X(v).toFixed(1)}" y="${H - 3}" text-anchor="middle" font-size="9" style="fill:var(--sub)">${Math.round(v * 100) / 100}s</text>`).join("") +
    `<line x1="${L}" x2="${W - R}" y1="${H - B + 3}" y2="${H - B + 3}" style="stroke:var(--line)"/>`;
  return `<svg viewBox="0 0 ${W} ${H}" role="img">${rowsHtml}${axis}</svg>` +
    `<div class="bm-lgd">${defs.map(s => `<span><i style="background:${s[1]}"></i>${s[2]}</span>`).join("")}</div>`;
}
function bmCmpPanel(runs) {
  const d = S.bench;
  d.cmp = (d.cmp || []).filter(id => runs.some(r => r.id === id));   // 削除された id を掃除
  if (!d.cmp.length) return "";
  const sel = d.cmp.map(id => runs.find(r => r.id === id)).filter(r => r && (r.stages || []).length);
  if (!sel.length) return "";
  const col = i => BM_COLORS[i % BM_COLORS.length];
  const concs = [...new Set(sel.flatMap(r => (r.stages || []).map(s => s.concurrency)))].sort((a, b) => a - b);
  if (!concs.length) return "";
  const conc = concs.includes(+d.cmpConc) ? +d.cmpConc : concs[concs.length - 1];   // 既定は最負荷段
  const mk = BM_METRICS[d.cmpMetric] ? d.cmpMetric : "decode", M = BM_METRICS[mk];
  const mSel = `<select onchange="bmCmpMetric(this.value)" style="width:130px">` +
    Object.keys(BM_METRICS).map(k => `<option value="${k}"${k === mk ? " selected" : ""}>${BM_METRICS[k].lab}</option>`).join("") + `</select>`;
  const base = sel[0];
  // インタリーブ実行が混ざっていたら「並列度」は「本数」の意味になる (単位の切り替え)
  const unit = sel.some(r => r.params && r.params.mode === "stagger") ? " 本" : "p";
  const rows = concs.map(c => {
    const bs = bmStageOf(base, c), bv = bs ? M.get(bs) : null;
    const vals = sel.map(r => { const st = bmStageOf(r, c); return st ? M.get(st) : null; });
    const nn = vals.filter(v => v != null);
    const best = nn.length > 1 ? (M.up ? Math.max(...nn) : Math.min(...nn)) : null;
    return `<tr><td>${c}${unit}</td>` + vals.map((v, i) =>
      `<td${v != null && v === best ? ' style="font-weight:700"' : ""}>${v == null ? "-" : M.fmt(v)}${i ? bmDeltaCell(v, bv, M.up) : ""}</td>`).join("") + `</tr>`;
  }).join("");
  const tbl = `<div class="bm-wrap" data-scrollkey="cmp:tbl"><table class="bm"><tr><th>${unit === "p" ? "並列度" : "本数"}</th>` +
    sel.map((r, i) => `<th style="text-align:center;color:${col(i)}">${esc(bmRunShort(r))}</th>`).join("") +
    `</tr>${rows}</table></div>` +
    `<div class="muted" style="font-size:11px;margin-top:4px">Δ = 基準 (${esc(bmRunShort(base))}) 比。太字 = その並列度で最良</div>`;
  // 並列度特性カーブも decode Σ 主指標 (その実行に decode が無い = 非 stream なら出力で代替)
  const sweep = sel.map((r, i) => ({color: col(i), label: bmRunShort(r),
    pts: (r.stages || []).map(s => { const v = bmDecSum(s); return v != null ? [s.concurrency, +v] : null; })
      .filter(Boolean).sort((a, b) => a[0] - b[0])}))
    .filter(s => s.pts.length > 1);
  const ts = sel.map((r, i) => { const st = bmStageOf(r, conc); return {color: col(i), label: `${bmRunShort(r)} (${conc}${unit})`, pts: st ? bmSeriesPts(st) : []}; })
    .filter(s => s.pts.length >= 2);
  const noSeries = sel.length - ts.length;
  const cSel = `<div class="row" style="gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center">
      <b style="font-size:13px">時間経過</b>
      <label style="font-size:12px">並列度 <select onchange="bmCmpConc(this.value)" style="width:90px">${concs.map(c => `<option value="${c}"${c === conc ? " selected" : ""}>${c}p</option>`).join("")}</select></label>
      <span class="muted" style="font-size:11px">毎秒生成されたトークン/s (全並列合算・実測) · 各段の開始時刻に重ね描き · 記録更新前の古い実行も全並列合算で表示</span></div>`;
  // フェーズ比較: 待ち (送信→最初のトークン) / デコード / 確定の平均秒を同じ段で実行ごとに
  // 積み上げて比較 (スイムレーンは 1 実行ずつの表示で「待ちがどちらの実行で長いか」が
  // 読めなかった対策)。内訳を公開するサーバーは キュー待ち/プレフィル まで分けて積む
  const phs = sel.map(r => { const st = bmStageOf(r, conc);
    return {label: bmRunShort(r), agg: st ? bmPhaseAgg(st) : null, decTps: st ? bmDecSum(st) : null}; });
  const phOk = phs.filter(e => e.agg).length, phMiss = sel.length - phOk;
  const phBlock = phOk ? `<div class="row" style="gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center">
      <b style="font-size:13px">フェーズ比較</b>
      <span class="muted" style="font-size:11px">リクエスト 1 本あたりの平均秒 (同じ時間スケール・積み上げ) · 時間経過と同じ ${conc}${unit} 段</span></div>` +
      `<div class="bm-chart">${bmPhaseCmpBlock(phs)}<div class="muted" style="font-size:11px">「待ち」の中身分割はサーバーが内訳を報告する実行のみ (strata 系 /metrics: キュー待ち=紫・プレフィル=琥珀)。非公開のサーバーは「待ち」にまとまった実測値です。<b>停止 (赤)</b> はデコード区間から切り出した空白時間 (チャンク間が検出閾値以上) — デコードの緑と足すと丸ごとのデコード秒に戻ります。行末 = その段の decode tok/s Σ (全並列合算) と合計平均秒 · 棒にホバーで内訳${phMiss ? ` · フェーズ記録なし ${phMiss} 件 (記録機能追加前の履歴)` : ""}</div></div>`
    : `<div class="muted" style="font-size:12px;margin-top:8px">選択した実行にフェーズ記録がありません (記録機能追加前の履歴です)。数値比較は下の表でできます</div>`;
  // スイムレーン比較 (ユーザー要望「この表示のまま比較したい」): 同じ段の実物レーンを
  // 実行ごとに並べ、横軸をその段の時間 0〜100% に正規化。絶対秒が違っても (114s vs 73s)
  // キュー待ちの階段・デコードの伸び・停止の配置を同じ尺度で重ねて読める。
  // 絶対秒は行ホバーと実行ごとの個別ビューに出す (ここは形の比較に専念)
  const lanes = sel.map((r, i) => { const st = bmStageOf(r, conc);
    return st && (st.reqs || []).length ? {r, st, c: col(i)} : null; }).filter(Boolean);
  const laneBlock = lanes.length ? `<div class="row" style="gap:8px;margin-top:12px;flex-wrap:wrap;align-items:center">
      <b style="font-size:13px">フェーズ比較 (スイムレーン)</b>${bmPhaseLegend(lanes.flatMap(o => o.st.reqs))}</div>` +
    `<div style="display:flex;flex-direction:column;gap:10px">` + lanes.map(o =>
      `<div><div class="row" style="gap:6px;align-items:center;margin-top:4px">
        <i style="background:${o.c};width:10px;height:10px;border-radius:2px;flex:none"></i>
        <b style="font-size:12px" class="ellip">${esc(bmRunShort(o.r))}</b>
        <span class="muted" style="font-size:11px">${bmF1(o.st.wall_secs)}s · ${o.st.ok} req${o.st.reqs_truncated ? " · 間引き表示" : ""}</span></div>
        <div class="bm-chart" style="margin-top:2px">${bmPhaseSvg(o.st.reqs, null, true)}</div></div>`).join("") + `</div>` +
    `<div class="muted" style="font-size:11px">各行 = リクエスト 1 本 (上から送信順)。横軸 = この ${conc}${unit} 段の時間を 0〜100% に正規化 (${lanes.map(o => Math.round(o.st.wall_secs)).join("s vs ")}s と違う絶対秒でも同じ尺度で形を比較できる)。絶対秒・内訳は行にホバー${sel.length - lanes.length ? ` · フェーズ記録なし ${sel.length - lanes.length} 件` : ""}</div>` : "";
  return `<div class="card bm-cmp">
    <div class="row"><b style="font-size:14px">結果比較 (${sel.length} 件)</b><span class="grow"></span>
      <button class="ghost" onclick="bmCmpClear()">選択を解除</button></div>
    ${sweep.length > 1 ? `<div class="bm-chart">${bmSvgChart(sweep, {ylab: "tok/s", xNum: true})}${bmLegend(sweep)}<div class="muted" style="font-size:11px">並列度に対する確定値の decode tok/s Σ (全並列合算。非 stream の実行は出力 tok/s。段の区切りでガタつかない全体像。1 本あたり平均が欲しければ比較指標の表で段ごとに確認できます)</div></div>` : ""}
    ${cSel}
    ${ts.length ? `<div class="bm-chart">${bmSvgChart(ts, {ylab: "tok/s"})}${bmLegend(ts)}</div>`
                : `<div class="muted" style="font-size:12px;margin-top:4px">この並列度の経時記録がありません (記録機能追加前の履歴です)。数値比較は下の表でできます</div>`}
    ${noSeries && ts.length ? `<div class="muted" style="font-size:11px">※ ${noSeries} 件はこの並列度の経時記録なし</div>` : ""}
    ${phBlock}
    ${laneBlock}
    <div class="row" style="gap:8px;margin-top:12px;align-items:center;flex-wrap:wrap">
      <b style="font-size:13px">比較指標</b>${mSel}
      <span class="muted" style="font-size:11px">同じ並列度のステージを横並びで</span></div>
    ${tbl}
  </div>`;
}
function bmCmpToggle(id) {
  const d = S.bench; d.cmp = d.cmp || [];
  const i = d.cmp.indexOf(id);
  if (i >= 0) d.cmp.splice(i, 1);
  else { if (d.cmp.length >= BM_CMP_MAX) return toast(`比較は最大 ${BM_CMP_MAX} 件までです`); d.cmp.push(id); }
  renderBench();
}
function bmCmpClear() { S.bench.cmp = []; renderBench(); }
function bmCmpMetric(v) { S.bench.cmpMetric = v; renderBench(); }
function bmCmpConc(v) { S.bench.cmpConc = +v; renderBench(); }
function bmMoreCols() { S.bench.moreCols = !S.bench.moreCols; renderBench(); }
/* key = 再描画後に横スクロール位置と details の開閉を復元するための安定キー
   (実行中は "a:<run id>"、履歴は "k:<run id>"。data-logkey とは別接頭辞にする)。
   .bm-wrap は overflow-x:auto の横スクロール欄なので、key を忘れると 2.5 秒ポーリング
   の innerHTML 差し替えで scrollLeft が 0 に戻る = 「右に寄せた列が左端に飛ぶ」。 */
function bmStageTable(stages, key) {
  // 凡例の閾値はステージに保存された値を使う (STALL_S を変えても表示が追随する)
  const th = (stages.find(s => s && s.stall_threshold_s != null) || {}).stall_threshold_s || 0.5;
  // 全ステージで stall が測れていない = stream OFF 等 (古い履歴は 0 件扱いしない)
  const anyStall = stages.some(s => s && (s.stall_worst_s != null || s.stall_threshold_s != null));
  // 補助列 (req/s・合計・p50) は既定たたむ: 12 列を横スクロールさせないと
  // 本体の数字が読めないほど詰まっていた対策。.bm-x + data-x で CSS が隠す。
  // decode tok/s は利用者に直結する体感速度なので既定表示のまま (比較指標にも収録)
  const X = !!S.bench.moreCols;
  return `<div class="row" style="gap:8px;margin-top:10px;flex-wrap:wrap;align-items:center">
      <button class="ghost" style="min-height:28px;font-size:12px" onclick="bmMoreCols()">${X ? "▾ 補助列を下げる" : "▸ 補助列 (req/s・合計 tok/s・遅延 p50)"}</button>
      <span class="muted" style="font-size:11px">先頭列は固定 / 表は左右にスクロールできます</span></div>
    <div class="bm-wrap" data-scrollkey="${esc(key)}"><table class="bm" data-x="${X ? 1 : 0}"><tr>` +
    `<th>${stages.some(s => s && s.mode === "stagger") ? "本数" : "並列度"}</th><th>OK</th><th class="bm-x">req/s</th><th title="全並列ストリームのデコード速度の合算 (デコード区間の合計トークン ÷ 最初→最後のトークン到着区間。stream ON のみ)。主指標。1 本あたりの平均はツールチップと p50/p90 列">decode tok/s Σ</th><th>出力 tok/s</th><th class="bm-x">合計 tok/s</th>` +
    `<th>TTFT avg</th><th>TTFT p99</th><th class="bm-x">遅延 p50</th><th>遅延 p99</th>` +
    `<th title="生成開始後、チャンクが最も長く途切れた秒数 (ステージ中の全リクエストのワースト)">最大停止</th>` +
    `<th title="${th}s 以上の停止が発生した延べ回数 (全リクエスト合計)">停止数</th></tr>` +
    stages.map(st => `<tr>
      <td>${st.concurrency}${st.mode === "stagger" ? " 本" : "p"}</td>
      <td>${st.ok}/${st.ok + st.fail}${st.aborted ? " <svg class=ic aria-hidden=true><use href=#i-stop /></svg>" : ""}</td>
      <td class="bm-x">${bmF2(st.rps)}</td><td><b title="1 本あたり平均 ${bmTps(st.decode_tps && st.decode_tps.avg)} tok/s">${bmTps(bmDecSum(st))}</b></td><td>${bmTps(st.out_tps)}</td><td class="bm-x">${bmTps(st.total_tps)}</td>
      <td>${bmF2(st.ttft && st.ttft.avg)}s</td><td>${bmF2(st.ttft && st.ttft.p99)}s</td>
      <td class="bm-x">${bmF2(st.latency && st.latency.p50)}s</td><td>${bmF2(st.latency && st.latency.p99)}s</td>` +
      bmStallCell(st) +
      `<td>${st.stall_count == null ? "-" : st.stall_count + "回"}</td></tr>`).join("") +
    `</table></div>` +
    `<div class="muted" style="font-size:11px;margin-top:4px">最大停止: 緑 = 人間の体感閾値 (${th}s) 以下 / 黄 = 2s 以下 / 赤 = それ以上。最初のトークンまでの待ちは TTFT 列を参照</div>` +
    (anyStall ? "" :
      `<div class="muted" style="font-size:11px;margin-top:4px">※ stream が OFF (または SSE でない応答) のため TTFT / 最大停止 / decode tok/s は測れません (遅延と出力 tok/s のみ)</div>`) +
    (stages.some(s => s.total_tps == null)
      ? `<div class="muted" style="font-size:11px;margin-top:4px">※ 入力トークン数 (usage.prompt_tokens) が一部の応答に無いため合計 tok/s は算出していません</div>` : "") +
    (stages.some(s => s.tokens_estimated)
      ? `<div class="muted" style="font-size:11px;margin-top:4px">※ usage を返さない応答があるためトークン数に文字数推定を含みます</div>` : "") +
    (stages.some(s => (s.errors || []).length)
      ? `<details class="adv" data-detailkey="${esc(key)}:err" style="margin-top:8px"><summary>エラー例</summary><div class="inner mono" style="font-size:11px;color:var(--err)">${
          stages.filter(s => (s.errors || []).length)
                .map(s => `<div>${s.concurrency}p: ${esc(s.errors[0])}</div>`).join("")}</div></details>` : "");
}
function renderBench() {
  if (!S.bench) bmInit();
  const ps = S.state?.profiles || [];
  const sel = $("#bm-profile");
  const sig = ps.map(p => p.id + ":" + p.name).join("|");
  const cur = sel.value;
  if (sig !== S.bench._pSig || ![...sel.options].some(o => o.value === cur)) {
    S.bench._pSig = sig;
    sel.innerHTML = ps.length
      ? ps.map(p => `<option value="${p.id}">${esc(p.name)} · ${esc(backendById(p.backend)?.name || p.backend)}</option>`).join("")
      : `<option value="">プロファイルがありません (作成してください)</option>`;
    if (cur) sel.value = cur;
  }
  const p = ps.find(x => x.id === cur) || null;
  const sess = p ? liveOf(p.id) : null;
  const st = $("#bm-prof-state");
  st.className = "badge " + (sess ? (sess.ready ? "ok" : "warn") : "");
  st.innerHTML = `<span class="dot"></span>` + (sess ? (sess.ready ? `稼働中 :${sess.port || "-"}` : "起動中…") : "未起動");
  $("#bm-prof-note").textContent = !p ? "先にプロファイルを作成してください。"
    : sess ? "起動中サーバーをそのまま使用します (再起動はしません)。"
    : ($("#bm-autostart").checked ? "未起動 → ベンチ開始時に自動起動して API 応答を待ちます。"
                                  : "未起動 → 「自動起動」を ON にするか、起動タブでスタートしてください。");
  $("#bm-start").disabled = S.bench.busy || !!S.bench.active || !p;
  $("#bm-note").textContent = S.bench.busy ? "開始中…"
    : (S.bench.active ? "ベンチマーク実行中です (同時に 1 件)" : "");
  const d = S.bench;
  // 削除された / 実行中の id の残選択を掃除 (タブバッジと比較パネルは同じ出典を使う)
  d.cmp = (d.cmp || []).filter(id => (d.runs || []).some(r => r.id === id && r.id !== d.active));
  bmTabBar();
  renderBenchActive(d.runs.find(r => r.id === d.active) || null);
  renderBenchHistory();
  renderBenchCompare();
}
/* タブバー: 選択表示 + 件数バッジ (履歴 = 完了系, 比較 = 選択中) + 実行中ドット。
   ペインは hidden の切り替えだけで、DOM は常に維持する (フォームの状態が飛ぶのを防ぐ) */
function bmTabBar() {
  const d = S.bench;
  BM_TABS.forEach(t => {
    const b = $("#bmt-" + t); if (!b) return;
    b.classList.toggle("on", d.tab === t);
    b.setAttribute("aria-selected", d.tab === t ? "true" : "false");
  });
  const panes = {run: "bmt-p-run", history: "bmt-p-history", compare: "bmt-p-compare"};
  Object.keys(panes).forEach(k => $("#" + panes[k]).classList.toggle("hidden", k !== d.tab));
  const hn = $("#bmt-hn");
  if (hn) hn.textContent = (d.runs || []).filter(r => r.id !== d.active).length;
  const cn = $("#bmt-cn");
  if (cn) { cn.textContent = (d.cmp || []).length; cn.classList.toggle("hidden", !(d.cmp || []).length); }
  const dot = $("#bmt-run-dot");
  if (dot) dot.classList.toggle("hidden", !d.active);
}
function renderBenchActive(run) {
  const el = $("#bm-active");
  if (!run) { el.innerHTML = ""; return; }
  // スクロール中は組み直しを後回し (結果表の横スクロールバーを掴んだままにできる)
  if (uiScrolling() && el.childElementCount) { deferRender("bm-act", () => renderBenchActive(run)); return; }
  const _ls = logScrollSnap(el);                // 2.5 秒ごとの再描画で表の横スクロールを保つ
  const pr = run.progress || {};
  const skew = run.status === "running" || run.status === "queued" ? (Date.now() / 1000) - (S.bench._recv || 0) : 0;
  const secs = (pr.elapsed || 0) + skew;
  const total = pr.total || 1;
  const pctw = pr.phase === "measure" ? Math.min(100, (pr.done || 0) / total * 100)
             : (pr.phase === "warmup" ? 4 : 0);
  const tps = pr.phase === "measure" && secs > 0.5 ? (pr.out_tokens || 0) / secs : null;
  // 主指標は decode Σ (全並列合算)。ライブでは経時サンプルの直近 1 秒 (agg) をそのまま
  // 使う = チャートの今。サンプルが無い/完了 0 件のうちは代替を出して黙って消さない
  const sr = pr.series || [];
  const lag = sr.length ? sr[sr.length - 1].agg : null;
  const dec = (pr.dec_n || 0) > 0 ? (pr.dec_sum || 0) / pr.dec_n : null;
  const ulab = (run.params || {}).mode === "stagger" ? " 本" : "p";   // インタリーブは本数表示
  const chips = (pr.stages || []).map((c, i) => {
    const st = (run.stages || [])[i];
    // st は「完了済み段」にしか存在しない (pr.stages は全予定並列度)。実行中・待機
    // の段では undefined なので、参照は必ず st の存在を確認した後で行うこと
    // (先に st.xxx と触ると TypeError → 画面全体が落ちる)。
    if (st) { const d = st.decode_tps && st.decode_tps.avg != null ? bmDecSum(st) : null;
      return `<span class="badge ok" title="${st.ok} req / ${bmF1(st.wall_secs)}s · ${d != null ? "decode Σ (全並列合算)" : "出力"}">${c}${ulab} ✓ ${bmTps(d != null ? d : st.out_tps)} t/s</span>`; }
    if (i === pr.stage && (run.status === "running" || run.status === "queued"))
      return `<span class="badge warn"><span class="dot"></span>${c}${ulab} 実行中</span>`;
    return `<span class="badge">${c}${ulab} 待機</span>`;
  }).join(" ");
  const phase = {queued: "待機中…", launching: "サーバー起動中… (数分かかります)", waiting: "サーバーの API 応答を待っています…",
                 warmup: "ウォームアップ中 (統計外)", measure: "計測中", done: "完了"}[pr.phase] || pr.phase || "";
  const doneNote = pr.phase === "measure"
    ? `リクエスト ${pr.done || 0}/${pr.total} (OK ${pr.ok || 0}${pr.fail ? ` / NG ${pr.fail}` : ""})` : "";
  // tpsNote は HTML 断片。下位で esc しない (esc すると <svg> がそのまま文字列になる)
  const tpsNote = lag != null && pr.phase === "measure"
    ? ` · <svg class=ic aria-hidden=true><use href=#i-bolt /></svg> decode Σ <b>${bmTps(lag)}</b> tok/s (直近 1 秒・全並列合算)`
    : dec != null ? ` · <svg class=ic aria-hidden=true><use href=#i-bolt /></svg> decode Σ <b>${bmTps(dec)}</b> tok/s (完了分・1 本あたり平均)`
    : tps != null ? ` · <svg class=ic aria-hidden=true><use href=#i-bolt /></svg> ${bmTps(tps)} tok/s 出力 (decode Σ は最初のトークンから)` : "";
  const modelNote = run.model ? `model: ${run.model}` : bmParamsSummary(run.params || {});
  el.innerHTML = `<div class="card" style="border-color:#c6dafc">
    <div class="row">
      ${bmStatusBadge(run)}
      <b class="grow ellip" style="font-size:15px">${esc(run.profile_name)}</b>
      <span class="chip ${esc(run.backend)}">${esc(run.backend_name)}</span>
      <button class="danger" style="min-height:34px;font-size:12.5px" onclick="bmCancel('${run.id}')"><svg class=ic aria-hidden=true><use href=#i-stop /></svg> 中断</button>
    </div>
    <div class="muted mono ellip" style="margin-top:4px;font-size:11px">${esc(modelNote)}</div>
    <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">${chips}</div>
    <div class="meter" style="margin:10px 0 4px"><div class="m-hd"><b>${esc(phase)}</b>
      <span class="r">${esc(doneNote)}${tpsNote} · ${bmF1(secs)}s</span></div>
      <div class="mbar"><i class="warn" style="width:${pctw}%"></i></div></div>
    ${bmLiveChart(run)}
    ${(pr.reqs || []).length ? bmPhaseBlock("フェーズ (ライブ)", pr.reqs, secs) : ""}
    ${(run.stages || []).length ? bmStageTable(run.stages, "a:" + run.id) : ""}
  </div>`;
  logScrollBack(el, _ls);
}
/* 履歴・管理タブ: 結果の一覧と管理操作だけ (結果を見る / 設定再実行 / 保存 / 削除 /
   一括クリア)。結果の表示・比較は「結果・比較」タブの役目なのでここに置かない */
function renderBenchHistory() {
  const el = $("#bm-history");
  const runs = (S.bench.runs || []).filter(r => r.id !== S.bench.active);
  if (!runs.length) {
    const empty = `<div class="empty" style="margin-top:12px">ベンチ履歴はまだありません。実行タブで条件をセットして <svg class=ic aria-hidden=true><use href=#i-play /></svg> ベンチ開始。</div>`;
    if (S.bench._bh !== empty) { S.bench._bh = empty; el.innerHTML = empty; }   // 空欄も毎回は書き直さない
    return;
  }
  // スクロール中は組み直しを後回し (摘まみを離してしまう)。枠が空のときは必ず描く
  if (uiScrolling() && el.childElementCount) { deferRender("bm-hist", renderBenchHistory); return; }
  const html = `<div class="row" style="margin-top:14px;align-items:center;flex-wrap:wrap">
      <h2 class="view-title" style="margin:0">履歴 (${runs.length})</h2>
      <span class="muted" style="font-size:11.5px">一覧と管理。結果の表示・比較は「結果・比較」タブへ</span><span class="grow"></span>
      <button class="ghost danger" title="完了済みの履歴をまとめて消します (実行中は残りません)" onclick="bmClearAll()"><svg class=ic aria-hidden=true><use href=#i-trash /></svg> 履歴をクリア</button>
    </div>` +
    runs.map(r => {
      const pk = bmPeakOf(r);
      return `<div class="card bm-run" data-run="${esc(r.id)}">
      <div class="row">
        ${bmStatusBadge(r)}
        <b class="grow ellip" style="font-size:14.5px">${esc(r.profile_name)}</b>
        <span class="chip ${esc(r.backend)}">${esc(r.backend_name)}</span>
      </div>
      <div class="muted" style="margin-top:5px;font-size:12px">${esc(bmParamsSummary(r.params || {}))}
        ${(r.stages || []).length ? ` · peak ${pk.dec ? "decode Σ" : "出力"} <b style="color:var(--text)">${bmTps(pk.v)}</b> tok/s` : ""}
        · ${bmWhen(r)} (${r.finished_at ? fmtAgo(r.finished_at) : fmtAgo(r.created_at)})
        ${r.error ? ` · <span style="color:var(--err)">${esc(String(r.error).slice(0, 90))}</span>` : ""}</div>
      <div class="row" style="margin-top:8px">
        <button onclick="bmViewRun('${r.id}')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> 結果を見る</button>
        <button onclick="bmRerun('${r.id}')"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 設定再実行</button>
        <button class="ghost" onclick="bmExport('${r.id}')"><svg class=ic aria-hidden=true><use href=#i-download /></svg> JSON</button>
        <span class="grow"></span>
        <button class="ghost danger" onclick="bmDelete('${r.id}')">削除</button>
      </div>
    </div>`;
    }).join("");
  // 中身が変わらなければ組み直さない (履歴は fmtAgo のラベルしか変わらないので、
  // これで再構築は分単位に 1 回になる)
  if (html === S.bench._bh && el.childElementCount) return;
  S.bench._bh = html;
  el.innerHTML = html;
}
/* 結果・比較タブ: 実行を選んで表示 — 1 件 = 結果の詳細 (経時チャート・段表・フェーズ
   スイムレーン)、2 件以上 = 経時カーブ / 並列度カーブ / 指標表で比較。
   管理操作 (削除・再実行) は置かず、表示に専念する */
function renderBenchCompare() {
  const el = $("#bm-compare");
  const d = S.bench;
  const runs = (d.runs || []).filter(r => r.id !== d.active);
  if (!runs.length) {
    const empty = `<div class="empty" style="margin-top:12px">表示する履歴がまだありません。<b>実行</b> タブでベンチを完了させると、ここに結果が並びます。</div>`;
    if (S.bench._bc !== empty) { S.bench._bc = empty; el.innerHTML = empty; }
    return;
  }
  if (uiScrolling() && el.childElementCount) { deferRender("bm-cmp", renderBenchCompare); return; }
  const picks = runs.map(r => {
    const on = (d.cmp || []).includes(r.id);
    const pk = bmPeakOf(r);
    return `<label class="row bm-pick" style="gap:10px;align-items:center;flex-wrap:wrap;padding:6px 8px;border-radius:8px;cursor:pointer${on ? ";background:#e8f0fe" : ""}">
      <span class="bm-ck"><input type="checkbox"${on ? " checked" : ""} onchange="bmCmpToggle('${r.id}')"></span>
      ${bmStatusBadge(r)}
      <b style="font-size:13px;white-space:nowrap">${esc(bmRunShort(r))}</b>
      <span class="muted" style="font-size:11.5px;overflow-wrap:anywhere">${esc(bmParamsSummary(r.params || {}))}${(r.stages || []).length ? ` · peak ${pk.dec ? "decode Σ" : "出力"} ${bmTps(pk.v)} tok/s` : ""}</span>
    </label>`;
  }).join("");
  const sel = (d.cmp || []).map(id => runs.find(r => r.id === id)).filter(Boolean);
  const detail = sel.map(r => `<div class="card">
      <div class="row">
        ${bmStatusBadge(r)}
        <b class="grow ellip" style="font-size:14.5px">結果詳細 — ${esc(r.profile_name)} <span class="muted" style="font-weight:400;font-size:12px">${bmWhen(r)}</span></b>
        <span class="chip ${esc(r.backend)}">${esc(r.backend_name)}</span>
      </div>
      <div class="muted" style="margin-top:4px;font-size:12px">${esc(bmParamsSummary(r.params || {}))}${r.error ? ` · <span style="color:var(--err)">${esc(String(r.error).slice(0, 120))}</span>` : ""}</div>
      ${bmLiveChart(r)}
      ${(r.stages || []).length ? bmStageTable(r.stages, "c:" + r.id) : ""}
      ${(r.stages || []).map(st => bmPhaseBlock(`フェーズ (${st.concurrency}${st.mode === "stagger" ? " 本" : "p"})`, st.reqs, st.wall_secs, st.reqs_truncated)).join("")}
      <div class="muted mono" style="margin-top:6px;font-size:11px;overflow-wrap:anywhere">model: ${esc(r.model || "-")} · session: ${esc(r.session_id || "-")}${r.auto_started ? " (自動起動)" : ""}${r.stopped_after ? " · 停止済み" : ""}${r.port ? ` · :${r.port}` : ""}</div>
      ${r.stop_error ? `<div class="muted" style="margin-top:4px;font-size:11px;color:var(--err)">サーバーの自動停止に失敗: ${esc(r.stop_error)}</div>` : ""}
      ${r.server_left_running ? `<div class="muted" style="margin-top:4px;font-size:11px;color:var(--warn)">※ launcher 再起動時に自動起動したサーバーが残っています。サーバータブで停止してください</div>` : ""}
    </div>`).join("");
  const html = `<div class="card" style="margin-top:14px">
      <div class="row" style="align-items:center;flex-wrap:wrap;gap:8px">
        <b style="font-size:14px">表示・比較する実行</b>
        <span class="muted" style="font-size:11.5px">1 件 = 結果の詳細 / 2 件以上 = 経時カーブ・並列度カーブ・指標表で比較 (最大 ${BM_CMP_MAX} 件)</span>
        <span class="grow"></span>
        ${(d.cmp || []).length ? `<button class="ghost" onclick="bmCmpClear()">選択を解除</button>` : ""}
      </div>
      <div style="margin-top:4px">${picks}</div>
    </div>` +
    (d.cmp.length >= 2 ? bmCmpPanel(runs) : "") +
    (sel.length ? `<div class="muted" style="margin:14px 2px 6px;font-size:12px">▼ 結果詳細 (${sel.length} 件)</div>` + detail
                 : `<div class="muted" style="margin-top:12px;font-size:12px">↑ 上で行を選択すると、ここに結果を表示します</div>`);
  if (html === S.bench._bc && el.childElementCount) return;
  S.bench._bc = html;
  const _ls = logScrollSnap(el);                // 組み直したときだけ横位置・開閉を復元
  el.innerHTML = html;
  logScrollBack(el, _ls);
}

