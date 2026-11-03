/* ===================================================================
   view: launch — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.launch = `
  <section id="view-launch" class="view hidden">
    <div id="running-strip"></div>
    <h2 class="view-title">起動するサーバーを選ぶ</h2>
    <div class="filters" id="launch-filters"></div>
    <div class="grid c2" id="launch-grid"></div>
  </section>
`;
function renderLaunch() {
  const strip = $("#running-strip");
  const live = liveSessions();
  strip.innerHTML = live.length ? `<div class="card" style="border-color:#ceead6">
    <div class="row"><b style="font-size:14px"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 起動中: ${live.length} サーバー</b><span class="grow"></span>
    <button class="ghost" onclick="switchTab('running')">管理へ →</button></div>
    <div class="row" style="margin-top:8px">${live.map(s => `
      <span class="badge ${s.ready?"ok":"warn"}" style="cursor:pointer" onclick="switchTab('running')">
      <span class="dot"></span>${esc(s.profile_name)} · ${esc(s.backend_name)} · :${s.port||"-"}</span>`).join("")}
    </div></div>` : "";
  const bks = S.state.backends;
  const f = $("#launch-filters");
  f.innerHTML = [{id:"all",name:"すべて"}, ...bks].map(b =>
    `<button class="fchip ${S.filter===b.id?"on":""}" onclick="S.filter='${b.id}';renderLaunch()">${esc(b.name)}</button>`).join("");
  const g = $("#launch-grid");
  const ps = S.state.profiles.filter(p => S.filter === "all" || p.backend === S.filter);
  if (!ps.length) {
    g.innerHTML = `<div class="empty" style="grid-column:1/-1">
      起動プロファイルがありません。<br><br>
      <button class="primary" onclick="openEditor()">＋ プロファイルを作成</button></div>`;
    return;
  }
  g.innerHTML = ps.map(p => {
    const b = backendById(p.backend), s = liveOf(p.id);
    const vals = p.values || {};
    const model = vals.model || "";
    return `<div class="card">
      <div class="row">
        <span class="chip ${esc(p.backend)}">${esc(b ? b.name : p.backend)}</span>
        <b class="grow ellip" style="font-size:15.5px">${esc(p.name)}</b>
        ${statusBadge(s)}
      </div>
      <div class="kvs" style="margin-top:10px">
        <span>モデル</span><span class="v ellip" title="${esc(model)}">${esc(model || "-")}</span>
        <span>ポート</span><span class="v mono">${esc(vals.port || b?.default_port || "-")}</span>
        ${p.note ? `<span>備考</span><span class="v">${esc(p.note)}</span>` : ""}
      </div>
      ${s ? `<div class="muted" style="margin-top:8px">稼働 ${s.status==="running"||s.status==="starting"?fmtUp(s.uptime):"-"} · PID ${s.pid}${s.ready?' · <a href="http://'+esc(S.state.system.ip)+':'+s.port+'" target="_blank" rel="noopener">API ↗</a>':''}</div>` : ""}
      <div class="row" style="margin-top:12px">
        ${s
          ? `<button class="danger" onclick="stopSession('${s.id}')" ${S.busy[s.id]?"disabled":""}>${S.busy[s.id]?'<span class="spin"></span>':'<svg class=ic aria-hidden=true><use href=#i-stop /></svg>'} 停止</button>
             <button onclick="switchTab('running');openLog('${s.id}')">ログ</button>
             ${s.ready?`<button onclick="openPlayground('${s.id}')"><svg class=ic aria-hidden=true><use href=#i-flask /></svg> Play</button>`:""}
             ${s.ready?`<button class="need-bench" onclick="openBench('${p.id}')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> Bench</button>`:""}`
          : `<button class="primary" onclick="startProfile('${p.id}')" ${S.busy[p.id]?"disabled":""}>${S.busy[p.id]?'<span class="spin"></span>':'<svg class=ic aria-hidden=true><use href=#i-play /></svg> 起動'}</button>
             <button class="need-bench" onclick="openBench('${p.id}')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg> Bench</button>`}
        ${calSupports(p.backend) ? `<button class="need-cal" title="この PC 向け調整をワンクリック実行 (Maya / Strata / FreeToken)" onclick="calProfileQuick('${p.id}')"><svg class=ic aria-hidden=true><use href=#i-target /></svg> キャリブ</button>` : ""}
        <span class="grow"></span>
        <button class="ghost need-edit" onclick="openEditor('${p.id}')">編集</button>
      </div>
    </div>`;
  }).join("");
}

