/* ===================================================================
   view: calibrate — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.calibrate = `
  <section id="view-calibrate" class="view hidden">
    <h2 class="view-title">キャリブレーション <span class="muted">Maya / Strata / FreeToken の「この PC 向け調整」を 1 ボタンで実行</span></h2>
    <div id="cal-active"></div>
    <div class="card">
      <div class="row" style="gap:8px">
        <select id="cal-profile" class="grow" style="min-width:180px" onchange="calPersist()"></select>
        <span class="badge" id="cal-prof-state"><span class="dot"></span>-</span>
      </div>
      <div class="muted" id="cal-prof-note" style="margin-top:6px"></div>
      <label class="switch" style="margin-top:6px"><input type="checkbox" id="cal-restart"><span class="track"></span><span class="lab" style="font-size:12.5px">調整後にサーバーを起動し直す (完了後に自動起動)</span></label>
      <div class="row" style="margin-top:10px">
        <button class="primary need-cal" id="cal-start" onclick="calStart()"><svg class=ic aria-hidden=true><use href=#i-target /></svg> キャリブレーション実行</button>
        <span class="grow"></span>
        <span class="muted" id="cal-note"></span>
      </div>
      <div class="muted mono" id="cal-preview" style="font-size:11px;margin-top:8px"></div>
    </div>
    <div id="cal-history"></div>
  </section>
`;
/* ============================================================= calibration */
/* Maya / Strata / FreeToken の「この PC 向け調整」を 1 ボタンで実行する。
   実行はバックエンド (POST /api/calibrations) が裏で走らせ、ここでは 2.5 秒ポーリングで
   進行 (step / ログ末尾 / 結果) を描画する。エンジン起動と config 書き換えを伴うため、
   実行前に同じプロファイルのサーバーを launcher が停止して GPU を独占する。
   実行は 1 件ずつ (バックエンドが 409 で拒否)、中断はプロセスグループごと SIGTERM。 */
const CAL_KEY = "llm-laun…cal.v1";
function calInit() {
  if (S.cal) return;                        // 何度呼ばれても状態を失わない (jobs / logs)
  let d = {};
  try { d = JSON.parse(localStorage.getItem(CAL_KEY) || "{}") || {}; } catch { d = {}; }
  S.cal = { jobs: [], active: null, busy: false, supported: [], _pSig: "", _ch: "", _recv: 0,
            logCursor: {}, logs: {}, openLog: "", logError: "", scSig: "" };
  if (d.profile) { $("#cal-profile").value = d.profile;
                   if ($("#sc-profile")) $("#sc-profile").value = d.profile; }
  if (d.restart === true) { $("#cal-restart").checked = true;
                            if ($("#sc-restart")) $("#sc-restart").checked = true; }
  $("#cal-profile").addEventListener("change", calPersist);
  $("#cal-restart").addEventListener("change", calPersist);
}
const calSaved = () => { try { return JSON.parse(localStorage.getItem(CAL_KEY) || "{}") || {}; } catch { return {}; } };
/* 選択 (プロファイル / 再起動) は <svg class=ic aria-hidden=true><use href=#i-target /></svg> タブと セットアップ③ で同一の値を指す。
   保存と同時に両セレクトへ写すので、どちらで選んでもう一方がズレない。 */
function calWrite(patch) {
  const cur = Object.assign(calSaved(), patch || {});
  try { localStorage.setItem(CAL_KEY, JSON.stringify(cur)); } catch { /* 容量超過等は無視 */ }
  if (cur.profile) ["#cal-profile", "#sc-profile"].forEach(id => {
    const el = $(id);
    if (el && [...el.options].some(o => o.value === cur.profile)) el.value = cur.profile;
  });
  ["#cal-restart", "#sc-restart"].forEach(id => { const el = $(id); if (el) el.checked = cur.restart === true; });
}
function calPersist() {
  if (!S.cal) return;
  calWrite({profile: $("#cal-profile").value, restart: $("#cal-restart").checked});
}
function openCalibration(pid) {
  if (!S.cal) calInit();
  if (pid) { $("#cal-profile").value = pid; calPersist(); }
  switchTab("calibrate");
  calPoll(true);
}
/* このバックエンドがキャリブレーションを持つか。
   supported は /api/calibrations の応答 (サーバー側 CAL_BACKENDS と同じ)。初回描画の前に
   ボタンを出すため、応答前は同じ集合をローカル既定値で持つ — ここがサーバーより狭いと
   セットアップ③ と「完了後にキャリブレーション」が最初の 1 描画だけ消える。 */
const CAL_FALLBACK = ["freetoken", "freetoken-fork", "maya", "strata", "strata-fork"];
function calSupports(bid) {
  const list = (S.cal && (S.cal.supported || []).length) ? S.cal.supported : CAL_FALLBACK;
  return list.includes(bid);
}
/* 1 ボタン: プロファイルを選び、確認 → 実行。キャリブレーション画面へ移動してライブ表示 */
function calProfileQuick(pid) {
  if (!S.cal) calInit();
  if (S.cal.active) { switchTab("calibrate"); calPoll(true); return toast("別のキャリブレーションが実行中です — 画面で中断できます"); }
  $("#cal-profile").value = pid; calPersist(); switchTab("calibrate");
  calStart();
}
/* 実行のコア (確認 → POST → ライブ追跡)。<svg class=ic aria-hidden=true><use href=#i-target /></svg> タブのボタンと セットアップ③ から共用する。
   opts.auto = インストール前に同意済みの自動計走だけ確認窓を省略する (離席中に窓待ちで止まらないため)。 */
async function calRun(pid, restart, opts) {
  if (!S.cal) calInit();
  const d = S.cal;
  if (d.busy || d.active) { toast("キャリブレーションがすでに実行中です"); return false; }
  const p = profileById(pid);
  const b = p && backendById(p.backend);
  if (!p || !calSupports(p.backend)) { toast("Maya / Strata / FreeToken のプロファイルで実行できます"); return false; }
  const live = liveOf(pid);
  const isFt = p.backend === "freetoken" || p.backend === "freetoken-fork";
  const t = { maya: "~10-15分 (DS4 は 25-40分)", "strata": "~15-30分 (measure) + apply",
              "freetoken": "~3-4時間 (全部測りの1発)", "freetoken-fork": "~3-4時間 (全部測りの1発)" }[p.backend] || "長時間";
  const warn = (live ? "稼働中のサーバーを停止して GPU を独占します。\n" : "") +
               `${b?.name || p.backend}: この PC 向け調整を実行します (${t}, PC はその間占有されます)。\n` +
               (p.backend === "strata" ? "設定は backup + smoke test 付きで適用されます。\n" : "") +
               (isFt ? "none / mtp(+chain) / lookup の自動選定 + 内部ノブの A/B まで全部実測し、勝者の --speculative と env を起動構成へ自動記載します。\n" : "") +
               "続行しますか?";
  if (!(opts && opts.auto) && !confirm(warn)) return false;
  d.busy = true; renderCalibrate(); renderSetup();
  try {
    const job = await api("/api/calibrations", {method: "POST",
      body: {profile_id: pid, restart_after: restart === true}});
    toast(`キャリブレーション開始: ${job.profile_name} [${job.backend_name}]`);
    d.active = job.id;
    calPoll(true);
    return true;
  } catch (e) {
    // メモリガードの拒否は理由が長い (必要な空き / 止めるべきもの) ので、消える前に読めるよう長めに出す
    toast("開始失敗: " + e.message, /空きメモリ/.test(e.message) ? 12000 : 3500);
    return false;
  }
  finally { d.busy = false; calPoll(true); renderSetup(); }
}
async function calStart() {
  const pid = $("#cal-profile").value;
  if (!pid) return toast("プロファイルを選択してください");
  calRun(pid, $("#cal-restart").checked);
}
async function calPoll(force) {
  calInit();
  try {
    const j = await api("/api/calibrations");
    S.cal.jobs = j.jobs || []; S.cal.active = j.active; S.cal.supported = j.supported || [];
    S.cal._recv = Date.now() / 1000;
    if (S.cal.active) await calPollLog(S.cal.active);
    calBadge();
    if (S.tab === "calibrate" || force) renderCalibrate();
    if (S.tab === "setup") renderSetup();     // セットアップ画面は ③ と履歴に計走状態を出す
  } catch { /* ignore */ }
}
async function calPollLog(jid) {
  const cur = S.cal.logCursor[jid] || 0;
  try {
    const r = await api(`/api/calibrations/${jid}/log?cursor=${cur}`);
    const lines = r.lines || [];
    S.cal.logs[jid] = ((S.cal.logs[jid] || "") + lines.map(l => l + "\n").join("")).slice(-20000);
    S.cal.logCursor[jid] = (r.start || 0) + lines.length;   // start は絶対行番号 (切り詰め窓の外は無視)
  } catch { /* ignore */ }
}
/* 履歴 (終了済み / 中断済み) のログを取得する。実行中の live tail は calPollLog が積むだけなので、
  完了して active から外れたジョブは cursor 0 から取り直す (サーバーはジョブの logs を末尾 LOG_TAIL 行
   まで保持している)。失敗した / 中断したジョブを「実行中表示のまま」放置しないための閲覧導線。 */
async function calOpenLog(jid) {
  if (!S.cal) calInit();
  S.cal.openLog = S.cal.openLog === jid ? "" : jid;
  S.cal.logError = "";
  renderCalibrate();
  if (!S.cal.openLog) return;
  try {
    const r = await api(`/api/calibrations/${jid}/log?cursor=0`);
    S.cal.logs[jid] = (r.lines || []).join("\n");
    S.cal.logCursor[jid] = (r.start || 0) + (r.lines || []).length;
  } catch (e) { S.cal.logError = e.message; }
  renderCalibrate();
}
function calLogBox(jid, maxH = 260) {
  const logs = S.cal.logs[jid] || "";
  const tail = logs.split("\n").filter(Boolean).slice(-120).join("\n");
  return `<div class="logwrap"><pre class="logbox" data-logkey="k:${esc(jid)}" style="max-height:${maxH}px">${esc(tail || "(ログ無し / 取得できませんでした)")}</pre></div>`;
}
function calBadge() {
  const nb = $("#nb-cal"); if (!nb) return;
  const on = !!S.cal.active;
  nb.classList.toggle("hidden", !on);
  nb.classList.toggle("run", on);
  nb.innerHTML = on ? IC("play") : "";
}
async function calCancel(id) {
  if (!confirm("キャリブレーションを中断しますか?\n調整中のエンジンは停止します (設定は書き換え前のまま / Strata は measure 中止)")) return;
  try { await api(`/api/calibrations/${id}/cancel`, {method: "POST"}); toast("中断要求を送信しました"); calPoll(true); }
  catch (e) { toast("失敗: " + e.message); }
}
async function calDelete(id) {
  if (!confirm("このキャリブレーション履歴を削除しますか?")) return;
  try { await api(`/api/calibrations/${id}`, {method: "DELETE"}); toast("削除しました"); calPoll(true); }
  catch (e) { toast("失敗: " + e.message); }
}
function calToggle(id) { S.cal.open = S.cal.open === id ? "" : id; renderCalibrate(); }
function calStatusBadge(r) {
  const map = {queued: ["info", "待機"], running: ["warn", "実行中"], done: ["ok", "完了"],
               failed: ["err", "失敗"], cancelled: ["", "中断"], lost: ["err", "中断 (launcher 再起動)"]};
  const [cls, label] = map[r.status] || ["", r.status];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}
const CAL_STEP_LABEL = {create: "profile 作成", measure: "measure", review: "review", apply: "apply",
                        calibrate: "実測調整", sweep: "MTP sweep", "calibrate-lookup": "lookup sweep",
                        auto: "自動選定", "phase-mtp": "MTP 実測", "phase-lookup": "lookup 実測",
                        tournament: "決勝"};
const calStepLabel = name => CAL_STEP_LABEL[name] || name;
function calStepChips(job) {
  const steps = job.steps || [];
  const chips = steps.map((name, i) => {
    const cur = job.step === name && job.status === "running";
    const idx = steps.indexOf(job.step);
    const cls = cur ? "warn" : (idx > i || job.status === "done" ? "ok" : "");
    return `<span class="badge ${cls}"><span class="dot"></span>${esc(calStepLabel(name))}</span>`;
  });
  // sweep 中は job.step が現セル名 ("min_p=0.3" 等) になるので別バッジで進行を出す
  if (job.status === "running" && job.step && steps.indexOf(job.step) < 0)
    chips.push(`<span class="badge warn"><span class="dot"></span>${esc(job.step)}</span>`);
  return chips.join(" ");
}
/* key = .bm-wrap の横スクロール / details を復元するキー (data-logkey と衝突させない
   ため実行中は "a:" 接頭辞で渡す。renderCalActive のログ欄は "k:" を使う) */
function calSettingsTable(rep, key) {
  const st = rep.settings || {};
  const keys = Object.keys(st);
  if (!keys.length) return `<div class="muted" style="font-size:11px;margin-top:6px">調整値は返されませんでした (ログ参照)</div>`;
  return `<div class="bm-wrap" data-scrollkey="${esc(key)}"><table class="bm"><tr><th>設定</th><th>実測値</th></tr>` +
    keys.map(k => `<tr><td class="mono">${esc(k)}</td><td><b>${esc(st[k])}</b></td></tr>`).join("") +
    (typeof rep.improvement_pct === "number"
      ? `<tr><td>基準比 (greedy プローブ)</td><td><b>${rep.improvement_pct >= 0 ? "+" : ""}${rep.improvement_pct}%</b></td></tr>` : "") +
    (rep.latency ? `<tr><td>レイテンシ内訳</td><td class="mono" style="font-size:11px">${esc(rep.latency)}</td></tr>` : "") +
    `</table></div>`;
}
/* 実行される計走の要約 (<svg class=ic aria-hidden=true><use href=#i-target /></svg> タブと セットアップ③ で同じ文を出す) */
function calPreview(p) {
  if (!p) return "";
  if (p.backend === "maya")
    return "maya.sh --calibrate --no-start --config <maya-<model>.json>  (起動せず実測 → config へ書き戻し)";
  if (p.backend === "strata" || p.backend === "strata-fork")
    return "strata.py engine calibrate --profile <保存profile> --measure --exclusive --out <report.json> → --review-result → --apply-result";
  return "launcher 内で MTP / lookup を実測 (auto 選定) → 勝者の --speculative と env を起動構成 (profile / 起動 JSON) へ書き戻し";
}
function renderCalibrate() {
  calInit();
  const ps = (S.state?.profiles || []).filter(p => calSupports(p.backend));
  const sel = $("#cal-profile");
  const cur = sel.value;
  const sig = ps.map(p => p.id + ":" + p.name).join("|");
  if (sig !== S.cal._pSig || ![...sel.options].some(o => o.value === cur)) {
    S.cal._pSig = sig;
    sel.innerHTML = ps.length
      ? ps.map(p => `<option value="${p.id}">${esc(p.name)} · ${esc(backendById(p.backend)?.name || p.backend)}</option>`).join("")
      : `<option value="">対象プロファイルがありません (Maya / Strata / FreeToken)</option>`;
    if (cur) sel.value = cur;
  }
  const p = ps.find(x => x.id === sel.value) || null;
  const sess = p ? liveOf(p.id) : null;
  const st = $("#cal-prof-state");
  st.className = "badge " + (sess ? (sess.ready ? "ok" : "warn") : "");
  st.innerHTML = `<span class="dot"></span>` + (sess ? (sess.ready ? `稼働中 :${sess.port || "-"}` : "起動中…") : "未起動");
  $("#cal-prof-note").textContent = !p ? "Maya / Strata / FreeToken のプロファイルを作成してください。"
    : sess ? "実行前に launcher がこのサーバーを停止します (GPU 独占 / その後は再起動は手動)"
    : ($("#cal-restart").checked ? "未起動 → 調整完了後に自動起動します。"
                                 : "未起動 → 調整のみ実行します (完了後は起動タブから開始)");
  $("#cal-start").disabled = S.cal.busy || !!S.cal.active || !p;
  $("#cal-note").textContent = S.cal.busy ? "開始中…"
    : (S.cal.active ? "キャリブレーション実行中です (同時に 1 件)" : "");
  $("#cal-preview").textContent = calPreview(p);
  const active = (S.cal.jobs || []).find(r => r.id === S.cal.active) || null;
  renderCalActive(active);
  renderCalHistory(active);
}
function renderCalActive(run) {
  const el = $("#cal-active");
  if (!run) { el.innerHTML = ""; return; }
  // スクロール中は組み直しを後回し (調整値表の横スクロールバーを掴んだままにできる)
  if (uiScrolling() && el.childElementCount) { deferRender("cal-act", () => renderCalActive(run)); return; }
  const _ls = logScrollSnap(el);
  const skew = run.status === "running" || run.status === "queued" ? (Date.now() / 1000) - (S.cal._recv || 0) : 0;
  const secs = (run.finished_at ? run.finished_at : Date.now() / 1000) - (run.started_at || 0);
  const logs = S.cal.logs[run.id] || "";
  const tail = logs.split("\n").filter(Boolean).slice(-25).join("\n");
  el.innerHTML = `<div class="card" style="border-color:#c6dafc">
    <div class="row">
      ${calStatusBadge(run)}
      <b class="grow ellip" style="font-size:15px">${esc(run.profile_name)}</b>
      <span class="chip ${esc(run.backend)}">${esc(run.backend_name)}</span>
      ${run.status === "running" || run.status === "queued"
        ? `<button class="danger" style="min-height:34px;font-size:12.5px" onclick="calCancel('${run.id}')"><svg class=ic aria-hidden=true><use href=#i-stop /></svg> 中断</button>`
        : `<button class="ghost" style="min-height:34px;font-size:12.5px" onclick="calPoll(true)"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg></button>`}
    </div>
    <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">&nbsp;${calStepChips(run)}</div>
    <div class="row" style="gap:8px;margin-top:8px">
      <span class="muted">${{queued: "待機中…", running: "実行中", done: "完了",
        failed: "失敗", cancelled: "中断", lost: "中断 (launcher 再起動)"}[run.status] || run.status}</span>
      <span class="muted mono">${fmtUp(secs)}</span>
      ${run.error ? `· <span style="color:var(--err)">${esc(String(run.error).slice(0, 160))}</span>` : ""}
      ${run.restart_after && run.status === "done" ? `· <span style="color:var(--ok)">再起動済み</span>` : ""}
    </div>
    ${run.report ? calSettingsTable(run.report, "a:" + run.id) : ""}
    <div class="logwrap"><pre class="logbox" data-logkey="k:${esc(run.id)}" style="max-height:260px">${esc(tail || "(ログ待ち…)")}</pre></div>
  </div>`;
  logScrollBack(el, _ls);
}
function renderCalHistory(active) {
  const el = $("#cal-history");
  const runs = (S.cal.jobs || []).filter(r => r.id !== active?.id);
  if (!runs.length) {
    const empty = `<div class="empty" style="margin-top:12px">キャリブレーション履歴はまだありません。上でプロファイルを選んで <svg class=ic aria-hidden=true><use href=#i-target /></svg> キャリブレーション実行。</div>`;
    if (S.cal._ch !== empty) { S.cal._ch = empty; el.innerHTML = empty; }   // 空欄も毎回は書き直さない
    return;
  }
  // スクロール中は組み直しを後回し (調整値表の横スクロールバーを掴んだままにできる)
  if (uiScrolling() && el.childElementCount) { deferRender("cal-hist", () => renderCalHistory(active)); return; }
  const html = `<h2 class="view-title" style="margin-top:18px">履歴 (${runs.length})</h2>` + runs.map(r => {
    const open = S.cal.open === r.id;
    return `<div class="card">
      <div class="row">
        ${calStatusBadge(r)}
        <b class="grow ellip" style="font-size:14.5px;cursor:pointer" onclick="calToggle('${r.id}')">${esc(r.profile_name)}</b>
        <span class="chip ${esc(r.backend)}">${esc(r.backend_name)}</span>
      </div>
      <div class="muted" style="margin-top:5px;font-size:12px">${esc((r.steps || []).join(" → "))}
        ${r.report && Object.keys(r.report.settings || {}).length
          ? " · 調整値: " + esc(Object.entries(r.report.settings).map(([k, v]) => `${k}=${v}`).join(" ")) : ""}
        ${r.error ? ` · <span style="color:var(--err)">${esc(String(r.error).slice(0, 120))}</span>` : ""}
        · ${r.finished_at ? fmtAgo(r.finished_at) : "-"}</div>
      <div class="row" style="margin-top:8px">
        <button onclick="calToggle('${r.id}')">${open ? "<svg class=ic aria-hidden=true><use href=#i-chev_up /></svg> 閉じる" : "<svg class=ic aria-hidden=true><use href=#i-article /></svg> 結果"}</button>
        <button class="ghost" onclick="calOpenLog('${r.id}')">${S.cal.openLog === r.id ? "<svg class=ic aria-hidden=true><use href=#i-chev_up /></svg> ログ閉じる" : "<svg class=ic aria-hidden=true><use href=#i-receipt /></svg> ログ"}</button>
        <span class="grow"></span>
        <button class="ghost danger" onclick="calDelete('${r.id}')">削除</button>
      </div>
      ${open ? `<div class="muted mono" style="margin-top:6px;font-size:11px;overflow-wrap:anywhere">
        ${r.error ? esc(r.error) : "完了"}${r.report && r.report.application_record ? ` · 適用記録: ${esc(r.report.application_record)}` : ""}</div>`
        : ""}
      ${S.cal.openLog === r.id ? `<div style="margin-top:6px">${S.cal.logError
        ? `<span style="color:var(--err)">ログ取得失敗: ${esc(S.cal.logError)}</span>`
        : calLogBox(r.id, 320)}</div>` : ""}
    </div>`;
  }).join("");
  // 中身が変わらなければ組み直さない (ログ本文が増えたときだけ組み直す = 横位置が飛ぶのはそのとき)
  if (html === S.cal._ch && el.childElementCount) return;
  S.cal._ch = html;
  const _ls = logScrollSnap(el);                // 組み直したときだけ位置・開閉を復元
  el.innerHTML = html;
  logScrollBack(el, _ls);
}

