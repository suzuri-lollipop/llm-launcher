/* ===================================================================
   view: profiles — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.profiles = `
  <section id="view-profiles" class="view hidden">
    <div class="row" style="margin-bottom:10px">
      <h2 class="view-title" style="margin:0">プロファイル管理 <span class="muted">1プロファイル = 1バックエンド</span></h2>
      <div class="grow"></div>
      <button class="ghost need-edit" id="pm-batch-btn" onclick="toggleBatch()"><svg class=ic aria-hidden=true><use href=#i-checkbox /></svg> 一括</button>
      <button class="ghost need-edit" onclick="exportAll()"><svg class=ic aria-hidden=true><use href=#i-download /></svg> 全部エクスポート</button>
      <button class="ghost need-edit" onclick="$('#import-file').click()"><svg class=ic aria-hidden=true><use href=#i-upload /></svg> インポート</button>
      <input type="file" id="import-file" accept="application/json,.json" style="display:none" onchange="importFile(this)">
      <button class="primary need-edit" onclick="openEditor()">＋ 新規</button>
    </div>
    <div class="row" style="margin-bottom:10px;gap:8px">
      <input type="text" id="pm-q" class="grow" style="min-width:140px" placeholder="名前 / モデル / バックエンド / メモで絞り込み" oninput="S.pm.q=this.value;renderProfiles()">
      <select id="pm-sort" style="width:auto" onchange="S.pm.sort=this.value;renderProfiles()">
        <option value="updated">更新順</option>
        <option value="name">名前順</option>
        <option value="backend">バックエンド順</option>
        <option value="used">使用回数順</option>
      </select>
    </div>
    <div class="filters" id="pm-filters"></div>
    <div id="batch-bar" class="card hidden" style="border-color:#c6dafc;padding:10px 14px">
      <div class="row">
        <label class="row" style="gap:6px;cursor:pointer;font-size:13.5px">
          <input type="checkbox" id="pm-selall" style="width:16px;height:16px;accent-color:var(--acc)" onchange="selectAllProfiles(this.checked)"> 全選択</label>
        <b style="font-size:13.5px" id="pm-sel-count"></b>
        <span class="grow"></span>
        <button class="ghost" style="min-height:34px;font-size:12.5px" onclick="exportSelected()">選択分エクスポート</button>
        <button class="ghost danger" style="min-height:34px;font-size:12.5px" onclick="deleteSelected()">選択分削除</button>
      </div>
    </div>
    <div class="grid" id="profiles-grid"></div>
  </section>
`;
/* ---------------- profiles 管理画面 ---------------- */
function profUsage(pid) {
  const ss = (S.state.sessions || []).filter(s => s.profile_id === pid);
  const last = ss.reduce((m, s) => Math.max(m, s.started_at || 0), 0);
  return {count: ss.length, last, fails: ss.filter(s => s.status === "failed").length};
}
function profOnCount(p) {
  const b = backendById(p.backend);
  const fields = b?.fields || [];
  if (p.on) {
    let n = fields.filter(f => f.type !== "bool" && (f.required || p.on[f.key] !== false) &&
                              String((p.values||{})[f.key] ?? "").trim() !== "").length;
    n += fields.filter(f => f.type === "bool" && (p.values||{})[f.key]).length;
    return n;
  }
  return Object.values(p.values || {}).filter(v => v !== "" && v !== null && v !== false).length;
}
function pmFilterSort(ps) {
  const q = (S.pm.q || "").toLowerCase().trim();
  let out = ps.filter(p => {
    if (S.pm.backend !== "all" && p.backend !== S.pm.backend) return false;
    if (!q) return true;
    const hay = [p.name, p.note, p.backend, backendById(p.backend)?.name, (p.values||{}).model || (p.values||{}).model_path]
      .join(" ").toLowerCase();
    return hay.includes(q);
  });
  const key = S.pm.sort || "updated";
  out.sort((a, c) => {
    if (key === "name") return a.name.localeCompare(c.name, "ja");
    if (key === "backend") return (a.backend + a.name).localeCompare(c.backend + c.name, "ja");
    if (key === "used") return profUsage(c.id).count - profUsage(a.id).count || (c.updated_at||0) - (a.updated_at||0);
    return (c.updated_at || 0) - (a.updated_at || 0);
  });
  return out;
}
function renderProfiles() {
  const g = $("#profiles-grid");
  const f = $("#pm-filters");
  const all = S.state.profiles || [];
  const bks = S.state.backends;
  f.innerHTML = [{id:"all", name:"すべて", n: all.length}, ...bks.map(bb =>
    ({id: bb.id, name: bb.name, n: all.filter(p => p.backend === bb.id).length}))]
    .map(bb => `<button class="fchip ${S.pm.backend===bb.id?"on":""}" onclick="S.pm.backend='${bb.id}';renderProfiles()">${esc(bb.name)} <span class="muted" style="color:inherit">${bb.n}</span></button>`).join("");
  $("#batch-bar").classList.toggle("hidden", !S.pm.batch);
  const ps = pmFilterSort(all);
  if (!ps.length) {
    g.innerHTML = `<div class="empty">${all.length ? "条件に合うプロファイルがありません。" :
      "プロファイルを作成すると、ここに・起動タブに並びます。<br><br><button class='primary' onclick='openEditor()'>＋ プロファイルを作成</button>"}</div>`;
    return;
  }
  g.innerHTML = ps.map(p => {
    const b = backendById(p.backend), s = liveOf(p.id);
    const v = p.values || {};
    const model = v.model || v.model_path || "";
    const u = profUsage(p.id);
    const port = v.port || b?.default_port || "";
    return `<div class="card">
      <div class="row">
        ${S.pm.batch ? `<input type="checkbox" class="pmsel" data-pid="${p.id}" style="width:18px;height:18px;accent-color:var(--acc)" ${S.pm.sel.has(p.id)?"checked":""} onchange="toggleProfileSel('${p.id}',this.checked)">` : ""}
        <span class="chip ${esc(p.backend)}">${esc(b ? b.name : p.backend)}</span>
        <b class="grow ellip" style="font-size:15.5px;cursor:pointer" onclick="openEditor('${p.id}')">${esc(p.name)}</b>
        ${statusBadge(s)}
      </div>
      <div class="kvs" style="margin-top:10px">
        <span>モデル</span><span class="v ellip" title="${esc(model)}">${esc(model || "-")}</span>
        <span>エンドポイント</span><span class="v mono">${port ? esc(`${S.state.system.ip}:${port}`) : "-"}</span>
        <span>有効パラメータ</span><span class="v">${profOnCount(p)} 個のフラグを送信</span>
        <span>使用履歴</span><span class="v">${u.count ? `${u.count} 回 · 最終 ${fmtAgo(u.last)}${u.fails ? ` <span style="color:var(--err)">(異常終了 ${u.fails})</span>` : ""}` : "未使用"}</span>
        ${p.extra_args ? `<span>追加引数(legacy)</span><span class="v ellip mono">${esc(p.extra_args)}</span>` : ""}
        <span>更新</span><span class="v">${fmtAgo(p.updated_at)}</span>
      </div>
      ${p.note ? `<div class="muted" style="margin-top:8px">${esc(p.note)}</div>` : ""}
      <div class="row" style="margin-top:12px">
        ${s ? `<button class="danger" onclick="stopSession('${s.id}')"><svg class=ic aria-hidden=true><use href=#i-stop /></svg> 停止</button>`
            : `<button class="primary" onclick="startProfile('${p.id}')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 起動</button>`}
        <button class="need-edit" onclick="openEditor('${p.id}')">編集</button>
        <button class="need-bench" onclick="openBench('${p.id}')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> Bench</button>
        ${calSupports(p.backend) ? `<button class="need-cal" title="この PC 向け調整をワンクリック実行 (Maya / Strata / FreeToken)" onclick="calProfileQuick('${p.id}')"><svg class=ic aria-hidden=true><use href=#i-target /></svg> キャリブ</button>` : ""}
        <button class="ghost need-edit" onclick="dupProfile('${p.id}')">複製</button>
        <button class="ghost need-edit" onclick="exportProfiles(['${p.id}'])">エクスポート</button>
        <span class="grow"></span>
        ${!s ? `<button class="ghost danger need-edit" onclick="delProfile('${p.id}')">削除</button>` : ""}
      </div>
    </div>`;
  }).join("");
  if (S.pm.batch) syncBatchBar();
}
function toggleBatch() {
  S.pm.batch = !S.pm.batch;
  if (!S.pm.batch) S.pm.sel.clear();
  $("#pm-batch-btn").classList.toggle("primary", S.pm.batch);
  renderProfiles();
}
function toggleProfileSel(pid, on) {
  if (on) S.pm.sel.add(pid); else S.pm.sel.delete(pid);
  syncBatchBar();
}
function selectAllProfiles(on) {
  pmFilterSort(S.state.profiles || []).forEach(p => on ? S.pm.sel.add(p.id) : S.pm.sel.delete(p.id));
  renderProfiles();
}
function syncBatchBar() {
  const c = $("#pm-sel-count"); if (c) c.textContent = `選択 ${S.pm.sel.size} 件`;
  const sa = $("#pm-selall");
  if (sa) sa.checked = S.pm.sel.size > 0 && S.pm.sel.size === (S.state.profiles || []).length;
}
async function deleteSelected() {
  const ids = [...S.pm.sel];
  if (!ids.length) return toast("選択されていません");
  if (!confirm(`${ids.length} 件を削除しますか? 起動中のものはスキップされます`)) return;
  let ok = 0, skip = 0;
  for (const id of ids) {
    try { await api(`/api/profiles/${id}`, {method:"DELETE"}); ok++; }
    catch { skip++; }
  }
  S.pm.sel.clear(); toast(`${ok} 件削除${skip ? ` / ${skip} 件スキップ (起動中等)` : ""}`); refresh(false);
}

/* ---- エクスポート / インポート (JSON) ---- */
function profExportShape(p) {   // 移植用: id/時刻を除いた再インポート可能な形
  return {name:p.name, backend:p.backend, values:p.values||{}, on:p.on??null,
          extra_args:p.extra_args||"", env:p.env||{}, cwd:p.cwd||"",
          command:p.command||"", custom_only:!!p.custom_only, note:p.note||""};
}
function downloadJson(obj, filename) {
  const blob = new Blob([JSON.stringify(obj, null, 2)], {type:"application/json;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob); a.download = filename;
  a.click(); URL.revokeObjectURL(a.href);
}
function exportProfiles(ids) {
  const list = (S.state.profiles || []).filter(p => ids.includes(p.id)).map(profExportShape);
  if (!list.length) return toast("エクスポート対象がありません");
  downloadJson({llm_launcher_profiles: 1, exported_at: new Date().toISOString(), profiles: list},
               `llm-launcher-profiles_${new Date().toISOString().slice(0,10)}.json`);
  toast(`${list.length} 件をエクスポートしました`);
}
function exportAll() { exportProfiles((S.state.profiles||[]).map(p=>p.id)); }
function exportSelected() {
  if (!S.pm.sel.size) return toast("選択されていません");
  exportProfiles([...S.pm.sel]);
}
async function importProfiles(list) {
  let ok = 0, bad = 0;
  for (const raw of list) {
    const p = {...raw};
    delete p.id; delete p.created_at; delete p.updated_at;
    if (!p.name || !p.backend || typeof p.values !== "object") { bad++; continue; }
    if (p.on == null) delete p.on;                      // null = レガシー送信 (値があれば送出)
    try { await api("/api/profiles", {method:"POST", body: p}); ok++; }
    catch (e) { bad++; }
  }
  toast(`インポート: ${ok} 件成功${bad ? ` / ${bad} 件失敗` : ""}`);
  refresh(false);
}
async function importFile(input) {
  const file = input.files && input.files[0];
  input.value = "";
  if (!file) return;
  try {
    const data = JSON.parse(await file.text());
    const list = Array.isArray(data) ? data : (data.profiles || []);
    if (!Array.isArray(list) || !list.length) return toast("プロファイル配列ではありません");
    if (!confirm(`${list.length} 件をインポートしますか?`)) return;
    importProfiles(list);
  } catch (e) { toast("読み込み失敗: " + e.message); }
}
async function dupProfile(pid) {
  const p = profileById(pid); if (!p) return;
  try {
    await api("/api/profiles", {method:"POST", body: {...p, name: p.name + " のコピー"}});
    toast("複製しました"); refresh(false);
  } catch (e) { toast("失敗: " + e.message); }
}
async function delProfile(pid) {
  if (!confirm("このプロファイルを削除しますか?")) return;
  try { await api(`/api/profiles/${pid}`, {method:"DELETE"}); toast("削除しました"); refresh(false); }
  catch (e) { toast("削除失敗: " + e.message); }
}

