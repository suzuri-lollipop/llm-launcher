#!/usr/bin/env bash
# llm-launcher OOM 対策インストーラ
#
#   sudo scripts/oom-protect-install.sh                  # 特権ヘルパー + sudoers
#   sudo scripts/oom-protect-install.sh --systemd        # + systemd システムサービス
#   sudo scripts/oom-protect-install.sh --swap 32G       # + スワップファイル追加
#   sudo scripts/oom-protect-install.sh --uninstall      # 削除
#
# やっていること
#   1. oom-protect ヘルパーを /usr/local/lib/llm-launcher/ に置き、sudoers で
#      「引数なしのそのパスだけ」NOPASSWD で許可する。
#      → llm-launcher は以後 起動ツリー全体 (モデル本体 + 後から湧く worker +
#        launcher 自身) の oom_score_adj を既定 -700 に下げられる。
#   2. --systemd: llm-launcher をシステムサービスとして起動するようにする。
#      → systemd のシステムマネージャが管理するので OOMScoreAdjust=-1000 が実際に効き、
#        systemd-oomd がスワップ逼迫で kill を始める前に (SwapUsedLimit 既定 90%)
#        MemoryHigh でカーネルが自発的にスロットリングする。ユーザーセッション
#        (systemd-run --user) では OOMScoreAdjust が無視されるため、必ずシステム側。
#   3. --swap: スワップを追加。MemAvailable 確保とスワップ監視の両方に効く。
#
set -euo pipefail

HELPER_SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/oom-protect-helper.py"
LIB_DIR="/usr/local/lib/llm-launcher"
HELPER="${LLM_LAUNCHER_OOM_HELPER:-$LIB_DIR/oom-protect}"
SUDOERS="/etc/sudoers.d/llm-launcher-oom-protect"
UNIT="/etc/systemd/system/llm-launcher.service"
SWAPFILE="/swapfile"
MODE="helper"
USERS=()
SWAP_SIZE=""
PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --systemd) MODE="helper+systemd"; shift ;;
    --swap) SWAP_SIZE="${2:?--swap は 32G 等のサイズを指定してください}"; shift 2 ;;
    --user) USERS+=("${2:?--user はユーザー名を指定してください}"); shift 2 ;;
    --root) PROJECT_ROOT="$(cd "$2" && pwd)"; shift 2 ;;
    --uninstall) MODE="uninstall"; shift ;;
    -h|--help) sed -n '2,25p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "不明なオプション: $1" >&2; exit 2 ;;
  esac
done

if [[ ${EUID} -ne 0 ]]; then
  echo "このスクリプトは root 権限が必要です: sudo scripts/oom-protect-install.sh" >&2
  exit 1
fi
if [[ ${#USERS[@]} -eq 0 ]]; then
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    USERS=("$SUDO_USER")
  else
    echo "--user で llm-launcher を実行するユーザー名を指定してください" >&2
    exit 2
  fi
fi
if [[ ! -r "$HELPER_SRC" ]]; then
  echo "ヘルパー本体が見つかりません: $HELPER_SRC" >&2
  exit 1
fi

if [[ "$MODE" == "uninstall" ]]; then
  rm -f "$SUDOERS" "$HELPER" "$UNIT"
  systemctl daemon-reload 2>/dev/null || true
  echo "削除しました: $HELPER $SUDOERS $UNIT"
  echo "(llm-launcher 側は自動で 'sudo ヘルパー未導入' に戻ります)"
  exit 0
fi

# ---- 1. 特権ヘルパー -------------------------------------------------------
install -d -m 0755 "$LIB_DIR"
install -m 0755 "$HELPER_SRC" "$HELPER"
TMP="$(mktemp)"
trap 'rm -f "$TMP"' EXIT

# 以前このスクリプトが壊れた sudoers を書き残していると、sudo が毎回パーサ警告を
# 出す (最悪 sudo 全体が失敗する)。検証前に退避ではなく削除する。
if [[ -f "$SUDOERS" ]] && ! visudo -cf "$SUDOERS" >/dev/null 2>&1; then
  echo "! 既存の $SUDOERS が構文エラーだったため削除します"
  rm -f "$SUDOERS"
fi

# requiretty を知らない sudo ビルドがある (sudo-rs など。unknown setting で弾かれる)。
# 未知の Defaults 行はファイル全体を壊すので、visudo が通る場合だけ追加する。
supports_requiretty() {
  local probe rc
  probe="$(mktemp)"
  printf 'Cmnd_Alias LLM_OOM_PROBE = /bin/true\nDefaults!LLM_OOM_PROBE !requiretty\n' >"$probe"
  visudo -cf "$probe" >/dev/null 2>&1
  rc=$?
  rm -f "$probe"
  return "$rc"
}
REQTTY=0
if supports_requiretty; then REQTTY=1; fi

{
  echo "# llm-launcher OOM 保護ヘルパー用 (scripts/oom-protect-install.sh が生成)"
  echo "# 引数なしの実行だけを許可。任意コマンド実行は許可しない。"
  echo "Cmnd_Alias LLM_OOM_PROTECT = $HELPER"
  for u in "${USERS[@]}"; do
    id -u "$u" >/dev/null 2>&1 || { echo "ユーザーが存在しません: $u" >&2; exit 2; }
    echo "$u ALL=(root) NOPASSWD: LLM_OOM_PROTECT"
  done
  # requiretty が有効な distro (RHEL 系) では、tty 無し (Web アプリからの呼び出し) で
  # sudo が拒否されるためスコープ付きで無効化する。未対応の sudo では出さない。
  if [[ "$REQTTY" == "1" ]]; then
    echo "Defaults!LLM_OOM_PROTECT !requiretty"
  fi
} >"$TMP"

# 書き込む前に検証する (/etc/sudoers.d の不正ファイルは sudo を壊す)
if ! visudo -cf "$TMP" >/dev/null; then
  echo "生成した sudoers が構文エラーです。$SUDOERS には書き込んでいません:" >&2
  visudo -cf "$TMP" >&2 || true
  sed 's/^/  /' "$TMP" >&2
  exit 1
fi
install -m 0440 "$TMP" "$SUDOERS"
if ! visudo -cf "$SUDOERS" >/dev/null; then
  rm -f "$SUDOERS"
  echo "sudoers 検証に失敗したため $SUDOERS を除去しました" >&2
  exit 1
fi
echo "✔ ヘルパー導入: $HELPER"
echo "✔ sudoers 許可: ${USERS[*]} -> $HELPER (NOPASSWD, 引数なし)"

# ---- 2. systemd システムサービス (任意) ------------------------------------
if [[ "$MODE" == "helper+systemd" ]]; then
  PY="$PROJECT_ROOT/.venv/bin/python"
  [[ -x "$PY" ]] || PY="$(command -v python3)"
  # 実メモリから上限を決める (既定: 85% で throttle / 92% で上限。編集前提値)
  mem_kb=$(awk '/^MemTotal:/{print $2}' /proc/meminfo 2>/dev/null || echo 0)
  [[ ${mem_kb:-0} -gt 0 ]] || mem_kb=$((16 * 1024 * 1024))
  MEM_HIGH="$(( mem_kb * 85 / 100 / 1024 ))M"
  MEM_MAX="$(( mem_kb * 92 / 100 / 1024 ))M"
  cat >"$UNIT" <<UNIT
[Unit]
Description=llm-launcher (model server launcher)
Documentation=file://$PROJECT_ROOT/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${USERS[0]}
WorkingDirectory=$PROJECT_ROOT
Environment=PYTHONPATH=$PROJECT_ROOT/src
Environment=LLM_LAUNCHER_HOST=127.0.0.1
Environment=LLM_LAUNCHER_PORT=8088
ExecStart=$PY -m llm_launcher
Restart=on-failure
RestartSec=5s
# ここに書いた値は systemd のシステムマネージャが適用するので実際に効く
# (ユーザーマネージャ / systemd-run --user では OOMScoreAdjust は無視される)
OOMScoreAdjust=-1000
OOMPolicy=continue
# メモリ上限: 超えたら kill ではなく throttle。上限まで使わせないことで
# ホスト全体 (Cline 等を含む) の OOM kill を防ぐ。既定は実メモリの 85% / 92%。
MemoryHigh=${MEM_HIGH}
MemoryMax=${MEM_MAX}
# systemd-oomd にこの unit を kill 候補から外してもらう (モデル本体は別 cgroup)
ManagedOOMPreference=avoid

[Install]
WantedBy=multi-user.target
UNIT
  systemctl daemon-reload
  echo "✔ ユニット作成: $UNIT (OOMScoreAdjust=-1000, MemoryHigh=$MEM_HIGH, MemoryMax=$MEM_MAX)"
  echo "  起動: sudo systemctl enable --now llm-launcher"
  echo "  ※ これ以降 llm-launcher は systemctl で起動してください (run.sh との二重起動注意)"
  echo "  ※ MemoryHigh/MemoryMax は実メモリに合わせて $UNIT を編集してください"
fi

# ---- 3. スワップ追加 (任意) ------------------------------------------------
if [[ -n "$SWAP_SIZE" ]]; then
  if swapon --show=NAME --noheadings | grep -q .; then
    echo "! 既存のスワップがあるため --swap はスキップしました: $(swapon --show=NAME --noheadings | tr '\n' ' ')"
  else
    if [[ -e "$SWAPFILE" ]]; then
      echo "! $SWAPFILE が既にあります。上書きしないためスキップ" >&2
    else
      local_mb=$(( $(numfmt --from=iec "$SWAP_SIZE") / 1024 ))
      dd if=/dev/zero of="$SWAPFILE" bs=1M count="$local_mb" status=none
      chmod 600 "$SWAPFILE"
      mkswap "$SWAPFILE" >/dev/null
      swapon "$SWAPFILE"
      if ! grep -q "^$SWAPFILE[[:space:]]" /etc/fstab; then
        echo "$SWAPFILE none swap defaults 0 0" >>/etc/fstab
      fi
      echo "✔ スワップ追加: $SWAP_SIZE ($SWAPFILE, /etc/fstab に永続化)"
    fi
  fi
fi

# ---- 検証 ------------------------------------------------------------------
echo
echo "検証:"
# 一時的な sleep プロセス (score 0 = 無害) に適用して sudo 経路そのものを確認する。
# 実際に負値を掛けるのは llm-launcher 本体の起動時 / 起動直後。
sleep 3 & TEST_PID=$!
if out=$(printf '%s\n0\n' "$TEST_PID" | sudo -n "$HELPER" 2>&1); then
  echo "  ✔ sudo 経路 OK: $out"
else
  echo "  ✘ ヘルパーが動きません ($out) — sudoers / noexec マウント / SELinux を確認してください"
fi
wait "$TEST_PID" 2>/dev/null || true
echo
echo "llm-launcher を再起動すると、設定 (data/settings.json の oom) どおりに適用されます。"
echo "UI の バックエンド設定 → OOM 対策 で適用状態を確認できます。"
