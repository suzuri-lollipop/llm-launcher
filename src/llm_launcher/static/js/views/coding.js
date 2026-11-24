/* ===================================================================
   view: coding — コーディングベンチマーク (pi ハーネスで実装を作って比較)
   =================================================================== */
"use strict";
VIEWS.coding = `
  <section id="view-coding" class="view hidden">
    <h2 class="view-title">コーディングベンチ <span class="muted">pi ハーネスにプロファイルのサーバーでバックエンド / フロントエンドを作らせ、実装を並べて比較</span></h2>
    <div class="bm-tabs" role="tablist">
      <button class="bm-tab" id="cbt-run" role="tab" aria-selected="false" onclick="cbSetTab('run')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 実行<span class="run-dot hidden" id="cbt-run-dot" title="コーディングベンチ実行中"></span></button>
      <button class="bm-tab" id="cbt-history" role="tab" aria-selected="false" onclick="cbSetTab('history')"><svg class=ic aria-hidden=true><use href=#i-history /></svg> 履歴 <span class="n" id="cbt-hn">0</span></button>
      <button class="bm-tab" id="cbt-viewer" role="tab" aria-selected="false" onclick="cbSetTab('viewer')"><svg class=ic aria-hidden=true><use href=#i-layers /></svg> ビューアー</button>
    </div>
    <div id="cbt-p-run">
    <div id="cb-active"></div>
    <div class="card">
      <div class="row" style="gap:8px">
        <select id="cb-profile" class="grow" style="min-width:180px" onchange="cbPersist()"></select>
        <span class="badge" id="cb-prof-state"><span class="dot"></span>-</span>
      </div>
      <div class="row" style="gap:8px;margin-top:8px;flex-wrap:wrap">
        <span class="badge" id="cb-pi-state"><span class="dot"></span>pi 確認中…</span>
        <button class="ghost need-bench hidden" id="cb-pi-install" onclick="cbInstallPi()" style="min-height:32px;padding:4px 10px"><svg class=ic aria-hidden=true><use href=#i-download /></svg> pi をインストール</button>
        <span class="badge" id="cb-sandbox-state"><span class="dot"></span>サンドボックス確認中…</span>
        <span class="muted" id="cb-env-note" style="font-size:11px"></span>
      </div>
      <div class="muted" style="margin-top:6px">ハーネス: <a href="https://github.com/earendil-works/pi" target="_blank" rel="noopener">pi (earendil-works/pi)</a> をサンドボックス (bubblewrap / Docker 自動選択) 内で実行します。ファイルアクセスは実行ディレクトリとスキルフォルダに制限されます。</div>
      <div style="margin-top:10px">
        <label class="f" for="cb-skills">スキル群フォルダ <span class="muted">(SKILL.md を再帰的に読み込み)</span></label>
        <div class="row" style="gap:6px">
          <input type="text" id="cb-skills" class="mono grow" placeholder="(デフォルト)" style="min-width:200px">
          <button class="ghost" onclick="cbResetField('skills')" style="min-height:36px">既定に戻す</button>
        </div>
        <div class="muted" id="cb-skills-note" style="font-size:11px;margin-top:2px"></div>
      </div>
      <div style="margin-top:8px">
        <label class="f" for="cb-agents">AGENTS.md <span class="muted">(ワークスペースにコピーされ pi が読み込む)</span></label>
        <div class="row" style="gap:6px">
          <input type="text" id="cb-agents" class="mono grow" placeholder="(デフォルト)" style="min-width:200px">
          <button class="ghost" onclick="cbResetField('agents')" style="min-height:36px">既定に戻す</button>
        </div>
      </div>
      <div style="margin-top:8px">
        <label class="f" for="cb-backend">バックエンド作成プロンプト <span class="muted">(フェーズ 1)</span></label>
        <textarea id="cb-backend" class="cb-prompt mono" rows="7" placeholder="(デフォルト)"></textarea>
        <div class="row" style="gap:6px;margin-top:2px"><button class="ghost" onclick="cbResetField('backend')" style="min-height:32px;padding:4px 10px">既定に戻す</button><span class="muted" id="cb-backend-n" style="font-size:11px"></span></div>
      </div>
      <div style="margin-top:8px">
        <label class="f" for="cb-frontend">フロントエンド作成プロンプト <span class="muted">(フェーズ 2・同じセッションを継続)</span></label>
        <textarea id="cb-frontend" class="cb-prompt mono" rows="7" placeholder="(デフォルト)"></textarea>
        <div class="row" style="gap:6px;margin-top:2px"><button class="ghost" onclick="cbResetField('frontend')" style="min-height:32px;padding:4px 10px">既定に戻す</button><span class="muted" id="cb-frontend-n" style="font-size:11px"></span></div>
      </div>
      <div class="row" style="gap:12px;flex-wrap:wrap;margin-top:10px">
        <div><label class="f" for="cb-model">モデル名 <span class="muted">(空 = 自動)</span></label><input type="text" id="cb-model" class="mono" placeholder="(自動)" style="width:180px"></div>
        <div><label class="f" for="cb-thinking">thinking</label><select id="cb-thinking" style="width:110px"><option value="off">off</option><option value="minimal">minimal</option><option value="low">low</option><option value="medium">medium</option><option value="high">high</option></select></div>
        <div><label class="f" for="cb-ptimeout">1 フェーズ上限 (秒)</label><input type="number" id="cb-ptimeout" min="30" max="14400" value="1800" style="width:100px"></div>
        <div class="grow" style="min-width:160px"><label class="f" for="cb-name">ラベル <span class="muted">(空 = 自動)</span></label><input type="text" id="cb-name" maxlength="120" placeholder="例: Qwen3-30B タスク管理" style="width:100%"></div>
      </div>
      <div class="row" style="gap:18px;flex-wrap:wrap;margin-top:8px">
        <label class="switch"><input type="checkbox" id="cb-autostart" checked><span class="track"></span><span class="lab" style="font-size:12.5px">未起動ならサーバーを自動起動</span></label>
        <label class="switch"><input type="checkbox" id="cb-stopafter"><span class="track"></span><span class="lab" style="font-size:12.5px">完了後、自動起動したサーバーを停止</span></label>
        <label class="switch"><input type="checkbox" id="cb-sandbox" checked><span class="track"></span><span class="lab" style="font-size:12.5px">サンドボックス内で実行</span></label>
      </div>
      <div class="row" style="margin-top:10px">
        <button class="primary need-bench" id="cb-start" onclick="cbStart()"><svg class=ic aria-hidden=true><use href=#i-play /></svg> コーディングベンチ開始</button>
        <span class="grow"></span>
        <span class="muted" id="cb-note"></span>
      </div>
    </div>
    </div>
    <div id="cbt-p-history" class="hidden"><div id="cb-history"></div></div>
    <div id="cbt-p-viewer" class="hidden"><div id="cb-viewer"></div></div>
  </section>
`;
/* ============================================================= coding bench */
/* pi ハーネス (https://github.com/earendil-works/pi) にプロファイルのサーバーで
   バックエンド → フロントエンドの順に実装を作らせる。実行はバックエンド
   (POST /api/coding/runs) がサンドボックス内で走らせ、2.5 秒ポーリングで進捗を描画。
   ビューアーは履歴から 2 件選んでプレビュー (生成アプリの起動 + 中継) を並べる。 */
const CB_KEY = "llm-laun…h.v1";
const CB_TABS = ["run", "history", "viewer"];
const CB_INPUTS = ["cb-profile", "cb-skills", "cb-agents", "cb-backend", "cb-frontend",
                   "cb-model", "cb-thinking", "cb-ptimeout", "cb-autostart",
                   "cb-stopafter", "cb-sandbox", "cb-name"];
const CB_PHASES = {queued: "待機", launching: "サーバー起動", waiting: "health 待ち",
                   backend: "バックエンド作成", frontend: "フロントエンド作成",
                   scanning: "走査", done: "完了"};

function cbInit() {
  let d = {};
  try { d = JSON.parse(localStorage.getItem(CB_KEY) || "{}") || {}; } catch { d = {}; }
  S.coding = { runs: [], active: null, assets: null, status: null, busy: false,
               tab: CB_TABS.includes(d.tab) ? d.tab : "run",
               selA: d.selA || "", selB: d.selB || "",
               cmp: Array.isArray(d.cmp) ? d.cmp : [],
               modeA: "preview", modeB: "preview",
               filesA: null, filesB: null, codeA: null, codeB: null,
               logOpen: false, logPhase: "backend", logText: "",
               _act: "", _hist: "", _psigA: "", _psigB: "", _codeRev: 0, _fv: {} };
  const set = (id, v) => { const el = $(id); if (el && v != null && v !== "") {
    if (el.type === "checkbox") el.checked = !!v; else el.value = v; } };
  set("#cb-profile", d.profile); set("#cb-skills", d.skills); set("#cb-agents", d.agents);
  set("#cb-backend", d.backend); set("#cb-frontend", d.frontend);
  set("#cb-model", d.model); set("#cb-thinking", d.thinking); set("#cb-ptimeout", d.ptimeout);
  set("#cb-name", d.name);
  if (d.autostart === false) $("#cb-autostart").checked = false;
  if (d.stopafter === true) $("#cb-stopafter").checked = true;
  if (d.sandbox === false) $("#cb-sandbox").checked = false;
  CB_INPUTS.forEach(id => { const el = $(`#${id}`); if (el) el.addEventListener("change", cbPersist); });
  ["cb-backend", "cb-frontend"].forEach(id => {
    const el = $(`#${id}`); if (el) el.addEventListener("input", cbPromptCount);
  });
  cbPromptCount();
  cbLoadAssets();
}
function cbPersist() {
  if (!S.coding) return;
  const v = id => $(id) ? $(id).value : "";
  try {
    localStorage.setItem(CB_KEY, JSON.stringify({
      profile: v("#cb-profile"), skills: v("#cb-skills"), agents: v("#cb-agents"),
      backend: v("#cb-backend"), frontend: v("#cb-frontend"),
      model: v("#cb-model"), thinking: v("#cb-thinking"), ptimeout: v("#cb-ptimeout"),
      name: v("#cb-name"),
      autostart: $("#cb-autostart").checked, stopafter: $("#cb-stopafter").checked,
      sandbox: $("#cb-sandbox").checked,
      tab: S.coding.tab, selA: S.coding.selA, selB: S.coding.selB,
      cmp: S.coding.cmp,
    }));
  } catch { /* ignore */ }
}
function cbPromptCount() {
  const b = $("#cb-backend"), f = $("#cb-frontend");
  if (b && $("#cb-backend-n")) $("#cb-backend-n").textContent = `${b.value.length} 文字`;
  if (f && $("#cb-frontend-n")) $("#cb-frontend-n").textContent = `${f.value.length} 文字`;
}
async function cbLoadAssets() {
  try {
    S.coding.assets = await api("/api/coding/assets");
    S.coding.status = await api("/api/coding/status");
  } catch { return; }
  const a = S.coding.assets;
  // 未入力の資材欄だけ既定値で埋める (ユーザーの編集は上書きしない)
  const fill = (id, v) => { const el = $(id); if (el && !el.value) el.value = v; };
  fill("#cb-skills", a.skills_dir); fill("#cb-agents", a.agents_path);
  fill("#cb-backend", a.backend_prompt); fill("#cb-frontend", a.frontend_prompt);
  cbPromptCount(); cbPersist();
  if (S.tab === "coding") renderCoding();
}
function cbResetField(kind) {
  const a = S.coding && S.coding.assets;
  if (!a) { toast("デフォルト資材を取得中です"); return; }
  const map = {skills: ["#cb-skills", a.skills_dir], agents: ["#cb-agents", a.agents_path],
               backend: ["#cb-backend", a.backend_prompt], frontend: ["#cb-frontend", a.frontend_prompt]};
  const [id, v] = map[kind];
  $(id).value = v;
  cbPromptCount(); cbPersist();
}
function cbSetTab(t) {
  if (!CB_TABS.includes(t)) return;
  S.coding.tab = t; cbPersist(); renderCoding();
}
async function cbPoll(force) {
  if (!S.coding) return;
  try {
    const d = await api("/api/coding/runs");
    S.coding.runs = d.runs || []; S.coding.active = d.active || null;
    if (S.tab === "coding") S.coding.status = await api("/api/coding/status");
  } catch { return; }
  try { if (S.tab === "coding" || force) renderCoding(); }
  catch (e) { console.error("coding render error:", e); }
}
async function cbInstallPi() {
  try {
    await api("/api/coding/pi/install", {method: "POST"});
    toast("pi のインストールを開始しました (npm install -g)");
  } catch (e) { toast("インストール開始失敗: " + e.message); }
  cbPoll(true);
}
async function cbStart() {
  const d = S.coding;
  if (!$("#cb-profile").value) { toast("プロファイルを選択してください"); return; }
  d.busy = true; renderCoding(); cbPersist();
  try {
    const body = {
      profile_id: $("#cb-profile").value,
      name: $("#cb-name").value,
      model: $("#cb-model").value,
      skills_dir: $("#cb-skills").value,
      agents_path: $("#cb-agents").value,
      backend_prompt: $("#cb-backend").value,
      frontend_prompt: $("#cb-frontend").value,
      thinking: $("#cb-thinking").value,
      phase_timeout: +$("#cb-ptimeout").value || 1800,
      auto_start: $("#cb-autostart").checked,
      stop_after: $("#cb-stopafter").checked,
      sandbox: $("#cb-sandbox").checked,
    };
    const run = await api("/api/coding/runs", {method: "POST", body});
    toast(`コーディングベンチを開始しました: ${run.name}`);
    d.logOpen = false;
    await cbPoll(true);
  } catch (e) { toast("開始失敗: " + e.message); }
  finally { d.busy = false; renderCoding(); }
}
async function cbCancel(id) {
  try { await api(`/api/coding/runs/${id}/cancel`, {method: "POST"}); toast("中断を要求しました"); }
  catch (e) { toast("中断失敗: " + e.message); }
  cbPoll(true);
}
async function cbDelete(id) {
  if (!confirm("この実行履歴と生成ファイルを削除しますか?")) return;
  try { await api(`/api/coding/runs/${id}`, {method: "DELETE"}); toast("削除しました"); }
  catch (e) { toast("削除失敗: " + e.message); }
  if (S.coding.selA === id) S.coding.selA = "";
  if (S.coding.selB === id) S.coding.selB = "";
  cbPoll(true);
}
async function cbClearAll() {
  if (!confirm("完了済みのコーディングベンチ履歴をすべて削除しますか? (生成ファイルも削除されます)")) return;
  try { const r = await api("/api/coding/runs", {method: "DELETE"}); toast(`${r.removed} 件削除しました`); }
  catch (e) { toast("削除失敗: " + e.message); }
  cbPoll(true);
}
function cbRerun(id) {
  const r = S.coding.runs.find(x => x.id === id);
  if (!r) return;
  const p = r.params || {};
  const set = (fid, v) => { const el = $(fid); if (el && v != null) el.value = v; };
  set("#cb-profile", r.profile_id); set("#cb-skills", p.skills_dir); set("#cb-agents", p.agents_path);
  set("#cb-backend", p.backend_prompt); set("#cb-frontend", p.frontend_prompt);
  set("#cb-model", p.model); set("#cb-thinking", p.thinking || "off");
  set("#cb-ptimeout", p.phase_timeout || 1800);
  $("#cb-autostart").checked = p.auto_start !== false;
  $("#cb-stopafter").checked = !!p.stop_after;
  $("#cb-sandbox").checked = p.sandbox !== false;
  cbPromptCount(); cbSetTab("run"); cbPersist();
  toast("実行フォームに読み込みました");
}
/* 履歴から選んでビューアーで起動 (「履歴で選択して起動」の入口) */
async function cbViewRun(id) {
  const d = S.coding;
  if (!d.selA || d.selA === id) d.selA = id;
  else if (!d.selB || d.selB === id) d.selB = id;
  else d.selB = id;
  d.codeA = d.codeB = null;
  cbSetTab("viewer"); cbPersist();
  const side = d.selA === id ? "A" : "B";
  const r = d.runs.find(x => x.id === id);
  if (r && !r.preview && !["queued", "running"].includes(r.status)) {
    try { await api(`/api/coding/runs/${id}/preview`, {method: "POST"}); }
    catch (e) { toast("プレビュー起動失敗: " + e.message); }
  }
  cbPoll(true);
}
function cbBadge(st) {
  const map = {completed: ["ok", "完了"], running: ["warn", "実行中"], queued: ["info", "待機"],
               failed: ["err", "失敗"], cancelled: ["", "中断"], lost: ["purple", "切断"]};
  const [cls, label] = map[st] || ["", st || "-"];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}
/* ------------------------------------------------------------- metrics 比較 */
/* フェーズ統計を実行単位の指標に集計 (古い履歴に無いフィールドは null) */
function cbAgg(r) {
  const phs = [r.phases && r.phases.backend, r.phases && r.phases.frontend].filter(Boolean);
  const sum = k => phs.reduce((a, p) => a + (p[k] || 0), 0);
  const calls = sum("tool_calls"), errs = sum("tool_errors");
  const tout = sum("tokens_out"), genSum = sum("gen_secs_sum"), genN = sum("gen_n");
  const ttftSum = sum("ttft_sum"), ttftN = sum("ttft_n");
  const turnSum = sum("turn_secs_sum"), turnN = sum("turn_n");
  return {
    secs: r.started_at != null && r.finished_at != null ? r.finished_at - r.started_at : null,
    bsecs: r.phases && r.phases.backend ? r.phases.backend.secs : null,
    fsecs: r.phases && r.phases.frontend ? r.phases.frontend.secs : null,
    turns: sum("turns"), calls, errs, errRate: calls ? errs / calls : null,
    tout, tin: sum("tokens_in"),
    genAvg: genN ? genSum / genN : null,
    turnAvg: turnN ? turnSum / turnN : null,
    ttftAvg: ttftN ? ttftSum / ttftN : null,
    tps: genSum > 0 ? tout / genSum : null,
    files: r.files ? r.files.count : 0, bytes: r.files ? r.files.bytes : 0,
  };
}
const CB_METRICS = [
  {k: "secs", label: "実装時間", fmt: v => fmtUp(v), better: "low"},
  {k: "bsecs", label: "うち backend", fmt: v => fmtUp(v), better: "low"},
  {k: "fsecs", label: "うち frontend", fmt: v => fmtUp(v), better: "low"},
  {k: "turns", label: "ターン数", fmt: v => v, better: null},
  {k: "calls", label: "ツール呼び出し", fmt: v => v, better: null},
  {k: "errs", label: "ツールミス (率)", fmt: (v, a) => `${v} (${a.errRate != null ? Math.round(a.errRate * 100) : 0}%)`, better: "low"},
  {k: "tout", label: "生成トークン", fmt: v => v, better: null},
  {k: "tin", label: "入力トークン", fmt: v => v, better: null},
  {k: "genAvg", label: "平均思考 (生成) 時間/ターン", fmt: v => v != null ? v.toFixed(2) + "s" : "-", better: "low"},
  {k: "turnAvg", label: "平均ターン時間 (ツール込み)", fmt: v => v != null ? v.toFixed(2) + "s" : "-", better: "low"},
  {k: "ttftAvg", label: "平均 TTFT", fmt: v => v != null ? v.toFixed(2) + "s" : "-", better: "low"},
  {k: "tps", label: "平均デコード速度", fmt: v => v != null ? v.toFixed(1) + " tok/s" : "-", better: "high"},
  {k: "files", label: "生成ファイル数", fmt: v => v, better: null},
];
/* 実行を列にした比較表。better=low/high の行は最良値を緑で強調 (2 件以上で差があるとき) */
function cbMetricsTable(runs, key) {
  const aggs = runs.map(cbAgg);
  const head = `<tr><th class="cb-metric">指標</th>${runs.map((r, i) =>
    `<th title="${esc(r.profile_name)} / ${esc(r.model || "")}">${esc(r.name)}${i === 0 ? ' <span class="muted">(基準)</span>' : ""}<div class="muted" style="font-weight:400">${esc(r.model || r.profile_name)}</div></th>`).join("")}</tr>`;
  const rows = CB_METRICS.map(m => {
    const vals = aggs.map(a => a[m.k]);
    let best = -1;
    if (m.better && runs.length >= 2) {
      const nn = vals.map((v, i) => [v, i]).filter(([v]) => v != null);
      if (nn.length >= 2 && new Set(nn.map(([v]) => v)).size >= 2) {
        nn.sort((a, b) => m.better === "low" ? a[0] - b[0] : b[0] - a[0]);
        best = nn[0][1];
      }
    }
    return `<tr><td class="cb-metric">${m.label}</td>${vals.map((v, i) =>
      `<td class="${i === best ? "cb-best" : ""}">${v == null ? "-" : m.fmt(v, aggs[i])}</td>`).join("")}</tr>`;
  }).join("");
  return `<div class="bm-wrap" data-scrollkey="${esc(key)}"><table class="cb-table"><thead>${head}</thead><tbody>${rows}</tbody></table></div>`;
}
function cbCmpToggle(id) {
  const d = S.coding;
  const i = d.cmp.indexOf(id);
  if (i >= 0) d.cmp.splice(i, 1);
  else { d.cmp.push(id); if (d.cmp.length > 6) d.cmp.shift(); }
  // チェック操作は S.cmp が正 = 今の DOM 状態を基準に引き直す (さもないと
  // formDirty が「未保存編集」と誤認して行更新を止め続ける)
  const rowsEl = $("#cb-history [data-rows]"); if (rowsEl) stampForm(rowsEl);
  cbPersist(); renderCodingHistory();
}
function cbCmpClear() { S.coding.cmp = []; cbPersist(); renderCodingHistory(); }
/* ------------------------------------------------------------- live card */
async function cbToggleLog() {
  S.coding.logOpen = !S.coding.logOpen;
  if (S.coding.logOpen) await cbFetchLog();
  renderCoding();
}
async function cbFetchLog() {
  const d = S.coding;
  const run = d.runs.find(r => r.id === d.active);
  const phase = run && ["backend", "frontend"].includes(run.progress.phase)
    ? run.progress.phase : d.logPhase;
  d.logPhase = phase;
  try {
    const r = await api(`/api/coding/runs/${d.active}/log?phase=${phase}`);
    d.logText = (r.log || "") + (r.stderr ? "\n--- stderr ---\n" + r.stderr : "");
  } catch { d.logText = "(ログ取得失敗)"; }
}
function renderCodingActive(run) {
  const el = $("#cb-active");
  if (!el) return;
  if (!run) { setHtml(el, "", "cb-act", () => renderCodingActive(S.coding.runs.find(r => r.id === S.coding.active) || null)); return; }
  if (uiScrolling() && el.childElementCount) { deferRender("cb-act", () => renderCodingActive(run)); return; }
  // ライブ出力 / ログを選択して読んでいる間・アニメーション中は組み直しを後回し
  if (uiBusyIn(el) && el.childElementCount) { deferRender("cb-act", () => renderCodingActive(run)); return; }
  const html = cbActiveCard(run);
  // ライブ更新は実行中だけ。完了済みカードは内容が変わらない限り組み直さない
  if (html === S.coding._act && el.childElementCount) return;
  S.coding._act = html;
  el.innerHTML = html;
}
/* 実行中カードの HTML (完了後は固定 = キャッシュで書き換えスキップ) */
function cbActiveCard(run) {
  const p = run.progress || {};
  const steps = ["launching", "waiting", "backend", "frontend", "scanning"];
  const idx = steps.indexOf(p.phase);
  const stepHtml = steps.map((s, i) => {
    const cls = i < idx ? "ok" : i === idx ? "warn" : "";
    return `<span class="badge ${cls}"><span class="dot"></span>${CB_PHASES[s] || s}</span>`;
  }).join(`<span class="muted">→</span>`);
  return `
  <div class="card cb-live">
    <div class="row">
      <b class="grow ellip">${esc(run.name)}</b>
      ${cbBadge(run.status)}
      <button class="danger" onclick="cbCancel('${run.id}')" style="min-height:34px;padding:5px 12px">${IC("stop")} 中断</button>
    </div>
    <div class="row" style="gap:6px;margin-top:8px">${stepHtml}</div>
    <div class="row" style="gap:14px;margin-top:8px;flex-wrap:wrap">
      <span class="muted">ターン <b>${p.turns || 0}</b></span>
      <span class="muted">ツール <b>${p.tool_calls || 0}</b></span>
      ${p.tool_errors ? `<span class="muted" style="color:var(--err)">ツールエラー <b>${p.tool_errors}</b></span>` : ""}
      <span class="muted">tokens <b>${(p.tokens_in || 0) + (p.tokens_out || 0)}</b> (in ${p.tokens_in || 0} / out ${p.tokens_out || 0})</span>
      <span class="muted">経過 <b>${fmtUp(p.elapsed || 0)}</b></span>
      ${p.gen_avg != null ? `<span class="muted">平均思考 <b>${p.gen_avg}s</b>/ターン</span>` : ""}
      ${p.last_tool ? `<span class="badge info">${IC("wrench")} ${esc(p.last_tool)}</span>` : ""}
    </div>
    ${run.warning ? `<div class="cb-warn">${IC("alert")} ${esc(run.warning)}</div>` : ""}
    ${p.text ? `<div class="cb-live-text muted">${esc(p.text)}</div>` : ""}
    <div class="row" style="margin-top:8px">
      <button class="ghost" onclick="cbToggleLog()" style="min-height:32px;padding:4px 10px">${IC("article")} ${S.coding.logOpen ? "ログを隠す" : "pi ログを見る"}</button>
      <span class="muted" style="font-size:11px">${esc(run.profile_name)} / ${esc(run.model || "(モデル解決中)")} / ${run.params && run.params.sandbox === false ? "サンドボックス OFF" : "サンドボックス内"}</span>
    </div>
    ${S.coding.logOpen ? `<pre class="cb-log mono">${esc(S.coding.logText || "(まだログがありません)")}</pre>` : ""}
  </div>`;
}
/* ------------------------------------------------------------- history */
function renderCodingHistory() {
  const el = $("#cb-history");
  if (!el) return;
  const runs = S.coding.runs;
  $("#cbt-hn").textContent = runs.length;
  if (uiScrolling() && el.childElementCount) { deferRender("cb-hist", renderCodingHistory); return; }
  // 選択 / チェックボックス操作中 / アニメーション中は組み直しを後回し
  if (uiBusyIn(el) && el.childElementCount) { deferRender("cb-hist", renderCodingHistory); return; }
  if (!runs.length) {
    const empty = `<div class="card muted">まだ実行履歴がありません。実行タブからコーディングベンチを開始してください。</div>`;
    if (S.coding._hist !== empty) { S.coding._hist = empty; el.innerHTML = empty; }   // 空欄も毎回は書き直さない
    return;
  }
  const rows = runs.map(r => {
    const secs = r.started_at != null && r.finished_at != null ? r.finished_at - r.started_at : null;
    const ph = r.phases || {};
    const tools = (ph.backend ? ph.backend.tool_calls : 0) + (ph.frontend ? ph.frontend.tool_calls : 0);
    const terr = (ph.backend ? ph.backend.tool_errors || 0 : 0) + (ph.frontend ? ph.frontend.tool_errors || 0 : 0);
    const toks = (ph.backend ? ph.backend.tokens_out : 0) + (ph.frontend ? ph.frontend.tokens_out : 0);
    const a = cbAgg(r);
    const viewable = !["queued", "running"].includes(r.status);
    const checked = S.coding.cmp.includes(r.id);
    return `
    <div class="card cb-hrow" data-row="${esc(r.id)}">
      <div class="row">
        <label class="cb-cmp" title="比較表に追加 (最大 6 件)"><input type="checkbox" ${checked ? "checked" : ""} onchange="cbCmpToggle('${r.id}')"><span>比較</span></label>
        <b class="grow ellip">${esc(r.name)}</b>
        ${cbBadge(r.status)}
        ${r.preview ? `<span class="badge ok"><span class="dot"></span>プレビュー中</span>` : ""}
      </div>
      <div class="muted" style="margin-top:4px">${esc(r.profile_name)} [${esc(r.backend_name || r.backend)}] / ${esc(r.model || "-")} / ${fmtDT(r.created_at)}</div>
      <div class="row" style="gap:12px;margin-top:4px;flex-wrap:wrap">
        <span class="muted">ファイル <b>${r.files ? r.files.count : 0}</b></span>
        <span class="muted">ツール <b>${tools}</b></span>
        ${terr ? `<span class="muted" style="color:var(--err)">ツールミス <b>${terr}</b></span>` : ""}
        <span class="muted">出力tok <b>${toks}</b></span>
        <span class="muted">所要 <b>${secs != null ? fmtUp(secs) : "-"}</b></span>
        ${a.genAvg != null ? `<span class="muted">平均思考 <b>${a.genAvg.toFixed(2)}s</b>/ターン</span>` : ""}
        ${a.tps != null ? `<span class="muted">デコード <b>${a.tps.toFixed(1)}</b> tok/s</span>` : ""}
        ${r.error ? `<span class="muted" style="color:var(--err)">${esc(r.error.slice(0, 120))}</span>` : ""}
      </div>
      ${r.warning ? `<div class="cb-warn">${IC("alert")} ${esc(r.warning)}</div>` : ""}
      <div class="row" style="gap:6px;margin-top:8px">
        <button class="primary" ${viewable ? "" : "disabled"} onclick="cbViewRun('${r.id}')" style="min-height:34px;padding:5px 12px">${IC("layers")} 見て比較</button>
        <button class="ghost" onclick="cbRerun('${r.id}')" style="min-height:34px;padding:5px 12px">${IC("refresh")} 再実行</button>
        <button class="ghost" onclick="cbDelete('${r.id}')" style="min-height:34px;padding:5px 12px;color:var(--err)">${IC("trash")} 削除</button>
      </div>
    </div>`;
  }).join("");
  const cmpRuns = S.coding.cmp.map(id => runs.find(r => r.id === id)).filter(Boolean);
  const cmpPanel = cmpRuns.length >= 2 ? `
    <div class="card">
      <div class="row" style="margin-bottom:6px">
        <b class="grow">${IC("chart")} メトリクス比較 (${cmpRuns.length} 件・先頭が基準)</b>
        <button class="ghost" onclick="cbCmpClear()" style="min-height:32px;padding:4px 10px">クリア</button>
      </div>
      ${cbMetricsTable(cmpRuns, "cbm:hist")}
      <div class="muted" style="font-size:11px;margin-top:6px">緑 = その行の最良値 (時間・ミスは少ない方、デコード速度は速い方)。ツールミス = pi の引数検証エラー等。平均思考時間 = アシスタント応答の生成開始〜完了の平均。</div>
    </div>` : "";
  const head = `
    <div class="row" style="margin-bottom:8px">
      <span class="muted grow">${runs.length} 件 (新しい順) ・「比較」チェックで最大 6 件までメトリクス比較表</span>
      <button class="ghost" onclick="cbClearAll()" style="color:var(--err)">${IC("trash")} 完了分を一括削除</button>
    </div>`;
  const html = head + cmpPanel + rows;
  // 中身が変わらなければ組み直さない (2.5 秒ごとの選択解除・チェック飛びを防ぐ)
  if (html === S.coding._hist && el.childElementCount) return;
  S.coding._hist = html;
  // 見出し / 比較表 / 行を分離: 変わった行だけ組み直す (patchRows)
  if (!el.querySelector("[data-rows]")) el.innerHTML = `<div data-head></div><div data-cmp></div><div data-rows></div>`;
  setHtml(el.querySelector("[data-head]"), head, "cb-hist-head", renderCodingHistory);
  setHtml(el.querySelector("[data-cmp]"), cmpPanel, "cb-hist-cmp", renderCodingHistory);
  patchRows(el.querySelector("[data-rows]"), rows, "cb-hist", renderCodingHistory);
}
/* ------------------------------------------------------------- viewer (2 実装並列) */
function cbSel(side, rid) {
  const d = S.coding;
  d["sel" + side] = rid;
  d["code" + side] = null;
  cbPersist(); renderCodingViewer();
}
function cbSwap() {
  const d = S.coding;
  [d.selA, d.selB] = [d.selB, d.selA];
  [d.modeA, d.modeB] = [d.modeB, d.modeA];
  [d.codeA, d.codeB] = [d.codeB, d.codeA];
  const a = $("#cb-selA"), b = $("#cb-selB");
  if (a) a.value = d.selA;
  if (b) b.value = d.selB;
  cbPersist(); renderCodingViewer();
}
function cbMode(side, m) {
  const d = S.coding;
  d["mode" + side] = m;
  d["code" + side] = null;
  renderCodingViewer();
  if (m === "files") cbEnsureFiles(side);
}
async function cbEnsureFiles(side) {
  const d = S.coding;
  const rid = d["sel" + side];
  if (!rid || (d["files" + side] && d._fv[side] === rid)) return;
  try {
    const r = await api(`/api/coding/runs/${rid}/files`);
    d["files" + side] = r.files || [];
    d._fv[side] = rid;
  } catch (e) { toast("ファイル一覧の取得失敗: " + e.message); d["files" + side] = []; }
  renderCodingViewer();
}
async function cbPickFile(side, path) {
  const d = S.coding;
  const rid = d["sel" + side];
  try {
    d["code" + side] = await api(`/api/coding/runs/${rid}/file?path=${encodeURIComponent(path)}`);
    d._codeRev++;
  } catch (e) { toast("ファイル取得失敗: " + e.message); }
  renderCodingViewer();
}
function cbCloseCode(side) {
  S.coding["code" + side] = null;
  renderCodingViewer();
}
async function cbPreviewStart(side) {
  const rid = S.coding["sel" + side];
  if (!rid) return;
  try { await api(`/api/coding/runs/${rid}/preview`, {method: "POST"}); }
  catch (e) { toast("プレビュー起動失敗: " + e.message); }
  cbPoll(true);
}
async function cbPreviewStop(side) {
  const rid = S.coding["sel" + side];
  if (!rid) return;
  try { await api(`/api/coding/runs/${rid}/preview`, {method: "DELETE"}); }
  catch (e) { toast("プレビュー停止失敗: " + e.message); }
  cbPoll(true);
}
function cbReload(side) {
  const f = $(`#cb-frame${side}`);
  if (!f) return;
  try { f.contentWindow.location.reload(); }
  catch { f.src = f.src; }
}
function cbViewerOptions(selected) {
  const runs = S.coding.runs.filter(r => !["queued", "running"].includes(r.status));
  return `<option value="">(実行を選択)</option>` + runs.map(r =>
    `<option value="${r.id}" ${r.id === selected ? "selected" : ""}>${esc(r.name)} — ${esc(r.profile_name)} (${fmtDT(r.created_at)})</option>`).join("");
}
function cbPane(side) {
  const d = S.coding;
  const rid = d["sel" + side];
  const run = d.runs.find(r => r.id === rid);
  const mode = d["mode" + side];
  if (!run) {
    return `<div class="cb-pane-hd"><b>実装 ${side}</b></div>
      <div class="cb-ph muted">上のドロップダウンから実行履歴を選択してください。<br>履歴タブの「見て比較」からも選べます。</div>`;
  }
  const pv = run.preview;
  const running = pv && (pv.mode === "static" || pv.port);
  const ph = run.phases || {};
  const tools = (ph.backend ? ph.backend.tool_calls : 0) + (ph.frontend ? ph.frontend.tool_calls : 0);
  const secs = run.started_at != null && run.finished_at != null ? run.finished_at - run.started_at : null;
  const stats = `<div class="muted cb-stats">${esc(run.profile_name)} / ${esc(run.model || "-")} ・
    ツール ${tools} ・ 出力tok ${(ph.backend ? ph.backend.tokens_out : 0) + (ph.frontend ? ph.frontend.tokens_out : 0)} ・
    ${secs != null ? fmtUp(secs) : "-"} ・ ファイル ${run.files ? run.files.count : 0}</div>`;
  let toolbar = `<div class="cb-toolbar">`;
  if (mode === "preview") {
    toolbar += running
      ? `<button onclick="cbPreviewStop('${side}')" style="min-height:32px;padding:4px 10px;color:var(--err)">${IC("stop")} 停止</button>
         <button onclick="cbReload('${side}')" style="min-height:32px;padding:4px 10px">${IC("refresh")} 再読込</button>
         <span class="badge ok"><span class="dot"></span>${pv.mode === "static" ? "静的配信中" : "ポート " + pv.port}</span>`
      : `<button class="primary" onclick="cbPreviewStart('${side}')" style="min-height:32px;padding:4px 10px">${IC("play")} 起動</button>
         <span class="muted" style="font-size:11px">生成アプリをサンドボックス内で起動して表示</span>`;
  } else {
    toolbar += `<button onclick="cbMode('${side}','preview')" style="min-height:32px;padding:4px 10px">${IC("monitor")} プレビューへ</button>`;
    if (d["code" + side]) toolbar += `<button onclick="cbCloseCode('${side}')" style="min-height:32px;padding:4px 10px">${IC("chev_up")} 一覧へ</button>`;
  }
  toolbar += `<span class="grow"></span>
    <select onchange="cbMode('${side}', this.value)" style="min-height:32px;padding:2px 8px">
      <option value="preview" ${mode === "preview" ? "selected" : ""}>プレビュー</option>
      <option value="files" ${mode === "files" ? "selected" : ""}>ファイル</option>
    </select></div>`;
  let body;
  if (mode === "preview") {
    if (running) {
      const src = pv.mode === "static"
        ? `/api/coding/runs/${rid}/static/`
        : `/api/coding/runs/${rid}/proxy/`;
      body = `<iframe id="cb-frame${side}" class="cb-frame" src="${src}" title="実装 ${side} のプレビュー"
        sandbox="allow-scripts allow-forms allow-same-origin allow-popups allow-modals"></iframe>`;
    } else {
      body = `<div class="cb-ph muted">プレビューは停止中です。<br>「起動」を押すと生成アプリ (backend/server.py 等) を実行します。<br>
        バックエンドが無い実装は frontend/ を静的配信します。</div>`;
    }
  } else {
    const code = d["code" + side];
    if (code) {
      body = `<div class="cb-code-hd mono ellip">${esc(code.path)} ${code.truncated ? '<span class="badge warn">先頭 512KB のみ</span>' : ""}</div>
        <pre class="cb-code mono">${esc(code.text)}</pre>`;
    } else {
      const files = d["files" + side];
      body = files == null
        ? `<div class="cb-ph muted">読み込み中…</div>`
        : files.length === 0
          ? `<div class="cb-ph muted">ファイルがありません</div>`
          : `<div class="cb-files">` + files.map(f =>
              `<button class="cb-file mono" onclick="cbPickFile('${side}', '${esc(f.path)}')">${IC("file")} ${esc(f.path)} <span class="muted">${f.size} B</span></button>`).join("") + `</div>`;
    }
  }
  return `
    <div class="cb-pane-hd"><b class="grow ellip">${esc(run.name)}</b> ${cbBadge(run.status)}</div>
    ${stats}${toolbar}<div class="cb-body">${body}</div>`;
}
function renderCodingViewer() {
  const el = $("#cb-viewer");
  if (!el) return;
  const d = S.coding;
  const viewable = d.runs.filter(r => !["queued", "running"].includes(r.status));
  if (!viewable.length) {
    setHtml(el, `<div class="card muted">まだ比較できる実行がありません。実行タブでコーディングベンチを完了させてください。</div>`, "cb-viewer", renderCodingViewer);
    return;
  }
  // 骨格 (選択行 + 2 ペイン) は一度だけ作る。ペインごとに差分描画するので、
  // A を変更しても B の iframe は読み直されない (ポーリングでも壊れない)。
  if (!$("#cb-selA") || !$("#cb-paneA")) {
    el.innerHTML = `
    <div class="row cb-selrow">
      <div class="grow"><label class="f">実装 A</label><select id="cb-selA" onchange="cbSel('A', this.value)"></select></div>
      <button class="ghost" onclick="cbSwap()" title="A/B 入れ替え" style="align-self:flex-end">${IC("refresh")} 入替</button>
      <div class="grow"><label class="f">実装 B</label><select id="cb-selB" onchange="cbSel('B', this.value)"></select></div>
    </div>
    <div id="cb-vtable"></div>
    <div class="cb-grid"><div class="cb-pane" id="cb-paneA"></div><div class="cb-pane" id="cb-paneB"></div></div>
    <div class="muted" style="margin-top:8px;font-size:11.5px">プレビューは launcher 経由で中継されるため LAN のスマホからも表示できます。生成アプリはサンドボックス内で起動しますが、ブラウザ上では launcher と同一オリジンで動く点に注意してください。</div>`;
  }
  // 選択肢は開いている (フォーカスがある) 間組み直さない = 選択が飛ばない
  setOptions($("#cb-selA"), cbViewerOptions(d.selA), "cb-selA", renderCodingViewer);
  setOptions($("#cb-selB"), cbViewerOptions(d.selB), "cb-selB", renderCodingViewer);
  // A/B 両方選択中はメトリクス比較表をペーンの上に出す (iframe には触らない)
  const vt = $("#cb-vtable");
  if (vt) {
    const ra = d.runs.find(r => r.id === d.selA), rb = d.runs.find(r => r.id === d.selB);
    const sig = (ra ? ra.id + ":" + (ra.finished_at || 0) : "") + "|" + (rb ? rb.id + ":" + (rb.finished_at || 0) : "");
    if (vt._sig !== sig) {
      vt._sig = sig;
      vt.innerHTML = ra && rb ? `<div class="card" style="padding:10px 12px">${cbMetricsTable([ra, rb], "cbm:viewer")}</div>` : "";
    }
  }
  ["A", "B"].forEach(side => {
    const rid = d["sel" + side];
    const run = d.runs.find(r => r.id === rid);
    const pv = run && run.preview ? (run.preview.mode + ":" + (run.preview.port || "")) : "";
    const code = d["code" + side];
    const files = d["files" + side];
    const sig = JSON.stringify([rid, d["mode" + side], pv, run ? run.status : "",
      code ? code.path : "", d._codeRev, d._fv[side], files ? files.length : -1]);
    if (sig !== d["_psig" + side]) {
      const paneEl = $("#cb-pane" + side);
      // プレビュー iframe を操作中 / コードを選択中はペインの組み直しを後回し
      if (uiBusyIn(paneEl) && paneEl.childElementCount) { deferRender("cb-pane" + side, renderCodingViewer); return; }
      d["_psig" + side] = sig;
      paneEl.innerHTML = cbPane(side);
    }
  });
  if (d.modeA === "files") cbEnsureFiles("A");
  if (d.modeB === "files") cbEnsureFiles("B");
}
/* ------------------------------------------------------------- dispatch */
function renderCoding() {
  const d = S.coding;
  if (!d) return;
  CB_TABS.forEach(t => {
    const b = $(`#cbt-${t}`), p = $(`#cbt-p-${t}`);
    if (b) { b.classList.toggle("on", d.tab === t); b.setAttribute("aria-selected", d.tab === t ? "true" : "false"); }
    if (p) p.classList.toggle("hidden", d.tab !== t);
  });
  $("#cbt-run-dot").classList.toggle("hidden", !d.active);
  /* プロファイル選択肢 (起動タブと同じく S.state.profiles から): setOptions =
     顔ぶれが変わらなければ触らない / ドロップダウンを開いている間は組み直さない。
     2.5 秒ポーリングのたびに選択肢を作り直して選択が飛ぶのを防ぐ。 */
  const sel = $("#cb-profile");
  const profs = S.state ? (S.state.profiles || []) : [];
  setOptions(sel, `<option value="">(プロファイルを選択)</option>` + profs.map(p =>
    `<option value="${p.id}">${esc(p.name)} [${esc((backendById(p.backend) || {}).name || p.backend)}]</option>`).join(""),
    "cb-profile", renderCoding);
  const live = sel.value ? liveOf(sel.value) : null;
  const pb = statusBadge(live).replace('class="badge', 'id="cb-prof-state" class="badge');
  if ($("#cb-prof-state")._badge !== pb) {
    $("#cb-prof-state").outerHTML = pb;
    $("#cb-prof-state")._badge = pb;
  }
  // pi / サンドボックスの状態チップ
  const st = d.status;
  if (st) {
    const pi = st.pi || {};
    const installing = pi.install && pi.install.status === "running";
    const piEl = $("#cb-pi-state");
    piEl.className = "badge " + (pi.available ? "ok" : installing ? "warn" : "err");
    piEl.innerHTML = `<span class="dot"></span>` + (pi.available
      ? `pi ${esc(pi.version || "")}` : installing ? "pi インストール中…" : "pi 未インストール");
    if (pi.install && pi.install.status === "failed") piEl.title = pi.install.log || "";
    $("#cb-pi-install").classList.toggle("hidden", pi.available || installing);
    const sb = st.sandbox || {};
    const sbEl = $("#cb-sandbox-state");
    sbEl.className = "badge " + (sb.backend ? "ok" : "warn");
    sbEl.innerHTML = `<span class="dot"></span>` + (sb.backend
      ? `サンドボックス: ${esc(sb.backend)}` : "サンドボックス不可");
    if (sb.hint) sbEl.title = sb.hint;
    $("#cb-env-note").textContent = st.exclusive_owner ? `実走ロック: ${st.exclusive_owner}` : "";
  }
  const a = d.assets;
  $("#cb-skills-note").textContent = a && a.skills
    ? `デフォルトスキル: ${a.skills.map(s => s.name).join(" / ")}` : "";
  $("#cb-start").disabled = d.busy || !!d.active;
  $("#cb-note").textContent = d.active ? "実行中は開始できません (同時実走は 1 件)" : "";
  renderCodingActive(d.runs.find(r => r.id === d.active) || null);
  renderCodingHistory();
  renderCodingViewer();
  if (d.logOpen && d.active) cbFetchLog().then(() => {
    const pre = $(".cb-log");
    const txt = d.logText || "(まだログがありません)";
    if (pre && pre.textContent !== txt) pre.textContent = txt;   // 変化なしの書き込みは選択を壊す
  });
}