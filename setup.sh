#!/usr/bin/env bash
# llm-launcher セットアップスクリプト — ランチャー本体 (依存 + submodule + サービス登録 + OOM 保護)
#
#   ./setup.sh                          # uv sync + submodule 取得 + サービス登録 + OOM 保護 (既定)
#   ./setup.sh --no-submodules          # submodule 取得をスキップ
#   ./setup.sh --full-submodules        # submodule を完全履歴で取得 (--depth 1 なし)
#   ./setup.sh --no-service             # systemd ユーザーサービス登録をスキップ
#   ./setup.sh --no-oom                 # OOM 保護 (kill 対策) ヘルパー導入をスキップ
#   ./setup.sh --fork-freetoken [REF]   # FreeToken fork も clone (既定: suzuri/product 安定版)
#   ./setup.sh --fork-strata [REF]      # Strata fork も clone (既定: リモートの既定ブランチ)
#   ./setup.sh --gen-fields             # 取得後に submodule ソースから fields 正表を再生成
#   ./setup.sh --install-uv             # uv が無いとき公式インストーラを確認なしで実行
#   ./setup.sh --start                  # 完了後に管理アプリをフォアグラウンド起動 (サービス稼働中ならスキップ)
#
# やっていること
#   1. uv を解決 (LLM_LAUNCHER_UV > PATH > ~/.local/bin/uv > ~/.cargo/bin/uv)。
#      無い場合は公式インストーラで導入 (対話確認あり。--install-uv で確認スキップ)。
#   2. uv sync — uv.lock から .venv を構築。Python 3.12 (.python-version) が
#      無い環境では uv が自動で取得する。完了後に import スモークテスト。
#   3. git submodule update --init --depth 1 — module/ 配下のバックエンド 7 種を取得。
#   4. (任意) module/fork/ への fork clone (既存 clone は触らない) と
#      fields 正表の再生成 (gen_fields)。
#   5. scripts/systemd-install.sh — systemd ユーザーサービスとして登録 (sudo 不要)
#      + 自動起動登録 + 起動。ユーザーマネージャーに到達できない環境では案内付きでスキップ。
#   6. sudo scripts/oom-protect-install.sh — OOM 保護 (kill 対策) のモード 1
#      (sudoers ヘルパー)。ユーザーサービスと併用可能。導入済みならスキップし、
#      新規導入時はヘルパー有効化のためサービスを再起動する。パスワードが必要なら
#      この段階だけで聞く (stdin が tty でなくても /dev/tty 経由で聞く)。sudo が使えない環境ではスキップ。
#      ※ モード 2 (--systemd) はユーザーサービスと二重起動になる同名のシステムユニット
#        (ポート 8088) を作るため、ここでは扱わない (必要なら手動で実行)。
#
# 注意
#   - バックエンド本体のインストール (.venv / ネイティブビルド / モデル DL) は
#     このスクリプトの範囲外。アプリ起動後に WebUI のセットアップタブ (📦) か
#     各 submodule の setup.sh / maya.sh で行う (README「セットアップ」参照)。
#   - HTTP(S)_PROXY / ALL_PROXY / NO_PROXY は環境変数からそのまま引き継ぐ
#     (git / uv / curl すべて。値は伏せ字付きで表示する)。
#   - OOM 保護の導入には sudo が必要。パスワードが要る場合はこの段階だけで聞く
#     (stdin が tty でなくても /dev/tty が開ければそこで聞く)。sudo が無い /
#     聞く端末が全く無い場合だけスキップして案内する。
#   - 再実行は安全 (べき等)。既存の fork clone や data/ は上書きしない。
#     サービス登録の再実行はユニット更新 + restart (data/ の状態は引き継がれる)。
#
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_ROOT"

SUBMODULES=1
FULL=0
FORK_FT=0
FT_REF="suzuri/product"
FORK_ST=0
ST_REF=""
GEN_FIELDS=0
INSTALL_UV=0
START=0
SERVICE=1
OOM=1
SERVICE_OK=0
OOM_OK=0
OOM_NEW=0

usage() { awk 'NR>1 { if (/^#/) { sub(/^# ?/, ""); print } else exit }' "${BASH_SOURCE[0]}"; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-submodules) SUBMODULES=0; shift ;;
    --full-submodules) FULL=1; shift ;;
    --no-service) SERVICE=0; shift ;;
    --no-oom) OOM=0; shift ;;
    --fork-freetoken)
      FORK_FT=1
      # 次トークンがオプションでなければブランチ指定として消費 (省略時は安定版)
      if [[ -n "${2:-}" && "${2:-}" != --* ]]; then FT_REF="$2"; shift; fi
      shift ;;
    --fork-strata)
      FORK_ST=1
      if [[ -n "${2:-}" && "${2:-}" != --* ]]; then ST_REF="$2"; shift; fi
      shift ;;
    --gen-fields) GEN_FIELDS=1; shift ;;
    --install-uv) INSTALL_UV=1; shift ;;
    --start) START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "不明なオプション: $1 (--help で使い方)" >&2; exit 2 ;;
  esac
done

[[ -f "$PROJECT_ROOT/pyproject.toml" ]] || { echo "✘ pyproject.toml がありません: $PROJECT_ROOT" >&2; exit 1; }

# プロキシは環境変数からそのまま引き継ぐ (git / uv / curl)。使用中なら伏せ字付きで表示
PROXY_SHOWN="${HTTPS_PROXY:-${https_proxy:-${ALL_PROXY:-${all_proxy:-${HTTP_PROXY:-${http_proxy:-}}}}}}"
if [[ -n "$PROXY_SHOWN" ]]; then
  echo "• プロキシ: $(printf '%s' "$PROXY_SHOWN" | sed -E 's#//[^/@]+@#//***@#') (環境変数から引き継ぎ)"
fi

# --- uv 解決 (installer.py / systemd-install.sh と同じ規約 + cargo bin) ---
UV_BIN=""
resolve_uv() {
  if [[ -n "${LLM_LAUNCHER_UV:-}" ]]; then
    if [[ -x "$LLM_LAUNCHER_UV" ]]; then UV_BIN="$LLM_LAUNCHER_UV"; return 0; fi
    echo "! LLM_LAUNCHER_UV が実行可能ではありません: $LLM_LAUNCHER_UV" >&2
  fi
  if command -v uv >/dev/null 2>&1; then UV_BIN="$(command -v uv)"; return 0; fi
  local c
  for c in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv"; do
    if [[ -x "$c" ]]; then UV_BIN="$c"; return 0; fi
  done
  return 1
}

if ! resolve_uv; then
  echo "! uv が見つかりません (LLM_LAUNCHER_UV / PATH / ~/.local/bin / ~/.cargo/bin)"
  if [[ "$INSTALL_UV" -ne 1 ]]; then
    if [[ -t 0 ]]; then
      ans=""
      read -r -p "  公式インストーラ (https://astral.sh/uv/install.sh) で導入しますか? [y/N] " ans || true
      if [[ "$ans" != [yY]* ]]; then
        echo "  中断しました。uv を導入してから再実行するか、--install-uv を指定してください。"
        exit 1
      fi
    else
      echo "  非対話環境です: --install-uv を付けて再実行するか、先に uv を導入してください。" >&2
      exit 1
    fi
  fi
  command -v curl >/dev/null 2>&1 || { echo "✘ uv の導入に curl が必要です" >&2; exit 1; }
  curl -LsSf https://astral.sh/uv/install.sh | sh
  resolve_uv || { echo "✘ uv を導入後も見つかりません (~/.local/bin を確認してください)" >&2; exit 1; }
fi
echo "✔ uv: $UV_BIN ($("$UV_BIN" --version))"

need_git() { command -v git >/dev/null 2>&1 || { echo "✘ git が見つかりません" >&2; exit 1; }; }

# --- 1. 依存インストール (uv.lock 同期。Python 3.12 は uv が自動取得) ---
echo "▶ 依存インストール (uv sync)"
"$UV_BIN" sync
"$UV_BIN" run python -c 'import llm_launcher.app; from llm_launcher import __version__; print(f"✔ llm_launcher {__version__} import OK (.venv 構築済み)")'

# --- 2. submodule 取得 (README 既定は --depth 1 の浅い checkout) ---
if [[ "$SUBMODULES" -eq 1 ]]; then
  need_git
  if [[ "$FULL" -eq 1 ]]; then
    echo "▶ submodule 取得 (git submodule update --init / 完全履歴)"
    git submodule update --init
  else
    echo "▶ submodule 取得 (git submodule update --init --depth 1)"
    git submodule update --init --depth 1
  fi
  echo "✔ submodule: $(git submodule status | wc -l) 件登録済み (module/)"
else
  echo "• submodule 取得はスキップ (--no-submodules)"
fi

# --- 3. (任意) fork clone — module/fork/ はフォルダのみ追跡・配下は追跡外 ---
# 既存ディレクトリは大小文字無視で探す (backends.py の fork_dir と同じ規則)
find_fork_dir() { # $1=小文字名 → 実在ディレクトリのパスを出力
  local d
  for d in "$PROJECT_ROOT"/module/fork/*/; do
    [[ -d "$d" ]] || continue
    if [[ "$(basename "$d" | tr '[:upper:]' '[:lower:]')" == "$1" ]]; then
      printf '%s' "${d%/}"
      return 0
    fi
  done
  return 1
}

clone_fork() { # $1=ディレクトリ名 $2=URL $3=ブランチ (空=リモート既定)
  local name="$1" url="$2" ref="$3" dir
  need_git
  if dir="$(find_fork_dir "$name")"; then
    echo "✔ fork は clone 済み: $dir (branch: $(git -C "$dir" branch --show-current 2>/dev/null || echo '?')) — そのままにします"
    echo "  ブランチ切替は WebUI セットアップタブ (📦) か git -C \"$dir\" checkout <branch> で"
    return 0
  fi
  dir="$PROJECT_ROOT/module/fork/$name"
  mkdir -p "$PROJECT_ROOT/module/fork"
  echo "▶ fork clone: $url → module/fork/$name${ref:+ (branch: $ref)}"
  if [[ -n "$ref" ]]; then
    git clone --branch "$ref" "$url" "$dir"
  else
    git clone "$url" "$dir"
  fi
  echo "✔ fork clone 完了: $dir (インストールは WebUI セットアップタブ 📦 から)"
}

if [[ "$FORK_FT" -eq 1 ]]; then
  clone_fork freetoken "https://github.com/suzuri-lollipop/FreeToken" "$FT_REF"
fi
if [[ "$FORK_ST" -eq 1 ]]; then
  clone_fork strata "https://github.com/suzuri-lollipop/Strata" "$ST_REF"
fi

# --- 4. (任意) fields 正表の再生成 (submodule を更新したら README 推奨) ---
if [[ "$GEN_FIELDS" -eq 1 ]]; then
  echo "▶ fields 正表を再生成 (python -m llm_launcher.gen_fields)"
  "$UV_BIN" run python -m llm_launcher.gen_fields --root "$PROJECT_ROOT"
  echo "✔ fields 正表: src/llm_launcher/fields/ (未 clone の submodule は skip されます)"
fi

# --- 5. サービス登録 (systemd ユーザーサービス・sudo 不要) ---
# 再実行時はユニット更新 + restart になる (data/ のプロファイル等は引き継がれる。
# KillMode=process のため稼働中バックエンドは巻き添えにならない)
if [[ "$SERVICE" -eq 1 ]]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "! systemctl が無いためサービス登録はスキップ (手動起動: $UV_BIN run llm-launcher)"
  else
    echo "▶ サービス登録 (scripts/systemd-install.sh)"
    if bash "$PROJECT_ROOT/scripts/systemd-install.sh"; then
      SERVICE_OK=1
    else
      echo "! サービス登録に失敗したためスキップ (systemd ユーザーマネージャー未起動 / 到達不可など)"
      echo "  OS 起動時から使うには Linger が必要: sudo loginctl enable-linger \$USER → 再ログイン後に再実行"
      echo "  手動起動: $UV_BIN run llm-launcher / 後から登録: scripts/systemd-install.sh"
    fi
  fi
else
  echo "• サービス登録はスキップ (--no-service)"
fi

# --- 6. OOM 保護 / kill 対策 (モード 1: sudoers ヘルパー・ユーザーサービスと併用可) ---
# モード 2 (--systemd) は同名のシステムユニット (ポート 8088) を作って二重起動になるため扱わない。
OOM_HELPER="/usr/local/lib/llm-launcher/oom-protect"
if [[ "$OOM" -eq 1 ]]; then
  if sudo -n -l "$OOM_HELPER" >/dev/null 2>&1; then
    echo "✔ OOM 保護は導入済み (sudo 経路確認 OK) — スキップ"
    OOM_OK=1
  elif [[ -x "$OOM_HELPER" ]]; then
    echo "✔ OOM 保護ヘルパーは導入済み: $OOM_HELPER — 再導入はスキップ"
    echo "  (このシェルからは sudo 経路を確認できません。確認: sudo -n -l $OOM_HELPER)"
    OOM_OK=1
  elif ! command -v sudo >/dev/null 2>&1; then
    echo "! sudo が無いため OOM 保護はスキップ"
    echo "  手動導入: sudo scripts/oom-protect-install.sh"
  elif [[ ! -t 0 ]] && ! sudo -n true >/dev/null 2>&1; then
    # stdin が tty でない (パイプ / エージェント実行など) かつ sudo にパスワードが要る。
    # 制御端末があるなら /dev/tty でこの段階だけパスワードを聞き、sudo で導入する
    # (スキップするとヘルパー未導入のままになり、モデルが OOM kill される)。
    # /dev/tty は「開けるか」で判定する (-r/-w はデバイスノードの権限しか見ない)。
    if (: >/dev/tty) 2>/dev/null; then
      echo "▶ OOM 保護導入 (sudo にパスワードが必要 — sudo を使うのはこの段階だけ)"
      OOM_PW=""
      if IFS= read -r -s -p "  sudo パスワード: " OOM_PW </dev/tty; then
        echo
        # printf は bash builtin なのでパスワードは ps に現れない。sudo -S で stdin から渡す
        if printf '%s\n' "$OOM_PW" | sudo -S -p '' bash "$PROJECT_ROOT/scripts/oom-protect-install.sh"; then
          OOM_OK=1; OOM_NEW=1
        fi
      else
        echo
      fi
      OOM_PW=""
      if [[ "$OOM_NEW" -ne 1 ]]; then
        echo "! OOM 保護の導入に失敗したためスキップ (セットアップ自体は完了しています)"
        echo "  手動で再試行: sudo scripts/oom-protect-install.sh"
      fi
    else
      echo "! sudo にパスワードが必要で、聞ける端末も無いため OOM 保護はスキップ (非対話環境)"
      echo "  対話シェルで再実行するか手動導入: sudo scripts/oom-protect-install.sh"
    fi
  else
    echo "▶ OOM 保護導入 (sudo scripts/oom-protect-install.sh)"
    if [[ "$EUID" -eq 0 ]]; then
      # root で実行されている場合は sudo 不要 (対象ユーザーは SUDO_USER か実行者)
      if bash "$PROJECT_ROOT/scripts/oom-protect-install.sh" --user "${SUDO_USER:-$(id -un)}"; then
        OOM_OK=1; OOM_NEW=1
      fi
    elif sudo bash "$PROJECT_ROOT/scripts/oom-protect-install.sh"; then
      OOM_OK=1; OOM_NEW=1
    fi
    if [[ "$OOM_NEW" -ne 1 ]]; then
      echo "! OOM 保護の導入に失敗したためスキップ (セットアップ自体は完了しています)"
      echo "  手動で再試行: sudo scripts/oom-protect-install.sh"
    fi
  fi
else
  echo "• OOM 保護はスキップ (--no-oom)"
fi

# 新規導入時はサービスを再起動してヘルパーを有効化 (起動ログに oom-guard: が出る)
if [[ "$OOM_NEW" -eq 1 && "$SERVICE_OK" -eq 1 ]]; then
  if systemctl --user is-active --quiet llm-launcher 2>/dev/null; then
    systemctl --user restart llm-launcher
    echo "✔ OOM ヘルパー有効化のためサービスを再起動しました (起動ログに oom-guard: が出ます)"
  fi
fi

# --- 完了 ---
# uv が PATH 外から解決された場合はフルパスで案内する
case ":$PATH:" in
  *":$(dirname "$UV_BIN"):"*) UV_CMD="uv" ;;
  *) UV_CMD="$UV_BIN" ;;
esac

if [[ "$START" -eq 1 ]]; then
  if [[ "$SERVICE_OK" -eq 1 ]] && systemctl --user is-active --quiet llm-launcher 2>/dev/null; then
    echo "• サービスが稼働中のためフォアグラウンド起動 (--start) はスキップします"
    echo "  ログ: journalctl --user -u llm-launcher -f / 停止: systemctl --user stop llm-launcher"
  else
    echo "✔ セットアップ完了 — 管理アプリを起動します (Ctrl+C で停止)"
    exec "$UV_BIN" run llm-launcher
  fi
fi

echo
echo "✔ セットアップ完了"
echo
if [[ "$SERVICE_OK" -eq 1 ]]; then
  echo "管理アプリ: systemd ユーザーサービスとして稼働中 (既定 0.0.0.0:8036 / 認証 ON)"
  echo "  初回アクセスで管理者アカウントを作成 (パスワード 8 文字以上)"
  echo "  状態: systemctl --user status llm-launcher / ログ: journalctl --user -u llm-launcher -f"
  echo "  停止等: systemctl --user {stop,restart} llm-launcher (稼働中バックエンドは巻き添えなし)"
else
  echo "管理アプリ起動: $UV_CMD run llm-launcher (既定 0.0.0.0:8036 / 認証 ON)"
  echo "  初回アクセスで管理者アカウントを作成 (パスワード 8 文字以上)"
  echo "  サービス登録 (常駐運用): scripts/systemd-install.sh"
fi
if [[ "$OOM_OK" -eq 1 ]]; then
  echo "OOM 保護 (kill 対策): 導入済み (sudoers ヘルパー — 起動ツリーの oom_score_adj を -700 に)"
elif [[ "$OOM" -eq 0 ]]; then
  echo "OOM 保護 (kill 対策): スキップ (--no-oom)"
else
  echo "OOM 保護 (kill 対策): 未導入 — sudo scripts/oom-protect-install.sh"
fi
echo "バックエンド本体の導入: 起動後 WebUI のセットアップタブ (📦) から"
echo "  (FreeToken / Strata / Basalt 等。手動派は README「セットアップ」)"
