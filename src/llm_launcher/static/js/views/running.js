/* ===================================================================
   view: running — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.running = `
  <section id="view-running" class="view hidden">
    <div class="row">
      <h2 class="view-title" style="margin:0">稼働中 <span class="muted" id="running-sub"></span></h2>
      <div class="grow"></div>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="switchTab('launch')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 起動する</button>
    </div>
    <div id="running-list" style="margin-top:10px"></div>
  </section>
`;
/* ---------------- 稼働中 view ---------------- */
function runningSig() {
  return liveSessions().map(s =>
    [s.id, s.status, s.ready ? 1 : 0, s.pid, s.detached ? 1 : 0, s.port ?? "", s.mem_state || "",
     s.oom_suspect ? 1 : 0, s.oom_swift_stop ? 1 : 0,
     s.oom ? `${s.oom.applied}/${s.oom.denied}/${s.oom.score}` : "",
     S.openLogs.has(s.id) ? 1 : 0].join(":")).join("|");
}
function renderRunning(force) {
  const list = $("#running-list");
  const live = liveSessions();
  $("#running-sub").textContent = live.length
    ? `— ${live.length} 件 (API 応答待ち ${live.filter(s => !s.ready).length} 件)` : "";
  if (!live.length) {
    if (S._ssSig !== "empty") {
      S._ssSig = "empty";
      list.innerHTML = `<div class="empty">起動中のサーバーはありません。<br>
        <span class="muted">「起動」タブからプロファイルを選んでスタートしてください。</span><br>
        <button class="primary" style="margin-top:10px" onclick="switchTab('launch')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 起動する</button></div>`;
    }
    return;
  }
  // 構造が変わらなければフル再構築をスキップ (ログの選択解除・「読み込み中…」化を防ぐ)
  const sig = runningSig();
  if (!force && sig === S._ssSig && list.childElementCount) {
    patchUptime(list, live);
    return;
  }
  S._ssSig = sig;
  list.innerHTML = live.map(sessionCard).join("");
  restoreLogBuffers();
}
function sessionCard(s) {
  const open = S.openLogs.has(s.id);
  const url = s.port ? `http://${esc(S.state.system.ip)}:${s.port}` : "";
  const oomTxt = s.oom_swift_stop
    ? `<span style="color:var(--warn)"><svg class=ic aria-hidden=true><use href=#i-shield /></svg> メモリ逼迫により launcher が安全停止しました</span>`
    : s.oom_suspect
    ? `<span style="color:var(--err)"><svg class=ic aria-hidden=true><use href=#i-error /></svg> OOM kill の可能性</span>`
    : (s.oom && s.oom.applied)
      ? `<svg class=ic aria-hidden=true><use href=#i-shield /></svg> 保護中 (oom_score_adj ${s.oom.score}${s.oom.via === "helper" ? " · sudo 経由" : ""} · ${s.oom.applied} プロセス)`
      : (s.oom && s.oom.error)
        ? `<span style="color:var(--warn)"><svg class=ic aria-hidden=true><use href=#i-shield /></svg> 未適用 — ${esc(s.oom.error)}</span>`
        : `<span class="muted">保護 未適用 / 設定オフ</span>`;
  const memTxt = (s.mem_state && s.mem_state !== "ok")
    ? ` <span style="color:${s.mem_state === "crit" ? "var(--err)" : "var(--warn)"}">· 空き ${esc(s.mem_available_gb)}GB</span>` : "";
  return `<div class="card" data-sid="${s.id}">
    <div class="row">
      ${statusBadge(s)}
      <b class="grow ellip" style="font-size:15px">${esc(s.profile_name)}</b>
      <span class="chip ${esc(s.backend)}">${esc(s.backend_name)}</span>
    </div>
    <div class="kvs" style="margin-top:8px">
      <span>PID</span><span class="v mono">${s.pid}${s.detached ? ' <span class="muted">(前マネージャーから引き継ぎ)</span>' : ""}</span>
      <span>稼働時間</span><span class="v"><span class="upt" data-sid="${s.id}">${fmtUp(s.uptime)}</span></span>
      <span>API</span><span class="v mono">${url ? `<a href="${url}" target="_blank" rel="noopener">:${s.port} ↗</a>` : "-"}</span>
      <span>OOM</span><span class="v" style="font-size:12.5px">${oomTxt}${memTxt}</span>
    </div>
    <pre class="cmdprev" style="max-height:44px">${esc(s.argv.join(" "))}</pre>
    ${s.error ? `<div class="muted" style="color:var(--err);margin-top:6px">${esc(s.error)}</div>` : ""}
    <div class="row" style="margin-top:10px">
      <button class="danger" onclick="stopSession('${s.id}')" ${S.busy[s.id]?"disabled":""}>${S.busy[s.id]?'<span class="spin"></span>':'<svg class=ic aria-hidden=true><use href=#i-stop /></svg> 停止'}</button>
      <button onclick="restartSession('${s.id}')"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 再起動</button>
      <button onclick="toggleLog('${s.id}')">${open ? "ログ閉じる" : "<svg class=ic aria-hidden=true><use href=#i-article /></svg> ログ"}</button>
      ${s.ready && s.port ? `<button onclick="openPlayground('${s.id}')"><svg class=ic aria-hidden=true><use href=#i-flask /></svg> プレイグラウンド</button>` : ""}
      ${s.ready && s.port ? `<button onclick="openBench('${s.profile_id}')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> ベンチ</button>` : ""}
      ${calSupports((profileById(s.profile_id) || {}).backend) ? `<button class="need-cal" onclick="calProfileQuick('${s.profile_id}')"><svg class=ic aria-hidden=true><use href=#i-target /></svg> キャリブ</button>` : ""}
    </div>
    ${logPanel(s.id, open)}
  </div>`;
}
function patchUptime(root, ss) {
  for (const s of ss) {
    const u = root.querySelector(`.upt[data-sid="${s.id}"]`);
    if (u) u.textContent = (u.dataset.pfx || "") + fmtUp(s.uptime);
  }
}

