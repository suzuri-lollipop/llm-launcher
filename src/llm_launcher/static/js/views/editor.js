/* ===================================================================
   editor: プロフィール編集ダイアログ — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
/* ============================================================= editor */
/* バックエンドを選ぶと、そのプロジェクトの起動パラメータ一式が
   既定値入りでフォームに表示される (引数をゼロから書く必要なし)
   各項目の ON/OFF が「フラグ自体を送るか」のスイッチ。OFF の間はモジュール側の
   auto / 生成設定判定に委ねられる (GUI 既定値で上書きしない)。 */
function defaultsFor(b) {
  const v = {};
  (b?.fields || []).forEach(f => {
    if (f.default) v[f.key] = f.default;
    else if (f.type === "bool") v[f.key] = false;
  });
  return v;
}
function defaultsOn(b, savedValues, savedOn) {
  const on = {};
  (b?.fields || []).forEach(f => {
    if (f.type === "bool") return;
    if (savedOn && f.key in savedOn) on[f.key] = !!savedOn[f.key];
    else if (savedOn === null) on[f.key] = String((savedValues||{})[f.key] ?? "").trim() !== "" || !!f.default_on; // 旧プロファイル: 従来動作
    else on[f.key] = !!f.default_on;
  });
  return on;
}
function openEditor(pid) {                 // 起点: 履歴経由で開く (戻るで閉じられる)
  go(routeUrl(S.tab, "editor", pid || "new"));
}
function openEditorDom(pid) {              // DOM 操作本体。ルーター (syncDialog) から呼ばれる
  if (!can("profiles.edit")) { toast("プロファイル編集は管理者が開示を OFF にしています"); return; }
  let draft;
  if (pid) {
    const p = profileById(pid); if (!p) return;
    if (p.redacted) { toast("設定値は管理者のみ閲覧できます (管理者に依頼してください)"); return; }
    draft = JSON.parse(JSON.stringify(p));
    if (draft.on === undefined || draft.on === null) {
      draft.on = defaultsOn(backendById(draft.backend), draft.values, null);
    } else {
      draft.on = defaultsOn(backendById(draft.backend), draft.values, draft.on);
    }
  } else {
    const b = backendById("vllm");
    draft = {name:"", backend:"vllm", values: defaultsFor(b), on: defaultsOn(b, {}, {}),
             extra_args:"", env:{},
             cwd:"", command:"", custom_only:false, note:""};
  }
  S.editing = draft;
  $("#modal").style.display = "flex"; $("#modal-veil").style.display = "block";
  renderEditor();
  if (!S.hf) loadHf().then(() => { if (S.editing) renderEditor(); });
}
function closeEditor() {                   // ✕ / Esc / 背景クリック / 保存成功
  if (_openDialog) history.back();         // 履歴条目が消える → applyRoute がダイアログを閉じる
  else closeEditorDom();
}
function closeEditorDom() {
  S.editing = null;
  $("#modal").style.display = "none"; $("#modal-veil").style.display = "none";
}
function setBackend(bid) {
  const d = S.editing; if (!d || d.backend === bid) return;
  const b = backendById(bid); if (!b) return;
  collectDraft();                       // 現在の入力値は env/command 等を保持
  d.backend = bid;
  d.values = defaultsFor(b);            // パラメータはバックエンド既定値から出し直し
  d.on = defaultsOn(b, {}, {});
  schedulePreview();
  renderEditor();
  toast(`${b.name} の起動パラメータを表示しました (既定値入り)`);
}
function inputVal(sel) { const el = $(sel); return el ? el.value : null; }
function collectDraft() {
  const d = S.editing; if (!d) return d;
  const n = inputVal("#ed-name"); if (n !== null) d.name = n;
  const q = inputVal("#ed-note"); if (q !== null) d.note = q;
  const x = inputVal("#ed-extra"); if (x !== null) d.extra_args = x;
  const w = inputVal("#ed-cwd");    if (w !== null) d.cwd = w;
  const m = inputVal("#ed-command"); if (m !== null) d.command = m;
  const o = $("#ed-custom");        if (o) d.custom_only = o.checked;
  const v = $("#ed-simple");        if (v) d.simple_visible = v.checked;
  const e = inputVal("#ed-env");    if (e !== null) d.env = parseEnv(e);
  document.querySelectorAll("[data-fkey]").forEach(el => {
    d.values[el.dataset.fkey] = el.type === "checkbox" ? el.checked : el.value;
  });
  document.querySelectorAll("[data-onkey]").forEach(el => {
    d.on[el.dataset.onkey] = el.checked;
  });
  return d;
}
function parseEnv(text) {
  const env = {};
  (text || "").split("\n").forEach(line => {
    line = line.trim(); if (!line) return;
    const i = line.indexOf("="); if (i < 0) return;
    env[line.slice(0, i).trim()] = line.slice(i + 1).trim();
  });
  return env;
}
const envToText = env => Object.entries(env || {}).map(([k, v]) => `${k}=${v}`).join("\n");

let previewTimer = null;
function schedulePreview() { clearTimeout(previewTimer); previewTimer = setTimeout(showPreview, 400); }
function editorBody() {
  const d = collectDraft();
  return {name:d.name, backend:d.backend, values:d.values, on:d.on, extra_args:d.extra_args,
          env:d.env, cwd:d.cwd, command:d.command, custom_only:d.custom_only, note:d.note,
          simple_visible:!!d.simple_visible};
}
async function showPreview() {
  const el = $("#ed-preview"); if (!el || !S.editing) return;
  try {
    const r = await api("/api/preview", {method:"POST", body: editorBody()});
    el.innerHTML = r.ok ? esc(r.command) : IC("alert") + " " + esc(r.error);
    el.style.color = r.ok ? "#188038" : "var(--err)";
  } catch (e) { el.innerHTML = IC("alert") + " " + esc(e.message); el.style.color = "var(--err)"; }
}
/* ---- HuggingFace Hub キャッシュからのモデル選択 ------------------------ */
const fmtSize = b => b > (1<<30) ? (b/(1<<30)).toFixed(1)+"GB" : b > (1<<20) ? Math.round(b/(1<<20))+"MB" : b;
async function loadHf(force = false) {
  if (S.hf && !force) return S.hf;
  try { S.hf = await api("/api/hf-models" + (force ? "?refresh=1" : "")); }
  catch { S.hf = {cache_dir:"?", exists:false, models:[]}; }
  return S.hf;
}
function hfPickerHtml(f) {
  const hf = S.hf;
  if (hf === undefined) return `<div class="muted" style="font-size:11.5px;margin-top:4px">HFキャッシュを確認中…</div>`;
  const dirNote = `<div class="muted mono" style="font-size:10.5px;margin-top:3px">${esc(hf.cache_dir)} · <a href="#" onclick="hfReload();return false">再スキャン</a></div>`;
  const isGguf = S.editing?.backend === "llamacpp";
  const opts = [];
  for (const m of (hf.models || [])) {
    const tag = m.source === "data" ? "[データ] " : "";
    if (isGguf) {
      for (const g of (m.gguf || [])) opts.push({v: `${m.snapshot}/${g.name}`, label: `${tag}${m.repo_id} · ${g.name} (${fmtSize(g.size)})`});
      if (!(m.gguf || []).length && m.has_weights !== false)
        opts.push({v: m.snapshot, label: `${tag}${m.repo_id} · (ggufなし・ディレクトリ指定)`});
    } else {
      opts.push({v: m.snapshot, label: `${tag}${m.repo_id}${m.revision ? " · rev " + m.revision : ""}`});
    }
  }
  if (!opts.length) return `<div class="muted" style="font-size:11.5px;margin-top:4px">キャッシュ / データフォルダにモデルがありません${isGguf ? " (gguf検出なし)" : ""}</div>${dirNote}`;
  return `
    <select style="margin-top:5px" onchange="hfPick('${f.key}', this.value)">
      <option value="">キャッシュ / データフォルダから選択 (${opts.length})</option>
      ${opts.map(o => `<option value="${esc(o.v)}">${esc(o.label)}</option>`).join("")}
    </select>${dirNote}`;
}
function hfPick(key, val) {
  if (!val) return;
  const el = $(`#edv-${key}`);
  if (!el) return;
  el.value = val;
  el.dispatchEvent(new Event("input", {bubbles: true}));
  toast("キャッシュのパスを設定しました");
}
async function hfReload() {
  S.hf = null; renderEditor();          // 「確認中…」表示
  await loadHf(true);
  if (S.editing) renderEditor();
}

function renderEditor() {
  const d = S.editing; if (!d) return;
  const b = backendById(d.backend);
  const sheet = $("#editor-sheet");
  const fieldHtml = f => {
    const val = d.values[f.key] ?? "";
    const id = `edv-${f.key}`;
    const help = f.help ? `<div class="muted" style="font-size:11px;margin-top:2px">${esc(f.help)}</div>` : "";
    if (f.type === "bool") return `
      <div><label class="switch"><input type="checkbox" id="${id}" data-fkey="${f.key}" ${val?"checked":""}>
      <span class="track"></span><span class="lab">${esc(f.label)} <span class="muted mono">${esc(f.flag)}</span></span></label>${help}</div>`;
    // 非 bool 項目: <svg class=ic aria-hidden=true><use href=#i-checkbox /></svg>=フラグを送る / OFF=モジュール側 auto 判定に委ねる
    const on = d.on ? d.on[f.key] !== false : !!f.default_on;
    const lock = f.required;
    const toggle = lock
      ? `<span class="onchip must">必須</span>`
      : `<label class="onchip" title="このフラグを送信するか"><input type="checkbox" data-onkey="${f.key}" ${on?"checked":""}><span>送信</span></label>`;
    const wrapCls = on || lock ? "fitem" : "fitem off";
    if (f.type === "select") return `
      <div class="${wrapCls}"><label class="f" for="${id}">${esc(f.label)} <span class="muted mono">${esc(f.flag)}</span>${toggle}</label>
      <select id="${id}" data-fkey="${f.key}" ${on||lock?"":"disabled"}>${(f.options||[]).map(o => `<option ${o===val?"selected":""} value="${esc(o)}">${o===""?"(指定しない)":esc(o)}</option>`).join("")}</select>${help}${!on&&!lock?autoNote(f):""}</div>`;
    const t = f.type === "int" || f.type === "float" ? "number" : "text";
    const isModel = f.key === "model" || f.key === "model_path";
    return `
      <div class="${wrapCls}"><label class="f" for="${id}">${esc(f.label)} <span class="muted mono">${esc(f.flag)}</span>${toggle}</label>
      <input type="${t}" ${f.type==="float"?'step="0.01"':""} inputmode="${t==="number"?"decimal":""}" id="${id}" data-fkey="${f.key}" value="${esc(val)}" placeholder="${esc(f.placeholder||"")}" ${on||lock?"":"disabled"}>${help}${!on&&!lock?autoNote(f):""}${isModel ? hfPickerHtml(f) : ""}</div>`;
  };
  const autoNote = f => `<div class="muted" style="font-size:11px;margin-top:2px;color:var(--warn)">未送信 — ${esc(f.flag)} は ${esc(b?.name||"")} 側の自動判定に任せます</div>`;
  // バックエンドごとにパラメータをセクション別へリスト化 (既定値プリフィル済み)
  const sections = (b?.sections?.length ? b.sections : [{id:"model", label:"モデル"}]);
  const fieldsHtml = sections.map(sec => {
    const fs = (b?.fields || []).filter(f => (f.section || "model") === sec.id);
    if (!fs.length) return "";
    return `<div class="sec-h">${esc(sec.label)} <span class="muted" style="font-weight:400">(${fs.length}項目)</span></div>` + fs.map(fieldHtml).join("");
  }).join("");
  // 起動 JSON 主体モード (Strata 方式 / FreeToken 系): JSON がエンジン引数の正で、
  // フォームはユーザー最小パラメータだけ。キャリブレーションの勝者も JSON へ自動記載される
  const launchCap = !!b?.launch_json;
  const cfgPath = String(d.values?.config || "").trim();
  const minimalFields = ["port", "api_key"]
    .map(k => (b?.fields || []).find(f => f.key === k)).filter(Boolean);
  const launchBlock = !launchCap ? "" : (cfgPath ? `
    <div class="sec-h">起動 JSON (Strata 方式) <span class="muted" style="font-weight:400">(エンジンパラメータは JSON 主体・キャリブ結果は自動記載)</span></div>
    <div class="fitem"><label class="f" for="ed-config">起動 JSON パス</label>
      <input type="text" id="ed-config" class="mono" data-fkey="config" value="${esc(cfgPath)}">
      <div class="row" style="gap:8px;margin-top:6px">
        <button type="button" class="ghost" onclick="launchJsonShow(this)">JSON 表示 / 編集</button>
        <button type="button" class="ghost" onclick="launchJsonRelease()">解除 (全項目モードへ戻す)</button>
      </div>
      <textarea id="ed-launch-json" class="mono" hidden style="width:100%;min-height:240px;margin-top:6px"></textarea>
      <button type="button" id="ed-launch-save" class="ghost" onclick="launchJsonSave()" hidden style="margin-top:6px">JSON を保存</button>
    </div>
    <div class="sec-h">ユーザーパラメータ (最小) <span class="muted" style="font-weight:400">(空 = JSON 側の値を使う)</span></div>
    ${minimalFields.map(fieldHtml).join("")}
  ` : `
    <div class="row" style="gap:8px;margin:2px 2px 10px">
      <button type="button" class="ghost" onclick="launchJsonExport()"><svg class=ic aria-hidden=true><use href=#i-file /></svg> 現在のパラメータから起動 JSON を作成 (Strata 方式へ切替)</button>
    </div>
  `);
  const paramsHtml = (launchCap && cfgPath) ? launchBlock : launchBlock + fieldsHtml;
  const legacyExtra = String(d.extra_args || "").trim();
  sheet.innerHTML = `
    <h3>${d.id ? "プロファイル編集" : "新規プロファイル"}</h3>
    <label class="f" for="ed-name">プロファイル名</label>
    <input type="text" id="ed-name" value="${esc(d.name)}" placeholder="例: Qwen2.5-7B / vLLM">
    <label class="f" for="ed-backend">LLMバックエンド</label>
    <select id="ed-backend" onchange="setBackend(this.value)">
      ${(S.state?.backends||[]).map(bb => `<option value="${bb.id}" ${bb.id===d.backend?"selected":""}>${esc(bb.name)}</option>`).join("")}
    </select>
    <div class="muted" style="margin-top:6px">${esc(b?.desc || "")}</div>
    <h2 class="view-title" style="margin:16px 2px 0">起動パラメータ <span class="muted">(${esc(b?.name||"")} 既定値入り・ON の項目だけコマンドに付きます。OFF の項目はモジュール側の自動判定に委ねられるため上書きされません)</span></h2>
    <div class="fgrid" style="margin-top:4px">${paramsHtml}</div>
    ${legacyExtra ? `
    <details class="adv" open>
      <summary>追加引数 (レガシー項目 — 現在の値: ${esc(legacyExtra)})</summary>
      <div class="inner">
        <textarea id="ed-extra" placeholder="多くの項目は上のリストから設定できます">${esc(d.extra_args)}</textarea>
      </div>
    </details>` : ""}
    <label class="f" for="ed-env">環境変数 (KEY=VALUE を1行ずつ)</label>
    <textarea id="ed-env" placeholder="CUDA_VISIBLE_DEVICES=0">${esc(envToText(d.env))}</textarea>
    <details class="adv" ${d.cwd||d.command||d.custom_only?"open":""}>
      <summary>高度な設定 (コマンド上書き / 作業ディレクトリ)</summary>
      <div class="inner">
        <label class="f" for="ed-command">起動コマンド上書き <span class="muted">空=既定 (${esc(b?.command||b?.default_command||"")})</span></label>
        <input type="text" id="ed-command" class="mono" value="${esc(d.command)}" placeholder="${esc(b?.default_command||"")}">
        <label class="f" for="ed-cwd">作業ディレクトリ (空 = プロジェクトルート)</label>
        <input type="text" id="ed-cwd" class="mono" value="${esc(d.cwd)}">
        <label class="switch"><input type="checkbox" id="ed-custom" ${d.custom_only?"checked":""}>
          <span class="track"></span><span class="lab" style="font-size:12.5px">コマンドのみモード (上のパラメータ自動付与を止める)</span></label>
        <div class="muted" style="font-size:11.5px">トークン: {python} {llama_server} {module} {root} と値変数 {model} {port} 等が使えます</div>
      </div>
    </details>
    <label class="f" for="ed-note">メモ</label>
    <input type="text" id="ed-note" value="${esc(d.note)}">
    <label class="switch" style="margin-top:10px"><input type="checkbox" id="ed-simple" ${d.simple_visible?"checked":""}>
      <span class="track"></span><span class="lab" style="font-size:12.5px">簡易画面に表示 (<a href="/simple" target="_blank" rel="noopener">/simple</a> に「開始 / 停止」ボタンを出す。ON にしたプロファイルだけロール user のユーザーから使えます)</span></label>
    <label class="f">起動コマンド プレビュー</label>
    <pre class="cmdprev" id="ed-preview">…</pre>
    <div class="sheet-actions">
      <button class="grow" onclick="closeEditor()">キャンセル</button>
      <button class="primary grow" onclick="saveEditor()">保存</button>
    </div>`;
  sheet.querySelectorAll("[data-fkey]").forEach(el => {
    const upd = () => {
      S.editing.values[el.dataset.fkey] = el.type === "checkbox" ? el.checked : el.value;
      schedulePreview();
    };
    el.addEventListener("input", upd);
    el.addEventListener("change", upd);
  });
  sheet.querySelectorAll("[data-onkey]").forEach(el => {
    el.addEventListener("change", () => {
      collectDraft();
      S.editing.on[el.dataset.onkey] = el.checked;
      renderEditor();            // 送信 OFF→ON で入力欄の disabled を解く
      schedulePreview();
    });
  });
  ["ed-extra","ed-env","ed-command","ed-cwd"].forEach(id => {
    const el = $(`#${id}`);
    if (el) el.addEventListener("input", () => { collectDraft(); schedulePreview(); });
  });
  $("#ed-custom").addEventListener("change", () => { collectDraft(); schedulePreview(); });
  $("#ed-simple").addEventListener("change", collectDraft);
  ["ed-name","ed-note"].forEach(id => $(`#${id}`).addEventListener("input", collectDraft));
  schedulePreview();
}
async function launchJsonExport() {
  const d = S.editing;
  if (!d || !d.id) return toast("先にプロファイルを保存してください (新規作成中は JSON を作れません)");
  try {
    collectDraft();
    await api(`/api/profiles/${d.id}`, {method:"PUT", body: editorBody()});   // 現値を保存してから書き出し
    const r = await api(`/api/profiles/${d.id}/launch-json`, {method:"POST"});
    toast("起動 JSON を作成しました");
    await refresh(false);
    go(routeUrl(S.tab, "editor", d.id), true);   // config 主体モードで開き直す (履歴は積まない)
  } catch (e) { toast("作成失敗: " + e.message); }
}
async function launchJsonShow(btn) {
  const d = S.editing;
  const ta = $("#ed-launch-json"), sv = $("#ed-launch-save");
  if (!ta.hidden) { ta.hidden = true; sv.hidden = true; return; }
  try {
    const r = await api(`/api/profiles/${d.id}/launch-json`);
    ta.value = r.exists ? JSON.stringify(r.launch, null, 1) : "";
    ta.hidden = false; sv.hidden = false;
  } catch (e) { toast("読めません: " + e.message); }
}
async function launchJsonSave() {
  const d = S.editing;
  try {
    await api(`/api/profiles/${d.id}/launch-json`,
              {method:"PUT", body:{text: $("#ed-launch-json").value}});
    toast("起動 JSON を保存しました");
    schedulePreview();
  } catch (e) { toast("保存失敗: " + e.message); }
}
function launchJsonRelease() {
  S.editing.values.config = "";
  renderEditor();               // 全項目モード表示へ (保存して確定)
  toast("解除しました — 「保存」で確定します (JSON ファイル自体は残ります)");
}
async function saveEditor() {
  const body = editorBody();
  if (!body.name.trim()) return toast("プロファイル名を入力してください");
  if (!body.backend) return toast("バックエンドを選択してください");
  try {
    if (S.editing.id) await api(`/api/profiles/${S.editing.id}`, {method:"PUT", body});
    else await api("/api/profiles", {method:"POST", body});
    toast("保存しました"); closeEditor(); refresh(false);
  } catch (e) { toast("保存失敗: " + e.message); }
}
document.addEventListener("keydown", e => {
  if (e.key !== "Escape") return;
  if (!$("#acct-pop").classList.contains("hidden")) closeAcct();   // 最前面はアカウントの吹き出し
  else if ($("#sidemen").classList.contains("open")) setMenu(false);
  else if ($("#umodal").style.display === "flex") umClose();
  else closeEditor();
});

