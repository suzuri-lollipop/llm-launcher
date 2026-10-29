#!/usr/bin/env bash
# .bashrc 等のプロキシ設定を systemd EnvironmentFile 形式で抽出する
#
#   scripts/proxy-env-from-bashrc.sh                  # 既定: ~/.bashrc
#   scripts/proxy-env-from-bashrc.sh FILE [FILE...]   # 対象ファイルを並べられる
#                                                     # (後のファイル / 後の行が勝つ)
#
# なぜ要るか:
#   systemd サービス (ユーザー / システムどちらも) は .bashrc を読まない。
#   インタラクティブシェルで export している HTTP_PROXY / HTTPS_PROXY /
#   ALL_PROXY / NO_PROXY が llm-launcher に届かず、セットアップタブの
#   git clone / uv pip / HF ダウンロードがダイレクト接続に落ちる。
#   systemd-install.sh / oom-protect-install.sh はこの出力を EnvironmentFile に
#   焼き込むので、サービスでも同じプロキシが効くようになる。
#
# 抽出規則:
#   - 対象はキー名が *_proxy で終わる `export KEY=VALUE` 行のみ
#     (http_proxy / https_proxy / ftp_proxy / all_proxy / no_proxy …、大小文字問わず)。
#     素の代入行 (export の無い VAR=...) は環境変数にならないので拾わない。
#   - 値の両端の対応する quote は剥がす。quote の無い値の末尾コメントは捨てる。
#   - 同一キーは最後の行が勝つ (.bashrc の後勝ちと同じ挙動)。空値は「設定なし」。
#   - 値に $VAR 展開が含まれる行は警告してスキップ (EnvironmentFile では展開されず
#     文字列のまま渡るので、そのまま焼くと壊れたプロキシ設定になる)。
#   - 出力は systemd EnvironmentFile 書式の KEY=VALUE を 1 行 1 設定。uv / pip /
#     git / requests は読み方の大小文字が違うため、検出したキーは大小両方の形で
#     書き出す (venv の python -m pip フォールバック対策)。
#   - 見つからなければ何も出力せず exit 0 (呼び出し側は空ファイルで済む)。
set -euo pipefail

files=("$@")
[[ ${#files[@]} -gt 0 ]] || files=("$HOME/.bashrc")

for f in "${files[@]}"; do
  [[ -r "$f" ]] || { echo "proxy-env-from-bashrc.sh: 読めません (スキップ): $f" >&2; continue; }
  awk '
    {
      line = $0
      sub(/\r$/, "", line)
      sub(/^[ \t]+/, "", line)
      if (line ~ /^#/ || line !~ /^export[ \t]/) next
      sub(/^export[ \t]+/, "", line)
      eq = index(line, "=")
      if (eq < 2) next
      key = substr(line, 1, eq - 1)
      val = substr(line, eq + 1)
      if (key !~ /^[A-Za-z_][A-Za-z0-9_]*$/) next
      if (tolower(key) !~ /_proxy$/) next
      key = tolower(key)
      gsub(/^[ \t]+/, "", val); gsub(/[ \t]+$/, "", val)
      c = substr(val, 1, 1)
      if (length(val) >= 2 && (c == "\042" || c == "\047") && substr(val, length(val)) == c)
        val = substr(val, 2, length(val) - 2)          # 両端 quote を剥がす
      else
        sub(/[ \t]+#.*$/, "", val)                     # quote 無し値の末尾コメントを捨てる
      if (val ~ /\$/) {                                 # $VAR 展開混じりは EnvironmentFile
        printf "proxy-env-from-bashrc.sh: %s は展開依存の値なのでスキップ: %s\n", key, val > "/dev/stderr"
        next                                            # では展開されず壊れた値になるので
      }
      if (val == "") { delete seen[key]; next }        # 空値 = 設定なし
      seen[key] = val
    }
    END {
      # 検出したキーは大小両形で書く (EnvironmentFile は後勝ちなので重複は最後が効く)
      for (k in seen) {
        print k "=" seen[k]
        print toupper(k) "=" seen[k]
      }
    }
  ' "$f"
done
