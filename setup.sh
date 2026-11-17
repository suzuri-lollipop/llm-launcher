#!/usr/bin/env bash
# llm-launcher セットアップスクリプト — ランチャー本体 (依存 + submodule + サービス登録 + OOM 保護)
#
#   ./setup.sh                          # uv sync + submodule 取得 + サービス登録 + OOM 保護 (既定)
#   ./setup.sh --no-submodules          # submodule 取得をスキップ
#   ./setup.sh --full-submodules        # submodule を完全履歴で取得 (--depth 1 なし)
#   ./setup.sh --no-service             # サービス登録をスキップ (OOM ヘルパーのみ導入)
#   ./setup.sh --no-oom                 # OOM 保護をスキップしユーザーサービスで登録 (sudo 不要)
#   ./setup.sh --ref [REF]              # llm-launcher 本体を指定バージョンに切替えてからセットアップ。
#                                       #   REF: product=suzuri/product (安定) / develop=suzuri/develop (開発) /
#                                       #   latest=最新タグ / 任意のブランチ・タグ・コミット (省略時: product)。
#                                       #   --ref 無しで端末実行なら対話選択 (既定: 現在のまま)
#   ./setup.sh --fork-freetoken [REF]   # FreeToken fork も clone (既定: suzuri/product 安定版。
#                                       #   ブランチの他 suzuri-* タグも指定可 — 例: suzuri-v1.0.1。
#                                       #   タグ一覧は WebUI セットアップのバージョン選択に出る)
#   ./setup.sh --fork-strata [REF]      # Strata fork も clone (既定: リモートの既定ブランチ)
#   ./setup.sh --gen-fields             # 取得後に submodule ソースから fields 正表を再生成
#   ./setup.sh --install-uv             # uv が無いとき公式インストーラを確認なしで実行
#   ./setup.sh --start                  # 完了後に管理アプリをフォアグラウンド起動 (サービス稼働中ならスキップ)
#
# やっていること
#   0. (任意) バージョン選択 (--ref / 対話) — origin を fetch して選んだブランチ / タグに
#      checkout し、切替先の setup.sh を再実行する (以降は切替先のコードで進む)。
#   1. uv を解決 (LLM_LAUNCHER_UV > PATH > ~/.local/bin/uv > ~/.cargo/bin/uv)。
#      無い場合は公式インストーラで導入 (対話確認あり。--install-uv で確認スキップ)。
#   2. uv sync — uv.lock から .venv を構築。Python 3.12 (.python-version) が
#      無い環境では uv が自動で取得する。完了後に import スモークテスト。
#   3. git submodule update --init --depth 1 — module/ 配下のバックエンド 7 種を取得。
#   4. (任意) module/fork/ への fork clone (既存 clone は触らない) と
#      fields 正表の再生成 (gen_fields)。
#   5. sudo scripts/oom-protect-install.sh --systemd — システムサービス登録 + 起動
#      + OOM 保護 (kill 対策) ヘルパー + sudoers を一括導入。Linger / ユーザーマネージャー
#      不要で OS 起動時から稼働し、OOMScoreAdjust=-1000 が実際に効く。既存のユーザー
#      サービスは無効化して二重起動を防ぐ。再実行時は待ち受け host/port を保持。
#      --no-oom → scripts/systemd-install.sh (ユーザーサービス・sudo 不要)。
#      --no-service → OOM ヘルパーのみ (モード 1・ユーザーサービスと併用可)。
#
# 注意
#   - バックエンド本体のインストール (.venv / ネイティブビルド / モデル DL) は
#     このスクリプトの範囲外。アプリ起動後に WebUI のセットアップタブ (📦) か
#     各 submodule の setup.sh / maya.sh で行う (README「セットアップ」参照)。
#   - HTTP(S)_PROXY / ALL_PROXY / NO_PROXY は環境変数からそのまま引き継ぐ
#     (git / uv / curl すべて。値は伏せ字付きで表示する)。
#   - サービス登録 + OOM 保護 (既定) には sudo が必要。パスワードが要る場合はこの段階だけで聞く
#     (stdin が tty でなくても /dev/tty が開ければそこで聞く)。sudo が無い / 聞く端末が全く無い
#     場合だけスキップして案内する。sudo 無しで運用するなら --no-oom (ユーザーサービス)。
#   - バージョン切替 (--ref / 対話選択) は追跡ファイルに未コミットの変更があると拒否する
#     (commit / stash してから再実行。未追跡の .venv / data/ は影響しない)。
#     ローカル独自コミットは壊さない (ブランチは fast-forward のみ / タグは detached HEAD)。
#     切替先に setup.sh が無い古いバージョンも拒否する。
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

need_git() { command -v git >/dev/null 2>&1 || { echo "✘ git が見つかりません" >&2; exit 1; }; }

# sudo でコマンドを実行する。パスワードが必要ならこの段階だけで聞く:
# パスワードレス sudo → 端末 (sudo が聞く) → stdin が tty でなければ /dev/tty で聞く → 端末無しは失敗。
# printf は bash builtin なのでパスワードは ps に現れない。使用後は変数をクリアする。
sudo_run() {
  if sudo -n true >/dev/null 2>&1; then
    sudo "$@"
    return $?
  fi
  if [[ -t 0 ]]; then
    sudo "$@"
    return $?
  fi
  if (: >/dev/tty) 2>/dev/null; then
    local pw="" rc=0
    IFS= read -r -s -p "  sudo パスワード: " pw </dev/tty || { echo; return 1; }
    echo
    printf '%s\n' "$pw" | sudo -S -p '' "$@" || rc=$?
    pw=""
    return "$rc"
  fi
  echo "! sudo パスワードを聞けません (端末がありません)" >&2
  return 1
}

# --ref [REF] は先に抽出する。切替先の setup.sh 再実行時には引き継がない
# (古いバージョンの setup.sh は --ref を知らないため「不明なオプション」で落ちる)。
REF_SEL=""
REEXEC_ARGS=()
while [[ $# -gt 0 ]]; do
  case "$1" in
    --ref)
      REF_SEL="product"
      # 次トークンがオプションでなければバージョン指定として消費 (省略時は安定版)
      if [[ -n "${2:-}" && "${2:-}" != --* ]]; then REF_SEL="$2"; shift; fi
      shift ;;
    *) REEXEC_ARGS+=("$1"); shift ;;
  esac
done
set -- ${REEXEC_ARGS[@]+"${REEXEC_ARGS[@]}"}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-submodules) SUBMODULES=0; shift ;;
    --full-submodules) FULL=1; shift ;;
    --no-service) SERVICE=0; shift ;;
    --no-oom) OOM=0; shift ;;
    --fork-freetoken)
      FORK_FT=1
      # 次トークンがオプションでなければブランチ / タグ指定として消費 (省略時は安定版)
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

# --- 0. バージョン選択 (llm-launcher 本体: --ref / 対話 → fetch + checkout + 再実行) ---
# installer.py の _switch_branch と同じ規約: 未コミット変更があれば切替を拒否し、
# ブランチは fast-forward のみ (ローカル独自コミットは壊さない)、タグは detached HEAD。
current_ref_name() { # 現在地の表示名 (ブランチ名 / タグ名 / 短縮 SHA)
  local b
  b="$(git branch --show-current 2>/dev/null || true)"
  if [[ -n "$b" ]]; then printf '%s' "$b"; return; fi
  git describe --tags --exact-match HEAD 2>/dev/null || git rev-parse --short HEAD
}

# 再実行時は選択をスキップ (切替先が新版 setup.sh でもメニュー / 切替が再発しないように)
if [[ -z "${LLM_LAUNCHER_SETUP_REF_DONE:-}" && -z "$REF_SEL" && -t 0 ]] \
   && git rev-parse --git-dir >/dev/null 2>&1; then
  LATEST_TAG="$(git tag --sort=-v:refname | head -n1)"
  echo "▶ llm-launcher のバージョン選択 (現在: $(current_ref_name) @ $(git rev-parse --short HEAD))"
  echo "  [1] suzuri/product (安定版)"
  echo "  [2] suzuri/develop (開発版)"
  echo "  [3] 最新タグ${LATEST_TAG:+ ($LATEST_TAG)}"
  echo "  [4] 現在のまま (既定)"
  ans=""
  read -r -p "  選択 [1-4 / Enter=4]: " ans || true
  case "$ans" in
    1) REF_SEL="product" ;;
    2) REF_SEL="develop" ;;
    3) REF_SEL="latest" ;;
    *) REF_SEL="" ;;
  esac
fi

if [[ -n "$REF_SEL" && -z "${LLM_LAUNCHER_SETUP_REF_DONE:-}" ]]; then
  if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "✘ git リポジトリではないため --ref ($REF_SEL) で切り替えられません (clone し直して実行してください)" >&2
    exit 1
  fi
  if [[ -n "$(git status --porcelain=v1 --untracked-files=no)" ]]; then
    echo "✘ 追跡ファイルに未コミットの変更があるため $REF_SEL に切り替えられません (現在: $(current_ref_name))" >&2
    echo "  commit / stash してから再実行してください (未追跡の .venv / data/ 等は切替に影響しません)" >&2
    exit 1
  fi
  # shallow clone は浅いまま追う (installer.py と同じ規約)
  SHALLOW=()
  if [[ -f "$(git rev-parse --git-dir)/shallow" ]]; then SHALLOW=(--depth 1); fi
  TAGS_FETCHED=0
  fetch_tags() { # タグ一覧を最新化 (ブランチもまとめて fetch。失敗時はローカル refs で続行を試す)
    if [[ "$TAGS_FETCHED" -eq 0 ]]; then
      git fetch "${SHALLOW[@]}" --tags origin \
        || echo "! git fetch --tags origin に失敗 — ローカルのタグ / ブランチで解決を試みます" >&2
      TAGS_FETCHED=1
    fi
  }

  REF_MODE="" REF_NAME=""
  case "$REF_SEL" in
    product) REF_MODE=branch; REF_NAME="suzuri/product" ;;
    develop) REF_MODE=branch; REF_NAME="suzuri/develop" ;;
    latest|latest-tag|tag) REF_MODE=latest ;;
    *) REF_MODE=auto; REF_NAME="$REF_SEL" ;;
  esac
  if [[ "$REF_MODE" == "latest" ]]; then
    fetch_tags
    REF_NAME="$(git tag --sort=-v:refname | head -n1)"
    if [[ -z "$REF_NAME" ]]; then
      echo "✘ タグが見つからないため最新タグ (--ref latest) を選べません" >&2; exit 1
    fi
    REF_MODE=tag
  elif [[ "$REF_MODE" == "branch" ]]; then
    echo "▶ git fetch origin $REF_NAME"
    if ! git fetch "${SHALLOW[@]}" origin "+refs/heads/$REF_NAME:refs/remotes/origin/$REF_NAME"; then
      echo "✘ git fetch origin $REF_NAME に失敗しました — ネットワーク / プロキシを確認して再実行してください" >&2
      exit 1
    fi
  else
    # 明示指定: origin ブランチ / タグ / ローカルコミットの順に解決
    fetch_tags
    if git show-ref --verify --quiet "refs/remotes/origin/$REF_NAME"; then REF_MODE=branch
    elif git show-ref --verify --quiet "refs/tags/$REF_NAME"; then REF_MODE=tag
    elif git rev-parse --verify --quiet "$REF_NAME^{commit}" >/dev/null; then REF_MODE=commit
    else
      echo "✘ ref が見つかりません: $REF_SEL (origin のブランチ / タグ / ローカルコミットのいずれでもありません)" >&2
      exit 1
    fi
  fi
  case "$REF_MODE" in
    branch) TARGET="$(git rev-parse "refs/remotes/origin/$REF_NAME")" ;;
    tag)    TARGET="$(git rev-parse "refs/tags/$REF_NAME^{commit}")" ;;
    commit) TARGET="$(git rev-parse "$REF_NAME^{commit}")" ;;
  esac

  HEAD_BEFORE="$(git rev-parse HEAD)"
  if [[ "$TARGET" == "$HEAD_BEFORE" ]]; then
    echo "✔ 既に $REF_NAME の位置です ($(git rev-parse --short HEAD)) — 切替はスキップ"
  else
    # 切替先に setup.sh が無い古いバージョンだと再実行できないので、事前に拒否する
    if ! git cat-file -e "$TARGET:setup.sh" 2>/dev/null; then
      echo "✘ 切替先 ($REF_NAME) に setup.sh がありません (古いバージョン) — 切替を中断します" >&2
      echo "  新しいブランチ / タグを選んでください (--ref product / develop / latest)" >&2
      exit 1
    fi
    echo "▶ バージョン切替: $(current_ref_name) → $REF_NAME ($(git rev-parse --short "$TARGET"))"
    if [[ "$REF_MODE" == "branch" ]]; then
      if git show-ref --verify --quiet "refs/heads/$REF_NAME"; then
        git checkout "$REF_NAME"
        git merge --ff-only "refs/remotes/origin/$REF_NAME" \
          || echo "! $REF_NAME はローカルに独自コミットがあるため origin の位置へは進めませんでした (現在のコミットで続けます)"
      else
        git checkout -b "$REF_NAME" --track "refs/remotes/origin/$REF_NAME"
      fi
    else
      # タグ / コミットは detached HEAD (ローカルブランチは作らない。installer.py と同じ規約)
      git checkout --detach "$TARGET"
    fi
    if [[ "$(git rev-parse HEAD)" == "$HEAD_BEFORE" ]]; then
      echo "✔ 位置は変わっていません — このままセットアップを続けます"
    else
      # checkout でこのスクリプト自体も変わるため、切替先の setup.sh を再実行する。
      # --ref は引き継がない (古い setup.sh は知らないオプションなので)。
      echo "✔ 切り替えました — 切替先の setup.sh を再実行します"
      export LLM_LAUNCHER_SETUP_REF_DONE=1
      exec bash "$PROJECT_ROOT/setup.sh" ${REEXEC_ARGS[@]+"${REEXEC_ARGS[@]}"}
    fi
  fi
fi
if git rev-parse --git-dir >/dev/null 2>&1; then
  echo "• llm-launcher 現在: $(current_ref_name) @ $(git rev-parse --short HEAD) (バージョン切替: --ref product|develop|latest / 端末では対話選択)"
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
    local cur
    cur="$(git -C "$dir" branch --show-current 2>/dev/null || true)"
    # タグ checkout は detached HEAD (ブランチ名が無い) なので一致するタグ名で現在地を示す
    if [[ -z "$cur" ]]; then
      cur="$(git -C "$dir" describe --tags --exact-match HEAD 2>/dev/null || echo '?')"
    fi
    echo "✔ fork は clone 済み: $dir (現在: $cur) — そのままにします"
    echo "  ブランチ / タグ切替は WebUI セットアップタブ (📦) か git -C \"$dir\" checkout <ref> で"
    return 0
  fi
  dir="$PROJECT_ROOT/module/fork/$name"
  mkdir -p "$PROJECT_ROOT/module/fork"
  echo "▶ fork clone: $url → module/fork/$name${ref:+ (ref: $ref)}"
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

# --- 5+6. サービス登録 + OOM 保護 (kill 対策) ---
# 既定: sudo scripts/oom-protect-install.sh --systemd がシステムサービスの登録 + 起動
# + OOM ヘルパー + sudoers を一括で行う (Linger / ユーザーマネージャー / ログインセッション
# 不要で OS 起動時から稼働。OOMScoreAdjust=-1000 が実際に効く)。sudo はこの段階だけ。
# 再実行時はユニット更新 + restart (data/ の状態は引き継ぎ。待ち受け host/port は保持)。
# --no-oom  → ユーザーサービス (scripts/systemd-install.sh・sudo 不要) + OOM スキップ。
# --no-service → OOM ヘルパーのみ (モード 1・ユーザーサービスと併用可)。
OOM_HELPER="/usr/local/lib/llm-launcher/oom-protect"
SERVICE_MODE=""
if [[ "$SERVICE" -eq 1 && "$OOM" -eq 1 ]]; then
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "! systemctl が無いためサービス登録 + OOM 保護はスキップ (手動起動: $UV_BIN run llm-launcher)"
  elif ! command -v sudo >/dev/null 2>&1; then
    echo "! sudo が無いためサービス登録 + OOM 保護はスキップ"
    echo "  手動: sudo scripts/oom-protect-install.sh --systemd / sudo 不要のユーザーサービス: scripts/systemd-install.sh"
  else
    echo "▶ サービス登録 + OOM 保護 (sudo scripts/oom-protect-install.sh --systemd)"
    if [[ "$EUID" -eq 0 ]]; then
      # root で実行されている場合は sudo 不要 (対象ユーザーは SUDO_USER か実行者)
      if bash "$PROJECT_ROOT/scripts/oom-protect-install.sh" --systemd --user "${SUDO_USER:-$(id -un)}"; then
        SERVICE_OK=1; OOM_OK=1; SERVICE_MODE=system
      fi
    elif sudo_run bash "$PROJECT_ROOT/scripts/oom-protect-install.sh" --systemd; then
      SERVICE_OK=1; OOM_OK=1; SERVICE_MODE=system
    fi
    if [[ "$SERVICE_OK" -ne 1 ]]; then
      echo "! システムサービス + OOM 保護の導入に失敗したためスキップ (セットアップ自体は完了しています)"
      echo "  手動で再試行: sudo scripts/oom-protect-install.sh --systemd"
      echo "  手動起動: $UV_BIN run llm-launcher"
    fi
  fi
elif [[ "$SERVICE" -eq 1 ]]; then
  # --no-oom: systemd ユーザーサービス (sudo 不要)。再実行時はユニット更新 + restart
  # (KillMode=process のため稼働中バックエンドは巻き添えにならない)
  if ! command -v systemctl >/dev/null 2>&1; then
    echo "! systemctl が無いためサービス登録はスキップ (手動起動: $UV_BIN run llm-launcher)"
  else
    # Web ターミナル等は XDG_RUNTIME_DIR が無いことがある: ランタイム dir があれば補う
    if [[ -z "${XDG_RUNTIME_DIR:-}" && -d "/run/user/$(id -u)" ]]; then
      export XDG_RUNTIME_DIR="/run/user/$(id -u)"
    fi
    echo "▶ サービス登録 (scripts/systemd-install.sh)"
    if bash "$PROJECT_ROOT/scripts/systemd-install.sh"; then
      SERVICE_OK=1; SERVICE_MODE=user
    else
      echo "! サービス登録に失敗したためスキップ (systemd ユーザーマネージャー未起動 / 到達不可など)"
      echo "  OS 起動時から使うには Linger が必要: sudo loginctl enable-linger \$USER → 再ログイン後に再実行"
      echo "  またはシステムサービス (sudo): sudo scripts/oom-protect-install.sh --systemd"
      echo "  手動起動: $UV_BIN run llm-launcher / 後から登録: scripts/systemd-install.sh"
    fi
  fi
elif [[ "$OOM" -eq 1 ]]; then
  # --no-service: OOM ヘルパーのみ (モード 1: sudoers ヘルパー・ユーザーサービスと併用可)
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
  else
    echo "▶ OOM 保護導入 (sudo scripts/oom-protect-install.sh)"
    if [[ "$EUID" -eq 0 ]]; then
      if bash "$PROJECT_ROOT/scripts/oom-protect-install.sh" --user "${SUDO_USER:-$(id -un)}"; then
        OOM_OK=1; OOM_NEW=1
      fi
    elif sudo_run bash "$PROJECT_ROOT/scripts/oom-protect-install.sh"; then
      OOM_OK=1; OOM_NEW=1
    fi
    if [[ "$OOM_NEW" -ne 1 ]]; then
      echo "! OOM 保護の導入に失敗したためスキップ (セットアップ自体は完了しています)"
      echo "  手動で再試行: sudo scripts/oom-protect-install.sh"
    fi
  fi
else
  echo "• サービス登録 + OOM 保護はスキップ (--no-service --no-oom)"
fi

# ヘルパー新規導入時にユーザーサービスが稼働中なら再起動して有効化 (起動ログに oom-guard: が出る)。
# システムサービスはインストーラ自身が起動 / 再起動まで済ませるのでここでは触らない。
if [[ "$OOM_NEW" -eq 1 && "$SERVICE_MODE" != "system" ]] \
   && systemctl --user is-active --quiet llm-launcher 2>/dev/null; then
  systemctl --user restart llm-launcher
  echo "✔ OOM ヘルパー有効化のためサービスを再起動しました (起動ログに oom-guard: が出ます)"
fi

# --- 完了 ---
# uv が PATH 外から解決された場合はフルパスで案内する
case ":$PATH:" in
  *":$(dirname "$UV_BIN"):"*) UV_CMD="uv" ;;
  *) UV_CMD="$UV_BIN" ;;
esac

service_is_active() {
  if [[ "$SERVICE_MODE" == "system" ]]; then
    systemctl is-active --quiet llm-launcher 2>/dev/null
  else
    systemctl --user is-active --quiet llm-launcher 2>/dev/null
  fi
}

if [[ "$START" -eq 1 ]]; then
  if [[ "$SERVICE_OK" -eq 1 ]] && service_is_active; then
    echo "• サービスが稼働中のためフォアグラウンド起動 (--start) はスキップします"
    if [[ "$SERVICE_MODE" == "system" ]]; then
      echo "  ログ: journalctl -u llm-launcher -f / 停止: sudo systemctl stop llm-launcher"
    else
      echo "  ログ: journalctl --user -u llm-launcher -f / 停止: systemctl --user stop llm-launcher"
    fi
  else
    echo "✔ セットアップ完了 — 管理アプリを起動します (Ctrl+C で停止)"
    exec "$UV_BIN" run llm-launcher
  fi
fi

echo
echo "✔ セットアップ完了"
echo
if [[ "$SERVICE_OK" -eq 1 && "$SERVICE_MODE" == "system" ]]; then
  echo "管理アプリ: systemd システムサービスとして稼働中 (OS 起動時から自動起動 / 認証 ON)"
  echo "  初回アクセスで管理者アカウントを作成 (パスワード 8 文字以上)"
  echo "  状態: systemctl status llm-launcher / ログ: journalctl -u llm-launcher -f"
  echo "  停止等: sudo systemctl {stop,restart} llm-launcher (稼働中バックエンドは巻き添えなし)"
elif [[ "$SERVICE_OK" -eq 1 ]]; then
  echo "管理アプリ: systemd ユーザーサービスとして稼働中 (既定 0.0.0.0:8036 / 認証 ON)"
  echo "  初回アクセスで管理者アカウントを作成 (パスワード 8 文字以上)"
  echo "  状態: systemctl --user status llm-launcher / ログ: journalctl --user -u llm-launcher -f"
  echo "  停止等: systemctl --user {stop,restart} llm-launcher (稼働中バックエンドは巻き添えなし)"
else
  echo "管理アプリ起動: $UV_CMD run llm-launcher (既定 0.0.0.0:8036 / 認証 ON)"
  echo "  初回アクセスで管理者アカウントを作成 (パスワード 8 文字以上)"
  echo "  サービス登録 (常駐運用): sudo scripts/oom-protect-install.sh --systemd"
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
