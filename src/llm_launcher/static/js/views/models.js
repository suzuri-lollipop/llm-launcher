/* ===================================================================
   view: models — 全バックエンド横断のモデル管理 (一覧 / ダウンロード / 削除)
   HF キャッシュ / model_data / Strata-data / Maya-data を 1 つの棚卸しに束ねる。
   起動 config JSON (strata-*.json / maya-*.json) はプロファイル側の設定なので
   棚卸しに出さない (管理はプロファイル管理の責務)。
   /api/models のフロントエンド。DL は /api/installs/download (セットアップと同じ
   ジョブ基盤)、削除は DELETE /api/models (サーバー側ガード + 確認ダイアログ)。
   =================================================================== */
"use strict";
VIEWS.models = `
  <section id="view-models" class="view hidden">
    <div class="row" style="margin-bottom:10px">
      <h2 class="view-title" style="margin:0">モデル管理 <span class="muted">全バックエンドのモデルを一覧 / ダウンロード / 削除</span></h2>
      <div class="grow"></div>
      <button class="ghost" onclick="mmRefresh(true)"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 再スキャン</button>
      <button class="primary" onclick="mmToggleDl()"><svg class=ic aria-hidden=true><use href=#i-download /></svg> モデルをダウンロード</button>
    </div>
    <div class="muted" id="mm-summary" style="font-size:12.5px;margin-bottom:10px"></div>

    <details class="adv hidden" id="mm-dl" data-detailkey="mm-dl">
      <summary><svg class=ic aria-hidden=true><use href=#i-download /></svg>&nbsp;モデルをダウンロード (Hugging Face)</summary>
      <div class="inner">
        <div class="fitem"><label class="f" for="mm-dl-backend">バックエンド (対応モデル一覧と保存先)</label>
          <select id="mm-dl-backend" onchange="mmDlSync()"></select>
          <div class="muted" id="mm-dl-dest" style="font-size:11.5px;margin-top:3px"></div>
        </div>
        <div class="fitem" id="mm-dl-cat-wrap"><label class="f" for="mm-dl-cat">対応モデルから選択</label>
          <select id="mm-dl-cat" onchange="mmDlPick()"></select>
        </div>
        <div class="fitem"><label class="f" for="mm-dl-repo">repo id (直接入力も可)</label>
          <input type="text" id="mm-dl-repo" class="mono" placeholder="org/model-name" oninput="mmDlSync()">
        </div>
        <div class="row" style="gap:10px;flex-wrap:wrap">
          <div class="fitem grow" style="min-width:140px"><label class="f" for="mm-dl-rev">revision (省略可)</label>
            <input type="text" id="mm-dl-rev" class="mono" placeholder="main"></div>
          <div class="fitem grow" style="min-width:140px"><label class="f" for="mm-dl-kbps">速度上限 KB/s (0 = 無制限)</label>
            <input type="number" id="mm-dl-kbps" min="0" value="0"></div>
        </div>
        <div class="fitem"><label class="f" for="mm-dl-patterns">ファイル絞り込み (カンマ区切り・省略可)</label>
          <input type="text" id="mm-dl-patterns" class="mono" placeholder="例: *.gguf / pack 1 個だけ">
        </div>
        <div class="row" style="margin-top:10px">
          <span class="muted" id="mm-dl-note" style="font-size:12px"></span>
          <span class="grow"></span>
          <button class="primary" id="mm-dl-start" onclick="mmDlStart()"><svg class=ic aria-hidden=true><use href=#i-download /></svg> ダウンロード開始</button>
        </div>
        <div class="muted hidden" id="mm-dl-wizard" style="margin-top:10px;font-size:12.5px"></div>
      </div>
    </details>

    <div id="mm-active"></div>

    <div class="row" style="margin:12px 0 10px;gap:8px">
      <input type="text" id="mm-q" class="grow" style="min-width:140px" placeholder="モデル名 / repo id / パスで絞り込み" oninput="S.mm.q=this.value;renderModels()">
      <select id="mm-sort" style="width:auto" onchange="S.mm.sort=this.value;renderModels()">
        <option value="mtime">新しい順</option>
        <option value="size">サイズ順</option>
        <option value="name">名前順</option>
      </select>
    </div>
    <div class="filters" id="mm-filters"></div>
    <div class="filters" id="mm-sources"></div>
    <div id="mm-list"></div>

    <h2 class="view-title" style="margin-top:18px">バックエンド別 対応モデル <span class="muted">各バックエンドが対応するモデルと取得状況</span></h2>
    <div id="mm-catalog"></div>
  </section>
`;

/* ---------------- 状態 ---------------- */
// 棚卸しは重みだけ (起動 config JSON = プロファイル側はプロファイル管理で扱う)
const MM_SRC = {
  cache: ["info", "HF キャッシュ"], data: ["purple", "データフォルダ"],
  "strata-data": ["", "Strata-data"], "maya-data": ["", "Maya-data"],
};
function mmInit() {
  if (S.mm) return;
  S.mm = {data: null, loading: false, ts: 0, q: "", backend: "all", source: "all",
          sort: "mtime", dlOpen: false, dlBackend: "", _sig: "", _catSig: "", _actSig: ""};
  // details の手動開閉を状態へ反映 (renderModels が開閉を強制しないように)
  const d = $("#mm-dl");
  if (d && !d._mmWired) {
    d._mmWired = true;
    d.addEventListener("toggle", () => { S.mm.dlOpen = d.open; });
  }
}
async function mmRefresh(force = false) {
  mmInit();
  if (S.mm.loading) return;
  if (!force && S.mm.data && Date.now() - S.mm.ts < 60000) return;
  S.mm.loading = true;
  try {
    S.mm.data = await api("/api/models" + (force ? "?refresh=1" : ""));
    S.mm.ts = Date.now();
  } catch (e) {
    if (force) toast("モデル一覧の取得に失敗: " + e.message);
  } finally {
    S.mm.loading = false;
    if (S.tab === "models") renderModels();
  }
}

/* ---------------- 描画 ---------------- */
const mmModels = () => (S.mm && S.mm.data && S.mm.data.models) || [];
const mmBackends = () => (S.mm && S.mm.data && S.mm.data.backends) || [];
const mmName = bid => (mmBackends().find(b => b.id === bid) || {}).name
  || (backendById(bid) || {}).name || bid;
const mmGB = b => b == null ? "-" : (b / 2**30 >= 10 ? Math.round(b / 2**30) + " GB" : (b / 2**30).toFixed(1) + " GB");

function mmFiltered() {
  const q = (S.mm.q || "").toLowerCase().trim();
  let out = mmModels().filter(m => {
    if (S.mm.backend !== "all" && !(m.backends || []).includes(S.mm.backend)) return false;
    if (S.mm.source !== "all" && m.source !== S.mm.source) return false;
    if (!q) return true;
    return [m.name, m.repo_id, m.path, ...(m.backends || []).map(mmName)]
      .join(" ").toLowerCase().includes(q);
  });
  const key = S.mm.sort || "mtime";
  out.sort((a, b) => key === "size" ? (b.size_bytes || 0) - (a.size_bytes || 0)
    : key === "name" ? String(a.name).localeCompare(String(b.name), "ja")
    : (b.mtime || 0) - (a.mtime || 0));
  return out;
}

function mmSummaryHtml() {
  const d = S.mm.data;
  if (!d) return S.mm.loading ? "モデルをスキャン中…" : "";
  const total = (d.models || []).reduce((s, m) => s + (m.size_bytes || 0), 0);
  const disks = (d.disks || []).map(x => `空き ${mmGB(x.free)}`).join(" / ");
  return `${(d.models || []).length} 件のモデル (計 ${mmGB(total)})`
    + (disks ? ` · ディスク ${disks}` : "")
    + ` · 置き場: ${(d.dirs || []).filter(x => x.exists).map(x => x.label).join(" / ")}`;
}

function mmCard(m) {
  const [scls, slabel] = MM_SRC[m.source] || ["", m.source];
  const fmt = (m.formats || []).map(f => `<span class="badge">${esc(f)}</span>`).join(" ");
  const chips = (m.backends || []).map(b => `<span class="chip ${esc(b)}">${esc(mmName(b))}</span>`).join(" ");
  const used = (m.used_by || []).map(u =>
    `<span class="badge warn" title="このモデルを参照しているプロファイル">${esc(u.name || u.id)}</span>`).join(" ");
  const note = m.note ? `<div class="muted" style="font-size:11.5px;margin-top:5px">${esc(m.note)}</div>` : "";
  const affected = (m.affected_configs || []).length
    ? `<div class="muted" style="font-size:11.5px;margin-top:3px">参照している設定 JSON: ${esc(m.affected_configs.join(", "))}</div>` : "";
  return `<div class="card">
    <div class="row">
      <span class="badge ${scls}">${esc(slabel)}</span>
      <b class="grow ellip" style="font-size:15px" title="${esc(m.name)}">${esc(m.name)}</b>
      ${m.running ? `<span class="badge ok"><span class="dot"></span>稼働中</span>` : ""}
      <span class="mono" style="font-size:13px;flex:none">${mmGB(m.size_bytes)}</span>
    </div>
    <div class="muted mono ellip" style="font-size:11.5px;margin-top:4px" title="${esc(m.path)}">${esc(shortPath(m.path))}</div>
    <div class="row" style="gap:6px;margin-top:7px;flex-wrap:wrap">${fmt} ${chips} ${used}</div>
    ${affected}${note}
    <div class="row" style="margin-top:9px">
      <button class="ghost" style="min-height:32px;font-size:12px" onclick="mmCopyPath('${esc(m.id)}')">${IC("clipboard")} パスをコピー</button>
      <span class="grow"></span>
      <button class="ghost danger" style="min-height:32px;font-size:12px" onclick="mmDelete('${esc(m.id)}')">${IC("trash")} 削除</button>
    </div>
  </div>`;
}

function mmCatalogHtml() {
  return mmBackends().map(b => {
    const n = b.supported.length, dl = b.supported.filter(s => s.downloaded).length;
    const rows = b.supported.map(s => `<div class="row" style="gap:8px;padding:7px 0;border-top:1px solid var(--line);flex-wrap:wrap">
        <span class="grow ellip" style="min-width:160px;font-size:13px">${esc(s.name || s.repo_id)}</span>
        <span class="muted mono ellip" style="font-size:11.5px;max-width:340px" title="${esc(s.repo_id)}">${esc(s.repo_id)}</span>
        ${s.downloaded
          ? `<span class="badge ok">取得済み</span>`
          : (b.wizard ? "" : `<button class="ghost" style="min-height:30px;font-size:12px" onclick="mmDlFor('${esc(b.id)}','${esc(s.repo_id)}')">${IC("download")} DL</button>`)}
      </div>`).join("");
    const wizardBtn = b.wizard
      ? `<button class="ghost" style="min-height:32px;font-size:12px" onclick="setupTab('${esc(b.id)}')">${IC("box")} セットアップウィザードを開く</button>` : "";
    const body = b.wizard
      ? `<div class="muted" style="font-size:12.5px;padding:8px 0">${esc(b.note)}</div>${wizardBtn}`
      : (n ? rows : `<div class="muted" style="font-size:12.5px;padding:8px 0">${esc(b.note)}</div>`
        + `<button class="ghost" style="min-height:32px;font-size:12px" onclick="mmDlFor('${esc(b.id)}','')">${IC("download")} repo id を指定してダウンロード</button>`);
    return `<details class="adv" data-detailkey="mm-cat-${esc(b.id)}" style="margin-top:8px">
      <summary><span class="chip ${esc(b.id)}">${esc(b.name)}</span>&nbsp;
        ${b.wizard ? "ウィザードでモデル設定" : `対応 ${n} 件 · 取得済み ${dl} 件`}
        ${b.cloned ? "" : `&nbsp;<span class="badge warn">未 clone</span>`}</summary>
      <div class="inner">${body}</div>
    </details>`;
  }).join("");
}

function renderModels() {
  mmInit();
  if (!S.mm.data && !S.mm.loading) mmRefresh();
  else if (S.mm.data && Date.now() - S.mm.ts > 60000) mmRefresh();   // 開いている間は 1 分ごとに静かに再取得
  $("#mm-summary").textContent = mmSummaryHtml();
  // ダウンロードフォームのバックエンド選択肢 (棚卸し or installable から)
  const dlSel = $("#mm-dl-backend");
  const bids = mmBackends().length ? mmBackends().map(b => b.id)
    : (S.ins && S.ins.installable || []).map(e => e.id);
  const bSig = bids.join("|");
  if (dlSel.dataset.sig !== bSig) {
    dlSel.dataset.sig = bSig;
    dlSel.innerHTML = bids.map(id => `<option value="${esc(id)}">${esc(mmName(id))}</option>`).join("");
    if (S.mm.dlBackend && bids.includes(S.mm.dlBackend)) dlSel.value = S.mm.dlBackend;
    else S.mm.dlBackend = dlSel.value;
    mmDlSync();
  }
  $("#mm-dl").classList.toggle("hidden", !S.mm.dlOpen);
  if (S.mm.dlOpen) mmDlNote();   // ジョブ実行中は開始ボタンを止め続ける
  // 実行中ダウンロード (セットアップと同じジョブ基盤のカードをそのまま使う)
  const act = (S.ins && S.ins.jobs || []).find(r => r.kind === "download"
    && (r.status === "running" || r.status === "queued"));
  const actHtml = act ? insActiveCard(act) : "";
  if (actHtml !== S.mm._actSig) {
    S.mm._actSig = actHtml;
    const el = $("#mm-active");
    const _ls = logScrollSnap(el);
    el.innerHTML = actHtml;
    logScrollBack(el, _ls);
  }
  // フィルタ (バックエンド / 置き場)
  const ms = mmModels();
  const cnt = bid => ms.filter(m => (m.backends || []).includes(bid)).length;
  $("#mm-filters").innerHTML = [{id: "all", name: "すべて", n: ms.length},
    ...mmBackends().map(b => ({id: b.id, name: b.name, n: cnt(b.id)}))]
    .map(f => `<button class="fchip ${S.mm.backend === f.id ? "on" : ""}" onclick="S.mm.backend='${f.id}';renderModels()">${esc(f.name)} <span class="muted" style="color:inherit">${f.n}</span></button>`).join("");
  const srcs = [...new Set(ms.map(m => m.source))];
  $("#mm-sources").innerHTML = [{id: "all", name: "すべての置き場"},
    ...srcs.map(s => ({id: s, name: (MM_SRC[s] || ["", s])[1]}))]
    .map(f => `<button class="fchip ${S.mm.source === f.id ? "on" : ""}" onclick="S.mm.source='${esc(f.id)}';renderModels()">${esc(f.name)}</button>`).join("");
  // 一覧 (中身が変わらなければ組み直さない)
  const list = mmFiltered();
  const html = !S.mm.data ? `<div class="empty">${S.mm.loading ? "スキャン中…" : "モデル一覧を取得できませんでした"}</div>`
    : (!list.length ? `<div class="empty">${ms.length ? "条件に合うモデルがありません。" :
        "モデルがまだありません。<br><br>「モデルをダウンロード」から取得するか、セットアップウィザード (📦) でバックエンドごとのモデル設定を実行してください。"}</div>`
      : list.map(mmCard).join(""));
  const sig = html.length + ":" + list.map(m => m.id + m.size_bytes + m.running + (m.used_by || []).length).join("|");
  const listEl = $("#mm-list");
  if (sig !== S.mm._sig) {
    S.mm._sig = sig;
    listEl.innerHTML = html;
  }
  // 対応モデル表
  const cat = mmCatalogHtml();
  if (cat !== S.mm._catSig) {
    S.mm._catSig = cat;
    const el = $("#mm-catalog");
    const _ls = logScrollSnap(el);
    el.innerHTML = cat;
    logScrollBack(el, _ls);
  }
}

/* ---------------- ダウンロードフォーム ---------------- */
function mmToggleDl() {
  mmInit();
  S.mm.dlOpen = !S.mm.dlOpen;
  $("#mm-dl").open = S.mm.dlOpen;
  renderModels();
}
const mmDlSpec = bid => {
  const b = mmBackends().find(x => x.id === bid);
  const ins = (S.ins && S.ins.installable || []).find(e => e.id === bid);
  return {backend: b || null, ins: ins || null};
};
function mmDlSync() {
  const bid = $("#mm-dl-backend").value;
  S.mm.dlBackend = bid;
  const {backend, ins} = mmDlSpec(bid);
  const wizard = !!(backend && backend.wizard);
  const supported = (backend && backend.supported) || (ins && ins.supported_models) || [];
  const cat = $("#mm-dl-cat");
  const sig = bid + "|" + supported.map(s => s.repo_id).join(",");
  if (cat.dataset.sig !== sig) {
    cat.dataset.sig = sig;
    cat.innerHTML = `<option value="">対応モデルから選択 (${supported.length})</option>`
      + supported.map(s => `<option value="${esc(s.repo_id)}"${s.downloaded ? " disabled" : ""}>${esc(s.name || s.repo_id)}${s.downloaded ? " (取得済み — 再利用は棚卸しから)" : ""}</option>`).join("");
  }
  $("#mm-dl-cat-wrap").style.display = supported.length && !wizard ? "" : "none";
  ["#mm-dl-repo", "#mm-dl-rev", "#mm-dl-patterns", "#mm-dl-kbps"].forEach(s => { const el = $(s); if (el) el.disabled = wizard; });
  $("#mm-dl-start").style.display = wizard ? "none" : "";
  const wiz = $("#mm-dl-wizard");
  wiz.classList.toggle("hidden", !wizard);
  if (wizard) {
    wiz.innerHTML = `${esc((backend && backend.note) || "このバックエンドのモデル設定はセットアップウィザードが担います")} `
      + `<button class="ghost" style="min-height:30px;font-size:12px" onclick="setupTab('${esc(bid)}')">${IC("box")} ウィザードを開く</button>`;
  }
  const modelData = ins && ins.model_data;
  $("#mm-dl-dest").textContent = wizard ? ""
    : (modelData ? `保存先: ${modelData}/<org>--<name> (完了後このバックエンドの起動 JSON にも記入)`
      : `保存先: HF キャッシュ (${(S.ins && S.ins.cacheDir) || "既定"})`);
  mmDlNote();
}
function mmDlPick() {
  const repo = $("#mm-dl-cat").value;
  if (!repo) return;
  $("#mm-dl-repo").value = repo;
  const {backend, ins} = mmDlSpec($("#mm-dl-backend").value);
  const supported = (backend && backend.supported) || (ins && ins.supported_models) || [];
  const ent = supported.find(s => s.repo_id === repo);
  $("#mm-dl-patterns").value = ent && ent.include && ent.include.length ? ent.include.join(", ") : "";
  mmDlNote();
}
function mmDlNote() {
  const lock = (S.ins && S.ins.active) || (typeof calBusy === "function" && calBusy());
  $("#mm-dl-start").disabled = !!lock;
  $("#mm-dl-note").textContent = lock ? "別のセットアップ / キャリブレーションジョブが実行中です (同時に 1 件)" : "";
}
function mmDlFor(bid, repo) {
  mmInit();
  S.mm.dlOpen = true;
  $("#mm-dl").open = true;
  S.mm.dlBackend = bid;
  renderModels();
  const sel = $("#mm-dl-backend");
  if ([...sel.options].some(o => o.value === bid)) sel.value = bid;
  mmDlSync();
  if (repo) {
    $("#mm-dl-cat").value = repo;
    mmDlPick();
  }
  $("#mm-dl").scrollIntoView({behavior: "smooth", block: "start"});
}
async function mmDlStart() {
  mmInit();
  const repo = ($("#mm-dl-repo").value || "").trim();
  if (!repo) return toast("repo id を入力してください");
  if (S.ins && S.ins.active) return toast("別のセットアップジョブが実行中です");
  if (typeof calBusy === "function" && calBusy()) return toast("キャリブレーション実行中は開始できません (同時に 1 件)");
  const bid = $("#mm-dl-backend").value;
  const {ins} = mmDlSpec(bid);
  const body = {repo_id: repo, revision: ($("#mm-dl-rev").value || "").trim(),
                backend: ins && ins.model_data ? bid : "",
                allow_patterns: ($("#mm-dl-patterns").value || "").split(",").map(s => s.trim()).filter(Boolean),
                max_kbps: parseInt($("#mm-dl-kbps").value || "0", 10) || 0};
  try {
    await api("/api/installs/download", {method: "POST", body});
    toast("ダウンロードを開始しました");
    insPoll(true);
  } catch (e) { toast("開始失敗: " + e.message); }
  renderModels();
}

/* ---------------- 削除 / パスコピー ---------------- */
function mmCopyPath(id) {
  const m = mmModels().find(x => x.id === id);
  if (!m) return;
  const done = () => toast("パスをコピーしました");
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(m.path).then(done).catch(() => mmCopyFallback(m.path, done));
  } else mmCopyFallback(m.path, done);
}
function mmCopyFallback(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text; ta.style.position = "fixed"; ta.style.top = "-9999px";
  document.body.appendChild(ta); ta.focus(); ta.select();
  let ok = false; try { ok = document.execCommand("copy"); } catch {}
  ta.remove();
  ok ? done() : toast("コピーできませんでした。パスを手動で選択してください");
}
async function mmDelete(id) {
  const m = mmModels().find(x => x.id === id);
  if (!m) return toast("エントリが見つかりません。再スキャンしてください");
  if (m.running) return toast("稼働中のサーバーが参照しています。停止してから削除してください");
  const lines = [`「${m.name}」を削除しますか?`, `場所: ${m.path}`, `サイズ: ${mmGB(m.size_bytes)}`];
  if ((m.affected_configs || []).length) lines.push(`この重みを参照している設定 JSON: ${m.affected_configs.join(", ")} (起動できなくなります)`);
  const force = (m.used_by || []).length > 0;
  if (force) lines.push(`⚠ 次のプロファイルが参照しています: ${m.used_by.map(u => u.name || u.id).join(", ")} — 削除すると起動できなくなります`);
  if (!confirm(lines.join("\n"))) return;
  try {
    const r = await api("/api/models", {method: "DELETE", body: {id, force}});
    toast(`削除しました (${mmGB(r.freed_bytes)} 解放)`);
    S.hf = null;                    // 編集画面の HF ピッカーも再スキャンさせる
    mmRefresh(true);
  } catch (e) {
    if (e.status === 409) toast("削除できません: " + e.message, 8000);
    else toast("削除失敗: " + e.message);
    mmRefresh(true);
  }
}
