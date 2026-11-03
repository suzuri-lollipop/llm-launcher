/* ===================================================================
   view: home — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.home = `
  <section id="view-home" class="view">
    <h2 class="view-title">ダッシュボード <span class="muted" id="home-clock">--:--:--</span></h2>
    <div id="home-notices"></div>
    <div class="tiles" id="home-tiles"></div>
    <div class="dash-2col" style="margin-top:12px">
      <div class="card">
        <div class="card-h">ホスト</div>
        <div class="kvs" id="home-host"></div>
      </div>
      <div class="card">
        <div class="card-h">リソース使用率</div>
        <div id="home-meters"><span class="muted"><span class="spin"></span> 読み込み中…</span></div>
      </div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="card-h">GPU</div>
      <div id="home-gpus"></div>
    </div>
    <div class="dash-2col" style="margin-top:12px">
      <div class="card" style="margin:0">
        <div class="card-h">稼働中サーバー <span class="badge" id="home-live-count" style="margin-left:auto"></span></div>
        <div id="home-live"></div>
      </div>
      <div class="card" style="margin:0">
        <div class="card-h">バックエンド状態</div>
        <div id="home-backends"></div>
      </div>
    </div>
    <div class="card" style="margin-top:12px">
      <div class="card-h">最近のセッション <button class="ghost" style="margin-left:auto;min-height:32px;padding:4px 10px;font-size:12px" onclick="switchTab('history')">すべて見る →</button></div>
      <div id="home-recent"></div>
    </div>
  </section>
`;
/* ---------------- dashboard (ホーム) ---------------- */
function fmtClock(){ const d=new Date(); $("#home-clock").textContent =
  d.toLocaleTimeString("ja-JP") + "  " + d.toLocaleDateString("ja-JP",{month:"numeric",day:"numeric","weekday":"short"}); }
function renderHome(){
  if (!S.state) return;
  tickHomeClock();
  renderHomeNotices();
  renderHomeTiles();
  renderHomeHost();
  renderHomeMeters();
  renderHomeGpus();
  renderHomeLive();
  renderHomeBackends();
  renderHomeRecent();
}
function renderHomeNotices(){
  const el = $("#home-notices"); const notes = [];
  const o = S.sys && S.sys.oom;
  if (o) {
    if (o.level === "warn") notes.push(`<div class="card" style="border-color:#f0c674;background:#fef7e0">
      <div class="row"><b style="color:var(--warn)"><svg class=ic aria-hidden=true><use href=#i-shield /></svg> OOM 対策が効いていません</b><span class="grow"></span>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="switchTab('backends')">OOM 対策設定へ →</button></div>
      <div class="muted mono" style="margin-top:4px;white-space:pre-wrap;word-break:break-all">${esc(o.verdict)}</div></div>`);
    if (o.state === "crit") notes.push(`<div class="card" style="border-color:#f5c0bb;background:#fce8e6">
      <div class="row"><b style="color:var(--err)"><svg class=ic aria-hidden=true><use href=#i-error /></svg> メモリ逼迫: 空き ${esc(o.available_gb)}GB / 予約 ${esc(o.min_available_gb)}GB</b><span class="grow"></span>
      <span class="muted">スワップ ${fmtGB(o.swap_free)} 空き</span></div>
      <div class="muted" style="margin-top:4px">この状態で起動すると OOM kill される可能性が高いです。重いプロセス (Cline 等) を止めるか、稼働中のサーバーを停止してください。</div></div>`);
    else if (o.state === "warn") notes.push(`<div class="card" style="border-color:#f0c674;background:#fef7e0">
      <div class="row"><b style="color:var(--warn)"><svg class=ic aria-hidden=true><use href=#i-alert /></svg> メモリが少なくなってきます: 空き ${esc(o.available_gb)}GB (予約 ${esc(o.min_available_gb)}GB)</b></div></div>`);
    if (o.oom_kills) notes.push(`<div class="card" style="border-color:#f5c0bb;background:#fce8e6">
      <div class="row"><b style="color:var(--err)"><svg class=ic aria-hidden=true><use href=#i-error /></svg> cgroup 内で OOM kill が ${esc(o.oom_kills)} 回発生</b><span class="grow"></span>
      <span class="muted mono">${esc(o.self_unit || "unit 不明")}${o.oomd ? " / systemd-oomd 動作中" : ""}</span></div>
      <div class="muted" style="margin-top:4px">セッションの異常終了ログと照合してください。モデル側が犠牲になっているなら保護設定を見直してください。</div></div>`);
  }
  const broken = (S.state.backends||[]).filter(b => !b.detection.installed);
  if (broken.length) notes.push(`<div class="card" style="border-color:#f5c0bb;background:#fce8e6">
    <div class="row"><b style="color:var(--err)"><svg class=ic aria-hidden=true><use href=#i-alert /></svg> ${broken.length} 個のバックエンドが未検出</b><span class="grow"></span>
    <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="switchTab('backends')">設定へ →</button></div>
    <div class="muted" style="margin-top:4px">${broken.map(b=>esc(b.name)).join(", ")} — インポート/ビルドが見つかりません</div></div>`);
  const failed = (S.state.sessions||[]).filter(s => s.status === "failed").slice(0,1);
  for (const s of failed) notes.push(`<div class="card" style="border-color:#f5c0bb;background:#fce8e6">
    <div class="row"><b style="color:var(--err)"><svg class=ic aria-hidden=true><use href=#i-block /></svg> 異常終了: ${esc(s.profile_name)}</b><span class="grow"></span>
    <span class="muted">${fmtAgo(s.finished_at)}</span>
    <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="switchTab('history');openLog('${s.id}')">ログ確認 →</button></div>
    ${s.error?`<div class="muted mono" style="margin-top:4px;color:var(--err);word-break:break-all">${esc(s.error)}</div>`:""}</div>`);
  if (!S.state.profiles.length) notes.push(`<div class="card" style="border-color:#c6dafc">
    <div class="row"><b><svg class=ic aria-hidden=true><use href=#i-info /></svg> ようこそ</b><span class="grow"></span>
    <button class="primary" style="min-height:32px;padding:4px 12px;font-size:12.5px" onclick="openEditor()">＋ プロファイル作成</button></div>
    <div class="muted" style="margin-top:4px">まずプロファイルを作成して、起動するサーバーを設定しましょう。</div></div>`);
  el.innerHTML = notes.join("");
}
function renderHomeTiles(){
  const live = liveSessions();
  const ready = live.filter(s=>s.ready).length;
  const running = live.filter(s=>["running","starting","stopping"].includes(s.status)).length;
  const installed = (S.state.backends||[]).filter(b=>b.detection.installed).length;
  const totalB = (S.state.backends||[]).length;
  const recentFails = (S.state.sessions||[]).filter(s=>s.status==="failed").length;
  const h = S.sys && S.sys.host;
  const gpuLoad = S.sys && S.sys.gpus && S.sys.gpus.length
    ? Math.round(S.sys.gpus.reduce((a,g)=>a+(g.util||0),0)/S.sys.gpus.length) : null;
  const tiles = [
    {k:"ok", ic:"<svg class=ic aria-hidden=true><use href=#i-circle /></svg>", v:`${ready}`, s:`稼働中 / 起動含め ${running}`, t:"稼働中", go:"running"},
    {k:"acc", ic:"<svg class=ic aria-hidden=true><use href=#i-clipboard /></svg>", v:`${S.state.profiles.length}`, s:"プロファイル", t:"起動画面", go:"launch"},
    {k: installed===totalB?"ok":"warn", ic:"<svg class=ic aria-hidden=true><use href=#i-puzzle /></svg>", v:`${installed}/${totalB}`, s:"バックエンド検出", t:"バックエンド", go:"backends"},
    {k: gpuLoad==null?"":(gpuLoad>=90?"err":gpuLoad>=70?"warn":"ok"), ic:"<svg class=ic aria-hidden=true><use href=#i-game /></svg>", v: gpuLoad==null?"-":gpuLoad+"%", s:"GPU 平均使用率", t:"", go:""},
    {k: recentFails?"err":"", ic:"<svg class=ic aria-hidden=true><use href=#i-block /></svg>", v:`${recentFails}`, s:"異常終了履歴", t:"セッション履歴", go:"history"},
    {k:"", ic:"<svg class=ic aria-hidden=true><use href=#i-speed /></svg>", v: h&&h.uptime?upHuman(h.uptime):"-", s:"ホスト稼働時間", t:"", go:""},
  ];
  $("#home-tiles").innerHTML = tiles.map(t=>
    `<button class="tile ${t.k}" ${t.go?`onclick="switchTab('${t.go}')"`:""} title="${esc(t.t||"")}">
      <div class="t-k">${t.ic} ${esc(t.s)}</div><div class="t-v">${esc(String(t.v))}</div>
      ${t.go?`<div class="t-s">${esc(t.t)} へ →</div>`:`<div class="t-s">&nbsp;</div>`}</button>`).join("");
}
function upHuman(sec){ sec=Math.floor(sec); const d=Math.floor(sec/86400),h=Math.floor(sec%86400/3600),m=Math.floor(sec%3600/60);
  return d?`${d}d${h}h`:h?`${h}h${m}m`:`${m}m`; }
function renderHomeHost(){
  const sys = S.state.system; const h = S.sys && S.sys.host;
  const rows = [
    ["ホスト名", esc(sys.hostname)],
    ["LAN IP", `<span class="mono">${esc(sys.ip)}</span> <a class="btn" style="min-height:0;padding:2px 8px;font-size:12px" href="http://${esc(sys.ip)}:${location.port||8036}" target="_blank" rel="noopener">この画面 ↗</a>`],
    ["OS", esc(sys.platform)],
    ["バージョン", `v${esc(sys.version)}`],
    ["プロジェクト", `<span class="mono" style="font-size:11px">${esc(sys.root)}</span>`],
  ];
  if (h){
    rows.push(["CPU", `${h.cpu_count} cores · load ${(h.load||[]).map(x=>x.toFixed(2)).join(" ")||"-"}`]);
    if (h.uptime!=null) rows.push(["稼働時間", upHuman(h.uptime)]);
  }
  $("#home-host").innerHTML = rows.map(([k,v])=>`<span>${k}</span><span class="v">${v}</span>`).join("");
}
function renderHomeMeters(){
  const el = $("#home-meters"); const s = S.sys;
  if (s === undefined){ el.innerHTML = `<span class="muted"><span class="spin"></span> 読み込み中…</span>`; return; }
  if (s === null){ el.innerHTML = `<span class="muted" style="color:var(--err)">システム統計を取得できません</span>`; return; }
  const h = s.host||{}; let html = "";
  if (h.memory){
    html += meter("メモリ", h.memory.used, h.memory.total, (u,t)=>`${fmtGB(u)} / ${fmtGB(t)}`);
    if (h.memory.swap_total) html += meter("スワップ", h.memory.swap_used, h.memory.swap_total, (u,t)=>`${fmtGB(u)} / ${fmtGB(t)}`);
  }
  const o = s.oom;
  if (o && o.total){
    // OOM 判定の基準は MemAvailable (キャッシュ差し引き後の実質空き) なので別枠で明示
    const cls = o.state === "crit" ? "err" : o.state === "warn" ? "warn" : "ok";
    html += `<div class="meter"><div class="m-hd"><b>OOM 判定用空き (MemAvailable)</b>
      <span class="r">${fmtGB(o.available)} / 予約 ${fmtGB(o.reserve)}</span></div>
      <div class="mbar"><i class="${cls}" style="width:${Math.min(100, o.used_pct)}%"></i></div></div>`;
  }
  if (h.disk) html += meter("ディスク", h.disk.used, h.disk.total, (u,t)=>`${fmtGB(u)} / ${fmtGB(t)}`);
  if (h.load && h.cpu_count){
    const pct = Math.min(100, h.load[0]/h.cpu_count*100);
    html += `<div class="meter"><div class="m-hd"><b>CPU負荷</b><span class="r">${h.load[0].toFixed(2)} / ${h.cpu_count} cores</span></div>
      <div class="mbar"><i class="${meterPct(pct)}" style="width:${pct}%"></i></div></div>`;
  }
  el.innerHTML = html || `<span class="muted">計測項目なし</span>`;
}
function renderHomeGpus(){
  const el = $("#home-gpus"); const s = S.sys;
  if (s === undefined){ el.innerHTML = `<span class="muted"><span class="spin"></span> GPU 検出中…</span>`; return; }
  if (s === null || !s.gpus || !s.gpus.length){
    el.innerHTML = `<span class="muted">${esc((s&&s.error)||"GPU がありません")}</span>`; return;
  }
  el.innerHTML = `<div class="grid" style="grid-template-columns:repeat(auto-fit,minmax(220px,1fr))">` + s.gpus.map(g=>{
    const memPct = g.mem_total_mib? (g.mem_used_mib/g.mem_total_mib*100):0;
    return `<div style="border:1px solid var(--line);border-radius:var(--r2);padding:10px 12px">
      <div class="row" style="gap:6px"><b style="font-size:13px">GPU ${esc(g.index)}</b>
        <span class="grow ellip muted" style="font-size:11.5px">${esc(g.name)}</span></div>
      <div class="meter" style="margin-top:8px"><div class="m-hd"><b>使用率</b><span class="r">${g.util??0}%</span></div>
        <div class="mbar"><i class="${meterPct(g.util||0)}" style="width:${g.util||0}%"></i></div></div>
      <div class="meter"><div class="m-hd"><b>VRAM</b><span class="r">${Math.round(memPct)}% · ${Math.round((g.mem_used_mib||0)/1024*10)/10}/${Math.round((g.mem_total_mib||0)/1024*10)/10}GB</span></div>
        <div class="mbar"><i class="${meterPct(memPct)}" style="width:${memPct}%"></i></div></div>
      <div class="row muted" style="gap:10px;margin-top:6px;font-size:11.5px">
        <span><svg class=ic aria-hidden=true><use href=#i-thermo /></svg> ${g.temp??'-'}°C</span>
        <span><svg class=ic aria-hidden=true><use href=#i-bolt /></svg> ${g.power??'-'}${g.power_limit?'/'+g.power_limit:''}W</span>
      </div></div>`;
  }).join("") + `</div>`;
}
function renderHomeLive(){
  const el = $("#home-live"); const live = liveSessions();
  const c = $("#home-live-count"); c.className = "badge " + (live.some(s=>s.ready)?"ok":"info");
  c.innerHTML = `<span class="dot"></span>${live.length}`;
  if (!live.length){ el.innerHTML = `<div class="empty" style="padding:16px 12px">起動中のサーバーはありません。<br>
    <button class="primary" style="margin-top:10px" onclick="switchTab('launch')"><svg class=ic aria-hidden=true><use href=#i-play /></svg> 起動する</button></div>`; return; }
  el.innerHTML = live.map(s=>{
    const url = s.port?`http://${esc(S.state.system.ip)}:${s.port}`:"";
    return `<div class="mini">
      ${statusBadge(s)}
      <span class="chip ${esc(s.backend)}">${esc(s.backend_name)}</span>
      <b class="grow ellip" style="font-size:13.5px">${esc(s.profile_name)}</b>
      <span class="lact">
        ${url?`<a class="btn" style="min-height:0;padding:4px 8px;font-size:12px" href="${url}" target="_blank" rel="noopener" title="OpenAI互換APIのルートを開く">:${s.port}↗</a>`:""}
        ${s.ready?`<button class="ghost" style="min-height:0;padding:4px 8px;font-size:12px" title="プレイグラウンドでチャット" onclick="openPlayground('${s.id}')"><svg class=ic aria-hidden=true><use href=#i-flask /></svg></button>`:""}
        ${s.ready?`<button class="ghost" style="min-height:0;padding:4px 8px;font-size:12px" title="ベンチマーク" onclick="openBench('${s.profile_id}')"><svg class=ic aria-hidden=true><use href=#i-chart /></svg></button>`:""}
        <span class="muted upt mono" data-sid="${s.id}" data-pfx="稼働 " style="font-size:11.5px">稼働 ${fmtUp(s.uptime)}</span>
        <button class="danger" style="min-height:0;padding:4px 10px;font-size:12px" onclick="stopSession('${s.id}')"><svg class=ic aria-hidden=true><use href=#i-stop /></svg></button>
      </span>
    </div>`;
  }).join("");
}
function renderHomeBackends(){
  const el = $("#home-backends");
  el.innerHTML = (S.state.backends||[]).map(b=>{
    const det=b.detection; const live=(S.state.sessions||[]).filter(s=>s.backend===b.id&&isLive(s)).length;
    const cls = det.installed?"ok":"err";
    return `<div class="mini">
      <span class="chip ${esc(b.id)}">${esc(b.name)}</span>
      <span class="grow ellip muted" style="font-size:11.5px" title="${esc(det.python||det.detail||b.module_path)}">${esc(shortPath(det.python||det.detail||b.module_path))}</span>
      ${live?`<span class="badge info"><span class="dot"></span>稼働 ${live}</span>`:""}
      <span class="badge ${cls}"><span class="dot"></span>${det.installed?"検出":"未検出"}</span>
    </div>`;
  }).join("");
}
function renderHomeRecent(){
  const el = $("#home-recent"); const ss = (S.state.sessions||[]).slice(0,6);
  if (!ss.length){ el.innerHTML = `<div class="muted" style="padding:8px 2px">まだ起動履歴がありません。</div>`; return; }
  el.innerHTML = ss.map(s=>`<div class="mini" style="cursor:pointer" title="このセッションを開く" onclick="gotoSession('${s.id}')">
    ${statusBadge(s)}
    <span class="grow ellip" style="font-size:13px">${esc(s.profile_name)}</span>
    <span class="chip ${esc(s.backend)}">${esc(s.backend_name)}</span>
    <span class="muted" style="font-size:11.5px">${
      isLive(s)?("稼働 "+fmtUp(s.uptime)) : (s.finished_at? fmtAgo(s.finished_at)+" に"+(s.status==="failed"?"異常終了":"終了") : "-")
    }</span>
  </div>`).join("");
}
let clockInterval = null;
function tickHomeClock(){ fmtClock(); if(!clockInterval) clockInterval=setInterval(fmtClock,1000); }


