/* ===================================================================
   view: history — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.history = `
  <section id="view-history" class="view hidden">
    <h2 class="view-title">セッション履歴 <span class="muted">終了した起動のログ確認・再実行・削除</span></h2>
    <div class="row" style="gap:8px">
      <input type="text" id="hist-q" class="grow" style="min-width:140px" placeholder="プロファイル / バックエンドで絞り込み" oninput="S.hist.q=this.value;renderHistory()">
    </div>
    <div class="filters" id="hist-filters"></div>
    <div class="muted" id="hist-count" style="font-size:11.5px;margin:-2px 2px 8px"></div>
    <div id="history-list"></div>
  </section>
`;
/* ---------------- セッション履歴 view ---------------- */
const HIST_FILTERS = [
  {id:"all",    n:"すべて",   f: () => true},
  {id:"failed", n:"異常終了", f: s => s.status === "failed"},
  {id:"exited", n:"終了",     f: s => s.status === "exited"},
  {id:"lost",   n:"切断記録", f: s => s.status === "lost"},
];
const deadSessions = () => (S.state.sessions || []).filter(s => !isLive(s));
function histFiltered() {
  const q = S.hist.q.trim().toLowerCase();
  const flt = HIST_FILTERS.find(f => f.id === S.hist.status) || HIST_FILTERS[0];
  return deadSessions().filter(s => flt.f(s) &&
    (!q || `${s.profile_name} ${s.backend_name} ${s.backend}`.toLowerCase().includes(q)));
}
function histSig() {
  return [S.hist.q.trim(), S.hist.status, S.hist.limit].join("|") + "::" +
    histFiltered().slice(0, S.hist.limit).map(s =>
      [s.id, s.status, s.exit_code ?? "", s.error ? 1 : 0, s.finished_at ?? "",
       S.openLogs.has(s.id) ? 1 : 0].join(":")).join("|");
}
function renderHistory(force) {
  const list = $("#history-list");
  const all = deadSessions();
  const fh = HIST_FILTERS.map(f =>
    `<button class="fchip ${S.hist.status===f.id?"on":""}" onclick="setHistStatus('${f.id}')">${f.n} <span class="muted" style="color:inherit">${all.filter(f.f).length}</span></button>`).join("");
  const fel = $("#hist-filters");
  if (fel.innerHTML !== fh) fel.innerHTML = fh;   // ポーリング毎の作り直し (フォーカス喪失) を避ける
  const rows = histFiltered();
  const shown = rows.slice(0, S.hist.limit);
  $("#hist-count").textContent = all.length
    ? `終了した起動 全 ${all.length} 件 — 条件一致 ${rows.length} 件 / 表示 ${shown.length} 件` : "";
  if (!all.length) {
    if (S._hsSig !== "empty") {
      S._hsSig = "empty";
      list.innerHTML = `<div class="empty">履歴はまだありません。<br>
        <span class="muted">サーバーを停止すると、ここにログ・再実行・削除が並びます。</span></div>`;
    }
    return;
  }
  if (!rows.length) {
    if (S._hsSig !== "nohit") {
      S._hsSig = "nohit";
      list.innerHTML = `<div class="empty">条件に合う履歴がありません。<br>
        <button class="ghost" style="margin-top:10px" onclick="clearHistFilter()">条件をクリア</button></div>`;
    }
    return;
  }
  // 構造が変わらなければフル再構築をスキップ (ログの選択解除を防ぐ)
  const sig = histSig();
  if (!force && sig === S._hsSig && list.childElementCount) return;
  S._hsSig = sig;
  list.innerHTML = shown.map(histRow).join("") +
    (rows.length > shown.length
      ? `<button class="ghost" style="width:100%" onclick="S.hist.limit+=30;renderHistory(true)">さらに表示 (残り ${rows.length - shown.length} 件)</button>`
      : "");
  restoreLogBuffers();
}
function clearHistFilter() {
  S.hist.q = ""; S.hist.status = "all"; S.hist.limit = 30;
  const q = $("#hist-q"); if (q) q.value = "";
  renderHistory(true);
}
function setHistStatus(id) { S.hist.status = id; S.hist.limit = 30; renderHistory(true); }
function histRow(s) {
  const open = S.openLogs.has(s.id);
  const span = s.finished_at && s.started_at ? s.finished_at - s.started_at : null;
  return `<div class="hrow" data-sid="${s.id}">
    <div class="hrow-hd" onclick="toggleLog('${s.id}')">
      ${statusBadge(s)}
      <span class="grow" style="min-width:0">
        <span class="hrow-t ellip" style="display:block">${esc(s.profile_name)}</span>
        <span class="hrow-m">
          <span class="mono">${fmtDT(s.started_at)} → ${fmtDT(s.finished_at)}</span>
          <span>稼働 ${fmtUp(span)}</span>
          ${s.exit_code != null ? `<span class="mono">code ${s.exit_code}</span>` : ""}
          <span class="mono">PID ${s.pid}</span>
        </span>
      </span>
      <span class="chip ${esc(s.backend)}">${esc(s.backend_name)}</span>
      <span class="muted" style="flex:none">${open ? "<svg class=ic aria-hidden=true><use href=#i-chev_up /></svg>" : "<svg class=ic aria-hidden=true><use href=#i-chev_down /></svg>"}</span>
    </div>
    ${s.error ? `<div class="hrow-act"><span class="mono" style="color:var(--err);word-break:break-all">${esc(s.error)}</span></div>` : ""}
    <div class="hrow-act">
      <button onclick="event.stopPropagation();toggleLog('${s.id}')">${open ? "ログ閉じる" : "<svg class=ic aria-hidden=true><use href=#i-article /></svg> ログ"}</button>
      <button onclick="event.stopPropagation();rerunProfile('${s.profile_id}')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 再実行</button>
      <span class="grow"></span>
      <button class="ghost" onclick="event.stopPropagation();deleteSession('${s.id}')">削除</button>
    </div>
    ${logPanel(s.id, open)}
  </div>`;
}
function rerunProfile(pid) {
  if (profileById(pid)) return startProfile(pid);
  toast("元のプロファイルは削除されています");
}
