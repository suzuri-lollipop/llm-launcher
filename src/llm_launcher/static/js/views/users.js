/* ===================================================================
   view: users — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
VIEWS.users = `
  <section id="view-users" class="view hidden">
    <h2 class="view-title">ユーザー管理 <span class="muted">ログインアカウントと API キー (管理者のみ)</span></h2>
    <div id="um-note" class="card hidden" style="margin-bottom:12px;border-color:#f0c674;background:#fef7e0"></div>
    <div class="grid c2" id="role-cards" style="margin-bottom:14px"></div>
    <div class="row" style="margin-bottom:10px">
      <span class="muted">ロール: <b>管理者</b> = 全機能 / <b>メンテナー</b> = 起動・停止 + 上の「開示する機能」で選んだ範囲 / <b>ユーザー</b> = 簡易画面 (<a href="/simple">/simple</a>) の開始・停止のみ</span>
      <span class="grow"></span>
      <button class="ghost" onclick="loadUsers()"><svg class=ic aria-hidden=true><use href=#i-refresh /></svg> 更新</button>
      <button class="primary" id="um-new" onclick="umOpen()">＋ 新規ユーザー</button>
    </div>
    <div class="grid c2" id="users-grid">
      <div class="empty"><span class="spin"></span> 読み込み中…</div>
    </div>
    <div class="muted" style="margin-top:12px;font-size:11.5px">
      API キーはスクリプトから <code style="font-size:11px">curl -H "Authorization: Bearer &lt;キー&gt;" http://&lt;host&gt;:8036/api/state</code> のように使えます。
      平文は発行 (再生成) したときだけ表示され、サーバーにはハッシュ保存されます。パスワードを変更するとそのユーザーのセッションは全て失効します。
    </div>
  </section>
`;
/* ============================================================= ユーザー管理 (管理者のみ) */
const UM = { users: [], editing: null, mode: "form", reveal: {} };

async function loadUsers() {
  const grid = $("#users-grid");
  try {
    const d = await api("/api/users");
    UM.users = d.users || [];
    renderUsers();
  } catch (e) {
    if (e.status === 400) {              // 認証を無効化して起動しているモード
      $("#um-note").innerHTML = `<b>認証が無効です</b> <span class="muted">${esc(e.message)}</span>`;
      $("#um-note").classList.remove("hidden");
      $("#um-new").classList.add("hidden");
    }
    grid.innerHTML = `<div class="empty">読み込みに失敗: ${esc(e.message)}</div>`;
  }
}

/* ---------------- ロール権限カード (管理者のみ表示) ---------------- */
const ROLECFG = { caps: [], maintainer: {}, simple: { contact: "" }, simple_profiles: [] };
async function loadRoles() {
  if (!AUTH.isAdmin) return;
  try {
    Object.assign(ROLECFG, await api("/api/roles"));
    renderRoleCards();
  } catch { /* 対応していないサーバーではカードを出さない */ }
}
function renderRoleCards() {
  const el = $("#role-cards");
  if (!el || !ROLECFG.caps.length) return;
  const rows = ROLECFG.caps.map(c => `
    <div class="perm-row">
      <label class="switch" style="flex:none"><input type="checkbox" ${ROLECFG.maintainer[c.key] ? "checked" : ""}
        onchange="setPerm('${c.key}', this.checked)"><span class="track"></span></label>
      <div class="grow"><div class="lab">${esc(c.label)}</div><div class="dsc">${esc(c.desc || "")}</div></div>
    </div>`).join("");
  const sp = ROLECFG.simple_profiles.map(p => esc(p.name)).join(" / ") || "なし";
  el.innerHTML = `
    <div class="card">
      <div class="card-h"><svg class=ic aria-hidden=true><use href=#i-wrench /></svg> メンテナーに開示する機能</div>
      <div class="muted" style="margin-bottom:6px">起動・停止・稼働中の一覧は常に使えます。
        ここでは閲覧 / 編集機能の ON・OFF を決めます (トグルごとにすぐ保存、相手は再ログイン不要)。</div>
      ${rows}
    </div>
    <div class="card">
      <div class="card-h"><svg class=ic aria-hidden=true><use href=#i-user /></svg> 簡易画面 (/simple) の設定</div>
      <div class="muted" style="margin-bottom:6px">ロール「ユーザー」の人はこの画面しか使えません。
        プロファイル編集の <b>簡易画面に表示</b> を ON にしたものだけが表示されます。<br>
        現在 ON: <b>${sp}</b></div>
      <label class="f" for="rs-contact">とまらないときに出す連絡先メッセージ</label>
      <input type="text" id="rs-contact" maxlength="120" value="${esc(ROLECFG.simple.contact)}"
             placeholder="例: とまらないときは 内線 1234 (総務 山田) へ">
      <div class="row" style="margin-top:10px">
        <button class="primary" onclick="saveSimpleContact()">保存</button>
        <a class="btn" href="/simple" target="_blank" rel="noopener">簡易画面を開く</a>
      </div>
    </div>`;
}
async function setPerm(key, on) {
  const label = (ROLECFG.caps.find(c => c.key === key) || {}).label || key;
  try {
    const r = await api("/api/roles/maintainer",
      { method: "PUT", body: { perms: { ...ROLECFG.maintainer, [key]: on } } });
    ROLECFG.maintainer = r.maintainer;
    await fetchAuthStatus(); applyAuthUi();
    toast(`「${label}」を ${on ? "ON" : "OFF"} にしました`);
  } catch (e) { toast("保存失敗: " + e.message); loadRoles(); }
}
async function saveSimpleContact() {
  try {
    const r = await api("/api/roles/simple",
      { method: "PUT", body: { contact: $("#rs-contact").value } });
    ROLECFG.simple = r.simple;
    toast("簡易画面の設定を保存しました");
  } catch (e) { toast("保存失敗: " + e.message); }
}
function renderUsers() {
  const grid = $("#users-grid");
  if (!UM.users.length) { grid.innerHTML = `<div class="empty">ユーザーがいません</div>`; return; }
  const me = AUTH.user && AUTH.user.id;
  grid.innerHTML = UM.users.map(u => {
    const key = UM.reveal[u.id];
    const uid = esc(u.id);
    return `<div class="card">
      <div class="row" style="gap:8px">
        ${roleBadge(u.role)}
        <b class="grow ellip mono">@${esc(u.username)}</b>
        ${u.id === me ? `<span class="badge">あなた</span>` : ``}
        <span class="badge ${u.enabled ? "ok" : "err"}><span class="dot"></span>${u.enabled ? "有効" : "無効"}</span>
      </div>
      <div class="kvs" style="margin-top:9px">
        <span>表示名</span><span class="v">${esc(u.display_name || "-")}</span>
        <span>最終ログイン</span><span class="v">${esc(fmtAgo(u.last_login_at))}</span>
        <span>作成 / 更新</span><span class="v">${esc(fmtAgo(u.created_at))} / ${esc(fmtAgo(u.updated_at))}</span>
      </div>
      <div class="keyline">
        <span class="muted">APIキー</span>
        ${key ? `<code id="um-key-${uid}">${esc(key)}</code>
          <button class="ghost" onclick="umCopyKey('${uid}')">コピー</button>`
        : `<code>${u.has_api_key ? "発行済み (平文は再表示されません)" : "未発行"}</code>`}
        <button class="ghost" onclick="umRotate('${uid}')">${u.has_api_key ? "再生成" : "発行"}</button>
      </div>
      <div class="um-actions">
        <button class="ghost" onclick="umOpen('${uid}')"><svg class=ic aria-hidden=true><use href=#i-edit /></svg> 編集</button>
        <button class="ghost" onclick="umOpen('${uid}','pw')"><svg class=ic aria-hidden=true><use href=#i-key /></svg> パスワード</button>
        <button class="ghost" onclick="umToggle('${uid}')">${u.enabled ? "無効化" : "有効化"}</button>
        <button class="danger" onclick="umDelete('${uid}')">削除</button>
      </div>
    </div>`;
  }).join("");
}

function umOpen(uid = "", mode = "form") {   // 起点: 履歴経由で開く (戻るで閉じられる)
  go(routeUrl(S.tab, "user", uid || "new", mode));
}
function umOpenDom(uid, mode) {              // DOM 操作本体。ルーター (syncDialog) から呼ばれる
  UM.editing = uid || null; UM.mode = mode;
  const u = uid ? UM.users.find(x => x.id === uid) : null;
  if (uid && !u) return;
  const sheet = $("#umodal-sheet");
  if (mode === "pw") {
    sheet.innerHTML = `<h3>パスワード変更 — @${esc(u.username)}</h3>
      <div class="muted">8 文字以上で入力してください。変更するとこのユーザーのログインセッションは全て失効します。</div>
      <label class="f" for="um-pw">新しいパスワード</label>
      <input type="password" id="um-pw" autocomplete="new-password" maxlength="256">
      <label class="f" for="um-pw2">確認</label>
      <input type="password" id="um-pw2" autocomplete="new-password" maxlength="256">
      <div class="sheet-actions">
        <button class="primary grow" onclick="umSavePw()">変更</button>
        <button class="ghost grow" onclick="umClose()">キャンセル</button>
      </div>`;
  } else {
    sheet.innerHTML = `<h3>${u ? `ユーザー編集 — @${esc(u.username)}` : "新規ユーザー"}</h3>
      <label class="f" for="um-name">ユーザー名 ${u ? `<span class="muted">(小文字英数字・変更不可)</span>` : `<span class="muted">(2〜32 文字の小文字英数字と . _ -)</span>`}</label>
      <input type="text" id="um-name" maxlength="32" autocapitalize="none" spellcheck="false"
             value="${esc(u ? u.username : "")}" ${u ? "disabled" : ""}>
      <label class="f" for="um-dname">表示名</label>
      <input type="text" id="um-dname" maxlength="60" value="${esc(u ? u.display_name : "")}">
      <label class="f" for="um-role">ロール</label>
      <select id="um-role">
        <option value="admin">管理者 — すべての機能 + ロール設定</option>
        <option value="maintainer">メンテナー — 起動・停止 + 管理者が ON にした機能</option>
        <option value="user">ユーザー — 簡易画面 (/simple) の開始・停止のみ</option>
      </select>
      <label class="switch"><input type="checkbox" id="um-enabled"><span class="track"></span>
        <span class="lab">アカウントを有効にする (無効にするとログイン不可)</span></label>
      <label class="f" for="um-pw">${u ? `新しいパスワード <span class="muted">(空欄 = 変更しない)</span>` : "パスワード (8 文字以上)"}</label>
      <input type="password" id="um-pw" autocomplete="new-password" maxlength="256">
      <div class="sheet-actions">
        <button class="primary grow" onclick="umSave()">保存</button>
        <button class="ghost grow" onclick="umClose()">キャンセル</button>
      </div>`;
    $("#um-role").value = u ? u.role : "user";
    $("#um-enabled").checked = u ? !!u.enabled : true;
  }
  $("#umodal").style.display = "flex";
  $("#umodal-veil").style.display = "block";
  setTimeout(() => {
    const el = $("#" + (mode === "pw" ? "um-pw" : (u ? "um-dname" : "um-name")));
    if (el) el.focus();
  }, 60);
}
function umClose() {
  if (_openDialog) history.back();         // 履歴条目が消える → applyRoute がダイアログを閉じる
  else umCloseDom();
}
function umCloseDom() {
  $("#umodal").style.display = "none";
  $("#umodal-veil").style.display = "none";
  UM.editing = null; UM.mode = "form";
}
function umReadPw(allowEmpty) {
  const p = $("#um-pw").value;
  if (!p) {
    if (allowEmpty) return "";
    toast("パスワードは 8 文字以上にしてください"); return null;
  }
  if (p.length < 8) { toast("パスワードは 8 文字以上にしてください"); return null; }
  return p;
}
async function umSave() {
  const isNew = !UM.editing;
  const pw = umReadPw(!isNew);
  if (pw === null) return;
  const body = {
    display_name: $("#um-dname").value.trim(),
    role: $("#um-role").value,
    enabled: $("#um-enabled").checked,
  };
  if (isNew) {
    body.username = $("#um-name").value.trim();
    if (!body.username) { toast("ユーザー名を入力してください"); return; }
    body.password = pw;
  } else if (pw) {
    body.password = pw;
  }
  try {
    if (isNew) await api("/api/users", { method: "POST", body });
    else await api(`/api/users/${UM.editing}`, { method: "PUT", body });
    toast(isNew ? "ユーザーを作成しました" : "保存しました");
    umClose();
    await loadUsers();
  } catch (e) { toast("失敗: " + e.message); }
}
async function umSavePw() {
  const p = $("#um-pw").value;
  if (p.length < 8) { toast("パスワードは 8 文字以上にしてください"); return; }
  if (p !== $("#um-pw2").value) { toast("確認用パスワードが一致しません"); return; }
  try {
    await api(`/api/users/${UM.editing}/password`, { method: "POST", body: { password: p } });
    toast("パスワードを変更しました (このユーザーのセッションは失効)");
    umClose();
    await loadUsers();
  } catch (e) { toast("失敗: " + e.message); }
}
async function umToggle(uid) {
  const u = UM.users.find(x => x.id === uid);
  if (!u) return;
  try {
    await api(`/api/users/${uid}`, { method: "PUT", body: { enabled: !u.enabled } });
    toast(u.enabled ? "無効化しました" : "有効化しました");
    await loadUsers();
  } catch (e) { toast("失敗: " + e.message); }
}
async function umDelete(uid) {
  const u = UM.users.find(x => x.id === uid);
  if (!u) return;
  if (!confirm(`ユーザー @${u.username} を削除しますか?\nログイン中のセッションも失効します。`)) return;
  try {
    await api(`/api/users/${uid}`, { method: "DELETE" });
    delete UM.reveal[uid];
    toast("削除しました");
    await loadUsers();
  } catch (e) { toast("失敗: " + e.message); }
}
async function umRotate(uid) {
  const u = UM.users.find(x => x.id === uid);
  if (!u) return;
  if (u.has_api_key && !confirm("API キーを再生成しますか?\n既存のキーは直ちに無効になります。")) return;
  try {
    const d = await api(`/api/users/${uid}/apikey`, { method: "POST" });
    UM.reveal[uid] = d.api_key;
    await loadUsers();
    toast("API キーを発行しました — 平文はこの 1 回だけ表示されます");
  } catch (e) { toast("失敗: " + e.message); }
}
function umCopyKey(uid) {
  const k = UM.reveal[uid];
  if (!k) return;
  (navigator.clipboard ? navigator.clipboard.writeText(k) : Promise.reject())
    .then(() => toast("コピーしました"))
    .catch(() => toast("自動コピーできない環境です — 表示されたキーを選択してコピーしてください"));
}

