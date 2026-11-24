/* ===================================================================
   core: 状態 / 共通ヘルパ / API / ポーリング / サイドメニュー / ルーター / ログ枠 — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
/* ============================================================= state */
const S = {
  state: null, tab: "home", filter: "all",
  sys: undefined,          // /api/system 結果 (undefined=未取得 null=失敗  object=OK)
  vram: {},                // sid -> FreeToken /v1/meminfo 中継 (GPU 欄の割り当て内訳)
  editing: null,           // ドラフト: {name,backend,values,on,extra_args,env,cwd,command,custom_only,note}
  hf: undefined,           // HF Hub キャッシュスキャン結果 (遅延取得)
  hdr: {host:"", ip:""},   // 右上アカウントチップの吹き出しに出すホスト情報 (refresh() が更新)
  openLogs: new Set(), logCursor: {}, logBuf: {},   // logBuf: sid -> 蓄積ログ文字列
  autoScroll: {}, busy: {}, _ssSig: "", _hsSig: "", _stateSig: undefined,
  pm: {q:"", sort:"updated", backend:"all", batch:false, sel:new Set()},   // 管理画面の状態
  hist: {q:"", status:"all", limit:30},   // セッション履歴画面の状態
  pg: null,              // プレイグラウンド状態 (pgInit() で遅延初期化)
  bench: null,           // ベンチマーク状態 (bmInit() で初期化)
  coding: null,          // コーディングベンチ状態 (cbInit() で初期化)
  cal: null,             // キャリブレーション状態 (calInit() で初期化)
  ins: null,             // セットアップ状態 (insInit() で初期化)
  mm: null,              // モデル管理状態 (mmInit() で初期化)
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
/* --- 操作中のオブジェクトは周期再描画に巻き込まない ---------------------------
   innerHTML の差し替えは要素ごと入れ替えるので、入力中のフォームは消え、選択して
   読んでいるテキストは解除され、アニメーションの途中なら最初に戻る。これが起きる
   のは「中身が変わったかどうか」ではなく「ポーリングが来たかどうか」で組み直して
   いたから。そこで周期再描画は次の順で判断する:
     (a) 中身が変わらなければ触らない (setHtml / setOptions のキャッシュ)
     (b) 箱が「操作中」(入力フォーカス / IME 変換 / マウス押下 / テキスト選択 /
         有限アニメーション進行中) なら、操作が済むまで後回し (deferRender)
     (c) フォームが未保存の編集を持っていれば (formDirty) 巻き戻さない
     (d) メーターなど構造が変わらない数値は in-place で書き換え、width トランジ
         ション (.mbar i) を生かす (meterPatch / patchUptime)
   初回 (箱が空) は後回しにしない = 画面を空白にしない。ユーザー操作由来の描画
   (フィルタクリック等) は、操作中の要素を箱が含まないので止まらない。 */
let _pressing = false, _pressT = 0, _composing = false;
window.addEventListener("pointerdown", () => { _pressing = true; _pressT = Date.now(); }, {passive: true, capture: true});
window.addEventListener("pointerup", () => { _pressing = false; }, {passive: true, capture: true});
window.addEventListener("pointercancel", () => { _pressing = false; }, {passive: true, capture: true});
window.addEventListener("blur", () => { _pressing = false; _composing = false; });
window.addEventListener("compositionstart", () => { _composing = true; }, {capture: true});
window.addEventListener("compositionend", () => { _composing = false; }, {capture: true});
/* この箱を今組み直すとユーザーの操作 / 閲覧 / アニメーションを壊すか。
   押下と変換は画面全体で短い時間なので箱を問わず止める (クリックの down→up の
   間に DOM が入れ替わるとクリック自体が死ぬ)。フォーカス / 選択 / アニメーション
   は箱の中だけを見る = 関係ない箱の更新は止めない。 */
function uiBusyIn(el) {
  if (!el || !el.isConnected) return false;
  // 押下は通常 down→up の一瞬。ウィンドウ外で離す等で up が届かない事故でも
  // 更新が永久に止まらないよう 5 秒で期限切れにする (長押しのスクロール drag は
  // uiScrolling / 選択 drag はテキスト選択側が引き続き守る)
  if (_pressing && Date.now() - _pressT < 5000) return true;
  if (_composing) return true;
  const ae = document.activeElement;
  if (ae && ae !== document.body && el.contains(ae) &&
      (ae.matches("input,select,textarea,iframe") || ae.isContentEditable)) return true;
  const sel = document.getSelection && document.getSelection();
  if (sel && !sel.isCollapsed && sel.rangeCount && el.contains(sel.anchorNode)) return true;
  // 有限のアニメーション / トランジションだけ待つ (spin / pulse / blink のような
  // 無限ループを数えると永遠に更新されなくなる)
  if (el.getAnimations) {
    for (const a of el.getAnimations({subtree: true})) {
      const t = a.effect && a.effect.getTiming && a.effect.getTiming();
      if (t && t.iterations === Infinity) continue;
      if (a.playState === "running" || a.playState === "pending") return true;
    }
  }
  return false;
}
/* フォームの未保存編集: 描画時の値 (data-base) と今の値が違えば dirty。保存 API が
   成功したら stampForm で基準を引き直し、リセット系は formReset で基準を捨てる。 */
const _fieldVal = f => (f.type === "checkbox" || f.type === "radio") ? (f.checked ? "1" : "0") : f.value;
function formDirty(el) {
  for (const f of el.querySelectorAll("input,select,textarea")) {
    if (f.dataset.base !== undefined && _fieldVal(f) !== f.dataset.base) return true;
  }
  return false;
}
function stampForm(el) {
  el.querySelectorAll("input,select,textarea").forEach(f => { f.dataset.base = _fieldVal(f); });
}
function formReset(el) {
  el.querySelectorAll("input,select,textarea").forEach(f => { delete f.dataset.base; });
}
/* 周期再描画の innerHTML 差し替え口。キャッシュ一致 → 何もしない / 操作中・未保存
   編集あり → deferRender で後回し (redo は描画関数そのもの。タイマー発火時に最新
   状態から組み直すので古い HTML を書き戻さない) / それ以外 → スクロール位置と
   details 開閉を保って差し替え、フォームの基準値を stamp する。 */
const _htmlCache = new WeakMap();
function setHtml(el, html, key, redo) {
  if (!el) return false;
  if (_htmlCache.get(el) === html) return false;
  if (el.childElementCount && (uiScrolling() || uiBusyIn(el) || formDirty(el))) {
    deferRender(key, redo || (() => setHtml(el, html, key, redo)));
    return false;
  }
  const _ls = logScrollSnap(el);
  el.innerHTML = html;
  logScrollBack(el, _ls);
  stampForm(el);
  _htmlCache.set(el, html);
  return true;
}
/* select の選択肢差し替え口。開いている (フォーカスがある) 間は組み直さない =
   ドロップダウンが閉じたり選択が飛んだりしない。閉じてから必要なら組み直す。
   値は前の選択 (pick があればそれ) を選択肢に残っていれば復元する。 */
function setOptions(sel, html, key, redo, pick) {
  if (!sel) return false;
  if (sel._optHtml === html && sel.childElementCount) return false;
  if (sel.childElementCount && (document.activeElement === sel || uiBusyIn(sel))) {
    deferRender(key, redo || (() => setOptions(sel, html, key, redo, pick)));
    return false;
  }
  const cur = pick != null ? pick : sel.value;
  sel.innerHTML = html;
  if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  sel._optHtml = html;
  return true;
}
/* フィルタチップ (.fchip) の列: 顔ぶれ (id + 名前 + 件数) が変わらない限り選択
   クラスだけ in-place で切り替える。クリックのたびに innerHTML を組み直すと
   フォーカス / ホバー / 押下のクリック自体が飛ぶ。顔ぶれが変わったときだけ
   setHtml で差し替える (操作中なら後回し)。 */
function patchChips(el, chips, onId, html, key, redo) {
  if (!el) return;
  const sig = chips.map(c => c.id + ":" + (c.name || "") + ":" + (c.n ?? "")).join("|");
  if (el._chipSig === sig && el.childElementCount === chips.length) {
    [...el.children].forEach((c, i) => c.classList.toggle("on", chips[i].id === onId));
    return;
  }
  if (setHtml(el, html, key, redo)) el._chipSig = sig;
}
/* メーターの数値を in-place で書き換える (構造 = ラベル・並び・周辺 HTML が同じ
   ときだけ)。innerHTML ごと組み直すと .mbar i の width トランジションが毎回初期化
   されて棒が跳ねる。既存ノードの style.width を書き換えれば CSS がそのまま animate
   する。構造が変わったら false を返すので呼び出し側は setHtml に落とす。 */
function _meterNorm(s) {
  return s.replace(/width:\s*[\d.]+%/g, "width:W")
          .replace(/<span class="r">[\s\S]*?<\/span>/g, '<span class="r">R</span>')
          // 棒の ok/warn/err クラスも値依存 (meterPct) なので構造比較からは外す
          .replace(/(<div class="mbar"><i class=")[^"]*(")/g, "$1C$2");
}
function _meterValues(oldM, newM) {
  const or = oldM.querySelector(".m-hd .r"), nr = newM.querySelector(".m-hd .r");
  if (or && nr && or.innerHTML !== nr.innerHTML) or.innerHTML = nr.innerHTML;
  const oi = oldM.querySelector(".mbar i"), ni = newM.querySelector(".mbar i");
  if (oi && ni) {
    if (oi.style.width !== ni.style.width) oi.style.width = ni.style.width;
    if (oi.className !== ni.className) oi.className = ni.className;
  }
}
function meterPatch(el, html) {
  if (!el || !el.childElementCount) return false;
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  if (_meterNorm(el.innerHTML) !== _meterNorm(tpl.innerHTML)) return false;
  const olds = el.querySelectorAll(".meter"), news = tpl.content.querySelectorAll(".meter");
  if (!olds.length || olds.length !== news.length) return false;
  olds.forEach((m, i) => _meterValues(m, news[i]));
  return true;
}
/* --- 自動更新は「必要なオブジェクト」にだけ適用する ---------------------------
   ポーリングは (1) 構造シグネチャ (stateSig) が変わったときだけ構造更新を配り、
   (2) 変わらなければ時間駆動のライブ欄 (tickLive: 稼働時間 / タブバッジ) だけを進める。
   構造更新の中身も一覧は行単位 (patchRows) で、変わった行だけ組み直す。
   ライブオブジェクトの例: セッション状態バッジ / 稼働時間 / メーター / GPU 統計 /
   デコード速度チャート / 実行中ジョブの進捗・ログ末尾 / タブバッジ / 時計。
   非ライブ (イベント駆動) の例: プロファイル / バックエンドカードの固定部 /
   フィルタ / フォーム / 履歴行の固定部 / 選択肢 / ビューアーの iframe。 */
/* 構造シグネチャ: 毎ポーリング動く値 (uptime / mem_available_gb / server_time) は
   除外し、描画に影響するフィールドだけを見る。分バケットを 1 つ含めるのは fmtAgo
   の「N 分前」表示を 1 分ごとに更新するため (patchRows が変わった行だけ直す)。 */
function stateSig(st) {
  if (!st) return "";
  const sess = (st.sessions || []).map(s => [s.id, s.status, s.ready ? 1 : 0, s.pid, s.port ?? "",
    s.detached ? 1 : 0, s.mem_state || "", s.oom_suspect ? 1 : 0, s.oom_swift_stop ? 1 : 0,
    s.oom ? `${s.oom.applied}/${s.oom.denied}/${s.oom.score}/${s.oom.error || ""}` : "",
    s.error || "", s.finished_at ?? "", s.exit_code ?? "", s.started_at ?? "",
    s.profile_id, s.backend].join(":")).join("|");
  const profs = (st.profiles || []).map(p => [p.id, p.name, p.backend, p.updated_at ?? "",
    JSON.stringify(p.values || {}), JSON.stringify(p.on ?? null), p.note || "",
    p.extra_args || "", JSON.stringify(p.env || {}), p.cwd || "", p.command || "",
    p.custom_only ? 1 : 0, p.simple_visible ? 1 : 0].join(":")).join("|");
  const bks = (st.backends || []).map(b => [b.id, b.name, b.command, b.default_command,
    b.python_override || "", b.binary_override || "", b.detection.installed ? 1 : 0,
    b.detection.module_exists ? 1 : 0, b.detection.python || "", b.detection.detail || ""].join(":")).join("|");
  const sys = st.system || {};
  return [sys.hostname, sys.ip, sys.version, sys.root, sys.platform,
          sess, profs, bks, Math.floor(Date.now() / 60000)].join("##");
}
/* 時間駆動のライブ欄だけを進める (状態が同じポーリングではこれ以外触らない)。 */
function tickLive() {
  renderTabBadges();
  if (!S.state) return;
  const live = liveSessions();
  if (S.tab === "running") patchUptime($("#running-list"), live);
  else if (S.tab === "launch") patchUptime($("#launch-grid"), live);
  else if (S.tab === "home") { patchUptime($("#home-live"), live); patchUptime($("#home-recent"), live); }
}
/* カード / 行一覧の選択的更新: 各行に data-row キーを付け、キー列が同じなら
   「変わった行だけ」差し替える。1 台の状態変化が他のカードのホバー / 選択 /
   ログスクロールを巻き込まない。行の比較はライブ DOM ではなく「前回書いた HTML」
   と行う (稼働時間 patch / ログ追記などの in-place 更新は差分と数えない)。
   顔ぶれ (キー + 順序) が変わったときだけ全体差し替え (setHtml と同じガード)。 */
function patchRows(el, html, key, redo) {
  if (!el) return false;
  if (_htmlCache.get(el) === html) return false;
  if (el.childElementCount && (uiScrolling() || uiBusyIn(el) || formDirty(el))) {
    deferRender(key, redo || (() => patchRows(el, html, key, redo)));
    return false;
  }
  const tpl = document.createElement("template");
  tpl.innerHTML = html;
  const news = [...tpl.content.children];
  const olds = [...el.children];
  const keyed = news.length > 0 && news.every(n => n.dataset.row !== undefined);
  if (keyed && olds.length === news.length &&
      olds.every((o, i) => o.dataset.row === news[i].dataset.row)) {
    news.forEach((n, i) => {
      const o = olds[i], nh = n.outerHTML;
      if (_htmlCache.get(o) === nh) return;   // 変わっていない行: 触らない
      const snap = logScrollSnap(o);
      o.replaceWith(n);
      logScrollBack(el, snap);              // 差し替えた行のログ位置 / 開閉だけ復元
      _htmlCache.set(n, nh);
    });
  } else {
    const snap = logScrollSnap(el);
    el.innerHTML = html;
    logScrollBack(el, snap);
    [...el.children].forEach((c, i) => { if (news[i]) _htmlCache.set(c, news[i].outerHTML); });
  }
  stampForm(el);
  _htmlCache.set(el, html);
  return true;
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
let refreshTimer = null, logTimer = null, sysTimer = null, clockTimer = null, benchTimer = null, calTimer = null, insTimer = null, codingTimer = null;
async function refresh(silent = true) {
  /* ランプ (live-dot) は「接続」専用。描画中の JS 例外を接続エラーと取り違えると、
     サーバーが健全なのに画面が赤く沈んで見える (実事故: 実行中の段バッジ組み立てで
     undefined 参照 → 履歴ごと消灯)。取得と描画を try で分ける。 */
  try {
    const next = await api("/api/state");
    $("#live-dot").classList.add("on");
    const sys = next.system;
    S.hdr = { host: sys.hostname, ip: sys.ip };   // 右上の吹き出しに出す (ヘッダにはアイコンだけ)
    renderAcct();
    /* 自動更新は必要なオブジェクトにだけ: 構造シグネチャが変わらなければ時間駆動の
       ライブ欄 (稼働時間 / タブバッジ) だけ進め、構造更新はデータが変わったときだけ
       配る。ユーザー操作由来 (silent=false) は常に全体を描く。 */
    const sig = stateSig(next);
    const unchanged = silent && S._stateSig !== undefined && sig === S._stateSig;
    S.state = next;
    S._stateSig = sig;
    if (unchanged) { tickLive(); return; }
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
  await refreshVram();
  if (S.tab === "home") renderHome();
  if (S.tab === "backends") renderOomCard();
}
/* GPU 欄の VRAM 割り当て内訳: 稼働中の FreeToken 系セッションだけ /v1/meminfo 中継を引く。
   Maya の GPU マップと同じ「カードに何が載っているか」をエンジンからもらう。取得失敗は
   前回の値を保つ (一時エラーで内訳を消さない)。セッションが無ければ空にしておく。 */
async function refreshVram() {
  const servers = (S.state?.sessions || []).filter(s =>
    isLive(s) && s.ready && (s.backend === "freetoken" || s.backend === "freetoken-fork"));
  if (!servers.length) { S.vram = {}; return; }
  const next = {};
  await Promise.all(servers.map(async s => {
    try { next[s.id] = await api(`/api/sessions/${encodeURIComponent(s.id)}/meminfo`); }
    catch { next[s.id] = S.vram[s.id] || null; }
  }));
  S.vram = next;
}
function startTimers() {
  clearInterval(refreshTimer); clearInterval(logTimer); clearInterval(sysTimer); clearInterval(benchTimer);
  clearInterval(calTimer); clearInterval(insTimer); clearInterval(codingTimer);
  refreshTimer = setInterval(() => refresh(true), 3000);
  logTimer = setInterval(pumpLogs, 1500);
  sysTimer = setInterval(refreshSystem, 4000);
  benchTimer = setInterval(bmPoll, 2500);
  calTimer = setInterval(calPoll, 2500);
  insTimer = setInterval(insPoll, 3000);
  codingTimer = setInterval(cbPoll, 2500);
  refreshSystem();
  bmPoll();
  calPoll();
  insPoll();
  cbPoll();
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
                  bench: "bench", coding: "bench", calibrate: "calibrate", setup: "install", models: "install",
                  profiles: "profiles.edit", backends: "backends.edit" };
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
                    "users","playground","bench","coding","calibrate","setup","models"];
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
  // GPU 欄の割り当て内訳はホーム表示中だけポーリングする (タブ復帰時に即取得)。
  if (tab === "home" && changed) refreshVram().then(() => { if (S.tab === "home") renderHome(); });
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
  // コーディングベンチのビューアー (2 実装並列) だけ画面幅を広げる
  document.querySelector("main").style.maxWidth =
    (S.tab === "coding" && S.coding && S.coding.tab === "viewer") ? "1600px" : "";
  renderTabBadges();
  if (S.tab === "home") renderHome();
  if (S.tab === "launch") renderLaunch();
  if (S.tab === "running") renderRunning(force);
  if (S.tab === "history") renderHistory(force);
  if (S.tab === "profiles") renderProfiles();
  if (S.tab === "backends") renderBackends();
  if (S.tab === "playground") renderPlayground();
  if (S.tab === "bench") renderBench();
  if (S.tab === "coding") renderCoding();
  if (S.tab === "calibrate") renderCalibrate();
  if (S.tab === "setup") renderSetup();
  if (S.tab === "models") renderModels();
}
function renderTabBadges() {
  const n = liveSessions().length;
  const nb = $("#nb-running");
  nb.classList.toggle("hidden", n === 0); nb.textContent = n;
  const f = (S.state.sessions || []).filter(s => s.status === "failed").length;
  const nf = $("#nb-fail");
  nf.classList.toggle("hidden", f === 0); nf.textContent = f;
  const nc = $("#nb-coding");
  if (nc) {
    const cbr = S.coding && S.coding.active ? 1 : 0;
    nc.classList.toggle("hidden", cbr === 0); nc.textContent = cbr;
  }
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
      <label class="switch" style="padding:4px 0;min-height:0"><input type="checkbox" ${S.autoScroll[sid]!==false?"checked":""} onchange="S.autoScroll['${sid}']=this.checked;this.dataset.base=this.checked?'1':'0'"><span class="track"></span><span class="lab" style="font-size:12px">自動スクロール</span></label>
      <span class="grow"></span>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="downloadLog('${sid}')"><svg class=ic aria-hidden=true><use href=#i-download /></svg> DL</button>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="copyLog('${sid}')"><svg class=ic aria-hidden=true><use href=#i-clipboard /></svg> コピー</button>
      <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="clearLogView('${sid}')">画面クリア</button>
    </div>
    <pre class="logbox" id="logbox-${sid}"></pre>
  </div>`;
}
/* ビルド直後: オープン中のログにバッファを復元 (テキスト選択の解除・「読み込み中…」化を防ぐ)。
   既に正しい内容の箱は書き換えない = 行単位更新で差し替わらなかった行の選択を守る。 */
function restoreLogBuffers() {
  for (const sid of S.openLogs) {
    const el = $(`#logbox-${sid}`);
    if (!el) continue;
    const buf = S.logBuf[sid] ?? "読み込み中…";
    if (el.dataset.len === String(buf.length) && el.textContent.length === buf.length) continue;
    el.textContent = buf;
    el.dataset.len = buf.length;
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
