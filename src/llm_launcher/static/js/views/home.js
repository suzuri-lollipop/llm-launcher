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
    <div class="card" style="margin-top:12px">
      <div class="card-h">デコード速度 <span class="muted" style="font-weight:400;letter-spacing:0;margin-left:4px">全スロット合算 · 直近 60 秒 · スロット毎のグラフは下表</span></div>
      <div class="par-tools">
        <label class="par-lab" for="home-par-select">サーバー</label>
        <select id="home-par-select" class="par-sel" onchange="parChoose(this.value)" aria-label="デコード速度を表示するサーバー"></select>
        <span class="badge" id="home-par-state"><span class="dot"></span>—</span>
        <span class="muted" id="home-par-count"></span>
        <span class="grow"></span>
        <span id="home-par-rate" class="par-rate" aria-live="polite">-</span>
      </div>
      <div id="home-par"><span class="muted"><span class="spin"></span> 読み込み中…</span></div>
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
  renderHomeParallel();
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

/* ---------------- dashboard: デコード速度 (live slot graph) ----------------
   basalt の Speed カード: /metrics の history を 60 秒分の折れ線にし、
   --parallel のスロットを 1 本ずつ色 / 太さで重ね、右端に現在値を置く。
   ここではブラウザ側で 1 秒ごとに取得したサンプルを 60 点溜める。
   Strata 系は live.slots の tok/s をスロット毎に描く。内訳の無い
   Maya などは全スロット合算だけを描く。 */
const PAR_HIST = 60;
const PAR_STEPS = [10, 20, 30, 50, 75, 100, 125, 150, 200, 250, 300, 400, 500,
                   600, 750, 800, 1000, 1200, 1500, 2000, 2500, 3000, 4000, 5000,
                   6000, 8000, 10000, 12000, 15000, 20000, 25000, 30000];
const PAR_SERVERS = new Set(["strata", "strata-fork", "maya"]);

function parInit(){
  if (!S.homePar) {
    S.homePar = { sid: "", samples: [], latest: null, error: "",
                  busy: false, sig: "", timer: null, slotLast: {} };
    setTimeout(parPoll, 0);
    S.homePar.timer = setInterval(parPoll, 1000);
    // 非表示タブの 1 秒タイマーはブラウザに 1 分/回へ絞られる (Chrome/Firefox の
    // timer throttling)。復帰時に間隔の再開を待たず即再取得しないと、生成中なのに
    // 状態バッジが最後に読んだ "待機" のまま取り残される。
    const resync = () => { if (document.visibilityState === "visible") parPoll(true); };
    document.addEventListener("visibilitychange", resync);
    window.addEventListener("pageshow", resync);
  }
  return S.homePar;
}
function parServers(){
  return (S.state.sessions || []).filter(s =>
    s.status === "running" && s.ready && PAR_SERVERS.has(s.backend));
}
function parChoose(sid){
  const p = parInit();
  p.sid = sid;
  p.samples = [];
  p.latest = null;
  p.error = "";
  p.slotLast = {};
  renderHomeParallel();
  parPoll(true);
}
async function parPoll(force){
  const p = parInit();
  if (S.tab !== "home" || !S.state) return;
  if (p.busy && !force) return;
  const servers = parServers();
  const ids = servers.map(s => s.id);
  if (!p.sid || !ids.includes(p.sid)) {
    p.sid = ids[0] || "";
    p.samples = [];
    p.latest = null;
    p.error = "";
    p.slotLast = {};
  }
  if (!p.sid){
    renderHomeParallel();
    return;
  }
  p.busy = true;
  const sid = p.sid;
  try {
    const data = await api(`/api/sessions/${encodeURIComponent(sid)}/parallel`);
    if (p.sid !== sid) return;             // 選択が切り替わったら古い結果は捨てる
    p.latest = data;
    p.error = "";
    const sample = parSample(data);
    const last = p.samples[p.samples.length - 1];
    // タイマー制限後や切断後 (間隔が空いた時) は、古い点に 1 点足すより
    // サーバー側の 60 秒履歴から描き直す (古い点が現在値のように並ぶのを防ぐ)。
    if (!p.samples.length || !last || sample.t - last.t > 5) p.samples = parHistorySamples(data);
    const tail = p.samples[p.samples.length - 1];
    if (!tail || sample.t > tail.t + 0.2) {
      p.samples.push(sample);
      if (p.samples.length > PAR_HIST) p.samples.splice(0, p.samples.length - PAR_HIST);
    }
  } catch (e) {
    p.error = (e.status === 404 || e.status === 405)
      ? "デコード速度の取得用 API が見つかりません。llm-launcher を再起動すると有効になります。"
      : (e.message || String(e));
  } finally {
    p.busy = false;
  }
  renderHomeParallel();
}
function parSample(d){
  const live = (d && d.live) || {};
  const slots = Array.isArray(d && d.slots) ? d.slots : [];
  return {
    t: typeof d.fetched_at === "number" ? d.fetched_at : Date.now() / 1000,
    total: typeof live.tok_s === "number" ? live.tok_s : null,
    slots: slots.map(s => ({
      slot: typeof s.slot === "number" ? s.slot : null,
      tok_s: typeof s.tok_s === "number" ? s.tok_s : null,
    })),
    active: typeof live.active === "number" ? live.active : null,
  };
}
function parHistorySamples(d){
  const hist = (d && d.history) || {};
  const raw = Array.isArray(hist.tok_s) ? hist.tok_s : [];
  const slotH = Array.isArray(hist.slot_tok_s) ? hist.slot_tok_s : [];
  const n = raw.length || slotH.reduce((m, a) => Math.max(m, (a || []).length), 0);
  if (!n) return [];
  const now = typeof d.fetched_at === "number" ? d.fetched_at : Date.now() / 1000;
  return Array.from({ length: n }, (_, i) => {
    const v = raw.length ? raw[raw.length - n + i] : null;
    return {
      t: now - (n - i),
      total: typeof v === "number" && Number.isFinite(v) ? v : null,
      slots: slotH.map((a, k) => {
        const sv = a && a[a.length - n + i];
        return { slot: k, tok_s: typeof sv === "number" && Number.isFinite(sv) ? sv : null };
      }).filter(x => x.tok_s != null),
      active: null,
    };
  });
}
function parNum(v){
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}
function parF1(v){
  const n = parNum(v);
  return n == null ? "-" : n.toLocaleString("ja-JP", {minimumFractionDigits: 1, maximumFractionDigits: 1});
}
function parInt(v){
  const n = parNum(v);
  return n == null ? "-" : Math.round(n).toLocaleString("ja-JP");
}
function parNice(v){
  if (!(v > 0)) return 20;
  return PAR_STEPS.find(s => s >= v * 1.15) || Math.ceil(v * 1.15 / 10000) * 10000;
}
function parPadded(arr){
  const pad = Math.max(0, PAR_HIST - arr.length);
  return Array(pad).fill(null).concat(arr);
}
function parSegments(values, yFn){
  const out = []; let cur = [];
  for (let i = 0; i < values.length; i++) {
    const v = values[i];
    if (!Number.isFinite(v)) { if (cur.length) { out.push(cur); cur = []; } continue; }
    cur.push(`${(i / (PAR_HIST - 1) * 1000).toFixed(1)},${yFn(v).toFixed(1)}`);
  }
  if (cur.length) out.push(cur);
  return out;
}
// basalt の Decode ヒーロー: 合算 (All) 1 本だけの 60 秒チャート。
// スロット毎の線はこのチャートには重ねず、下の Slot 行で各自のグラフを持つ。
function parChart(agg, max){
  const W = 1000, H = 200, top = 12, bottom = 10;
  const y = v => H - bottom - Math.min(Math.max(v, 0) / max, 1) * (H - top - bottom);
  let out = "";
  [0, 0.25, 0.5, 0.75, 1].forEach(f => {
    const py = H - bottom - f * (H - top - bottom);
    out += `<line x1="0" y1="${py.toFixed(1)}" x2="${W}" y2="${py.toFixed(1)}" stroke="var(--line)" stroke-width="1" stroke-dasharray="2 5" vector-effect="non-scaling-stroke"/>`;
  });
  parSegments(agg, y).forEach(pts => {
    out += `<polyline points="${pts.join(" ")}" fill="none" stroke="var(--acc)" stroke-width="2.1" stroke-linejoin="round" stroke-linecap="round" vector-effect="non-scaling-stroke"/>`;
  });
  let last = -1;
  for (let i = agg.length - 1; i >= 0; i--) if (Number.isFinite(agg[i])) { last = i; break; }
  let lab = "";
  if (last >= 0) {
    out += `<circle cx="${(last / (PAR_HIST - 1) * W).toFixed(1)}" cy="${y(agg[last]).toFixed(1)}" r="2.4" fill="var(--acc)" vector-effect="non-scaling-stroke"/>`;
    lab = `<span class="par-cl" style="top:${(y(agg[last]) / H * 100).toFixed(1)}%;color:var(--acc)">全体</span>`;
  }
  return `<div class="par-chart"><svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" role="img" aria-label="全スロット合算のデコード速度、直近 60 秒">${out}</svg>${lab}</div>`;
}
// basalt の slotRows のミニグラフ列: スロット毎の 60 秒スパークライン
function parSpark(values, max){
  const W = 160, H = 30, top = 3, bottom = 3;
  const base = `<line x1="0" y1="${H - bottom}" x2="${W}" y2="${H - bottom}" stroke="var(--line)" stroke-width="1.4" vector-effect="non-scaling-stroke"/>`;
  const y = v => H - bottom - Math.min(Math.max(v, 0) / max, 1) * (H - top - bottom);
  const segs = []; let cur = [];
  for (let i = 0; i < (values || []).length; i++) {
    const v = values[i];
    if (!Number.isFinite(v)) { if (cur.length) { segs.push(cur); cur = []; } continue; }
    cur.push(`${(i / (values.length - 1) * W).toFixed(1)},${y(v).toFixed(1)}`);
  }
  if (cur.length) segs.push(cur);
  const lines = segs.map(pts => `<polyline points="${pts.join(" ")}" fill="none" stroke="var(--acc)" stroke-width="1.4" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>`).join("");
  return `<svg viewBox="0 0 ${W} ${H}" preserveAspectRatio="none" aria-hidden="true">${base}${lines}</svg>`;
}
// basalt の slotRows と同じグリッド: Slot N + 状態 / tok/s / Last 60 s (スロット
// 毎の独立グラフ) / Output tok / Context。行は常に全スロット分keepingし、待機行は
// 値を薄くして最後の数字だけ残す (グリッドの形を変えない)。Accept 列は
// Strata の /metrics がスロット毎の下書き数を出さないため無い。
function parSlotTable(latest, series, p){
  const slots = latest && Array.isArray(latest.slots) ? latest.slots : [];
  if (!slots.length){
    const live = latest && latest.live || {};
    const active = parNum(live.active);
    return `<div class="par-note">このサーバーはスロット毎の内訳を /metrics に出していません。${active != null ? "アクティブ " + parInt(active) + " 本・" : ""}全スロット合算のデコード速度だけを描いています。</div>`;
  }
  const st = {
    idle: ["待機", ""], reading: ["読み込み", "warn"], prefill: ["読み込み", "warn"],
    generating: ["生成中", "ok"], decoding: ["生成中", "ok"],   // basalt/Strata とも decoding を返す
    writing: ["生成中", "ok"], "": ["-", ""],
  };
  const maxCtx = parNum(latest.server && latest.server.max_context);
  const ctxLabel = v => v != null && v > 0 ? (v >= 1000 ? Math.round(v / 1000) + "k" : String(v)) : "?";
  const max = parNice(Math.max(20, ...series.map(a => Math.max(0, ...a.map(v => parNum(v) || 0)))));
  const rows = slots.map((s, k) => {
    const idx = s.slot == null ? k : s.slot;
    const busy = (s.state || "") !== "idle";
    const prompt = s.prompt_tokens != null ? s.prompt_tokens : s.held_tokens;
    if (busy) p.slotLast[idx] = { state: s.state, tok_s: s.tok_s, generated: s.generated, prompt };
    const shown = busy ? { state: s.state, tok_s: s.tok_s, generated: s.generated, prompt }
                       : (p.slotLast[idx] || { state: "idle", prompt });
    const [label, cls] = (busy ? st[s.state || ""] : st.idle) || [s.state || "-", ""];
    const mut = busy ? "" : " mut";
    const ctx = busy ? prompt : (s.held_tokens != null ? s.held_tokens : shown.prompt);
    const acc = s.accept != null ? s.accept + "%" : "-";
    return `<div class="par-srow" role="row">
      <span class="par-slotid"><span class="nm">Slot ${idx + 1}</span><i class="${cls}">${esc(label)}</i></span>
      <span class="par-num${mut}">${parF1(shown.tok_s)}</span>
      <span class="par-sk">${parSpark(series[k] || [], max)}</span>
      <span class="par-num par-gen${mut}">${parInt(shown.generated)}</span>
      <span class="par-num par-ctx${mut}">${parInt(ctx)} / ${ctxLabel(s.n_ctx || maxCtx)}</span>
      <span class="par-num par-acc${mut}">${acc}</span>
    </div>`;
  }).join("");
  const busyN = slots.filter(s => (s.state || "") !== "idle").length;
  return `<div class="par-slots" role="table" aria-label="スロット別の稼働と直近 60 秒">
    <div class="par-srow par-head" role="row">
      <span>Slot</span><span>tok/s</span><span>Last 60 s</span><span class="par-gen">Output tok</span><span class="par-ctx">Context</span><span class="par-acc">Accept</span>
    </div>${rows}
  </div>
  <div class="par-foot">${busyN} / ${slots.length} スロット稼働${parNum(latest.live && latest.live.queued) > 0 ? ` · 待ち ${parInt(latest.live.queued)}` : ""}</div>`;
}
function renderHomeParallel(){
  const el = $("#home-par"), sel = $("#home-par-select");
  if (!el || !sel) return;
  const p = parInit();
  const servers = parServers();
  const sig = servers.map(s => `${s.id}|${s.profile_name}|${s.backend}|${s.port || ""}`).join("|");
  if (p.sig !== sig) {
    sel.innerHTML = servers.length
      ? servers.map(s => `<option value="${esc(s.id)}">${esc(s.profile_name)} · ${esc(s.backend_name)} :${esc(s.port || "?")}</option>`).join("")
      : `<option value="">— 並列 /metrics 対応サーバーなし —</option>`;
    p.sig = sig;
  }
  const stateEl = $("#home-par-state"), cntEl = $("#home-par-count"), rateEl = $("#home-par-rate");
  if (!servers.length){
    sel.value = "";
    stateEl.className = "badge info";
    stateEl.innerHTML = `<span class="dot"></span>未検出`;
    cntEl.textContent = "";
    rateEl.innerHTML = "-";
    el.innerHTML = `<div class="par-empty">
      Strata / Maya の並列内訳 (/metrics) を公開するサーバーが稼働していません。<br>
      スロット毎のグラフを描くには、起動タブから対応サーバーを起動してください。
    </div>`;
    return;
  }
  if (!p.sid || !servers.some(s => s.id === p.sid)) {
    p.sid = servers[0].id;
    p.samples = [];
    p.latest = null;
    p.error = "";
  }
  sel.value = p.sid;
  const latest = p.latest;
  const live = latest && latest.live || {};
  const state = live.state || "";
  const stateMap = {
    generating: ["ok", "生成中"], reading: ["warn", "読み込み"], idle: ["", "待機"],
    unloaded: ["", "未ロード"], loading: ["warn", "ロード中"],
  };
  const [stateCls, stateLabel] = stateMap[state] || ["", state || "—"];
  stateEl.className = "badge " + stateCls;
  stateEl.innerHTML = `<span class="dot"></span>${esc(stateLabel)}`;
  const parallel = parNum(live.parallel), running = parNum(live.running);
  const bits = [];
  if (parallel != null && parallel > 1) bits.push(`並列 ${parInt(parallel)}`);
  else if (running != null && running > 1) bits.push(`稼働 ${parInt(running)} 本`);
  if (parNum(live.waiting) > 0) bits.push(`待ち ${parInt(live.waiting)}`);
  cntEl.textContent = bits.join(" · ");
  const nowTok = parNum(live.tok_s);
  rateEl.innerHTML = nowTok != null
    ? `<b>${parF1(nowTok)}</b><span class="par-unit">tok/s</span><span class="par-sub">全スロット合算</span>`
    : `<span class="par-sub">-</span>`;
  if (!latest && !p.error) {
    el.innerHTML = `<span class="muted"><span class="spin"></span> サーバーの /metrics を待っています…</span>`;
    return;
  }
  const slots = Array.isArray(latest && latest.slots) ? latest.slots : [];
  const slotSeries = slots.map(meta => parPadded(p.samples.map(x => {
    const found = (x.slots || []).find(item => item.slot === meta.slot);
    return found ? found.tok_s : null;
  })));
  const agg = parPadded(p.samples.map(x => x.total));
  const values = [];
  agg.forEach(v => { if (parNum(v) != null) values.push(v); });
  const max = parNice(Math.max(20, ...values));
  let chart;
  if (p.samples.length < 2) {
    chart = `<div class="par-chart-empty"><span class="spin"></span> 次の 1 秒サンプルを待っています…</div>`;
  } else {
    chart = parChart(agg, max);
  }
  const errNote = p.error ? `<div class="par-note err">${esc(p.error)}</div>` : "";
  const model = latest && latest.server && latest.server.model ? latest.server.model : "";
  const meta = `<div class="par-meta">
    <span>${model ? esc(model) : ""}</span>
    <span>max ${parInt(max)} tok/s</span>
    <span>${p.samples.length} 点 / ${PAR_HIST} 秒</span>
  </div>`;
  el.innerHTML = `<div class="par-chart-wrap">${meta}${chart}
    <div class="par-axis"><span>60 秒前</span><span>30 秒</span><span>現在</span></div>
  </div>${errNote}${parSlotTable(latest, slotSeries, p)}`;
}

