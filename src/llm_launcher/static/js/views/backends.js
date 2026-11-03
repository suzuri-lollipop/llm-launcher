/* ===================================================================
   view: backends — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.backends = `
  <section id="view-backends" class="view hidden">
    <h2 class="view-title">バックエンド設定 <span class="muted">(module/ のサブモジュール)</span></h2>
    <div class="card" id="oom-card" style="margin-bottom:14px"></div>
    <div class="grid c2" id="backends-grid"></div>
  </section>
`;
/* ============================================================= backends view */
/* ---------------- OOM 対策パネル ---------------- */
function renderOomCard(){
  const el = $("#oom-card"); if (!el) return;
  const o = S.sys && S.sys.oom;
  if (!o){ el.innerHTML = `<div class="muted"><span class="spin"></span> OOM 対策の状態を読み込み中…</div>`; return; }
  if (document.activeElement && document.activeElement.matches("input,select,textarea")
      && el.contains(document.activeElement)) return;   // 入力中に書き換えない
  const badge = o.level === "ok" ? `<span class="badge ok"><span class="dot"></span>保護適用中</span>`
              : o.level === "warn" ? `<span class="badge warn"><span class="dot"></span>要注意</span>`
              : `<span class="badge"><span class="dot"></span>無効</span>`;
  const cls = o.state === "crit" ? "err" : o.state === "warn" ? "warn" : "ok";
  const vcol = o.level === "off" ? "var(--sub)" : `var(--${o.level === "ok" ? "acc" : o.level})`;
  el.innerHTML = `
    <div class="row"><b style="font-size:15px"><svg class=ic aria-hidden=true><use href=#i-shield /></svg> OOM 対策</b>
      <span class="muted" style="font-size:12px">重い CLI と同居する環境でモデルが最初に kill されるのを防ぐ</span>
      <span class="grow"></span>${badge}</div>
    <div class="mono" style="margin-top:8px;white-space:pre-wrap;word-break:break-all;font-size:12.5px;color:${vcol}">${esc(o.verdict)}</div>
    <div class="meter" style="margin-top:10px"><div class="m-hd"><b>空きメモリ (MemAvailable)</b>
      <span class="r">${fmtGB(o.available)} 空き / 予約 ${fmtGB(o.reserve)} · 使用 ${esc(o.used_pct)}%</span></div>
      <div class="mbar"><i class="${cls}" style="width:${Math.min(100,o.used_pct)}%"></i></div></div>
    <div class="kvs" style="margin-top:10px">
      <span>oom_score_adj</span><span class="v mono">${esc(o.score_adj)}${o.self_applied ? "" : " (未適用)"} / 実測 ${esc(o.self_score ?? "-")}${o.self_via ? (o.self_via === "helper" ? " · sudo 経由" : " · 直接") : ""}</span>
      <span>権限 (CAP_SYS_RESOURCE)</span><span class="v">${o.can_lower ? "あり — 直接書き込み可" : "なし — sudo ヘルパーが必要"}</span>
      <span>sudo ヘルパー</span><span class="v mono">${o.helper ? "導入済み" : "未導入"}</span>
      <span>起動前ガード</span><span class="v">${esc(o.guard_mode)}${o.guard_mode === "block" ? " (空き不足なら起動を拒否)" : o.guard_mode === "warn" ? " (警告のみで起動)" : " (無効)"}</span>
      <span>systemd-oomd / cgroup</span><span class="v mono">${o.oomd ? "動作中" : "停止中"} / ${esc(o.self_unit || "-")} · oom_kill ${esc(o.oom_kills ?? "-")}</span>
    </div>
    <div class="row" style="margin-top:12px;gap:12px;flex-wrap:wrap;align-items:flex-end">
      <label class="row" style="gap:6px;cursor:pointer;font-size:13px"><input type="checkbox" id="oom-en" ${o.enabled?"checked":""} style="width:16px;height:16px;accent-color:var(--acc)"> 有効</label>
      <label class="f" style="margin:0">score_adj<input type="number" id="oom-score" min="-1000" max="0" step="50" value="${esc(o.score_adj)}" style="width:96px;margin-left:6px"></label>
      <label class="f" style="margin:0">予約 GB<input type="number" id="oom-gb" min="0" max="512" step="0.5" value="${esc(o.min_available_gb)}" style="width:88px;margin-left:6px"></label>
      <label class="f" style="margin:0">ガード<select id="oom-mode" style="width:auto;margin-left:6px">${
        ["block","warn","off"].map(m => `<option value="${m}"${o.guard_mode === m ? " selected" : ""}>${m}</option>`).join("")}</select></label>
      <label class="row" style="gap:6px;cursor:pointer;font-size:13px"><input type="checkbox" id="oom-reapply" ${o.reapply?"checked":""} style="width:16px;height:16px;accent-color:var(--acc)"> 定期再適用</label>
      <label class="row" style="gap:6px;cursor:pointer;font-size:13px"><input type="checkbox" id="oom-swift" ${o.swift_stop?"checked":""} style="width:16px;height:16px;accent-color:var(--acc)"> 逼迫時 safety stop</label>
      <span class="grow"></span>
      <button class="primary need-oom" onclick="saveOom()">OOM 設定を保存 / 再適用</button>
    </div>
    <div class="muted" style="font-size:11.5px;margin-top:8px">負の score_adj はカーネル特権 (CAP_SYS_RESOURCE) が必要で、一般ユーザーでは直接書けません。
      下のコマンドで「そのパスだけ」を許可する sudo ヘルパーを導入すると、起動ツリー全体 (モデル本体 + 後から湧く worker + launcher 自身) に適用されます。</div>
    <pre class="cmdprev" style="max-height:none">${esc(o.install_hint || "")}</pre>`;
}
async function saveOom(){
  const body = {
    enabled: $("#oom-en").checked, score_adj: Number($("#oom-score").value || 0),
    min_available_gb: Number($("#oom-gb").value || 0), guard_mode: $("#oom-mode").value,
    reapply: $("#oom-reapply").checked, swift_stop: $("#oom-swift").checked,
  };
  try {
    const o = await api("/api/oom", {method:"POST", body});
    if (!S.sys) S.sys = {};
    S.sys.oom = o;
    renderOomCard();
    toast(o.level === "ok" ? "OOM 設定を適用しました" : "保存しました (権限が足りず未適用): " + o.verdict.split("\n")[0]);
  } catch (e) { toast("保存失敗: " + e.message); }
}
function renderBackends() {
  renderOomCard();
  const g = $("#backends-grid");
  g.innerHTML = S.state.backends.map(b => {
    const det = b.detection;
    const inst = det.installed
      ? `<span class="badge ok"><span class="dot"></span>インストール済み</span>`
      : `<span class="badge err"><span class="dot"></span>未検出</span>`;
    const mod = det.module_exists
      ? `<span class="badge ok"><span class="dot"></span>submodule ✓</span>`
      : `<span class="badge warn"><span class="dot"></span>module/ 未取得</span>`;
    return `<div class="card">
      <div class="row">
        <span class="chip ${esc(b.id)}">${esc(b.name)}</span>
        <b class="grow ellip" style="font-size:15px">${esc(b.name)}</b>
        <button class="ghost" style="min-height:32px;padding:4px 10px;font-size:12px" onclick="redetect('${b.id}')">再検出</button>
      </div>
      <div class="row" style="margin-top:8px">${mod} ${inst}
        ${b.installable ? (det.installed
          ? `<button class="ghost need-install" style="min-height:28px;padding:3px 10px;font-size:12px" title="セットアップ画面で依存を入れ直します" onclick="setupTab('${b.id}')"><svg class=ic aria-hidden=true><use href=#i-box /></svg> 再インストール</button>`
          : `<button class="primary need-install" style="min-height:28px;padding:3px 10px;font-size:12px" title="clone / .venv / ${esc(insToolLabel((S.ins || {}).tool))} をこの画面から実行します" onclick="setupTab('${b.id}')"><svg class=ic aria-hidden=true><use href=#i-box /></svg> インストール</button>`) : ""}
        <a class="muted" href="${esc(b.repo)}" target="_blank" rel="noopener" style="font-size:12px">${esc(b.repo.replace("https://github.com/",""))}</a>
      </div>
      <div class="kvs" style="margin-top:10px">
        <span>サブモジュール</span><span class="v mono">${esc(b.module_path)}</span>
        <span>コマンド</span><span class="v mono ellip" title="${esc(b.command)}">${esc(b.command)}</span>
        <span>検出詳細</span><span class="v muted" style="font-size:11.5px">${esc(det.detail || "-")}</span>
      </div>
      <label class="f">コマンド上書き <span class="muted">(空 = 既定)</span></label>
      <input type="text" class="mono" id="bk-cmd-${b.id}" value="${esc(b.command !== b.default_command ? b.command : "")}" placeholder="${esc(b.default_command)}">
      <label class="f">${b.kind === "python" ? "Python パス ({python} に展開)" : "バイナリパス ({llama_server} に展開)"}</label>
      <input type="text" class="mono" id="bk-py-${b.id}" value="${esc(b.kind==="python" ? (b.python_override || det.python || "") : (b.binary_override || ""))}" placeholder="${b.kind==="python" ? "既定: 自動検出 (module/.venv → GUI .venv → launcher)" : "既定: 自動検出 (build/bin or PATH)"}">
      ${b.kind === "python" ? `<div class="muted" style="font-size:11px;margin-top:3px">既定の解決順: ${esc(b.module_path)}/.venv → GUI(llm-launcher)の .venv → launcher 自身。検出は各候補で実際の import を試し、成功したものを使用します</div>` : ""}
      <div class="row" style="margin-top:12px">
        <button class="primary need-backend" onclick="saveBackend('${b.id}')">保存</button>
        <button class="ghost need-backend" onclick="resetBackend('${b.id}')">リセット</button>
        <span class="grow"></span>
        <button class="ghost need-edit" onclick="newProfileFor('${b.id}')">このバックエンドで新規プロファイル</button>
      </div>
    </div>`;
  }).join("");
}
async function saveBackend(bid) {
  const b = backendById(bid);
  const body = {
    command: inputVal(`#bk-cmd-${bid}`) || "",
    python: b.kind === "python" ? (inputVal(`#bk-py-${bid}`) || "") : null,
    command_binary: b.kind !== "python" ? (inputVal(`#bk-py-${bid}`) || "") : null,
  };
  try { await api(`/api/backends/${bid}`, {method:"PUT", body}); toast("保存しました"); refresh(false); }
  catch (e) { toast("失敗: " + e.message); }
}
async function resetBackend(bid) {
  if (!confirm("このバックエンドの上書き設定を初期化しますか?")) return;
  try { await api(`/api/backends/${bid}`, {method:"PUT", body:{reset:true}}); toast("初期化しました"); refresh(false); }
  catch (e) { toast("失敗: " + e.message); }
}
async function redetect(bid) {
  try { await api(`/api/backends/${bid}/detect`, {method:"POST"}); refresh(false); }
  catch (e) { toast("検出失敗: " + e.message); }
}
function newProfileFor(bid) {
  const b = backendById(bid); if (!b) return;
  S.editing = {name:"", backend:bid, values: defaultsFor(b), on: defaultsOn(b, {}, {}),
               extra_args:"", env:{}, cwd:"", command:"", custom_only:false, note:""};
  $("#modal").style.display = "flex"; $("#modal-veil").style.display = "block";
  renderEditor();
}

