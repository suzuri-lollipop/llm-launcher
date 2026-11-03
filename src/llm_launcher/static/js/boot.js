/* ===================================================================
   boot: ビューを <main> に差し込んでから初期化 → 起動 — 旧 index.html からの分割ファイル (classic script)
   =================================================================== */
"use strict";
mountViews();                    // VIEWS → <main>。全ての DOM 配線より先に必須
pgInit();
bmInit();
calInit();
insInit();
$("#pg-input").addEventListener("keydown", e => {
  if (e.key !== "Enter" || e.shiftKey) return;
  if (e.isComposing || e.keyCode === 229) return;   // IME 変換中の Enter では送信しない
  e.preventDefault(); pgSend();
});
["pg-sys", "pg-temp", "pg-topp", "pg-mtok", "pg-stream"].forEach(id =>
  $(`#${id}`).addEventListener("change", pgPersist));
applyRoute();                    // 初期ルート解決 (メニュー選択 / 直接リンク)
boot();                       // 認証状態を確認してから本体 UI を起動する
setInterval(renderTabBadges, 1000);
