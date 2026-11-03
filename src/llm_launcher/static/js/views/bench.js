/* ===================================================================
   view: bench — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.bench = `
  <section id="view-bench" class="view hidden">
    <h2 class="view-title">ベンチマーク <span class="muted">作成済みプロファイルのサーバー (OpenAI 互換 API) を直接計測</span></h2>
    <div id="bm-active"></div>
    <div class="card">
      <div class="row" style="gap:8px">
        <select id="bm-profile" class="grow" style="min-width:180px" onchange="bmPersist()"></select>
        <span class="badge" id="bm-prof-state"><span class="dot"></span>-</span>
      </div>
      <div class="muted" id="bm-prof-note" style="margin-top:6px"></div>
      <div class="row" style="gap:12px;flex-wrap:wrap;margin-top:4px">
        <div><label class="f" for="bm-conc">並列度 (カンマ区切り)</label><input type="text" id="bm-conc" value="1,8" style="width:110px"></div>
        <div><label class="f" for="bm-num">リクエスト数/段</label><input type="number" id="bm-num" min="1" value="24" style="width:96px"></div>
        <div><label class="f" for="bm-warm">warmup</label><input type="number" id="bm-warm" min="0" value="1" style="width:76px"></div>
        <div><label class="f" for="bm-inlen">prompt 語数≈</label><input type="number" id="bm-inlen" min="8" value="128" style="width:90px"></div>
        <div><label class="f" for="bm-outlen">max_tokens</label><input type="number" id="bm-outlen" min="1" value="128" style="width:96px"></div>
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
      <div class="row" style="margin-top:10px">
        <button class="primary need-bench" id="bm-start" onclick="bmStart()"><svg class=ic aria-hidden=true><use href=#i-play /></svg> ベンチ開始</button>
        <span class="grow"></span>
        <span class="muted" id="bm-note"></span>
      </div>
    </div>
    <div id="bm-history"></div>
  </section>
`;
/* ============================================================= benchmarks */
/* 作成済みプロファイルのサーバーを launcher 内蔵ランナーで計測する。
   実行はバックエンド (POST /api/benchmarks) が裏で走らせ、ここでは 2.5 秒ポーリングで
   progress / 履歴を描画する。並列度リスト (例 "1,8,32") の各値が 1 ステージ。 */
const BM_KEY = "llm-laun…h.v1";
const BM_INPUTS = ["bm-profile","bm-conc","bm-num","bm-warm","bm-inlen","bm-outlen",
                   "bm-temp","bm-endpoint","bm-stream","bm-autostart","bm-stopafter","bm-model"];
function bmInit() {
  let d = {};
  try { d = JSON.parse(localStorage.getItem(BM_KEY) || "{}") || {}; } catch { d = {}; }
  S.bench = { runs: [], active: null, busy: false, open: "", _pSig: "", _bh: "", _recv: 0 };
  const set = (id, v) => { const el = $(id); if (el && v != null && v !== "") {
    if (el.type === "checkbox") el.checked = !!v; else el.value = v; } };
  set("#bm-profile", d.profile); set("#bm-conc", d.conc); set("#bm-num", d.num);
  set("#bm-warm", d.warm); set("#bm-inlen", d.inlen); set("#bm-outlen", d.outlen);
  set("#bm-temp", d.temp); set("#bm-endpoint", d.endpoint);
  set("#bm-model", d.model);
  if (d.stream === false) $("#bm-stream").checked = false;
  if (d.autostart === false) $("#bm-autostart").checked = false;
  if (d.stopafter === true) $("#bm-stopafter").checked = true;
  BM_INPUTS.forEach(id => $(`#${id}`).addEventListener("change", bmPersist));
}
function bmPersist() {
  if (!S.bench) return;
  const v = id => $(id) ? $(id).value : "";
  try {
    localStorage.setItem(BM_KEY, JSON.stringify({
      profile: v("#bm-profile"), conc: v("#bm-conc"), num: v("#bm-num"), warm: v("#bm-warm"),
      inlen: v("#bm-inlen"), outlen: v("#bm-outlen"), temp: v("#bm-temp"),
      endpoint: v("#bm-endpoint"), model: v("#bm-model"),
      stream: $("#bm-stream").checked, autostart: $("#bm-autostart").checked,
      stopafter: $("#bm-stopafter").checked,
    }));
  } catch { /* 容量超過等は無視 */ }
}
function openBench(pid) {
  if (!S.bench) bmInit();
  if (pid) { $("#bm-profile").value = pid; bmPersist(); }
  switchTab("bench");
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
    concurrency: conc.length ? conc : [8],
    num_prompts: num("#bm-num", 24),
    input_len: num("#bm-inlen", 128),
    output_len: num("#bm-outlen", 128),
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
  try {
    const j = await api("/api/benchmarks");
    S.bench.runs = j.runs || []; S.bench.active = j.active; S.bench._recv = Date.now() / 1000;
    bmBadge();
    if (S.tab === "bench" || force) renderBench();
  } catch { /* ignore */ }
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
  try { await api(`/api/benchmarks/${id}`, {method: "DELETE"}); toast("削除しました"); bmPoll(true); }
  catch (e) { toast("失敗: " + e.message); }
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
  set("#bm-outlen", p.output_len); set("#bm-temp", p.temperature); set("#bm-endpoint", p.endpoint);
  set("#bm-stream", p.stream); set("#bm-autostart", p.auto_start); set("#bm-stopafter", p.stop_after);
  set("#bm-model", p.model || "");
  bmPersist(); renderBench();
  toast("パラメータを読み込みました (<svg class=ic aria-hidden=true><use href=#i-play /></svg> ベンチ開始で実行)");
}
function bmToggle(id) { S.bench.open = S.bench.open === id ? "" : id; renderBench(); }
const bmF1 = x => x == null ? "-" : (+x).toFixed(1);
const bmF2 = x => x == null ? "-" : (+x).toFixed(2);
const bmTps = x => x == null ? "-" : (+x >= 100 ? String(Math.round(x)) : (+x).toFixed(1));
function bmStatusBadge(r) {
  const map = {queued: ["info", "待機"], running: ["warn", "実行中"], completed: ["ok", "完了"],
               failed: ["err", "失敗"], cancelled: ["", "中断"], lost: ["purple", "切断記録"]};
  const [cls, label] = map[r.status] || ["", r.status];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}
function bmParamsSummary(p) {
  const c = (p.concurrency || []).join("/") || "-";
  return `${p.num_prompts}req × ${c}並列 · in≈${p.input_len}語 · out≤${p.output_len}t · ` +
    `${p.endpoint === "chat" ? "chat" : "completions"}${p.stream ? "+stream" : ""}` +
    (p.temperature ? ` · temp${p.temperature}` : " · greedy");
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
/* key = 再描画後に横スクロール位置と details の開閉を復元するための安定キー
   (実行中は "a:<run id>"、履歴は "k:<run id>"。data-logkey とは別接頭辞にする)。
   .bm-wrap は overflow-x:auto の横スクロール欄なので、key を忘れると 2.5 秒ポーリング
   の innerHTML 差し替えで scrollLeft が 0 に戻る = 「右に寄せた列が左端に飛ぶ」。 */
function bmStageTable(stages, key) {
  // 凡例の閾値はステージに保存された値を使う (STALL_S を変えても表示が追随する)
  const th = (stages.find(s => s && s.stall_threshold_s != null) || {}).stall_threshold_s || 0.5;
  // 全ステージで stall が測れていない = stream OFF 等 (古い履歴は 0 件扱いしない)
  const anyStall = stages.some(s => s && (s.stall_worst_s != null || s.stall_threshold_s != null));
  return `<div class="bm-wrap" data-scrollkey="${esc(key)}"><table class="bm"><tr>` +
    `<th>並列度</th><th>OK</th><th>req/s</th><th>出力 tok/s</th><th>合計 tok/s</th>` +
    `<th>TTFT avg</th><th>TTFT p99</th><th>遅延 p50</th><th>遅延 p99</th><th>decode tok/s</th>` +
    `<th title="生成開始後、チャンクが最も長く途切れた秒数 (ステージ中の全リクエストのワースト)">最大停止</th>` +
    `<th title="${th}s 以上の停止が発生した延べ回数 (全リクエスト合計)">停止数</th></tr>` +
    stages.map(st => `<tr>
      <td>${st.concurrency}p</td>
      <td>${st.ok}/${st.ok + st.fail}${st.aborted ? " <svg class=ic aria-hidden=true><use href=#i-stop /></svg>" : ""}</td>
      <td>${bmF2(st.rps)}</td><td><b>${bmTps(st.out_tps)}</b></td><td>${bmTps(st.total_tps)}</td>
      <td>${bmF2(st.ttft && st.ttft.avg)}s</td><td>${bmF2(st.ttft && st.ttft.p99)}s</td>
      <td>${bmF2(st.latency && st.latency.p50)}s</td><td>${bmF2(st.latency && st.latency.p99)}s</td>
      <td>${bmTps(st.decode_tps && st.decode_tps.avg)}</td>` +
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
  renderBenchActive(S.bench.runs.find(r => r.id === S.bench.active) || null);
  renderBenchHistory();
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
  const chips = (pr.stages || []).map((c, i) => {
    const st = (run.stages || [])[i];
    if (st) return `<span class="badge ok" title="${st.ok} req / ${bmF1(st.wall_secs)}s">${c}p ✓ ${bmTps(st.out_tps)} t/s</span>`;
    if (i === pr.stage && (run.status === "running" || run.status === "queued"))
      return `<span class="badge warn"><span class="dot"></span>${c}p 実行中</span>`;
    return `<span class="badge">${c}p 待機</span>`;
  }).join(" ");
  const phase = {queued: "待機中…", launching: "サーバー起動中… (数分かかります)", waiting: "サーバーの API 応答を待っています…",
                 warmup: "ウォームアップ中 (統計外)", measure: "計測中", done: "完了"}[pr.phase] || pr.phase || "";
  const doneNote = pr.phase === "measure"
    ? `リクエスト ${pr.done || 0}/${pr.total} (OK ${pr.ok || 0}${pr.fail ? ` / NG ${pr.fail}` : ""})` : "";
  // tpsNote は HTML 断片。下位で esc しない (esc すると <svg> がそのまま文字列になる)
  const tpsNote = tps != null ? ` · <svg class=ic aria-hidden=true><use href=#i-bolt /></svg> ${bmTps(tps)} tok/s 累積` : "";
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
    ${(run.stages || []).length ? bmStageTable(run.stages, "a:" + run.id) : ""}
  </div>`;
  logScrollBack(el, _ls);
}
function renderBenchHistory() {
  const el = $("#bm-history");
  const runs = (S.bench.runs || []).filter(r => r.id !== S.bench.active);
  if (!runs.length) {
    const empty = `<div class="empty" style="margin-top:12px">ベンチ履歴はまだありません。上で条件をセットして <svg class=ic aria-hidden=true><use href=#i-play /></svg> ベンチ開始。</div>`;
    if (S.bench._bh !== empty) { S.bench._bh = empty; el.innerHTML = empty; }   // 空欄も毎回は書き直さない
    return;
  }
  // スクロール中は組み直しを後回し (摘まみを離してしまう)。枠が空のときは必ず描く
  if (uiScrolling() && el.childElementCount) { deferRender("bm-hist", renderBenchHistory); return; }
  const html = `<h2 class="view-title" style="margin-top:18px">履歴 (${runs.length})</h2>` + runs.map(r => {
    const open = S.bench.open === r.id;
    const best = Math.max(0, ...(r.stages || []).map(s => s.out_tps || 0));
    return `<div class="card">
      <div class="row">
        ${bmStatusBadge(r)}
        <b class="grow ellip" style="font-size:14.5px;cursor:pointer" onclick="bmToggle('${r.id}')">${esc(r.profile_name)}</b>
        <span class="chip ${esc(r.backend)}">${esc(r.backend_name)}</span>
      </div>
      <div class="muted" style="margin-top:5px;font-size:12px">${esc(bmParamsSummary(r.params || {}))}
        ${(r.stages || []).length ? ` · peak 出力 <b style="color:var(--text)">${bmTps(best)}</b> tok/s` : ""}
        ${r.error ? ` · <span style="color:var(--err)">${esc(String(r.error).slice(0, 90))}</span>` : ""}
        · ${r.finished_at ? fmtAgo(r.finished_at) : "-"}</div>
      <div class="row" style="margin-top:8px">
        <button onclick="bmToggle('${r.id}')">${open ? "<svg class=ic aria-hidden=true><use href=#i-chev_up /></svg> 閉じる" : "<svg class=ic aria-hidden=true><use href=#i-chart /></svg> 結果"}</button>
        <button onclick="bmRerun('${r.id}')"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 設定再実行</button>
        <button class="ghost" onclick="bmExport('${r.id}')"><svg class=ic aria-hidden=true><use href=#i-download /></svg> JSON</button>
        <span class="grow"></span>
        <button class="ghost danger" onclick="bmDelete('${r.id}')">削除</button>
      </div>
      ${open && (r.stages || []).length ? bmStageTable(r.stages, "k:" + r.id) : ""}
      ${open ? `<div class="muted mono" style="margin-top:6px;font-size:11px;overflow-wrap:anywhere">
        model: ${esc(r.model || "-")} · session: ${esc(r.session_id || "-")}${r.auto_started ? " (自動起動)" : ""}${r.stopped_after ? " · 停止済み" : ""}${r.port ? ` · :${r.port}` : ""}</div>` : ""}
      ${open && r.stop_error ? `<div class="muted" style="margin-top:4px;font-size:11px;color:var(--err)">サーバーの自動停止に失敗: ${esc(r.stop_error)}</div>` : ""}
      ${open && r.server_left_running ? `<div class="muted" style="margin-top:4px;font-size:11px;color:var(--warn)">※ launcher 再起動時に自動起動したサーバーが残っています。サーバータブで停止してください</div>` : ""}
    </div>`;
  }).join("");
  // 中身が変わらなければ組み直さない = 横スクロールバーの摘まみ・開いた details を殺さない
  // (履歴は fmtAgo のラベルしか変わらないので、これで再構築は分単位に 1 回になる)
  if (html === S.bench._bh && el.childElementCount) return;
  S.bench._bh = html;
  const _ls = logScrollSnap(el);                // 組み直したときだけ横位置・開閉を復元
  el.innerHTML = html;
  logScrollBack(el, _ls);
}

