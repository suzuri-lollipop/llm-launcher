/* ===================================================================
   view: setup — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.setup = `
  <section id="view-setup" class="view hidden">
    <h2 class="view-title">セットアップ <span class="muted">インストール / モデルダウンロード / 調整 は新しいステップ式ウィザードで行います</span></h2>
    <div class="card" style="text-align:center;padding:36px 16px">
      <div style="line-height:1;color:var(--sub)"><svg class=ic style="width:44px;height:44px" aria-hidden=true><use href=#i-box /></svg></div>
      <p class="muted" style="margin:12px 0 4px">1 画面 1 質問のセットアップ画面を用意しました。<br>
        案内にしたがって <b>インストール → モデルのダウンロード → 調整 (任意)</b> を順番に実行できます。</p>
      <button class="primary" style="max-width:300px;margin:14px auto 0" onclick="location.href='/setup'">セットアップを開始する</button>
      <p class="muted" id="ins-run-note" style="margin-top:12px;font-size:12px"></p>
    </div>
  </section>
`;
/* ============================================================= セットアップ (① インストール / ② モデルDL / ③ キャリブレーション) */
/* シェルを介さない導線: /api/installs を叩くだけ。calibrate と同じ fire-and-forget + poll。
   install は clone / venv / uv pip、download は HF キャッシュへの転送で progress に進捗が載る。
   ③ の計走は /api/calibrations のまま (calTimer が全体で見張っているのでここでは描画だけ)。 */
const INS_ST = {queued: ["info", "待機"], running: ["warn", "実行中"], done: ["ok", "完了"],
                failed: ["err", "失敗"], cancelled: ["", "中断"], lost: ["err", "中断 (launcher 再起動)"]};
const INS_STEPS = {clone: "clone", branch: "ブランチ切替", venv: "venv 作成", precheck: "確認",
                   install: "uv pip install", cmake: "cmake 設定", "cmake-build": "cmake ビルド",
                   engine: "エンジン受信", verify: "import 確認"};
/* install ステップの実体は uv があれば uv pip、無い環境だけ venv の pip。
   手順名はサーバーが返す tool (実行する argv と同じ判定) で出す。 */
const insToolLabel = tool => tool === "pip" ? "pip install" : "uv pip install";
const insStepName = (name, job) =>
  name === "install" ? insToolLabel((job || {}).tool) : (INS_STEPS[name] || name);
/* セットアップ画面で走るジョブ = インストール / DL とキャリブレーションの両方 */
const calUiOn = () => can("calibrate");
const calBusy = () => calUiOn() && !!(S.cal && (S.cal.active || S.cal.busy));
const insBusy = () => !!(S.ins && (S.ins.active || S.ins.busy));
function insInit() {
  if (S.ins) return;                        // 何度呼ばれても状態を失わない (jobs / logs)
  S.ins = { jobs: [], active: null, installable: [], cacheDir: "", busy: false, _bSig: "",
            _rSig: "", _mSig: "", _ia: "", _ih: "", refPick: {}, tool: "uv", calAfter: null, scPick: "",
            logs: {}, logCursor: {}, open: "", notified: {} };
}
async function insPoll(force) {
  insInit();
  try {
    const j = await api("/api/installs");
    S.ins.jobs = j.jobs || []; S.ins.active = j.active;
    S.ins.installable = j.installable || []; S.ins.cacheDir = j.cache_dir || "";
    S.ins.tool = j.tool || "uv";
    const act = S.ins.jobs.find(r => r.id === S.ins.active);
    if (act) {
      await insPollLog(act.id);
      // 完了時の後処理は 1 回だけ: DL はモデル一覧を再スキャン、install は検出を再取得
      if (!S.ins.notified[act.id]) {
        if (act.kind === "download" && act.status === "done") {
          S.ins.notified[act.id] = 1; loadHf(true); toast("モデルのダウンロードが完了しました");
        } else if (act.kind === "install" && act.status === "done") {
          S.ins.notified[act.id] = 1; await refresh(false);
          await insAfterInstall(act);       // 「インストール後に計走」の約束をここで果たす
        }
      }
    }
    insBadge();
    if (S.tab === "setup" || force) renderSetup();
  } catch { /* ignore */ }
}
async function insPollLog(jid) {
  const cur = S.ins.logCursor[jid] || 0;
  try {
    let r = await api(`/api/installs/${jid}/log?cursor=${cur}`);
    if ((r.total || 0) < cur) r = await api(`/api/installs/${jid}/log?cursor=0`);  // cursor/total は絶対行番号。total が減る = リセットのみ
    const lines = r.lines || [];
    if (r.start === cur && lines.length)
      S.ins.logs[jid] = ((S.ins.logs[jid] || "") + (S.ins.logs[jid] ? "\n" : "") + lines.join("\n")).slice(-40000);
    else if (r.start > cur)   // 古い行がログ窓 (LOG_TAIL) から切り詰められた
      S.ins.logs[jid] = ((S.ins.logs[jid] ? S.ins.logs[jid] + "\n" : "") + `(${r.start - cur} 行はログ窓から切り詰め済み)\n` + lines.join("\n")).slice(-40000);
    else if (r.start === 0) S.ins.logs[jid] = lines.join("\n");
    S.ins.logCursor[jid] = r.start + lines.length;
  } catch { /* ignore */ }
}
function insBadge() {
  const nb = $("#nb-ins"); if (!nb) return;
  const on = !!S.ins.active || calBusy();   // ③ の計走もこの画面のジョブ (メニューのバッジ)
  nb.classList.toggle("hidden", !on);
  nb.classList.toggle("run", on);
  nb.innerHTML = on ? IC("play") : "";
}
async function insStart() {
  insInit();
  const bid = $("#ins-backend").value;
  if (!bid) { toast("インストールするバックエンドを選んでください"); return; }
  if (S.ins.active) { toast("別のセットアップジョブが実行中です"); return; }
  if (calBusy()) return toast("キャリブレーション実行中は開始できません (GPU 独占 / 同時に 1 件)");
  const nm = insName(bid), sp = insSpec(bid), ref = insRef(bid);
  const tl = insToolLabel(S.ins.tool);
  const what = !ref ? `clone / .venv 作成 / ${tl}`
    : (!sp || !sp.cloned ? `${ref} を clone → .venv 作成 → ${tl}`
      : (sp.current_ref === ref ? `既存 clone (${ref}) のまま .venv 作成 / ${tl}`
        : `ブランチを ${sp.current_ref || "?"} → ${ref} に切り替えて .venv 作成 / ${tl}`));
  const calloc = calUiOn() && $("#ins-cal").checked === true && !!sp && calSupports(sp.id);
  if (!confirm(`${nm} をインストールしますか?\n${what} を実行します (初回は数 GB をダウンロード)`
    + (calloc ? "\n\n完了後、このバックエンドのプロファイルがあれば キャリブレーション を自動で続けます"
                 + " (GPU を独占、Maya ~10-15分 / Strata ~15-30分 / FreeToken ~3-4時間)" : ""))) return;
  S.ins.busy = true; renderSetup();
  try {
    const job = await api("/api/installs", {method: "POST", body: {backend: bid, clean: $("#ins-clean").checked === true,
                                                       ref}});
    S.ins.calAfter = calloc ? {id: job && job.id, backend: bid} : null;
    toast(calloc ? "インストールを開始しました (完了後に変調を自動実行)" : "インストールを開始しました");
  } catch (e) {
    toast("開始失敗: " + e.message);
  } finally {
    S.ins.busy = false; insPoll(true);
  }
}
function dlPickModel() {                 // 対応モデル一覧 → repo id に反映 (保存先が model_data 系なら従う)
  const v = inputVal("#dl-model"); if (!v) return;
  $("#dl-repo").value = v;
  // 一覧エントリの include (例: basalt の pack 1 個) をファイル絞り込みへ入れる
  const ent = (S.ins.installable || []).flatMap(e => e.supported_models || []).find(m => m.repo_id === v);
  if (ent && ent.include && ent.include.length) $("#dl-patterns").value = ent.include.join(", ");
  const sp = insSpec($("#ins-backend").value);
  if (sp && sp.model_data && inputVal("#dl-dest") !== sp.id) {
    const dd = $("#dl-dest");
    if ([...dd.options].some(o => o.value === sp.id)) dd.value = sp.id;
    renderSetup();
  }
}
async function dlStart() {
  insInit();
  const repo = (inputVal("#dl-repo") || "").trim();
  if (!repo) { toast("repo id を入力してください"); return; }
  if (S.ins.active) { toast("別のセットアップジョブが実行中です"); return; }
  if (calBusy()) return toast("キャリブレーション実行中は開始できません (同時に 1 件)");
  const body = {repo_id: repo, revision: (inputVal("#dl-rev") || "").trim(),
                backend: inputVal("#dl-dest") || "",
                allow_patterns: (inputVal("#dl-patterns") || "").split(",").map(s => s.trim()).filter(Boolean)};
  try {
    await api("/api/installs/download", {method: "POST", body});
    toast("ダウンロードを開始しました");
  } catch (e) { toast("開始失敗: " + e.message); }
  insPoll(true);
}
async function insCancel(id) {
  if (!confirm(`セットアップジョブを中断しますか?\nインストール中の ${insToolLabel((S.ins || {}).tool)} / ダウンロードが中止されます`)) return;
  try { await api(`/${insEp(id)}/${id}/cancel`, {method: "POST"}); toast("中断要求を送信しました"); insPoll(true); }
  catch (e) { toast("失敗: " + e.message); }
}
async function insDelete(id) {
  const ep = insEp(id);
  if (!confirm(ep === "calibrations"
    ? "このキャリブレーション履歴を削除しますか?\nレポート (report) も一緒に消えます"
    : "このセットアップ履歴を削除しますか?")) return;
  try {
    await api(`/${ep}/${id}`, {method: "DELETE"}); toast("削除しました");
    if (ep === "calibrations") { delete S.ins.logs[id]; await calPoll(true); }
    insPoll(true);
  }
  catch (e) { toast("失敗: " + e.message); }
}
async function insToggle(id) {
  S.ins.open = S.ins.open === id ? "" : id;
  if (S.ins.open === id && !S.ins.logs[id]) {          // 履歴のログは開いた時に 1 回だけ取得
    try {
      const r = await api(`/${insEp(id)}/${id}/log?cursor=0`);
      S.ins.logs[id] = (r.lines || []).join("\n");
    } catch { S.ins.logs[id] = "(ログ取得失敗)"; }
  }
  renderSetup();
}
/* セットアップは /setup のステップ式ウィザード (static/setup.html) へ画面遷移する。
   バックエンド id を渡すとその項目が预选択された状態で開く。 */
function setupTab(bid) {
  location.href = "/setup" + (bid ? "?backend=" + encodeURIComponent(bid) : "");
}
const insBytes = n => { n = n || 0;
  return n >= 2**30 ? (n / 2**30).toFixed(2) + " GiB"
       : n >= 2**20 ? (n / 2**20).toFixed(1) + " MB" : Math.round(n / 2**10) + " KiB"; };
function insStatusBadge(r) {
  const [cls, label] = INS_ST[r.status] || ["", r.status];
  return `<span class="badge ${cls}"><span class="dot"></span>${label}</span>`;
}
function insStepChips(job) {
  const steps = job.steps || [];
  const idx = steps.indexOf(job.step);
  return steps.map((name, i) => {
    const cur = job.step === name && job.status === "running";
    const cls = cur ? "warn" : (idx > i || job.status === "done" ? "ok" : "");
    return `<span class="badge ${cls}"><span class="dot"></span>${esc(insStepName(name, job))}</span>`;
  }).join(" ");
}
function insProg(run) {                                 // download の進捗 (累計 bytes / 現ファイル %)
  const pr = run.progress || {};
  if (!pr.done && !pr.file) return "";
  const pct = pr.file_total ? Math.min(100, 100 * pr.done / pr.file_total) : 0;
  return `<div class="row" style="gap:8px;margin-top:6px;flex-wrap:wrap">
    <span class="mono" style="font-size:12px">計 ${insBytes(pr.done)} · 現ファイル ${pr.file_total ? insBytes(pr.done) + " / " + insBytes(pr.file_total) : "-"}
     · ${insBytes(pr.speed_bps)}/s · ${pr.files_done || 0} ファイル完了${pr.file ? " · " + esc(String(pr.file).slice(0, 48)) : ""}</span>
    ${pct ? `<div style="flex:1;min-width:120px;height:8px;background:var(--line);border-radius:4px;overflow:hidden"><div style="height:100%;width:${pct.toFixed(1)}%;background:var(--acc)"></div></div>` : ""}
  </div>`;
}
const insSpec = bid => (S.ins.installable || []).find(e => e.id === bid) || null;
/* fork は未 clone 時 /api/state に出ないので、表示名は installable 側から引く */
const insName = bid => (insSpec(bid) || {}).name || (backendById(bid) || {}).name || bid;
/* 選択中のブランチ (refs を持つバックエンドだけ)。未選択なら spec の既定 */
const insRef = bid => {
  const sp = insSpec(bid); if (!sp) return "";
  const refs = sp.refs || []; if (!refs.length) return "";
  const pick = S.ins.refPick[bid];
  if (refs.some(r => r.ref === pick)) return pick;
  if (refs.some(r => r.ref === sp.current_ref)) return sp.current_ref;
  return refs.some(r => r.ref === sp.ref) ? sp.ref : refs[0].ref;
};
function insPickBackend() { renderSetup(); }
function insPickRef() { S.ins.refPick[$("#ins-backend").value] = $("#ins-ref").value; renderSetup(); }
const insKind = r => r.kind === "download" ? "download"
  : (r.kind === "install" ? "install" : "calibrate");
const insTitle = r => insKind(r) === "calibrate"
  ? `<svg class=ic aria-hidden=true><use href=#i-target /></svg> ${esc(r.profile_name || r.profile_id)} <span class="muted mono" style="font-size:12px">${esc(r.backend_name || r.backend)}</span>`
  : insKind(r) === "download" ? `<svg class=ic aria-hidden=true><use href=#i-download /></svg> ${esc(r.repo_id)}`
  : `<svg class=ic aria-hidden=true><use href=#i-box /></svg> ${esc(insName(r.backend))}`
    + (r.ref ? ` <span class="muted mono" style="font-size:12px">${esc(r.ref)}</span>` : "");
/* ---- セットアップ③: <svg class=ic aria-hidden=true><use href=#i-target /></svg> タブと同じ選択・同じ実行コア (calRun) を使う薄い UI ---- */
const scProfiles = () => calUiOn() ? (((S.state || {}).profiles || []).filter(p => calSupports(p.backend))) : [];
function scPick() {
  if (!S.cal) calInit();
  S.ins.scPick = $("#sc-profile").value;
  calWrite({profile: S.ins.scPick, restart: $("#sc-restart").checked});
  renderSetup();
}
function scStart() {
  insInit();
  if (insBusy()) return toast("インストール / ダウンロード実行中は開始できません (同時に 1 件)");
  const pid = $("#sc-profile").value;
  if (!pid) return toast("キャリブレーションするプロファイルを選択してください");
  S.ins.scPick = pid;
  calRun(pid, $("#sc-restart").checked);
}
/* インストール成功後の自動計走。① のスイッチで同意した分だけ走る。
   対象が 0 件 / 複数なら走らせず、その理由をその場で伝える
   (プロファイルやモデルパスを勝手に作らない = ③ で選ばせる)。 */
async function insAfterInstall(job) {
  const pend = S.ins.calAfter;
  if (!pend || pend.id !== job.id) return;
  S.ins.calAfter = null;
  const bid = pend.backend;
  if (!calUiOn() || !calSupports(bid)) return;
  if (calBusy()) return toast("キャリブレーションがすでに走っているため、自動実行はスキップしました", 8000);
  const cands = scProfiles().filter(p => p.backend === bid);
  if (!cands.length)
    return toast(`${insName(bid)} のプロファイルが無いのでキャリブレーションはスキップしました`
                 + " — プロファイルを作成してから ③ で実行してください", 11000);
  if (cands.length > 1) {
    S.ins.scPick = cands[0].id; renderSetup();
    return toast(`対象プロファイルが ${cands.length} 件あります — セットアップ③ で選んで実行してください`, 11000);
  }
  const p = cands[0];
  S.ins.scPick = p.id;
  if (await calRun(p.id, calSaved().restart === true, {auto: true}))
    toast(`インストール完了 → ${p.name} のキャリブレーションを自動開始しました`, 9000);
  renderSetup();
}
function renderSetupCal(busy) {
  const card = $("#sc-card");
  if (!card) return;
  if (!calUiOn()) { card.classList.add("hidden"); return; }   // 計走権限が無いユーザーには ③ を出さない
  card.classList.remove("hidden");
  if (!S.cal) calInit();
  const sel = $("#sc-profile"), ps = scProfiles();
  const sig = ps.map(p => p.id + ":" + p.name).join("|");
  if (sig !== S.cal.scSig || ![...sel.options].some(o => o.value === sel.value)) {
    S.cal.scSig = sig;
    const keep = S.ins.scPick || sel.value || calSaved().profile || "";
    sel.innerHTML = ps.length
      ? ps.map(p => `<option value="${p.id}">${esc(p.name)} · ${esc(backendById(p.backend)?.name || p.backend)}</option>`).join("")
      : `<option value="">対象プロファイルがありません (Maya / Strata / FreeToken 系)</option>`;
    const want = ps.some(p => p.id === keep) ? keep
      : (ps.find(p => p.backend === $("#ins-backend").value) || ps[0] || {}).id || "";
    sel.value = want;
    if (want) S.ins.scPick = want;
  }
  const p = ps.find(x => x.id === sel.value) || null;
  const calOn = !!S.cal.active;
  $("#sc-start").disabled = busy || S.cal.busy || calOn || !p;
  $("#sc-restart").disabled = busy || calOn;
  $("#sc-note").textContent = S.cal.busy ? "開始中…"
    : (calOn ? "キャリブレーション実行中です (同時に 1 件)"
      : (busy ? "インストール / ダウンロード実行中…" : ""));
  const sess = p ? liveOf(p.id) : null;
  $("#sc-state").innerHTML = !ps.length
    ? `キャリブレーション対応のプロファイルがまだありません。`
      + `インストール → <b>プロファイル作成</b> → モデルダウンロード の順に揃うと キャリブレーション が使えるようになります`
      + ` <button class="ghost" style="min-height:26px;padding:2px 9px;font-size:12px" onclick="switchTab('profiles')"><svg class=ic aria-hidden=true><use href=#i-clipboard /></svg> プロファイルへ</button>`
    : (!p ? "対象プロファイルがありません"
      : (sess ? `このプロファイルのサーバーは稼働中 (:${sess.port || "-"}) — 実行中に停止して GPU を独占します`
        : ($("#sc-restart").checked ? "未起動 → 調整後に自動起動します"
                                    : "未起動 → 調整のみ実行します (完了後は起動タブから開始)"))
        + (busy ? " / 他のジョブが終わり次第実行できます" : ""));
  $("#sc-preview").textContent = calPreview(p);
}
function renderSetup() {
  insInit();
  const sel = $("#ins-backend");
  if (!sel) {                                   // セットアップは /setup ウィザードへ移行済み。
    const n = $("#ins-run-note");               // 旧フォームが無い場合は「実行中」の案内だけ出す。
    if (n) n.textContent = (S.ins.active || calBusy())
      ? "いまジョブが実行中です — 進行中は /setup のセットアップ画面で見られます" : "";
    return;
  }
  const specs = S.ins.installable || [];
  const ids = specs.map(e => e.id);
  if (ids.join("|") !== S.ins._bSig || ![...sel.options].some(o => o.value === sel.value)) {
    S.ins._bSig = ids.join("|");
    const cur = sel.value;
    sel.innerHTML = specs.length
      ? specs.map(e => `<option value="${esc(e.id)}">${esc(e.name)} · ${esc(e.module_path)}</option>`).join("")
      : `<option value="">UI インストール可能なバックエンドがありません</option>`;
    if ([...sel.options].some(o => o.value === cur)) sel.value = cur;
  }
  const sp = insSpec(sel.value);
  // ブランチ選択: refs を持つバックエンドだけ出す (選択はバックエンドごとに記憶)
  const rs = $("#ins-ref");
  const refs = (sp && sp.refs) || [];
  const rSig = sel.value + "|" + refs.map(r => r.ref).join(",") + "|" + ((sp && sp.current_ref) || "") + (sp && sp.cloned ? "C" : "");
  if (rSig !== S.ins._rSig) {
    S.ins._rSig = rSig;
    rs.innerHTML = refs.map(r => `<option value="${esc(r.ref)}">${esc(r.label)} (${esc(r.ref)})</option>`).join("");
    rs.value = insRef(sel.value);
  }
  rs.style.display = refs.length ? "" : "none";
  const ref = insRef(sel.value);
  $("#ins-hfdir").textContent = S.ins.cacheDir || "(HF キャッシュ)";
  // 保存先: HF キャッシュ既定 + model_data を持つバックエンド (fork なら Freetoken-data)
  const dd = $("#dl-dest");
  const dOpts = [{v: "", l: "HF キャッシュ"}].concat(
    specs.filter(e => e.model_data).map(e => ({v: e.id, l: `${e.name} のデータフォルダ`})));
  const dSig = dOpts.map(o => o.v).join("|");
  if (dSig !== S.ins._dSig) {
    S.ins._dSig = dSig;
    const cur = dd.value;
    dd.innerHTML = dOpts.map(o => `<option value="${esc(o.v)}">${esc(o.l)}</option>`).join("");
    if ([...dd.options].some(o => o.value === cur)) dd.value = cur;
  }
  const dSp = insSpec(dd.value);
  // 対応モデルドロップダウン: 保存先 (なければ ①) のバックエンドが docs/models.md の
  // known-good 表を持つときだけ出す。選ぶと repo id に入り、model_data 系なら保存先も従う
  const ms = $("#dl-model");
  const mListOwner = (dSp && dSp.supported_models && dSp.supported_models.length) ? dSp : sp;
  const mlist = (mListOwner && mListOwner.supported_models) || [];
  const mSig = ((mListOwner && mListOwner.id) || "") + "|" + mlist.map(m => m.repo_id).join(",");
  if (mSig !== S.ins._mSig) {
    S.ins._mSig = mSig;
    ms.innerHTML = `<option value="">対応モデルから選択 (${mlist.length})</option>`
      + mlist.map(m => `<optgroup label="${esc(m.name)}">`
                   + `<option value="${esc(m.repo_id)}">${esc(m.repo_id)}</option></optgroup>`).join("");
  }
  ms.hidden = !mlist.length;
  const calLock = calBusy(), insLock = !!S.ins.active;
  $("#ins-tool").textContent = insToolLabel(S.ins.tool);
  $("#dl-note").textContent = calLock ? "キャリブレーション実行中のため開始できません"
    : (insLock ? "実行中のジョブがあります"
      : (dSp ? `保存先: ${dSp.model_data}/<org>--<name> (完了後このバックエンドの起動 JSON にも記入)` : ""));
  $("#ins-start").disabled = S.ins.busy || insLock || calLock || !specs.length;
  $("#dl-start").disabled = insLock || calLock;
  $("#ins-note").textContent = S.ins.busy ? "開始中…"
    : (insLock ? "セットアップ実行中です (同時に 1 件)" : (calLock ? "キャリブレーション実行中です (同時に 1 件)" : ""));
  // ① の「インストール後にキャリブレーション」は、選んだバックエンドが計走対象のときだけ出す
  const canCal = calUiOn() && !!sp && calSupports(sp.id);
  $("#ins-calloc").style.display = canCal ? "" : "none";
  if (!canCal) $("#ins-cal").checked = false;
  $("#ins-branch").textContent = !sp ? "" : (!refs.length ? "" :
    (!sp.cloned ? `未 clone — ${ref} を clone します`
      : (sp.current_ref === ref ? `既存 clone は ${ref} 済み — ブランチは変えません`
        : (sp.current_ref ? `既存 clone は ${sp.current_ref} — ${ref} へ切り替えます (未コミットの変更があれば失敗)`
          : `既存 clone のブランチが判別できないため ${ref} への切替を試みます`))));
  const tl = insToolLabel((sp && sp.tool) || S.ins.tool);
  /* 手順の末尾: requirements 系 (pyproject の無い Basalt など) は -r + cmake ビルド、
     Strata (engine_fetch) は -r + 公式リリースからのエンジン受信、
     packaging 系は従来どおり -e ".[extras]"。cmake / engine は実在チェック込みの verify で締める */
  const tail = sp.requirements
    ? `${tl} -r ${sp.requirements}`
      + (sp.cmake ? " → cmake configure + build (bin) → import + エンジン確認" : "")
      + (sp.engine_fetch ? " → エンジン受信 (公式リリース) → import + エンジン確認" : "")
    : `${tl} -e ".[${sp.extras}]"`;
  $("#ins-preview").textContent = !sp ? "" : (!sp.cloned
    ? `git clone --depth 1 ${ref ? "-b " + ref + " " : ""}${sp.repo} ${sp.module_path} → .venv 作成 → ${tail}`
    : (sp.current_ref === ref
        ? `既存 clone (${ref}) → .venv 作成 → ${tail}`
        : `git fetch + checkout ${ref} → .venv 作成 → ${tail}`));
  const busy = insLock || S.ins.busy;
  renderSetupCal(busy);
  const active = (S.ins.jobs || []).find(r => r.id === S.ins.active) || null;
  renderInsActive(active);
  renderInsHistory(active);
}
/* 上の実行中カード: インストール / DL と キャリブレーション を1つの枠に並べる
   (同時に走る組み合わせは作れないが、どちらが走っていてもここに出るので画面が迷子にならない) */
function renderInsActive(run) {
  const el = $("#ins-active");
  if (!el) return;
  const c = S.cal || {};
  const cal = calUiOn() ? (c.jobs || []).find(r => r.id === c.active) : null;
  const html = (run ? insActiveCard(run) : "") + (cal ? calActiveCardSetup(cal) : "");
  // 中身が変わらなければ組み直さない (ログ末尾が変わったときだけ位置を復元すればいい)
  if (html === S.ins._ia && el.childElementCount) return;
  // スクロール中は組み直しを後回し (表やログ欄を掴んだままにできる)
  if (uiScrolling() && el.childElementCount) { deferRender("ins-act", () => renderInsActive(run)); return; }
  S.ins._ia = html;
  const _ls = logScrollSnap(el);
  el.innerHTML = html;
  logScrollBack(el, _ls);
}
function insActiveCard(run) {
  const secs = (run.finished_at ? run.finished_at : Date.now() / 1000) - (run.started_at || Date.now() / 1000);
  const tail = (S.ins.logs[run.id] || "").split("\n").filter(Boolean).slice(-30).join("\n");
  const live = run.status === "running" || run.status === "queued";
  return `<div class="card" style="border-color:#c6dafc">
    <div class="row">
      ${insStatusBadge(run)}
      <b class="grow ellip" style="font-size:15px">${insTitle(run)}${run.kind === "install" && run.clean ? ` <span class="muted" style="font-size:12px">(クリーン)</span>` : ""}</b>
      ${live ? `<button class="danger" style="min-height:34px;font-size:12.5px" onclick="insCancel('${run.id}')"><svg class=ic aria-hidden=true><use href=#i-stop /></svg> 中断</button>`
             : `<button class="ghost" style="min-height:34px;font-size:12.5px" onclick="insPoll(true)"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 最新化</button>`}
    </div>
    ${run.kind === "install" ? `<div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">${insStepChips(run)}</div>`
    : run.kind === "download" ? insProg(run) : ""}
    <div class="row" style="gap:8px;margin-top:8px">
      <span class="muted mono">${fmtUp(secs)}</span>
      ${run.error ? `<span style="color:var(--err);font-size:12px">${esc(String(run.error).slice(0, 200))}</span>` : ""}
    </div>
    <div class="logwrap"><pre class="logbox" data-logkey="k:${esc(run.id)}" style="max-height:260px">${esc(tail || "(ログ待ち…)")}</pre></div>
  </div>`;
}
function calActiveCardSetup(run) {
  const secs = (run.finished_at ? run.finished_at : Date.now() / 1000) - (run.started_at || Date.now() / 1000);
  const tail = ((S.cal || {}).logs[run.id] || "").split("\n").filter(Boolean).slice(-30).join("\n");
  const live = run.status === "running" || run.status === "queued";
  return `<div class="card" style="border-color:#c6dafc">
    <div class="row">
      ${insStatusBadge(run)}
      <b class="grow ellip" style="font-size:15px">${insTitle(run)} <span class="muted" style="font-size:12px">③ キャリブレーション</span></b>
      ${live ? `<button class="danger" style="min-height:34px;font-size:12.5px" onclick="calCancel('${run.id}')"><svg class=ic aria-hidden=true><use href=#i-stop /></svg> 中断</button>`
             : `<button class="ghost" style="min-height:34px;font-size:12.5px" onclick="calPoll(true)"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 最新化</button>`}
    </div>
    <div class="row" style="gap:6px;margin-top:8px;flex-wrap:wrap">${calStepChips(run)}</div>
    <div class="row" style="gap:8px;margin-top:8px">
      <span class="muted mono">${fmtUp(secs)}</span>
      ${run.error ? `<span style="color:var(--err);font-size:12px">${esc(String(run.error).slice(0, 200))}</span>` : ""}
    </div>
    <div class="logwrap"><pre class="logbox" data-logkey="k:${esc(run.id)}" style="max-height:260px">${esc(tail || "(ログ待ち…)")}</pre></div>
  </div>`;
}
/* 履歴は 3 種 (インストール / モデルDL / キャリブレーション) を開始時刻順で1本に束ねる。
   id が ins… / cal… で分かれるので、ログ取得と削除は接頭辞で API を選ぶ。 */
const insEp = id => String(id || "").indexOf("cal") === 0 ? "calibrations" : "installs";
const INS_KIND_LABEL = {install: "インストール", download: "モデルDL", calibrate: "キャリブレーション"};
function insMerged() {
  const rows = (S.ins.jobs || []).map(r => Object.assign({_k: insKind(r)}, r));
  if (calUiOn()) ((S.cal || {}).jobs || []).forEach(r => rows.push(Object.assign({_k: "calibrate"}, r)));
  return rows.sort((a, b) => (b.started_at || 0) - (a.started_at || 0));
}
function renderInsHistory(active) {
  const el = $("#ins-history");
  const busy = {};
  if (active) busy[active.id] = 1;
  if (calUiOn() && S.cal && S.cal.active) busy[S.cal.active] = 1;
  const runs = insMerged().filter(r => !busy[r.id]);
  if (!runs.length) {
    const empty = `<div class="empty" style="margin-top:12px">セットアップ履歴はまだありません。</div>`;
    if (S.ins._ih !== empty) { S.ins._ih = empty; el.innerHTML = empty; }   // 空欄も毎回は書き直さない
    return;
  }
  // スクロール中は組み直しを後回し (ログ欄・表を掴んだままにできる)
  if (uiScrolling() && el.childElementCount) { deferRender("ins-hist", () => renderInsHistory(active)); return; }
  const html = `<h2 class="view-title" style="margin-top:18px">履歴 (${runs.length})</h2>` + runs.map(r => {
    const open = S.ins.open === r.id;
    const tail = (S.ins.logs[r.id] || "").split("\n").filter(Boolean).slice(-120).join("\n");
    const detail = r._k === "download"
      ? esc(r.repo_id) + (r.revision ? " @ " + esc(r.revision) : "") + (r.progress ? " · 計 " + insBytes(r.progress.done) : "")
      : r._k === "calibrate"
        ? esc((r.steps || []).map(calStepLabel).join(" → ")) + (r.mode ? ` · ${esc(r.mode)}` : "")
        : esc((r.steps || []).map(s => insStepName(s, r)).join(" → "));
    return `<div class="card">
      <div class="row">
        ${insStatusBadge(r)}
        <b class="grow ellip" style="font-size:14.5px;cursor:pointer" onclick="insToggle('${r.id}')">${insTitle(r)}</b>
        <span class="muted" style="font-size:12px">${INS_KIND_LABEL[r._k]}</span>
      </div>
      <div class="muted" style="margin-top:5px;font-size:12px">
        ${detail}
        ${r.error ? ` · <span style="color:var(--err)">${esc(String(r.error).slice(0, 120))}</span>` : ""}
        · ${r.finished_at ? fmtAgo(r.finished_at) : "-"}</div>
      <div class="row" style="margin-top:8px">
        <button onclick="insToggle('${r.id}')">${open ? "<svg class=ic aria-hidden=true><use href=#i-chev_up /></svg> 閉じる" : "<svg class=ic aria-hidden=true><use href=#i-receipt /></svg> ログ"}</button>
        ${r._k === "calibrate" ? `<button class="ghost" onclick="openCalibration('${esc(r.profile_id)}')"><svg class=ic aria-hidden=true><use href=#i-target /></svg> レポート</button>` : ""}
        <span class="grow"></span>
        <button class="ghost danger" onclick="insDelete('${r.id}')">削除</button>
      </div>
      ${open ? `<div class="logwrap" style="padding:0"><pre class="logbox" data-logkey="k:${esc(r.id)}" style="max-height:320px;margin-top:6px">${esc(tail || "(ログ無し)")}</pre></div>` : ""}
    </div>`;
  }).join("");
  // 中身が変わらなければ組み直さない (開いたログ欄の高さが飛ぶのもこれで消える)
  if (html === S.ins._ih && el.childElementCount) return;
  S.ins._ih = html;
  const _ls = logScrollSnap(el);                // 組み直したときだけ位置・開閉を復元
  el.innerHTML = html;
  logScrollBack(el, _ls);
}

