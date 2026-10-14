#!/usr/bin/env bash
# llm-launcher systemd ユーザーサービス インストーラ
#
#   scripts/systemd-install.sh               # インストール + 自動起動登録 + 起動
#   scripts/systemd-install.sh --root PATH   # プロジェクトルートを明示指定
#   scripts/systemd-install.sh --host HOST   # 待ち受けホスト指定 (既定: アプリ既定 0.0.0.0)
#   scripts/systemd-install.sh --port PORT   # 待ち受けポート指定 (既定: アプリ既定 8036)
#   scripts/systemd-install.sh --uninstall   # 停止 + 登録解除 + ユニット削除
#
# やっていること
#   1. ~/.config/systemd/user/llm-launcher.service を生成し
#      ExecStart = <uv の絶対パス> run llm-launcher で起動する (sudo 不要)。
#      ※ ユーザーマネージャーは PATH に ~/.local/bin を持たないため絶対パス必須
#   2. systemctl --user daemon-reload + enable (+ 起動)。再実行ならユニット更新と
#      restart を行う (プロファイルは data/ に残るので状態は引き継がれる)。
#   3. Linger が有効ならログインしていなくても OS 起動時に立ち上がる。
#      無効な場合は enable するコマンドを案内する。
#
# 設計メモ
#   - KillMode=process: バックエンド (vLLM 等) は start_new_session で detached 起動
#     する設計のため、launcher の restart / stop で稼働中サーバーを巻き添えにしない
#     (README「動作仕様メモ」の引き継ぎ)。実行中サーバーごと止めたい場合は先に UI から
#     停止してから systemctl --user stop すること。
#   - OOM 対策はモード 1 (sudoers ヘルパー) なら本サービスからも有効。
#     モード 2 (sudo scripts/oom-protect-install.sh --systemd) はシステム側に同名の
#     別ユニット (/etc/systemd/system/llm-launcher.service, venv python 実行) を作る
#     ため併用すると二重起動になる。切り替える時はどちらかを disable すること。
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
UNIT="$UNIT_DIR/llm-launcher.service"
HOST=""
PORT=""
MODE="install"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --root) PROJECT_ROOT="$(cd "${2:?--root にはパスを指定してください}" && pwd)"; shift 2 ;;
    --host) HOST="${2:?--host には値を指定してください}"; shift 2 ;;
    --port) PORT="${2:?--port には値を指定してください}"; shift 2 ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,28p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; exit 2 ;;
  esac
done

# systemctl --user に到達できない環境用のチェック
if ! systemctl --user is-system-running >/dev/null 2>&1; then
  echo "ユーザー systemd マネージャーに接続できません (user manager が動いていない?)" >&2
  echo "  先に Linger を有効にして再ログインしてください: sudo loginctl enable-linger \$USER" >&2
  exit 1
fi

if [[ "$MODE" == "uninstall" ]]; then
  systemctl --user disable --now llm-launcher 2>/dev/null || true
  rm -f "$UNIT"
  systemctl --user daemon-reload
  systemctl --user reset-failed llm-launcher 2>/dev/null || true
  echo "✔ 削除しました: $UNIT"
  echo "  (detached で動き続けているバックエンドサーバーは停止されません — 必要なら UI から停止)"
  exit 0
fi

# uv は絶対パスで埋め込む (ユーザーマネージャーの PATH に ~/.local/bin は無い)
if command -v uv >/dev/null 2>&1; then
  UV_BIN="$(command -v uv)"
elif [[ -x "$HOME/.local/bin/uv" ]]; then
  UV_BIN="$HOME/.local/bin/uv"
else
  echo "uv が見つかりません (PATH または ~/.local/bin/uv)" >&2
  exit 1
fi

[[ -f "$PROJECT_ROOT/pyproject.toml" ]] || { echo "pyproject.toml がありません: $PROJECT_ROOT (--root を確認)" >&2; exit 1; }
[[ -d "$PROJECT_ROOT/module" ]] || echo "! $PROJECT_ROOT/module が見当たりません (バックエンド検出は失敗します)" >&2

# 明示指定されたものだけ環境変数化 (未指定はアプリ既定 0.0.0.0:8036)
ENV_BLOCK=""
if [[ -n "$HOST" ]]; then ENV_BLOCK+="Environment=LLM_LAUNCHER_HOST=$HOST"$'\n'; fi
if [[ -n "$PORT" ]]; then ENV_BLOCK+="Environment=LLM_LAUNCHER_PORT=$PORT"$'\n'; fi

install -d -m 0755 "$UNIT_DIR"
cat >"$UNIT" <<UNIT
# scripts/systemd-install.sh が生成 (手修正した内容は再実行で上書きされる)
[Unit]
Description=llm-launcher (vLLM / SGLang / llama.cpp / FreeToken 管理 Web UI)
Documentation=file://$PROJECT_ROOT/README.md

[Service]
Type=simple
WorkingDirectory=$PROJECT_ROOT
Environment=LLM_LAUNCHER_ROOT=$PROJECT_ROOT
Environment=PYTHONUNBUFFERED=1
${ENV_BLOCK}ExecStart=$UV_BIN run llm-launcher
Restart=on-failure
RestartSec=5s
# バックエンドは start_new_session で detached 起動する設計のため、launcher の
# restart / stop で稼働中サーバーを巻き添えにしない (再起動後「引き継ぎ」で再検出)。
# systemd はこの設定を非推奨扱いだが、本アプリの設計 (README 動作仕様メモ) に合わせている
KillMode=process

[Install]
WantedBy=default.target
UNIT
systemctl --user daemon-reload
echo "✔ ユニット生成: $UNIT"

if systemctl --user is-active --quiet llm-launcher 2>/dev/null; then
  systemctl --user restart llm-launcher
  echo "✔ 再起動しました (既に稼働中だったため)"
else
  CHECK_PORT="${PORT:-8036}"
  # -H でヘッダー行を消す (ヘッダーだけでも grep . に引っかかるため)
  if ss -ltnH "sport = :$CHECK_PORT" 2>/dev/null | grep -q .; then
    echo "! ポート $CHECK_PORT が使用中です (手動起動中?) — サービスは再試行を繰り返します"
    echo "  手動インスタンスを止める: pkill -f 'uv run llm-launcher' (または Ctrl+C)"
  fi
  systemctl --user start llm-launcher
  echo "✔ 起動しました"
fi
systemctl --user enable llm-launcher >/dev/null
echo "✔ 自動起動登録: systemctl --user enable llm-launcher (WantedBy=default.target)"

if ! loginctl show-user "$USER" 2>/dev/null | grep -q "^Linger=yes"; then
  echo "! Linger が無効です — ログインしていない間 / OS 起動直後には立ち上がりません"
  echo "  有効化: sudo loginctl enable-linger \$USER"
fi

echo
echo "検証:"
sleep 2
if systemctl --user is-active --quiet llm-launcher; then
  echo "  ✔ 稼働中: $(systemctl --user is-active llm-launcher)"
  for i in 1 2 3 4 5; do
    if curl -fsS -o /dev/null -m 2 "http://127.0.0.1:${PORT:-8036}/" 2>/dev/null; then
      echo "  ✔ HTTP 応答: http://127.0.0.1:${PORT:-8036}/"
      break
    fi
    sleep 1
  done || true
  if ! curl -fsS -o /dev/null -m 2 "http://127.0.0.1:${PORT:-8036}/" 2>/dev/null; then
    echo "  (まだ HTTP 応答がありません — 初回 uv 同期中の可能性。ログを確認: journalctl --user -u llm-launcher -f)"
  fi
else
  echo "  ✘ 起動に失敗しました:"
  systemctl --user status llm-launcher --no-pager -l || true
  journalctl --user -u llm-launcher -n 30 --no-pager || true
  exit 1
fi
echo
echo "ログ:   journalctl --user -u llm-launcher -f"
echo "状態:   systemctl --user status llm-launcher"
echo "停止等: systemctl --user {stop,restart} llm-launcher  (稼働中バックエンドは巻き添えなし)"
