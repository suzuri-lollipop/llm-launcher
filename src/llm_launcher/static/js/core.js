/* ===================================================================
   core: 状態 / 共通ヘルパ / API / ポーリング / サイドメニュー / ルーター / ログ枠 — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
/* ============================================================= state */
const S = {
  state: null, tab: "home", filter: "all",
  sys: undefined,          // /api/system 結果 (undefined=未取得 null=失敗  object=OK)
  editing: null,           // ドラフト: {name,backend,values,on,extra_args,env,cwd,command,custom_only,note}
  hf: undefined,           // HF Hub キャッシュスキャン結果 (遅延取得)
  hdr: {host:"", ip:""},   // 右上アカウントチップの吹き出しに出すホスト情報 (refresh() が更新)
  openLogs: new Set(), logCursor: {}, logBuf: {},   // logBuf: sid -> 蓄積ログ文字列
  autoScroll: {}, busy: {}, _ssSig: "", _hsSig: "",
  pm: {q:"", sort:"updated", backend:"all", batch:false, sel:new Set()},   // 管理画面の状態
  hist: {q:"", status:"all", limit:30},   // セッション履歴画面の状態
  pg: null,              // プレイグラウンド状態 (pgInit() で遅延初期化)
  bench: null,           // ベンチマーク状態 (bmInit() で初期化)
  cal: null,             // キャリブレーション状態 (calInit() で初期化)
  ins: null,             // セットアップ状態 (insInit() で初期化)
};
const $ = (q, el=document) => el.querySelector(q);
const esc = s => String(s ?? "").replace(/[&<>"']/g, c => ({
  "&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const IC = n => `<svg class=ic aria-hidden=true><use href=#i-${n} /></svg>`;
const fmtAgo = ts => {
  if (!ts) return "-";
  const d = Math.max(0, Date.now()/1000 - ts);
  if (d < 60) return "たった今"; if (d < 3600) return Math.floor(d/60)+"分前";
  if (d < 86400) return Math.floor(d/3600)+"時間前"; return Math.floor(d/86400)+"日前";
};
const fmtUp = sec => {
  if (sec == null) return "-";
  sec = Math.floor(sec);
  const h = Math.floor(sec/3600), m = Math.floor(sec%3600/60);
  return h ? `${h}h${String(m).padStart(2,"0")}m` : m ? `${m}m${String(sec%60).padStart(2,"0")}s` : `${sec}s`;
};
const fmtDT = ts => ts
  ? new Date(ts * 1000).toLocaleString("ja-JP", {month:"2-digit", day:"2-digit", hour:"2-digit", minute:"2-digit"})
  : "-";
/* 長い絶対パスは /module/ 以降だけ表示 (末尾 ellipsis で .venv/bin/python 等が隠れる対策)。
   前綴がパス文字列のときだけ短縮し、「未検出 (…)」等のメッセージ文はそのまま通す。全文は title に出す */
const shortPath = p => {
  const s = String(p ?? "");
  const i = s.indexOf("/module/");
  if (i > 0 && /^[\w./+\-@~]+$/.test(s.slice(0, i))) return "…" + s.slice(i);   // 前綴がパスなら /module/ 以降へ短縮
  const v = s.startsWith("/") ? s.indexOf("/.venv/") : -1;   // プロジェクト直下 .venv のときも末尾が見えるように
  return v > 0 ? "…" + s.slice(v) : s;
};
/* --- 周期再描画 (ポーリング) とスクロール操作の調停 -----------------------------
   innerHTML の差し替えは要素を入れ替えるので、横スクロールバーの摘まみを掴んでいれば
   離されるし、ホイールやタッチの慣性も死ぬ。そのために (a) 中身が変わらなければ組み直
   さない、(b) スクロール操作中は組み直しを少し後回しにする、を組み合わせる。 */
/* 操作中かどうか: scroll はバブリングしないが capture で window に届くので、結果表
   (.bm-wrap) など要素単位のスクロールもまとめて見られる。passive なので描画を邪魔しない */
let _scrollHoldUntil = 0;
window.addEventListener("scroll", () => { _scrollHoldUntil = Date.now() + 350; },
                        {passive: true, capture: true});
const uiScrolling = () => Date.now() < _scrollHoldUntil;
/* 保留にする再描画は 1 枠 1 本 (ポーリングごとに積算するとタイマーが溜まる)。操作が
   350ms 空いた時点で 1 回だけ走らせるので、遅れても 1 ポーリング分 + 0.4 秒で追いつく。
   呼び出し側は「枠が空のとき (初回) は後回しにしない」こと = 画面が空白にならない。 */
const _deferTimers = {};
function deferRender(key, fn) {
  if (_deferTimers[key]) return;
  _deferTimers[key] = setTimeout(() => {
    delete _deferTimers[key];
    if (uiScrolling()) { deferRender(key, fn); return; }
    fn();
  }, 400);
}

async function api(path, opts = {}) {
  const r = await fetch(path, {
    headers: {"Content-Type": "application/json"},
    ...opts,
    body: opts.body ? JSON.stringify(opts.body) : undefined,
  });
  let data = null;
  try { data = await r.json(); } catch { /* ignore */ }
  if (!r.ok) {
    const e = new Error((data && data.detail) || `HTTP ${r.status}`); e.status = r.status;
    // セッション切れ: 定期取得を止めてログイン画面へ戻す (認証 API 自身の 401 は除く)
    if (r.status === 401 && !path.startsWith("/api/auth/")) onUnauthorized();
    throw e;
  }
  return data;
}
function toast(msg, ms = 3500) {
  const t = $("#toast"); t.textContent = msg; t.style.display = "block";
  clearTimeout(t._h); t._h = setTimeout(() => t.style.display = "none", ms);
}
const backendById = id => (S.state?.backends || []).find(b => b.id === id);
const profileById = id => (S.state?.profiles || []).find(p => p.id === id);
const isLive = s => s && ["running","starting","stopping"].includes(s.status);
const liveSessions = () => (S.state?.sessions || []).filter(isLive);
const liveOf = pid => (S.state?.sessions || []).find(s => s.profile_id === pid && isLive(s));
const sessionById = sid => (S.state?.sessions || []).find(s => s.id === sid);

function statusBadge(s) {
  if (!s) return `<span class="badge"><span class="dot"></span>未起動</span>`;
  const map = {
    running:  s.ready ? ["ok","稼働中"] : ["warn","起動中"],
    starting: ["warn","起動中"], stopping: ["info","停止中"],
    exited:   ["","終了"], failed: ["err","異常終了"], lost: ["purple","切断記録"],
  };
  const [cls, label] = map[s.status] || ["", s.status];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}

/* ============================================================= polling */
let refreshTimer = null, logTimer = null, sysTimer = null, clockTimer = null, benchTimer = null, calTimer = null, insTimer = null;
async function refresh(silent = true) {
  /* ランプ (live-dot) は「接続」専用。描画中の JS 例外を接続エラーと取り違えると、
     サーバーが健全なのに画面が赤く沈んで見える (実事故: 実行中の段バッジ組み立てで
     undefined 参照 → 履歴ごと消灯)。取得と描画を try で分ける。 */
  try {
    S.state = await api("/api/state");
    $("#live-dot").classList.add("on");
    const sys = S.state.system;
    S.hdr = { host: sys.hostname, ip: sys.ip };   // 右上の吹き出しに出す (ヘッダにはアイコンだけ)
    renderAcct();
  } catch (e) {
    $("#live-dot").classList.remove("on");
    S.hdr = { err: true }; renderAcct();          // 接続エラーは吹き出しの中と live-dot の赤で示す
    if (!silent) toast("状態取得に失敗: " + e.message);
    return;
  }
  try { renderAll(!silent); }
  catch (e) { console.error("render error:", e); }  // 描画失敗はランプを落とさずコンソールへ
}
async function refreshSystem() {
  try { S.sys = await api("/api/system"); }
  catch { S.sys = null; }
  if (S.tab === "home") renderHome();
  if (S.tab === "backends") renderOomCard();
}
function startTimers() {
  clearInterval(refreshTimer); clearInterval(logTimer); clearInterval(sysTimer); clearInterval(benchTimer);
  clearInterval(calTimer); clearInterval(insTimer);
  refreshTimer = setInterval(() => refresh(true), 3000);
  logTimer = setInterval(pumpLogs, 1500);
  sysTimer = setInterval(refreshSystem, 4000);
  benchTimer = setInterval(bmPoll, 2500);
  calTimer = setInterval(calPoll, 2500);
  insTimer = setInterval(insPoll, 3000);
  refreshSystem();
  bmPoll();
  calPoll();
  insPoll();
}

/* ============================================================= side menu (ハンバーガー) */
/* ☰ はヘッダー、✕ はドロワーの中。閉じるとドロワーは画面の外へ出る (横バーは残らない) */
function setMenu(open) {
  $("#sidemen").classList.toggle("open", open);
  $("#menu-veil").classList.toggle("on", open);
  document.body.classList.toggle("menu-open", open);
  $("#menu-btn").setAttribute("aria-expanded", open ? "true" : "false");
}
$("#menu-btn").addEventListener("click", () => setMenu(!$("#sidemen").classList.contains("open")));
$("#menu-close").addEventListener("click", () => setMenu(false));
document.querySelectorAll("nav#sidemen button[data-tab]").forEach(b => {
  b.addEventListener("click", () => {
    setMenu(false);
    /* セットアップは専用ウィザード (/setup) へ画面遷移する */
    if (b.dataset.tab === "setup") { location.href = "/setup"; return; }
    switchTab(b.dataset.tab);
  });
});
/* ロールで開けるタブ。起動 / 稼働中は常に開ける (メンテナーの基本操作)。users は管理者のみ */
const TAB_CAP = { home: "dashboard", history: "history", playground: "playground",
                  bench: "bench", calibrate: "calibrate", setup: "install", profiles: "profiles.edit", backends: "backends.edit" };
function tabAllowed(tab) {
  if (tab === "users") return AUTH.isAdmin;
  const cap = TAB_CAP[tab];
  return cap ? can(cap) : true;
}

/* ============================================================= ルーター (history API 連携)
   ビュー切替を URL ハッシュ (#/launch など) に載せ、ブラウザの「戻る」を有効にする。
   ダイアログ (プロフィール / ユーザー編集) もハッシュに状態を持たせる
   (#/profiles/editor/new など) ので、戻るとダイアログだけ閉じて一覧に戻る。
   直接リンクされたダイアログは列表だけ開いてダイアログ側を捨てる (戻るとアプリ外へ出るため)。
*/
const ROUTE_TABS = ["home","launch","running","history","profiles","backends",
                    "users","playground","bench","calibrate","setup"];
let _openDialog = null;   // 開いているダイアログ {kind, arg, mode}
let _firstApply = true;   // 初回 (ダイヤルアップ / 直接リンク) のルート解決
function routeUrl(tab, kind, arg, mode) {
  let h = "#/" + tab;
  if (kind) h += "/" + kind + "/" + encodeURIComponent(arg || "new");
  if (kind === "user" && mode && mode !== "form") h += "/" + mode;
  return h;
}
function parseHash() {
  const seg = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const tab = ROUTE_TABS.indexOf(seg[0]) >= 0 ? seg[0] : "launch";
  let kind = null, arg = "", mode = "form";
  if (seg.length >= 3 && (seg[1] === "editor" || seg[1] === "user")) {
    kind = seg[1]; arg = decodeURIComponent(seg[2]); mode = seg[3] === "pw" ? "pw" : "form";
  }
  return { tab, kind, arg, mode };
}
function go(hash, replace) {                   // URL 変更の唯一の入口
  if (location.hash === hash) { applyRoute(); return; }   // 同一 URL は hashchange が起きない
  if (replace) { history.replaceState(null, "", hash); applyRoute(); }
  else location.hash = hash;                   // hashchange 経由で applyRoute
}
function switchTab(tab) {                      // メニュークリック等: 履歴を 1 件積む
  if (_openDialog) go(routeUrl(tab), true);    // ダイアログが開いていれば積まずに置換
  else go(routeUrl(tab));
}
function defaultTab() { return AUTH.booted && tabAllowed("home") ? "home" : "launch"; }
function applyRoute() {
  const first = _firstApply; _firstApply = false;
  const raw = location.hash !== "" && location.hash !== "#";
  const r = parseHash();
  let tab = raw ? r.tab : defaultTab();
  if (AUTH.booted && !tabAllowed(tab)) { go("#/launch", true); return; }   // 権限喪失: そのビューを捨てる
  if (raw) {                                   // 不正な URL は履歴を増やさず正規化
    const canon = routeUrl(tab, first ? null : r.kind, r.arg, r.mode);
    if (location.hash !== canon) { go(canon, true); return; }
  }
  const changed = tab !== S.tab;
  if (changed || first) {
    closeAcct();
    document.querySelectorAll("nav#sidemen button[data-tab]").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
    document.querySelectorAll("main section.view").forEach(v => v.classList.toggle("hidden", v.id !== "view-" + tab));
    S.tab = tab;
  }
  syncDialog(r);
  renderAll();
  if (tab === "users" && !r.kind) { loadUsers(); loadRoles(); }
}
function syncDialog(r) {
  const cur = _openDialog;
  const same = !!cur && cur.kind === r.kind && cur.arg === (r.kind ? r.arg : "") &&
               (cur.kind !== "user" || cur.mode === r.mode);
  if (!same) closeDialogDom();
  if (r.kind === "editor" && !same && can("profiles.edit")) {
    openEditorDom(r.arg === "new" ? "" : r.arg);
    _openDialog = { kind: "editor", arg: r.arg, mode: "form" };
  } else if (r.kind === "user" && !same && AUTH.isAdmin) {
    umOpenDom(r.arg === "new" ? "" : r.arg, r.mode);
    _openDialog = { kind: "user", arg: r.arg, mode: r.mode };
  }
}
function closeDialogDom() {
  if (!_openDialog) return;
  if (_openDialog.kind === "editor") closeEditorDom(); else umCloseDom();
  _openDialog = null;
}
addEventListener("hashchange", applyRoute);

/* 各ビューの HTML は js/views/<tab>.js に VIEWS.<tab> として埋め込まれている
   (旧 index.html の <main> 直下インラインマークアップ)。起動時にこれを <main> へ
   差し込んでから配線する。 */
const VIEWS = {};
function mountViews() {
  document.querySelector("main").innerHTML = ROUTE_TABS.map(t => VIEWS[t]).join("\n");
  document.querySelectorAll("main section.view").forEach(v => v.classList.add("hidden"));
  const v = document.querySelector("#view-" + S.tab); if (v) v.classList.remove("hidden");
}
/* ============================================================= render */
function renderAll(force) {
  if (!S.state) return;
  renderTabBadges();
  if (S.tab === "home") renderHome();
  if (S.tab === "launch") renderLaunch();
  if (S.tab === "running") renderRunning(force);
  if (S.tab === "history") renderHistory(force);
  if (S.tab === "profiles") renderProfiles();
  if (S.tab === "backends") renderBackends();
  if (S.tab === "playground") renderPlayground();
  if (S.tab === "bench") renderBench();
  if (S.tab === "calibrate") renderCalibrate();
  if (S.tab === "setup") renderSetup();
}
function renderTabBadges() {
  const n = liveSessions().length;
  const nb = $("#nb-running");
  nb.classList.toggle("hidden", n === 0); nb.textContent = n;
  const f = (S.state.sessions || []).filter(s => s.status === "failed").length;
  const nf = $("#nb-fail");
  nf.classList.toggle("hidden", f === 0); nf.textContent = f;
}

function fmtGB(b){ if(b==null) return "-"; const g=b/1073741824; return g>=10?Math.round(g)+"GB":g.toFixed(1)+"GB"; }
function meterPct(p){ return p>=90?"err":p>=70?"warn":"ok"; }
function meter(label, used, total, unitFn){
  const pct = total ? Math.min(100, used/total*100) : 0;
  return `<div class="meter"><div class="m-hd"><b>${esc(label)}</b>
    <span class="r">${unitFn?unitFn(used,total):(Math.round(pct)+"%")}</span></div>
    <div class="mbar"><i class="${meterPct(pct)}" style="width:${pct}%"></i></div></div>`;
}
/* ---------------- actions ---------------- */
async function startProfile(pid, force = false) {
  S.busy[pid] = true; renderAll(true);
  try {
    const s = await api("/api/sessions", {method:"POST", body:{profile_id: pid, force}});
    toast(`起動しました: ${s.profile_name} [${s.backend_name}] (pid ${s.pid})`);
    S.openLogs.add(s.id);
    switchTab("running");
  } catch (e) {
    if (e.status === 409 && /force/.test(e.message)) {
      if (confirm(e.message + "\n\n強制的に起動しますか?")) return startProfile(pid, true);
    } else toast("起動失敗: " + e.message);
  } finally { S.busy[pid] = false; renderAll(true); }
}
async function stopSession(sid) {
  S.busy[sid] = true; renderAll(true);
  try { await api(`/api/sessions/${sid}/stop`, {method:"POST"}); toast("停止しました"); }
  catch (e) { toast("停止失敗: " + e.message); }
  finally { S.busy[sid] = false; refresh(false); }
}
async function restartSession(sid, force = false) {
  S.busy[sid] = true; renderAll(true);
  try {
    const s = await api(`/api/sessions/${sid}/restart`, {method:"POST", body:{force}});
    toast("再起動しました");
    if (S.openLogs.has(sid)) { S.openLogs.delete(sid); S.openLogs.add(s.id); delete S.logCursor[s.id]; delete S.logBuf[s.id]; }
  } catch (e) {
    if (e.status === 409 && /force/.test(e.message)) {
      if (confirm(e.message + "\n\n強制的に再起動しますか?")) return restartSession(sid, true);
    } else toast("再起動失敗: " + e.message);
  } finally { S.busy[sid] = false; refresh(false); }
}
async function deleteSession(sid) {
  if (!confirm("履歴とログを削除しますか?")) return;
  try {
    await api(`/api/sessions/${sid}`, {method:"DELETE"});
    S.openLogs.delete(sid); delete S.logBuf[sid]; delete S.logCursor[sid]; toast("削除しました"); refresh(false);
  } catch (e) { toast("削除失敗: " + e.message); }
}

/* ---------------- 稼働中 / セッション履歴 (共通パーツ) ---------------- */
/* ライブログパネル。logwrap- / logbox- の ID は pumpLogs・copyLog・downloadLog との共通契約 */
function logPanel(sid, open) {
  return `<div class="logwrap ${open ? "" : "hidden"}" id="logwrap-${sid}">
    <div class="row" style="margin-top:8px">
      <label class="switch" style="padding:4px 0;min-height:0"><input type="checkbox" ${S.autoScroll[sid]!==false?"checked":""} onchange="S.autoScroll['${sid}']=this.checked"><span class="track"></span><span class="lab" style="font-size:12px">自動スクロール</span></label>
      <span class="grow"></span>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="downloadLog('${sid}')"><svg class=ic aria-hidden=true><use href=#i-download /></svg> DL</button>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="copyLog('${sid}')"><svg class=ic aria-hidden=true><use href=#i-clipboard /></svg> コピー</button>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="clearLogView('${sid}')">画面クリア</button>
    </div>
    <pre class="logbox" id="logbox-${sid}"></pre>
  </div>`;
}
/* ビルド直後: オープン中のログにバッファを復元 (テキスト選択の解除・「読み込み中…」化を防ぐ) */
function restoreLogBuffers() {
  for (const sid of S.openLogs) {
    const el = $(`#logbox-${sid}`);
    if (!el) continue;
    el.textContent = S.logBuf[sid] ?? "読み込み中…";
    el.dataset.len = (S.logBuf[sid] || "").length;
    el.scrollTop = el.scrollHeight;
  }
}

function toggleLog(sid) {
  if (S.openLogs.has(sid)) S.openLogs.delete(sid);
  else { S.openLogs.add(sid); pumpLogs(); }
  if (isLive(sessionById(sid))) renderRunning(true); else renderHistory(true);
}
function clearLogView(sid) {
  S.logBuf[sid] = "";
  const el = $(`#logbox-${sid}`);
  if (el) { el.textContent = ""; el.dataset.len = 0; }
}
async function copyLog(sid) {
  const txt = S.logBuf[sid] ?? "";
  if (!txt) return toast("ログはまだ読み込まれていません");
  try {
    await navigator.clipboard.writeText(txt);
    toast(`ログをコピーしました (${txt.length.toLocaleString()} 文字)`);
  } catch {
    // http(非安全)コンテキスト用フォールバック
    const ta = document.createElement("textarea");
    ta.value = txt; ta.style.position="fixed"; ta.style.top="-9999px";
    document.body.appendChild(ta); ta.focus(); ta.setSelectionRange(0, txt.length);
    let ok = false; try { ok = document.execCommand("copy"); } catch {}
    ta.remove();
    toast(ok ? `ログをコピーしました (${txt.length.toLocaleString()} 文字)` : "コピー権限がありません。画面クリア前に手動選択でお願いします");
  }
}
async function downloadLog(sid) {
  const s = (S.state.sessions||[]).find(x=>x.id===sid);
  let txt = S.logBuf[sid];
  if (txt == null) { try { txt = (await api(`/api/sessions/${sid}/log?cursor=0`)).text; } catch(e){ return toast("取得失敗: "+e.message);} }
  const blob = new Blob([txt||""], {type:"text/plain;charset=utf-8"});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${(s?.profile_name||"server")}_${sid}.log`;
  a.click(); URL.revokeObjectURL(a.href);
}
async function pumpLogs() {
  for (const sid of S.openLogs) {
    try {
      const cur = S.logCursor[sid] || 0;
      const r = await api(`/api/sessions/${sid}/log?cursor=${cur}`);
      const hadBuf = S.logBuf[sid] != null;
      let buf = S.logBuf[sid];
      if (buf == null || r.cursor < cur) buf = r.text;          // 初回 / リセット
      else if (r.text) buf += r.text;                            // 差分追記
      S.logBuf[sid] = buf;
      S.logCursor[sid] = r.cursor;
      const el = $(`#logbox-${sid}`);
      if (!el) continue;
      const written = el.dataset.len != null ? +el.dataset.len : -1;
      // 初回填成 / リセット / 復元直後 → 全文を確定。以降は差分のみ追記して選択を壊さない
      if (!hadBuf || written < 0 || written > buf.length) {
        el.textContent = buf;
      } else if (buf.length > written) {
        el.insertAdjacentText("beforeend", buf.slice(written));
      }
      el.dataset.len = buf.length;
      if (S.autoScroll[sid] !== false &&
          el.scrollHeight - el.scrollTop - el.clientHeight < 160) {
        el.scrollTop = el.scrollHeight;
      }
    } catch { /* ignore */ }
  }
}
function openLog(sid) { if (!S.openLogs.has(sid)) toggleLog(sid); }
/* セッション単位で正しいタブへ飛ぶ (稼働中 → 稼働中タブ / 終了済み → 履歴タブ) */
function gotoSession(sid) {
  switchTab(isLive(sessionById(sid)) ? "running" : "history");
  openLog(sid);
}

/* 周期再描画 (innerHTML 差し替え) で入れ子のスクロール欄の位置を保つ。これがないと
   ポーリングのたびに scrollTop / scrollLeft が 0 に戻り「ログの一番上・結果表の左端へ
   飛ぶ」。縦は末尾にいた場合だけ追従 (新ログで下へ)、横 (.bm-wrap のベンチ / キャリの
   結果表) はその位置を復元する。details の開閉も覚えるので、開いたままにしたときの
   高さ変化でページが揺れる問題も同時に消える。対象は data-logkey / data-scrollkey /
   data-detailkey を付けた要素だけ。data-logkey は "k:"、data-scrollkey は実行中 "a:" /
   履歴 "k:" を接頭辞にする (同じ箱内でログ欄と表がキーを衝突させないため)。 */
function logScrollSnap(el) {
  const m = {};
  el.querySelectorAll("[data-logkey],[data-scrollkey]").forEach(p => {
    m[p.dataset.logkey || p.dataset.scrollkey] = {top: p.scrollTop, left: p.scrollLeft,
      // 末尾追従はログ欄の意味 (結果表は縦に溢れないので常に「末尾」扱いになってしまう)
      stick: p.hasAttribute("data-logkey")
               ? p.scrollHeight - p.scrollTop - p.clientHeight < 60 : false};
  });
  el.querySelectorAll("[data-detailkey]").forEach(d => { m[d.dataset.detailkey] = {open: d.open}; });
  return m;
}
function logScrollBack(el, m) {
  // details を先に開いてから位置を戻す (開閉で高さが変わるので順序を逆にすると縦位置がズレる)
  el.querySelectorAll("[data-detailkey]").forEach(d => {
    const s = m[d.dataset.detailkey];
    if (s && s.open !== undefined) d.open = s.open;
  });
  el.querySelectorAll("[data-logkey],[data-scrollkey]").forEach(p => {
    const s = m[p.dataset.logkey || p.dataset.scrollkey];
    if (!s || s.open !== undefined) return;
    // 同じ値は書かない (毎ポーリングの無意味な書き込みが「スクロールを引き戻す」原因)
    const top = s.stick ? p.scrollHeight : s.top;
    if (Math.abs(p.scrollTop - top) > 0.5) p.scrollTop = top;
    if (Math.abs(p.scrollLeft - s.left) > 0.5) p.scrollLeft = s.left;
  });
}
