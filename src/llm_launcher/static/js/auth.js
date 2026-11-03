/* ===================================================================
   auth: 認証 / ロール / ログインタイル / 起動 — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
/* ============================================================= 認証 */
const AUTH = { enabled: true, needsSetup: false, user: null, isAdmin: false,
               role: "", perms: {}, simpleOnly: false, mode: "login", booted: false };

/* 機能権限の判定 (キーはサーバー側 rbac.CAPS と同じ)。admin は全機能、user は簡易画面のみ。
   ここで隠すものは API 側でも 403 になる = 表示と認可が同じキーを見る。 */
function can(cap) {
  if (AUTH.role === "admin") return true;
  return !!(AUTH.perms && AUTH.perms[cap]);
}

function loginError(msg) {
  const el = $("#login-err");
  if (!msg) { el.textContent = ""; el.classList.add("hidden"); return; }
  el.textContent = msg; el.classList.remove("hidden");
}
function setLoginMode(mode) {
  AUTH.mode = mode === "setup" ? "setup" : "login";
  const setup = AUTH.mode === "setup";
  $("#login-sub").textContent = setup
    ? "初めての起動です。管理者アカウントを作成してください"
    : "ユーザー名とパスワードを入力してください";
  $("#login-pw2wrap").classList.toggle("hidden", !setup);
  $("#login-rememberwrap").classList.toggle("hidden", setup);
  $("#login-btn").textContent = setup ? "管理者を作成して開始" : "ログイン";
  $("#login-pass").setAttribute("autocomplete", setup ? "new-password" : "current-password");
  $("#login-foot").textContent = setup
    ? "パスワードは 8 文字以上。PBKDF2-HMAC-SHA256 でハッシュ化して保存し、平文では残しません"
    : "ログイン失敗が 5 回続くと 30 秒間ロックされます";
  loginError("");
}
function showLogin(msg) {
  closeAcct();                                    // ログイン画面へ戻るなら吹き出しも閉じる
  setLoginMode(AUTH.needsSetup ? "setup" : "login");
  $("#login").classList.remove("hidden");
  document.body.classList.add("auth-lock");
  if (msg) loginError(msg);
  setTimeout(() => { const el = $("#login-user"); if (el) el.focus(); }, 60);
}
function hideLogin() {
  $("#login").classList.add("hidden");
  document.body.classList.remove("auth-lock");
}
/* /simple (ロール user 専用画面) は自前のログインフォームを持たない。ログアウトや
   セッション切れでここに送られてくるときは sessionStorage に案内と「ログイン状態を
   保持」の既定を置いていくので、ログイン画面を出すときに 1 回だけ反映する。 */
let LOGIN_HINT = null;
function readLoginHint() {
  LOGIN_HINT = null;
  try {
    const raw = sessionStorage.getItem("llm.login");
    sessionStorage.removeItem("llm.login");
    const h = raw ? JSON.parse(raw) : null;
    if (h && typeof h === "object") LOGIN_HINT = h;
  } catch { /* 読めなければ通常のログイン画面として出す */ }
}
function showLoginHint() {
  const h = LOGIN_HINT; LOGIN_HINT = null;
  const el = $("#login-note");
  if (!el) return;
  el.textContent = ""; el.classList.add("hidden");
  if (!h) return;
  if (h.remember) { const c = $("#login-remember"); if (c) c.checked = true; }
  if (h.note) { el.textContent = h.note; el.classList.remove("hidden"); }
}
async function fetchAuthStatus() {
  try {
    const st = await api("/api/auth/status");
    AUTH.enabled = st.auth_enabled !== false;
    AUTH.needsSetup = !!st.needs_setup;
    AUTH.user = st.user || null;
    AUTH.isAdmin = !!st.is_admin;
    AUTH.role = st.role || "";
    AUTH.perms = st.perms || {};
    AUTH.simpleOnly = !!st.simple_only;
  } catch {
    // 認証 API が読めない (旧サーバー等) → 認証なしとして通常の起動を続ける
    AUTH.enabled = false; AUTH.needsSetup = false; AUTH.user = null; AUTH.isAdmin = true;
    AUTH.role = "admin"; AUTH.perms = {}; AUTH.simpleOnly = false;
  }
}
const ROLE_BADGE = { admin: ["管理者", "purple"], maintainer: ["メンテナー", "info"],
                     user: ["ユーザー", "ok"] };
function roleBadge(role) {
  const r = ROLE_BADGE[role] || [role || "ユーザー", ""];
  return `<span class="badge ${r[1]}">${esc(r[0])}</span>`;
}
function applyAuthUi() {
  renderAcct();                                   // 右上はアバター 1 個。名前・ロール等は吹き出しの中
  // ボタン類 (body の no-* クラスと .need-* で一括に隠す)
  const b = document.body.classList;
  b.toggle("no-admin", !AUTH.isAdmin);
  b.toggle("no-edit", !can("profiles.edit"));
  b.toggle("no-bench", !can("bench"));
  b.toggle("no-cal", !can("calibrate"));
  b.toggle("no-oom", !can("oom.edit"));
  b.toggle("no-backend", !can("backends.edit"));
  b.toggle("no-dashboard", !can("dashboard"));
  b.toggle("no-install", !can("install"));
  // メニュー項目
  [["#mi-home", "dashboard"], ["#mi-history", "history"], ["#mi-playground", "playground"],
   ["#mi-bench", "bench"], ["#mi-calibrate", "calibrate"], ["#mi-profiles", "profiles.edit"],
   ["#mi-backends", "backends.edit"], ["#mi-setup", "install"]]
    .forEach(([id, cap]) => $(id).classList.toggle("hidden", !can(cap)));
  $("#mi-users").classList.toggle("hidden", !AUTH.isAdmin);
  if (!location.hash || location.hash === "#") applyRoute();   // 既定ビュー (dashboard 権限なら home) を反映
  else if (!tabAllowed(S.tab)) go("#/launch", true);            // ログイン後: 開けないビューを捨てる
}
/* ---------------- 右上アカウントチップ (ヘッダの情報を 1 アイコンに集約) ----------------
   以前ヘッダに並べていた ホスト名 / IP / @ユーザー名 / ロール / ログアウト を、アバター
   1 個の吹き出しにまとめた (Google のアカウントチップ相当)。表示内容は renderAcct() が
   毎回 DOM を更新する (ログイン状態・ロール・ホスト情報の 3 つが変わりうるため)。 */
const ROLE_COLOR = { admin: "#8430ce", maintainer: "#1a73e8", user: "#188038" };
function acctLabel(u) {
  if (!u) return "";
  return (u.display_name && u.display_name !== u.username) ? u.display_name : u.username;
}
function initialsOf(name) {
  const s = String(name || "").trim();
  if (!s) return "?";
  const parts = s.split(/[\s._\-@/]+/).filter(Boolean);
  return (parts.length > 1 ? parts[0][0] + parts[1][0] : s.slice(0, 2)).toUpperCase();
}
function renderAcct() {
  const btn = $("#acct-btn"), av = $("#acct-avatar"), pop = $("#acct-pop");
  if (!btn || !av || !pop) return;
  const u = AUTH.user;
  const col = u ? (ROLE_COLOR[u.role] || "var(--acc)") : "var(--sub)";
  const inner = u ? esc(initialsOf(acctLabel(u))) : IC("user");
  av.innerHTML = inner; av.style.background = col;
  $("#ap-avatar").innerHTML = inner; $("#ap-avatar").style.background = col;
  $("#ap-name").textContent = u ? acctLabel(u) : (AUTH.enabled ? "未ログイン" : "認証を無効にして起動中");
  $("#ap-sub").textContent = u ? "@" + u.username : "";
  $("#ap-role").innerHTML = u ? roleBadge(u.role) : "";
  const h = S.hdr || {};
  $("#ap-host").textContent = h.err ? "取得失敗" : (h.host || "-");
  $("#ap-ip").textContent = h.err ? "-" : (h.ip || "-");
  const note = $("#ap-note");
  const showNote = !!h.err || !u;
  note.textContent = h.err
    ? "状態を取得できません (左上のインジケータが赤です)"
    : (AUTH.enabled ? "ログインしていないため、ログアウトはできません"
                    : "ログアウトはありません (--no-auth / LLM_LAUNCHER_AUTH=0 で起動中)");
  note.classList.toggle("hidden", !showNote);
  const showLogout = !!u, showUsers = !!AUTH.isAdmin;
  $("#ap-logout").classList.toggle("hidden", !showLogout);
  $("#ap-users").classList.toggle("hidden", !showUsers);
  $("#ap-menu-sep").classList.toggle("hidden", !(showLogout || showUsers));
  $("#ap-logout-sep").classList.toggle("hidden", !(showLogout && showUsers));
  btn.title = u ? `${acctLabel(u)} (@${u.username})` : "アカウント";
}
function openAcct() {
  renderAcct();
  $("#acct").classList.add("open");
  $("#acct-pop").classList.remove("hidden");
  $("#acct-btn").setAttribute("aria-expanded", "true");
}
function closeAcct() {
  const wrap = $("#acct"), pop = $("#acct-pop"), btn = $("#acct-btn");
  if (wrap) wrap.classList.remove("open");
  if (pop) pop.classList.add("hidden");
  if (btn) btn.setAttribute("aria-expanded", "false");
}
function toggleAcct(ev) {
  if (ev) ev.stopPropagation();                   // 開いたクリックで外側判定をしない
  if ($("#acct-pop").classList.contains("hidden")) openAcct(); else closeAcct();
}
document.addEventListener("click", e => {
  const wrap = $("#acct");
  if (wrap && !wrap.contains(e.target)) closeAcct();
});
async function loginSubmit(ev) {
  ev.preventDefault();
  const btn = $("#login-btn"), label = btn.textContent;
  const user = $("#login-user").value.trim(), pass = $("#login-pass").value;
  loginError("");
  if (AUTH.mode === "setup") {
    if (pass.length < 8) { loginError("パスワードは 8 文字以上にしてください"); return false; }
    if (pass !== $("#login-pw2").value) { loginError("パスワード (確認) が一致しません"); return false; }
  }
  btn.disabled = true; btn.textContent = "処理中…";
  let errMsg = "";
  try {
    if (AUTH.mode === "setup") {
      await api("/api/auth/setup", { method: "POST", body: { username: user, password: pass } });
      AUTH.needsSetup = false;
    } else {
      await api("/api/auth/login", { method: "POST",
        body: { username: user, password: pass, remember: $("#login-remember").checked } });
    }
    await fetchAuthStatus();
    if (AUTH.simpleOnly) {                    // ロール user は管理画面を使わせない
      toast(`ようこそ、${(AUTH.user && (AUTH.user.display_name || AUTH.user.username)) || ""} さん`);
      location.replace("/simple");
      return false;
    }
    applyAuthUi(); hideLogin();
    $("#login-pass").value = ""; $("#login-pw2").value = "";
    toast(`ようこそ、${(AUTH.user && (AUTH.user.display_name || AUTH.user.username)) || ""} さん`);
    await refresh(false); startTimers();
  } catch (e) {
    errMsg = e.message || "ログインに失敗しました";
  }
  btn.disabled = false;
  setLoginMode(AUTH.needsSetup ? "setup" : "login");   // 文面を戻してから
  btn.textContent = label;
  if (errMsg) loginError(errMsg);                      // 失敗メッセージを後がけする
  return false;
}
async function logout() {
  try { await api("/api/auth/logout", { method: "POST" }); } catch { /* Cookie が無ければ画面だけ戻す */ }
  AUTH.user = null; AUTH.isAdmin = false; AUTH.role = ""; AUTH.perms = {}; AUTH.simpleOnly = false;
  stopTimers(); applyAuthUi(); showLogin();
  toast("ログアウトしました");
}
function stopTimers() {
  clearInterval(refreshTimer); clearInterval(logTimer);
  clearInterval(sysTimer); clearInterval(benchTimer);
  refreshTimer = logTimer = sysTimer = benchTimer = null;
}
function onUnauthorized() {
  if (!AUTH.booted || !AUTH.user) return;      // boot 前の 401 は boot 側で処理する
  AUTH.user = null; AUTH.isAdmin = false; AUTH.role = ""; AUTH.perms = {}; AUTH.simpleOnly = false;
  stopTimers(); applyAuthUi();
  showLogin("セッションが切れました。再度ログインしてください");
}
async function boot() {
  readLoginHint();                                   // /simple からのログアウト等の案内 (1 回だけ)
  await fetchAuthStatus();
  if (AUTH.simpleOnly) { location.replace("/simple"); return; }   // ロール user は簡易画面へ
  applyAuthUi();
  AUTH.booted = true;
  if (!AUTH.enabled || AUTH.user) { hideLogin(); await refresh(false); startTimers(); }
  else { showLogin(); showLoginHint(); }
}

