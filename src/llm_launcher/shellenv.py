"""サービス起動時のシェル環境変数フォールバック (プロキシ / モデル置き場)。

systemd サービス (ユーザー / システムどちらも) は .bashrc を読まない。
インストーラ (scripts/systemd-install.sh / oom-protect-install.sh) は
*_PROXY だけを EnvironmentFile に抽出するが、それは「インストール時点」の
値でしかなく、ユニットが古い / 後から .bashrc を変えた / インストーラを
再実行せず restart しただけ、の場合はサービスにプロキシが届かない。
セットアップタブの git / uv / pip は launcher の os.environ を引き継ぐ
(installer.install_env) ため、アップデート時のモジュールインストールが
ダイレクト接続に落ちる (2026-11-18 報告)。

モデル置き場の解決も同じ穴を持つ: fork_model_root は FREETOKEN_DATA_DIR、
hf_hub_cache_dir は HF_HOME / HF_HUB_CACHE を環境変数から読む。端末
(シェル) でこれらを export して setup.sh / HF CLI でモデルを置いたのに、
サービス側が既定の置き場をスキャンすると「モデルがあるのにダウンロード
済みモデルが見つからない」になる (別端末でサービス運用している環境)。

そこで起動時 (main) に、これらのキーに限ってプロセス環境に無いものを
~/.bashrc 等の export 行から補完する。明示的な環境変数 (EnvironmentFile /
Environment= / 端末の export) が常に優先 — 空値のキーだけ埋める。

抽出規則は scripts/proxy-env-from-bashrc.sh と同じ:
  - `export KEY=VALUE` 行のみ (素の代入は環境変数にならないので拾わない)
  - quote 付き値は閉じ quote の内側を取る (末尾コメント付きも可) /
    quote 無し値の末尾コメントは捨てる
  - $VAR 展開を含む値はスキップ (展開されず壊れた値になるため)
  - 空値は「設定なし」(前の値を消す) / 同一キーは後の行が勝つ
ファイルは ~/.profile → ~/.bash_profile → ~/.bashrc の順に読み、後の
ファイルが勝つ (ログインシェルが .bashrc を最後に source する慣習に合わせる)。
"""
from __future__ import annotations

import os
import re
from pathlib import Path

# 補完対象キー。プロキシは小文字正規形 (install_env が大小両形に揃える)、
# 置き場系は大文字正規形 (huggingface_hub / fork setup.sh が読む形)。
PROXY_KEYS = ("http_proxy", "https_proxy", "all_proxy", "no_proxy")
PLACE_KEYS = ("HF_HOME", "HF_HUB_CACHE", "FREETOKEN_DATA_DIR")

_EXPORT_RE = re.compile(r"^export\s+([A-Za-z_][A-Za-z0-9_]*)=(.*)$")

# 起動時に補完したキー (proxy_log_line 等の出所表示用)。main() 経由の
# apply_shell_env() だけが記録する (テストの明示 environ 渡しは記録しない)。
_APPLIED: dict[str, str] = {}


def _canonical(key: str) -> str | None:
    low = key.lower()
    if low in PROXY_KEYS:
        return low
    if key.upper() in PLACE_KEYS:
        return key.upper()
    return None


def extract_exports(text: str) -> dict[str, str]:
    """シェル設定テキストから対象キーの export 値を抽出する (純粋関数)。"""
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if line.startswith("#"):
            continue
        m = _EXPORT_RE.match(line)
        if not m:
            continue
        canon = _canonical(m.group(1))
        if canon is None:
            continue
        val = m.group(2).strip()
        if val and val[0] in "\"'":
            end = val.find(val[0], 1)           # 閉じ quote の内側を取る (末尾コメントは無視)
            if end > 0:
                val = val[1:end]
        else:
            val = re.sub(r"\s+#.*$", "", val)   # quote 無し値の末尾コメント
        if "$" in val:
            continue                            # 展開依存の値は焼かない
        if not val:
            out.pop(canon, None)                # 空値 = 設定なし
            continue
        out[canon] = val
    return out


def rc_files(home: Path | None = None) -> list[Path]:
    """読むシェル設定ファイル (後のファイルが勝つ)。"""
    h = home if home is not None else Path.home()
    return [h / ".profile", h / ".bash_profile", h / ".bashrc"]


def shell_env_fallback(home: Path | None = None) -> dict[str, str]:
    """シェル設定ファイル群から対象キーを抽出 (読めないファイルは飛ばす)。"""
    out: dict[str, str] = {}
    for f in rc_files(home):
        try:
            text = f.read_text("utf-8", errors="replace")
        except OSError:
            continue
        out.update(extract_exports(text))
    return out


def apply_shell_env(environ: dict[str, str] | None = None,
                    home: Path | None = None) -> dict[str, str]:
    """プロセス環境に無い対象キーをシェル設定から補完する。

    補完した {キー: 値} を返す。明示値 (空でない) は決して上書きしない。
    environ 省略時は os.environ を直接埋め、出所表示用に記録する。
    """
    env = os.environ if environ is None else environ
    applied: dict[str, str] = {}
    for k, v in shell_env_fallback(home).items():
        if not str(env.get(k) or "").strip():
            env[k] = v
            applied[k] = v
    if environ is None:
        _APPLIED.update(applied)
    return applied


def applied_keys() -> dict[str, str]:
    """起動時にシェル設定から補完したキー (コピー)。"""
    return dict(_APPLIED)


def mask_secret(val: str) -> str:
    """プロキシ URL の user:password を伏せ字にする (ログ表示用)。"""
    return re.sub(r"://([^/@:]+):([^@/]+)@", r"://\1:****@", val)


def describe_applied(applied: dict[str, str]) -> str:
    """補完内容の 1 行表示 (プロキシ値は伏せ字・置き場パスはそのまま)。"""
    parts = []
    for k in sorted(applied):
        v = applied[k]
        parts.append(f"{k}={mask_secret(v) if k in PROXY_KEYS else v}")
    return ", ".join(parts)
