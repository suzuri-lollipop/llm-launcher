"""コーディングベンチマーク: pi ハーネス (https://github.com/earendil-works/pi) に
プロファイルのサーバー (OpenAI 互換 API) でバックエンド / フロントエンドを作らせ、
実装を履歴として保存して 2 件並べて比較できる機能。

流れ:
  1. 起動プロファイルを選択 (未起動なら自動起動) → health 待ち → model 解決
  2. 実行ディレクトリ data/coding/runs/<rid>/ を用意
     - workspace/   … pi がコードを書く場所 (AGENTS.md をコピー)
     - pi-home/     … PI_CODING_AGENT_DIR (models.json で launcher のサーバーを指す)
     - sessions/    … pi のセッション JSONL (backend / frontend で同じセッションを継続)
     - logs/        … pi の JSONL 生ログ +  readable ダイジェスト + stderr
  3. pi --mode json を backend プロンプト → frontend プロンプトの順に実行
     (bubblewrap サンドボックス内。ファイルアクセスは実行ディレクトリと
      スキルフォルダのみに制限。ネットワークはモデルサーバーへ届くよう共有)
  4. 完了後 workspace を走査してファイル数 / バイト数を記録

ビューアー用:
  - preview_start: 生成アプリをサンドボックス内で起動 (PORT 環境変数でポート渡し)
    backend エントリが無ければ frontend/ を launcher が静的配信
  - app.py の /api/coding/runs/<rid>/proxy/… が 127.0.0.1:<port> へ中継
    (LAN のスマホからも iframe で見えるように launcher 同一オリジン経由)

デフォルト資材 (data/coding/defaults/): スキル群 / AGENTS.md / プロンプト。
無いときはこのモジュールの定数から自動生成する (gitignore された data/ のため
コード側が単一の源)。"""

from __future__ import annotations

import asyncio
import contextlib
import json
import mimetypes
import os
import re
import shutil
import signal
import socket
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .backends import api_key_from_argv
from .process import VIA_CODING
from .store import JsonStore, new_id, now

MAX_RUNS = 50            # 履歴として保持する最大件数 (完了分のみ整理)
SAVE_THROTTLE = 2.0      # 進行状況の永続化間隔 (秒)
PI_PKG = "@earendil-works/pi-coding-agent"
PI_PROVIDER = "launcher"  # models.json に作るプロバイダ ID
FILE_SCAN_MAX = 4000      # workspace 走査のファイル数上限
FILE_READ_MAX = 512 * 1024        # コードビューアーの 1 ファイル読み込み上限
LOG_TAIL_MAX = 64 * 1024          # ライブログ API が返す末尾バイト数
TRANSCRIPT_MAX_BYTES = 4 * 1024 * 1024  # 出力履歴 API が返す本文の総上限
PROXY_BODY_MAX = 32 * 1024 * 1024  # プレビュー中継の応答上限
SKIP_DIRS = {"node_modules", ".git", "__pycache__", ".venv", ".pi", ".pytest_cache"}
PREVIEW_CANDIDATES = ("backend/server.py", "backend/app.py", "backend/main.py",
                      "backend/run.py", "server.py", "app.py", "main.py", "run.py")
PREVIEW_READY_TIMEOUT = 20.0

# ------------------------------------------------------------------ デフォルト資材

DEFAULT_BACKEND_PROMPT = """\
タスク管理アプリのバックエンドを backend/ ディレクトリに作成してください。

要件:
- Python 3 の標準ライブラリのみ (外部パッケージ・pip install・ネットワークアクセス禁止)
- エントリポイントは backend/server.py。127.0.0.1 で listen し、ポートは環境変数 PORT (既定 8000)
- REST API (JSON):
  - GET /api/tasks → タスク一覧
  - POST /api/tasks {"title": "..."} → 作成 (id, title, done, created_at を持つ)
  - PATCH /api/tasks/{id} {"title"?, "done"?} → 更新
  - DELETE /api/tasks/{id} → 削除
  - GET /api/health → {"ok": true}
- データは backend/data.json に永続化 (複数リクエストで壊れないように)
- 静的ファイルも配信する: / は ../frontend/index.html、/style.css 等は ../frontend/ から
  (正しい Content-Type で。パストラバーサル対策を入れる)
- アプリ内の URL はすべて先頭スラッシュ無しの相対パス (URL プレフィックス付きで配信される可能性がある)
- 完成したら自分で動作確認 (サーバー起動 → curl か python で API 確認 → 停止) してから終える
"""

DEFAULT_FRONTEND_PROMPT = """\
タスク管理アプリのフロントエンドを frontend/ ディレクトリに作成してください。
バックエンド (backend/server.py) は実装済みで、frontend/ を静的配信します。

要件:
- index.html / style.css / app.js (vanilla JS。フレームワーク・外部 CDN 禁止)
- fetch やリンクはすべて先頭スラッシュ無しの相対パス (例: fetch("api/tasks"))
- 機能: タスク一覧表示、追加フォーム、完了トグル (チェックボックス)、削除ボタン、残り件数表示
- 読みやすく綺麗な UI (CSS 変数、レスポンシブ。ダークモード対応があると望ましい)
- fetch 失敗時は画面にエラーメッセージを出す
- 完成したら自分でバックエンドを起動して画面と API の連携を確認 (確認後は停止) してから終える
"""

DEFAULT_AGENTS_MD = """\
# AGENTS.md — コーディングベンチマーク ワークスペースルール

## レイアウト
- backend/  … バックエンド (Python 3 標準ライブラリのみ。エントリは backend/server.py)
- frontend/ … フロントエンド (静的 HTML/CSS/JS。バックエンドが配信する)

## 絶対ルール
- ネットアクセス禁止。pip install / npm install / CDN 読み込みをしない
- URL はすべて相対パス (先頭スラッシュ無し)。URL プレフィックス付きで配信される可能性がある
- サーバーは 127.0.0.1 で listen。ポートは環境変数 PORT (既定 8000)
- データは workspace 内のファイルに永続化する
- workspace の外にファイルを作らない・変更しない
- 完了前に自分で動作確認をする (サーバー起動 → 確認 → 停止)

## 品質
- シンプルで読みやすいコード。コメントは日本語でよい
- エラーハンドリング (不正入力 → 400、存在しない → 404)
- 依存関係を増やさない。標準ライブラリと vanilla JS で完結させる
"""

DEFAULT_SKILLS: dict[str, str] = {
    "python-stdlib-backend": """\
---
name: python-stdlib-backend
description: Python 標準ライブラリだけで JSON REST API と静的ファイル配信サーバーを作る。バックエンド実装・server.py・http.server・JSON 永続化のときに使う。
---

# Python 標準ライブラリ バックエンド

外部パッケージ禁止の環境で HTTP サーバーを作るときの型。

## 構成
- `http.server.ThreadingHTTPServer` + `BaseHTTPRequestHandler` を使う
- ポートは `int(os.environ.get("PORT", "8000"))`、ホストは `127.0.0.1`
- ルーティングは `self.path` を `urllib.parse.urlsplit` してパス部分で行う
  (クエリ文字列をパス比較に混ぜない)

## JSON API
- 読み込み: `int(self.headers.get("Content-Length", 0))` 分だけ `self.rfile.read`
- 応答: `self.send_response(code)` → `Content-Type: application/json; charset=utf-8` →
  `Content-Length` → `end_headers()` → `self.wfile.write(json.dumps(...).encode())`
- 不正 JSON / 必須欠落は 400、存在しない id は 404
- データ永続化は `threading.Lock` で囲んで JSON ファイルへ `json.dump`
  (書き込みは tempfile + os.replace でアトミックに)

## 静的配信
- `mimetypes.guess_type` で Content-Type を決める (不明は application/octet-stream)
- パスは `Path(base, rel).resolve()` して `base` の内側か確認 (トラバーサル対策)
- ディレクトリなら index.html を返す

## 動作確認
- `PORT=<空きポート> python3 backend/server.py &` で起動し、curl か urllib で確認したら必ず kill
""",
    "vanilla-frontend": """\
---
name: vanilla-frontend
description: フレームワーク無しの HTML/CSS/JS でフロントエンドを作る。index.html・fetch・相対パス・レスポンシブ UI のときに使う。
---

# Vanilla フロントエンド

框架・CDN 禁止の環境でフロントエンドを作るときの型。

## 構成
- index.html / style.css / app.js の 3 ファイルを基本にする
- `<script src="app.js" defer>`、`<link rel="stylesheet" href="style.css">`
  (すべて相対パス。先頭スラッシュを付けない)

## API 呼び出し
- fetch も相対パス: `fetch("api/tasks")`、`fetch("api/tasks/" + id, {method: "PATCH", ...})`
- ラッパーを 1 つ作る: 応答 !ok のとき本文を読んで Error を投げ、画面にメッセージ表示
- 送信系は `Content-Type: application/json` を付ける

## UI
- セマンティック HTML (header/main/ul/form/button)
- CSS 変数で色を管理し `@media (prefers-color-scheme: dark)` に対応
- レスポンシブ: 幅 600px 前後で 1 カラムに落ちる
- ボタンはキーボード操作可能・aria-label を付ける
- 状態変更 (追加/完了/削除) 後は一覧を再取得して描画し直す (単純に保つ)
""",
    "self-check": """\
---
name: self-check
description: 実装の完了前に自分で動作確認をする手順。サーバー起動・API 確認・静的配信確認・停止までの自己検証に使う。
---

# 自己検証チェックリスト

完了を宣言する前に必ず実施する。

1. 空きポートを探す: `python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1]);s.close()'`
2. サーバー起動: `PORT=<ポート> python3 backend/server.py > /tmp/server.log 2>&1 &`
   (サンドボックス内では /tmp が使える)
3. 起動待ち (最大 10 秒): health エンドポイントを urllib でポーリング
4. API 確認: 作成 → 一覧 → 更新 → 削除 の順に curl か python で叩き、期待レスポンスか確認
5. 静的配信確認: / が HTML を返すか、Content-Type が正しいか、`/../` が拒否されるか
6. サーバーを必ず kill する (`kill %1` または pid 指定)。ログにエラーが無いか確認
7. 失敗したら修正して 1 からやり直す。確認せずに完了としない
""",
}


# pi に持たせる DeepSeek Harness 風のファイルツール群 (拡張 = TypeScript)。
# pi 組み込みの read / edit / write / grep を同名登録で置き換え、glob を追加する
# (pi は拡張ツールを組み込みより優先する)。意味論は DSH に合わせる:
#   read  … 行番号付き / offset・limit (既定・上限 2000 行) / 1 行 2000 文字・
#           出力 50KB の打ち切り / 続きは offset で
#   edit  … リテラル置換 / replace_all=false のとき old_string はちょうど 1 回
#   write … 全文書き込み (親ディレクトリ自動作成)
#   grep  … 正規表現検索 / ファイルごとにグルーピング + 行番号 / 250 件上限
#   glob  … パターンでファイル探索 / 更新時刻順 / 100 件上限
# bash は pi 組み込みのまま (自己検証のサーバー起動等に必要)。
# typebox / pi-coding-agent の import は pi のローダが本体側へ alias するので
# サンドボックス内に node_modules を持ち込む必要は無い。
DEFAULT_PI_EXTENSION = r'''/**
 * dsh-tools: DeepSeek Harness 風のファイル編集 / 検索 / 閲覧ツールを pi に持たせる。
 * read / edit / write / grep は pi 組み込みを同名で置き換え、glob を追加する。
 * (launcher が生成する既定資材。data/coding/defaults/extensions/dsh-tools.ts)
 */
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { withFileMutationQueue } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import * as fs from "node:fs/promises";
import * as path from "node:path";

const READ_LIMIT = 2000;          // 1 回に返す行数の既定かつ上限
const READ_MAX_LINE = 2000;       // 1 行の最大文字数
const READ_MAX_BYTES = 50 * 1024; // 出力ウィンドウの最大バイト数
const GREP_MAX_MATCHES = 250;     // インラインに残すマッチ数
const GREP_MAX_LINE_BYTES = 2000; // マッチ行プレビューの最大バイト数
const GREP_SCAN_BUDGET = 10000;   // 走査の打ち切り (これ以上は数えない)
const GLOB_MAX_RESULTS = 100;     // glob が返すパス数
const FILE_READ_MAX = 8 * 1024 * 1024; // grep / glob 走査で読むファイル上限
const SKIP_DIRS = new Set(["node_modules", ".git", "__pycache__", ".venv", ".pi", ".pytest_cache"]);

/** モデルが @ 付きで渡してくるパスを正規化し、cwd 基準で解決する。 */
function resolvePath(cwd: string, p: string): string {
  const cleaned = String(p ?? "").trim().replace(/^@+/, "");
  if (!cleaned) throw new Error("path must be a non-empty string");
  return path.resolve(cwd, cleaned);
}

function truncateLine(line: string): string {
  return line.length > READ_MAX_LINE
    ? line.substring(0, READ_MAX_LINE) + `... (line truncated to ${READ_MAX_LINE} chars)`
    : line;
}

function previewLine(line: string): string {
  if (Buffer.byteLength(line, "utf8") <= GREP_MAX_LINE_BYTES) return line;
  return Buffer.from(line, "utf8").subarray(0, GREP_MAX_LINE_BYTES).toString("utf8") + "… (line truncated)";
}

/** glob → 正規表現 (** / * / ? / {a,b} / [abc] 対応)。 */
function globToRegex(pat: string): RegExp {
  let re = "";
  let i = 0;
  while (i < pat.length) {
    const c = pat[i];
    if (c === "*") {
      if (pat[i + 1] === "*") {
        if (pat[i + 2] === "/") { re += "(?:.*/)?"; i += 3; }
        else { re += ".*"; i += 2; }
      } else { re += "[^/]*"; i++; }
    } else if (c === "?") { re += "[^/]"; i++; }
    else if (c === "{") {
      let depth = 1, j = i + 1, cur = "";
      const parts: string[] = [];
      while (j < pat.length && depth > 0) {
        const d = pat[j];
        if (d === "{") depth++;
        else if (d === "}") { depth--; if (depth === 0) break; }
        if (depth === 1 && d === ",") { parts.push(cur); cur = ""; }
        else cur += d;
        j++;
      }
      parts.push(cur);
      re += "(?:" + parts.map((p) => globToRegex(p).source).join("|") + ")";
      i = j + 1;
    } else if (c === "[") {
      let j = i + 1, cls = "[";
      if (pat[j] === "!" || pat[j] === "^") { cls += "^"; j++; }
      while (j < pat.length && pat[j] !== "]") { cls += pat[j]; j++; }
      re += cls + "]";
      i = j + 1;
    } else {
      re += c.replace(/[.+^${}()|[\]\\]/g, "\\$&");
      i++;
    }
  }
  return new RegExp("^" + re + "$");
}

/** include / glob のマッチ: "/" を含まないパターンは任意深さの basename に効く。 */
function globMatch(pat: string, rel: string): boolean {
  const rx = globToRegex(pat);
  const target = pat.includes("/") ? rel.split(path.sep).join("/") : path.basename(rel);
  return rx.test(target);
}

async function* walkFiles(root: string): AsyncGenerator<{ abs: string; rel: string }> {
  const stack = [root];
  while (stack.length) {
    const dir = stack.pop()!;
    let entries;
    try { entries = await fs.readdir(dir, { withFileTypes: true }); }
    catch { continue; }
    for (const e of entries) {
      const abs = path.join(dir, e.name);
      if (e.isDirectory()) { if (!SKIP_DIRS.has(e.name)) stack.push(abs); }
      else if (e.isFile()) yield { abs, rel: path.relative(root, abs) };
    }
  }
}

function textResult(text: string) {
  return { content: [{ type: "text" as const, text }], details: {} };
}

export default function (pi: ExtensionAPI) {
  /* ------------------------------------------------------------ read (閲覧) */
  pi.registerTool({
    name: "read",
    label: "Read",
    description: "Read a UTF-8 text file and return line-numbered content. "
      + "Use offset and limit to read a line range or continue a large file.",
    promptSnippet: "read: Read a UTF-8 text file with line numbers (offset/limit)",
    promptGuidelines: [
      "Use the read tool — not shell commands like cat, sed, head, or tail — to inspect text files. "
      + "Use offset and limit to read a line range or continue a large file.",
    ],
    parameters: Type.Object({
      file_path: Type.String({ description: "Path to read (absolute or relative to the working directory)." }),
      offset: Type.Optional(Type.Number({ description: "1-based first line to return. Defaults to 1." })),
      limit: Type.Optional(Type.Number({ description: `Maximum number of lines to return. Defaults to ${READ_LIMIT}.` })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const p = resolvePath(ctx.cwd, params.file_path);
      const offset = params.offset ?? 1;
      const limit = params.limit ?? READ_LIMIT;
      for (const [v, name] of [[offset, "offset"], [limit, "limit"]] as const) {
        if (!Number.isInteger(v) || v < 1) throw new Error(`${name} must be a positive integer`);
      }
      if (limit > READ_LIMIT) throw new Error(`limit must be less than or equal to ${READ_LIMIT}`);
      const st = await fs.stat(p).catch(() => null);
      if (!st) throw new Error(`File not found: ${p}`);
      if (st.isDirectory()) throw new Error(`${p} is a directory, not a file`);
      const raw = await fs.readFile(p, "utf8");
      if (raw.includes("\0")) throw new Error(`Cannot read as text (binary file): ${p}`);
      let lines = raw.length ? raw.split("\n") : [];
      if (raw.endsWith("\n")) lines = lines.slice(0, -1);
      const totalLines = lines.length;
      if (offset > totalLines && !(totalLines === 0 && offset === 1)) {
        throw new Error(`offset ${offset} is out of range for "${p}" (${totalLines} lines)`);
      }
      const out: string[] = [];
      let bytes = 0, capped = false, endLine = offset - 1;
      for (let n = offset; n <= Math.min(totalLines, offset + limit - 1); n++) {
        let text = lines[n - 1];
        if (text.endsWith("\r")) text = text.slice(0, -1);
        text = truncateLine(text);
        const size = Buffer.byteLength(text, "utf8") + (out.length ? 1 : 0);
        if (bytes + size > READ_MAX_BYTES) { capped = true; break; }
        bytes += size;
        out.push(`${n}: ${text}`);
        endLine = n;
      }
      const footer = capped
        ? `(Output capped. Showing lines ${offset}-${endLine}. Use offset=${endLine + 1} to continue.)`
        : endLine < totalLines
          ? `(Showing lines ${offset}-${endLine} of ${totalLines}. Use offset=${endLine + 1} to continue.)`
          : `(End of file - total ${totalLines} lines)`;
      const body = out.length ? out.join("\n") + "\n\n" + footer : footer;
      return textResult(`<path>${p}</path>\n<type>file</type>\n<content>\n${body}\n</content>`);
    },
  });

  /* ------------------------------------------------------------ edit (編集) */
  pi.registerTool({
    name: "edit",
    label: "Edit",
    description: "Edit an existing UTF-8 text file by replacing literal text. "
      + "When replace_all is false (default), old_string must appear exactly once.",
    promptSnippet: "edit: Replace literal text in a file (old_string/new_string, replace_all)",
    promptGuidelines: [
      "Use the edit tool for targeted changes to existing files. old_string must match the file "
      + "exactly, including whitespace; read the file first if unsure. When replace_all is false, "
      + "old_string must appear exactly once — add surrounding context to make it unique.",
    ],
    parameters: Type.Object({
      file_path: Type.String({ description: "Path to edit (absolute or relative to the working directory)." }),
      old_string: Type.String({ description: "Literal text to replace." }),
      new_string: Type.String({ description: "Literal replacement text. Use an empty string to delete the match." }),
      replace_all: Type.Optional(Type.Boolean({ description: "Replace all matches. Defaults to false; when false, old_string must appear exactly once." })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const p = resolvePath(ctx.cwd, params.file_path);
      const oldS = params.old_string, newS = params.new_string;
      const all = params.replace_all ?? false;
      if (!oldS.length) throw new Error("old_string must be a non-empty string");
      if (oldS === newS) throw new Error("old_string and new_string must differ");
      return withFileMutationQueue(p, async () => {
        const content = await fs.readFile(p, "utf8").catch(() => null);
        if (content === null) throw new Error(`File not found: ${p}. Use write to create new files.`);
        const count = content.split(oldS).length - 1;
        if (count === 0) {
          throw new Error(`old_string not found in ${p}. Read the file and copy the exact text (including whitespace and indentation).`);
        }
        if (!all && count > 1) {
          throw new Error(`old_string appears ${count} times in ${p}; when replace_all is false it must appear exactly once. `
            + "Add surrounding context to make it unique, or set replace_all=true.");
        }
        const next = all ? content.split(oldS).join(newS) : content.replace(oldS, newS);
        await fs.writeFile(p, next, "utf8");
        const n = all ? count : 1;
        return textResult(`The file ${p} has been updated (${n} replacement${n === 1 ? "" : "s"}).`);
      });
    },
  });

  /* ------------------------------------------------------------ write (作成 / 全置換) */
  pi.registerTool({
    name: "write",
    label: "Write",
    description: "Create or fully replace a UTF-8 text file. Parent directories are created automatically.",
    promptSnippet: "write: Create or fully replace a text file (file_path, content)",
    promptGuidelines: [
      "Use the write tool to create new files or fully replace a file's content; "
      + "prefer the edit tool for small changes to existing files.",
    ],
    parameters: Type.Object({
      file_path: Type.String({ description: "Path to write (absolute or relative to the working directory)." }),
      content: Type.String({ description: "Full UTF-8 text content to write." }),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      const p = resolvePath(ctx.cwd, params.file_path);
      return withFileMutationQueue(p, async () => {
        await fs.mkdir(path.dirname(p), { recursive: true });
        await fs.writeFile(p, params.content, "utf8");
        return textResult(`Wrote ${Buffer.byteLength(params.content, "utf8")} bytes to ${p}.`);
      });
    },
  });

  /* ------------------------------------------------------------ grep (内容検索) */
  pi.registerTool({
    name: "grep",
    label: "Grep",
    description: "Search file contents with a regular expression (ripgrep-style syntax). "
      + "Returns matching lines with line numbers, grouped by file. "
      + "Use include (one glob like \"*.py\" or \"*.{ts,tsx}\") to filter files.",
    promptSnippet: "grep: Search file contents by regex (pattern, path, include)",
    promptGuidelines: [
      "Use the grep tool to search file contents by regular expression instead of bash grep/rg. "
      + "include must be one positive glob (use {a,b} alternation, not a comma-separated list).",
    ],
    parameters: Type.Object({
      pattern: Type.String({ description: "Regular expression to search for (ripgrep syntax)." }),
      path: Type.Optional(Type.String({ description: "File or directory to search in. Defaults to the working directory." })),
      include: Type.Optional(Type.String({ description: "One glob filter for which files to search (e.g. \"*.ts\"). Negation is not supported." })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      if (!params.pattern.length) throw new Error("pattern must be a non-empty string");
      const include = params.include === "" ? undefined : params.include;
      if (include !== undefined) {
        if (!include.trim().length) throw new Error("include must be a non-empty glob when given");
        if (include.startsWith("!")) throw new Error('include must be a positive glob filter; negated patterns ("!…") are not supported');
        let depth = 0;
        for (const ch of include) {
          if (ch === "{") depth++;
          else if (ch === "}") depth = Math.max(0, depth - 1);
          else if (ch === "," && depth === 0) {
            throw new Error("include must be one glob, not a comma-separated list (use {a,b} alternation instead)");
          }
        }
      }
      let rx: RegExp;
      try { rx = new RegExp(params.pattern); }
      catch (e) { throw new Error(`Invalid regular expression: ${(e as Error).message}`); }
      const base = params.path ? resolvePath(ctx.cwd, params.path) : ctx.cwd;
      const st = await fs.stat(base).catch(() => null);
      if (!st) throw new Error(`Path not found: ${base}`);
      const files: { abs: string; rel: string }[] = [];
      if (st.isFile()) files.push({ abs: base, rel: path.basename(base) });
      else for await (const f of walkFiles(base)) files.push(f);
      const groups = new Map<string, string[]>();
      let seen = 0, kept = 0, budgetHit = false;
      outer:
      for (const f of files) {
        if (include !== undefined && !globMatch(include, f.rel)) continue;
        let raw: string;
        try {
          const fst = await fs.stat(f.abs);
          if (fst.size > FILE_READ_MAX) continue;
          const buf = await fs.readFile(f.abs);
          if (buf.subarray(0, 8192).includes(0)) continue;   // バイナリはスキップ
          raw = buf.toString("utf8");
        } catch { continue; }
        const lines = raw.split("\n");
        const rel = path.relative(ctx.cwd, f.abs);
        for (let i = 0; i < lines.length; i++) {
          if (!rx.test(lines[i])) continue;
          seen++;
          if (seen > GREP_SCAN_BUDGET) { budgetHit = true; break outer; }
          if (kept < GREP_MAX_MATCHES) {
            kept++;
            let line = lines[i];
            if (line.endsWith("\r")) line = line.slice(0, -1);
            const g = groups.get(rel) ?? [];
            g.push(`Line ${i + 1}: ${previewLine(line)}`);
            groups.set(rel, g);
          }
        }
      }
      if (!seen) return textResult("No matches found");
      const body = [...groups.entries()].map(([p, ls]) => `${p}\n${ls.join("\n")}`).join("\n\n");
      const header = kept < seen || budgetHit
        ? `Found ${kept} of ${budgetHit ? GREP_SCAN_BUDGET + "+" : seen} matches`
        : `Found ${seen} ${seen === 1 ? "match" : "matches"}`;
      const footer = kept < seen || budgetHit
        ? "\n\n(Narrow pattern, path, or include to see more.)" : "";
      return textResult(`${header}\n\n${body}${footer}`);
    },
  });

  /* ------------------------------------------------------------ glob (パス探索) */
  pi.registerTool({
    name: "glob",
    label: "Glob",
    description: "Find files whose paths match a glob pattern (e.g. \"**/*.ts\", \"src/**/*.py\"). "
      + "A pattern with no \"/\" matches the basename at any depth. "
      + `Returns up to ${GLOB_MAX_RESULTS} file paths sorted by modification time (newest first).`,
    promptSnippet: "glob: Find files by path pattern, sorted by modification time",
    promptGuidelines: [
      "Use the glob tool to discover files by path pattern instead of bash find or ls. "
      + "Patterns support **, *, ?, {a,b} alternation and [abc] classes.",
    ],
    parameters: Type.Object({
      pattern: Type.String({ description: "Glob pattern to match file paths against (e.g. \"**/*.ts\")." }),
      path: Type.Optional(Type.String({ description: "Directory to search in. Defaults to the working directory." })),
    }),
    async execute(_toolCallId, params, _signal, _onUpdate, ctx) {
      if (!params.pattern.length) throw new Error("pattern must be a non-empty string");
      const base = params.path ? resolvePath(ctx.cwd, params.path) : ctx.cwd;
      const st = await fs.stat(base).catch(() => null);
      if (!st || !st.isDirectory()) throw new Error(`Directory not found: ${base}`);
      const found: { rel: string; mtime: number }[] = [];
      for await (const f of walkFiles(base)) {
        if (!globMatch(params.pattern, f.rel)) continue;
        try { found.push({ rel: f.rel, mtime: (await fs.stat(f.abs)).mtimeMs }); }
        catch { /* 消えたファイルは無視 */ }
      }
      if (!found.length) return textResult(`No files found matching pattern: ${params.pattern}`);
      found.sort((a, b) => b.mtime - a.mtime);
      const shown = found.slice(0, GLOB_MAX_RESULTS).map((f) => f.rel);
      const footer = found.length > shown.length
        ? `\n\n(Showing ${shown.length} of ${found.length} paths in modification-time order. Narrow the pattern to see more.)`
        : "";
      return textResult(shown.join("\n") + footer);
    },
  });
}
'''


def _skill_meta(text: str) -> tuple[str, str]:
    """SKILL.md の frontmatter から name / description を取り出す (簡易パーサ)。"""
    name = desc = ""
    m = re.match(r"^---\s*\n(.*?)\n---", text, re.DOTALL)
    if m:
        for line in m.group(1).splitlines():
            if line.startswith("name:"):
                name = line.split(":", 1)[1].strip()
            elif line.startswith("description:"):
                desc = line.split(":", 1)[1].strip()
    return name, desc


class CodingError(Exception):
    """ユーザーに提示するコーディングベンチマーク失敗。"""


class _Cancelled(Exception):
    """キャンセル検出 (内部シグナル)。"""


# ------------------------------------------------------------------ デフォルト資材の管理


class CodingAssets:
    """data/coding/defaults/ の生成と参照。

    スキル群フォルダ / AGENTS.md / プロンプトの既定値をコードから生成する
    (data/ は gitignore のためリポジトリには置かない)。ユーザーがファイルを
    編集しても上書きしない (無いときだけ作る)。"""

    def __init__(self, root: Path):
        self.base = root / "data" / "coding"
        self.defaults_dir = self.base / "defaults"
        self.runs_dir = self.base / "runs"
        self.skills_dir = self.defaults_dir / "skills"
        self.agents_path = self.defaults_dir / "AGENTS.md"
        self.prompts_path = self.defaults_dir / "prompts.json"
        self.extension_path = self.defaults_dir / "extensions" / "dsh-tools.ts"

    def ensure_defaults(self) -> None:
        self.skills_dir.mkdir(parents=True, exist_ok=True)
        for name, text in DEFAULT_SKILLS.items():
            p = self.skills_dir / name / "SKILL.md"
            if not p.exists():
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_text(text, encoding="utf-8")
        if not self.agents_path.exists():
            self.agents_path.write_text(DEFAULT_AGENTS_MD, encoding="utf-8")
        if not self.prompts_path.exists():
            self.prompts_path.write_text(json.dumps(
                {"backend_prompt": DEFAULT_BACKEND_PROMPT,
                 "frontend_prompt": DEFAULT_FRONTEND_PROMPT},
                ensure_ascii=False, indent=2), encoding="utf-8")
        if not self.extension_path.exists():
            self.extension_path.parent.mkdir(parents=True, exist_ok=True)
            self.extension_path.write_text(DEFAULT_PI_EXTENSION, encoding="utf-8")

    def default_prompts(self) -> dict[str, str]:
        self.ensure_defaults()
        try:
            data = json.loads(self.prompts_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            data = {}
        return {
            "backend_prompt": str(data.get("backend_prompt") or DEFAULT_BACKEND_PROMPT),
            "frontend_prompt": str(data.get("frontend_prompt") or DEFAULT_FRONTEND_PROMPT),
        }

    def list_skills(self, folder: Path | None = None) -> list[dict[str, Any]]:
        """スキルフォルダ内の SKILL.md を再帰的に列挙 (name / description 付き)。"""
        folder = folder or self.skills_dir
        out: list[dict[str, Any]] = []
        if not folder.is_dir():
            return out
        for p in sorted(folder.rglob("SKILL.md")):
            if any(part in SKIP_DIRS for part in p.parts):
                continue
            try:
                text = p.read_text("utf-8")
            except OSError:
                continue
            name, desc = _skill_meta(text)
            out.append({"name": name or p.parent.name, "description": desc[:200],
                        "dir": str(p.parent)})
            if len(out) >= 200:
                break
        return out

    def defaults_info(self) -> dict[str, Any]:
        prompts = self.default_prompts()
        return {
            "skills_dir": str(self.skills_dir),
            "agents_path": str(self.agents_path),
            "extension_path": str(self.extension_path),
            "backend_prompt": prompts["backend_prompt"],
            "frontend_prompt": prompts["frontend_prompt"],
            "skills": self.list_skills(),
        }


# ------------------------------------------------------------------ pi ハーネス


class PiTool:
    """pi CLI の解決・バージョン確認・npm インストール。

    解決順: settings.json coding.pi_command (明示指定) → PATH 上の pi →
    npm グローバル bin の候補。インストールは
    `npm install -g --ignore-scripts @earendil-works/pi-coding-agent`
    (pi 公式 README の npm 手順。ライフサイクルスクリプトは実行しない)。"""

    VERSION_TTL = 60.0

    def __init__(self, settings_store: JsonStore, log_path: Path):
        self.settings = settings_store
        self.log_path = log_path
        self._version_cache: tuple[float, str] | None = None
        self.install_state: dict[str, Any] = {"status": "idle", "log": "",
                                              "started_at": None, "finished_at": None}
        self._install_task: asyncio.Task | None = None

    def command(self) -> str:
        override = str((self.settings.load().get("coding") or {}).get("pi_command") or "").strip()
        if override:
            return override
        found = shutil.which("pi")
        if found:
            return found
        # PATH に乗っていない環境向けの候補。pi.dev のインストールスクリプトは
        # ~/.local/bin/pi/ に一式を置き、その中の pi が実行本体 (ディレクトリなので
        # which では見つからない)。npm グローバルは /usr/local/bin 等。
        for cand in (str(Path.home() / ".local" / "bin" / "pi" / "pi"),
                     "/usr/local/bin/pi", str(Path.home() / ".local" / "bin" / "pi"),
                     str(Path.home() / ".npm-global" / "bin" / "pi")):
            p = Path(cand)
            if p.is_file() and os.access(p, os.X_OK):
                return cand
        return ""

    def available(self) -> bool:
        cmd = self.command()
        return bool(cmd) and (Path(cmd).exists() or shutil.which(cmd) is not None)

    async def version(self, force: bool = False) -> str:
        if not force and self._version_cache and time.monotonic() - self._version_cache[0] < self.VERSION_TTL:
            return self._version_cache[1]
        cmd = self.command()
        if not cmd:
            return ""
        try:
            proc = await asyncio.create_subprocess_exec(
                cmd, "--version", stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL)
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=30)
            ver = out.decode("utf-8", "replace").strip().splitlines()[-1] if out.strip() else ""
        except Exception:  # noqa: BLE001 — 検出失敗は「未導入」と同じ扱い
            ver = ""
        self._version_cache = (time.monotonic(), ver)
        return ver

    async def info(self) -> dict[str, Any]:
        return {"available": self.available(), "command": self.command(),
                "version": await self.version(), "package": PI_PKG,
                "install": dict(self.install_state)}

    # ------------------------------------------------------------ npm install

    def start_install(self) -> dict[str, Any]:
        if self.install_state["status"] == "running":
            raise CodingError("pi のインストールが実行中です")
        npm = shutil.which("npm")
        if not npm:
            raise CodingError("npm が見つかりません。Node.js 22.19+ を導入してください "
                              "(pi のインストールに必要)")
        self.install_state = {"status": "running", "log": "",
                              "started_at": now(), "finished_at": None}
        self._install_task = asyncio.create_task(self._install_job(npm))
        return dict(self.install_state)

    async def _install_job(self, npm: str) -> None:
        lines: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                npm, "install", "-g", "--ignore-scripts", PI_PKG,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
            assert proc.stdout is not None
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                text = line.decode("utf-8", "replace").rstrip()
                lines.append(text)
                self.install_state["log"] = "\n".join(lines[-60:])
            rc = await proc.wait()
            self.install_state["status"] = "completed" if rc == 0 else "failed"
            if rc != 0:
                self.install_state["log"] += f"\n[npm exit {rc}]"
        except Exception as e:  # noqa: BLE001
            self.install_state["status"] = "failed"
            self.install_state["log"] += f"\n{type(e).__name__}: {e}"
        finally:
            self.install_state["finished_at"] = now()
            self._version_cache = None
            with contextlib.suppress(OSError):
                self.log_path.parent.mkdir(parents=True, exist_ok=True)
                self.log_path.write_text(self.install_state["log"], encoding="utf-8")


# ------------------------------------------------------------------ サンドボックス
# 2 バックエンド: bubblewrap (軽量・userns が許可されている環境) と Docker
# (userns が制限されている環境向け。pi 公式もコンテナ化を推奨)。
# 自動選択: bwrap が「実際に動く」なら bwrap、駄目なら docker、どちらも駄目なら
# サンドボックス ON の実行を拒否する (OFF 明示時のみ隔離なしで実行)。

SANDBOX_IMAGE_DEFAULT = "llm-launcher/coding-sandbox:1"   # python3 + curl + git + procps
SANDBOX_IMAGE_FALLBACK = "python:3.12-slim"               # 未ビルド時のフォールバック (python3 は在る)
CONTAINER_PREFIX = "llmcb-"
_probe_cache: dict[str, Any] = {}


def bwrap_available() -> bool:
    return shutil.which("bwrap") is not None


def bwrap_works() -> bool:
    """bwrap が実際に namespace を作れるか (Ubuntu 26.04 等は非特権 userns を
    AppArmor で制限しており、バイナリがあっても動かないことがある)。"""
    if not bwrap_available():
        return False
    if "bwrap" not in _probe_cache:
        try:
            r = subprocess.run(
                ["bwrap", "--ro-bind", "/usr", "/usr", "--dev", "/dev",
                 "--proc", "/proc", "true"],
                capture_output=True, timeout=10)
            _probe_cache["bwrap"] = r.returncode == 0
        except Exception:  # noqa: BLE001
            _probe_cache["bwrap"] = False
    return bool(_probe_cache["bwrap"])


def docker_available() -> bool:
    return shutil.which("docker") is not None


def docker_works() -> bool:
    if not docker_available():
        return False
    if "docker" not in _probe_cache:
        try:
            r = subprocess.run(["docker", "info", "--format", "{{.ServerVersion}}"],
                               capture_output=True, timeout=15)
            _probe_cache["docker"] = r.returncode == 0
        except Exception:  # noqa: BLE001
            _probe_cache["docker"] = False
    return bool(_probe_cache["docker"])


def sandbox_image(settings: dict[str, Any] | None = None) -> str:
    return str(((settings or {}).get("coding") or {}).get("sandbox_image")
               or "").strip() or SANDBOX_IMAGE_DEFAULT


def effective_sandbox_image(settings: dict[str, Any] | None = None) -> str:
    """実際に使うイメージ。既定 (coding-sandbox) が未ビルドなら python:3.12-slim へ
    フォールバックする (pi の mount はパッケージ単体なので python3 は消えない)。
    明示指定 (coding.sandbox_image) が無ければ従来どおり未取得 = 起動拒否 + hint。"""
    img = sandbox_image(settings)
    if docker_image_present(img):
        return img
    explicit = str(((settings or {}).get("coding") or {}).get("sandbox_image")
                   or "").strip()
    if not explicit and docker_image_present(SANDBOX_IMAGE_FALLBACK):
        return SANDBOX_IMAGE_FALLBACK
    return img


def docker_image_present(image: str) -> bool:
    key = f"image:{image}"
    if key not in _probe_cache:
        try:
            r = subprocess.run(["docker", "image", "inspect", image],
                               capture_output=True, timeout=15)
            _probe_cache[key] = r.returncode == 0
        except Exception:  # noqa: BLE001
            _probe_cache[key] = False
    return bool(_probe_cache[key])


def sandbox_backend(settings: dict[str, Any] | None = None) -> str:
    """使えるサンドバックエンドを自動選択 ("bwrap" | "docker" | "")。"""
    if bwrap_works():
        return "bwrap"
    if docker_works() and docker_image_present(effective_sandbox_image(settings)):
        return "docker"
    return ""


def sandbox_info(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    backend = sandbox_backend(settings)
    img = effective_sandbox_image(settings)
    hint = ""
    if not backend:
        if docker_works() and not docker_image_present(img):
            hint = (f"./scripts/coding-sandbox-build.sh で {SANDBOX_IMAGE_DEFAULT} を"
                    f"ビルドするか docker pull {img}")
        elif bwrap_available() and not bwrap_works():
            hint = ("bwrap は userns 制限で動作しません。Docker を導入するか "
                    f"./scripts/coding-sandbox-build.sh で {SANDBOX_IMAGE_DEFAULT} を"
                    "ビルドしてください")
        else:
            hint = "sudo apt install bubblewrap / Docker のいずれかを導入してください"
    elif img == SANDBOX_IMAGE_FALLBACK:
        hint = (f"{SANDBOX_IMAGE_DEFAULT} 未ビルドのため {SANDBOX_IMAGE_FALLBACK} で実行中"
                " (curl / git は入りません)。./scripts/coding-sandbox-build.sh でビルドできます")
    return {"bwrap": bwrap_available(), "bwrap_ok": bwrap_works(),
            "docker": docker_available(), "docker_ok": docker_works(),
            "backend": backend, "image": img,
            "image_present": docker_image_present(img) if docker_works() else False,
            "hint": hint}


def _bind_args(src: Path, dst: Path, ro: bool) -> list[str]:
    """存在するパスだけ bind。シンボリックリンク (/bin → usr/bin 等) は symlink で再現。"""
    if src.is_symlink():
        return ["--symlink", os.readlink(src), str(dst)]
    if not src.exists():
        return []
    return ["--ro-bind" if ro else "--bind", str(src), str(dst)]


def sandbox_argv(run_dir: Path, cwd: Path, *, ro_binds: list[Path] | None = None) -> list[str]:
    """pi / 生成アプリを閉じ込める bwrap の argv (この後ろに実行コマンドを続ける)。

    - ホストのシステムディレクトリ (/usr /etc …) は読み取り専用で bind
    - run_dir だけ書き込み可 (workspace / pi-home / sessions / home は全部この中)
    - スキルフォルダ等は読み取り専用で追加 bind
    - PID / UTS / cgroup 名前空間を分離。--die-with-parent で launcher 終了時に道連れ
    - ネットワーク名前空間は共有 (127.0.0.1 のモデルサーバー / プレビューへ届くため)
    - HOME は run_dir 内を指す (本物の ~/.pi や ~/.ssh に触らせない)"""
    argv = ["bwrap", "--unshare-pid", "--unshare-uts", "--unshare-cgroup",
            "--die-with-parent", "--new-session", "--cap-drop", "ALL"]
    for d in ("/usr", "/etc", "/bin", "/sbin", "/lib", "/lib64", "/opt"):
        argv += _bind_args(Path(d), Path(d), ro=True)
    argv += ["--proc", "/proc", "--dev", "/dev",
             "--tmpfs", "/tmp", "--tmpfs", "/var/tmp", "--tmpfs", "/run"]
    argv += _bind_args(run_dir, run_dir, ro=False)
    for p in ro_binds or []:
        argv += _bind_args(p, p, ro=True)
    argv += ["--chdir", str(cwd)]
    return argv


def pi_runtime_mounts(pi_cmd: str) -> list[Path]:
    """docker に読み取り専用で渡す pi 実行環境のパス。

    pi.dev インストーラの standalone 配置 (~/.local/bin/pi/ 一式) はディレクトリごと。
    npm グローバル等は **パッケージ本体だけ** を bind する。/usr/local ごと bind すると
    イメージの /usr/local が上書きされ、python:3.12-slim の python3
    (/usr/local/bin/python3) が消えて「サンドボックスに Python が無い」になる
    (2026-11-24 のベンチで両モデルとも python3 を見つけられなかった実害)。
    node はホストバイナリをファイル mount する (bookworm glibc と互換)。
    ランチャ symlink は mount しない: ファイル mount だと cli.js がパッケージ外に
    単体配置され、ESM の相対 import (./chunks/...) が解決できなくなる。
    代わりに docker 側は解決済み実パス (パッケージ mount 内) を実行する。"""
    p = Path(pi_cmd).resolve()
    if p.parent.name == "pi" and (p.parent / "package.json").exists():
        return [p.parent]
    mounts: list[Path] = []
    pkg = p.parent
    while pkg != pkg.parent and not (pkg / "package.json").exists():
        pkg = pkg.parent
    mounts.append(pkg if (pkg / "package.json").exists() else p.parent)
    node = shutil.which("node")
    if node:
        np = Path(node)
        if not any(np == m or np.is_relative_to(m) for m in mounts if m.is_dir()):
            mounts.append(np)
    return mounts


def docker_argv(run_dir: Path, cwd: Path, *, ro_binds: list[Path] | None = None,
                env: dict[str, str] | None = None, image: str, name: str,
                pi_mounts: list[Path] | None = None) -> list[str]:
    """Docker サンドボックスの argv (この後ろに実行コマンドを続ける)。

    - run_dir だけ rw ボリューム。スキル / pi 実行環境は ro
    - --network host で 127.0.0.1 のモデルサーバー / プレビューポートへ届く
    - --user で launcher と同じ uid (生成ファイルの所有者を保つ)
    - --init でシグナル転送 + ゾンビ回収。--rm で終了時自動削除
    - 名前は llmcb- 接頭辞で統一 (中断 / 起動時回復で docker rm -f するため)"""
    argv = ["docker", "run", "--rm", "--init", "--name", name,
            "--network", "host", "--user", f"{os.getuid()}:{os.getgid()}",
            "-v", f"{run_dir}:{run_dir}", "-w", str(cwd)]
    for p in (ro_binds or []) + (pi_mounts or []):
        if p.exists():
            argv += ["-v", f"{p}:{p}:ro"]
    for k, v in (env or {}).items():
        argv += ["-e", f"{k}={v}"]
    argv += [image]
    return argv


def docker_rm(name: str) -> None:
    """名前指定でコンテナを強制削除 (中断・回復用。失敗は握り潰す)。"""
    with contextlib.suppress(Exception):
        subprocess.run(["docker", "rm", "-f", name],
                       capture_output=True, timeout=30)


def docker_cleanup_stale() -> None:
    """launcher 再起動後に残った llmcb- コンテナを一掃する。"""
    if not docker_works():
        return
    with contextlib.suppress(Exception):
        r = subprocess.run(["docker", "ps", "-aq", "--filter", f"name={CONTAINER_PREFIX}"],
                           capture_output=True, timeout=15)
        ids = r.stdout.split()
        if ids:
            subprocess.run(["docker", "rm", "-f", *[i.decode() for i in ids]],
                           capture_output=True, timeout=60)


def sandbox_env(home: Path, pi_dir: Path, session_dir: Path) -> dict[str, str]:
    """サンドボックス内の pi に渡す環境 (本物の HOME / 認証情報を持ち込まない)。"""
    return {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin",
        "HOME": str(home),
        "PI_CODING_AGENT_DIR": str(pi_dir),
        "PI_CODING_AGENT_SESSION_DIR": str(session_dir),
        "PI_OFFLINE": "1",             # モデルカタログ等の自動ネットアクセスを止める
        "PI_SKIP_VERSION_CHECK": "1",
        "PI_TELEMETRY": "0",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }


# ------------------------------------------------------------------ workspace 走査 / ファイル参照


def safe_join(base: Path, rel: str) -> Path:
    """base の内側に収まるパスだけ返す (トラバーサル拒否)。"""
    rel = (rel or "").strip().lstrip("/")
    if not rel:
        return base
    p = (base / rel).resolve()
    base = base.resolve()
    if p != base and not p.is_relative_to(base):
        raise CodingError("ワークスペース外のパスは参照できません")
    return p


def scan_workspace(ws: Path) -> dict[str, Any]:
    count = total = 0
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for f in filenames:
            count += 1
            with contextlib.suppress(OSError):
                total += (Path(dirpath) / f).stat().st_size
            if count >= FILE_SCAN_MAX:
                return {"count": count, "bytes": total, "truncated": True}
    return {"count": count, "bytes": total, "truncated": False}


def file_tree(ws: Path) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for dirpath, dirnames, filenames in os.walk(ws):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            p = Path(dirpath) / f
            try:
                size = p.stat().st_size
            except OSError:
                continue
            out.append({"path": str(p.relative_to(ws)), "size": size})
            if len(out) >= FILE_SCAN_MAX:
                return out
    return out


def guess_type(path: str) -> str:
    ctype, _ = mimetypes.guess_type(path)
    if path.endswith((".js", ".mjs")):
        ctype = "text/javascript"
    elif path.endswith(".md"):
        ctype = "text/markdown"
    return ctype or "application/octet-stream"


# ------------------------------------------------------------------ モデル能力の検出
# thinking は「モデルで設定できるものだけ」出すための単一の源。
# 判定材料: (1) サーバーが /v1/models で公開する effort 語彙 (あればその段階)、
# (2) プロファイルの argv に reasoning パーサが設定されているか
# (サーバーが reasoning_content を分離する = 思考対応の明示設定)、
# (3) モデル名が既知の思考ファミリーか。語彙が無ければテンプレート由来の
# ON/OFF が基本なので off/on の 2 値にする。

THINKING_NAME_RE = re.compile(
    r"qwen3|deepseek[-_]?r1|deepseek[-_]?v3|glm[-_]?4|inkling|gpt[-_]?oss", re.I)
QWEN_THINKING_RE = re.compile(r"qwen3", re.I)


def argv_flag(argv: list[str], name: str) -> str:
    """argv から --name <value> / --name=<value> の値を取る (無ければ "")。"""
    for i, tok in enumerate(argv):
        if tok == name and i + 1 < len(argv):
            return argv[i + 1]
        if tok.startswith(name + "="):
            return tok.split("=", 1)[1]
    return ""


def thinking_capability(argv: list[str], model_name: str,
                        efforts: list[str] | None = None,
                        default_effort: str | None = None) -> dict[str, Any]:
    """(プロファイル argv, モデル名, サーバー effort 語彙) → thinking 設定の出せる範囲。

    戻り値: {capable, qwen, levels, source}
      capable … 思考に対応しているか / qwen … enable_thinking 配線が必要か
      levels  … UI に出してよいレベル (off のみ / off+on / off+実 effort 段階)
      source  … 判定の根拠 (表示用)

    サーバーが /v1/models で effort 語彙を公開するなら、その段階をそのまま出す
    (Inkling-2 の none/minimal/low/medium/high/xhigh/max、Qwen3.8-Flash-Next の
    xhigh/medium/low)。語彙はサーバーがチェックポイントのテンプレートを probe した
    結果なのでファミリーを問わず権威。"none" は effort 専用モデルの off 歯車、
    Qwen は off が enable_thinking トグルなので語彙に "none" が無い — どちらでも
    off + 段階を出す。qwen フラグは配線 (enable_thinking の要否) だけを決める。"""
    parser = argv_flag(argv, "--reasoning-parser")
    reasoning = argv_flag(argv, "--reasoning")
    name_hit = bool(THINKING_NAME_RE.search(model_name or ""))
    qwen = bool(QWEN_THINKING_RE.search(model_name or "")) or "qwen3" in parser
    if efforts:
        levels = ["off"] + [e for e in efforts if e != "none"]
        return {"capable": True, "qwen": qwen, "levels": levels,
                "source": "サーバーの effort 語彙 (/v1/models)",
                "efforts": list(efforts), "default": default_effort}
    if parser:
        capable, source = True, "プロファイルの reasoning-parser"
    elif reasoning in ("on", "auto"):
        capable, source = True, "プロファイルの reasoning 設定"
    elif name_hit:
        capable, source = True, "モデル名 (思考ファミリー)"
    else:
        capable, source = False, ""
    return {"capable": capable, "qwen": qwen,
            "levels": ["off", "on"] if capable else ["off"],
            "source": source}


def model_efforts(base: str, auth: dict[str, str] | None = None) -> tuple[list[str], str | None]:
    """サーバーの /v1/models から (supported_reasoning_efforts, default_reasoning_effort)。
    未取得・非対応サーバーは ([], None)。"""
    try:
        req = urllib.request.Request(f"{base}/v1/models", headers=auth or {})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return [], None
    for it in (data.get("data") or []):
        if isinstance(it, dict):
            eff = it.get("supported_reasoning_efforts")
            if isinstance(eff, list) and eff:
                return [str(e) for e in eff], it.get("default_reasoning_effort")
    return [], None


def model_ids(base: str, auth: dict[str, str] | None = None) -> list[str]:
    """サーバーの /v1/models からモデル id 一覧を取得 (失敗は [])。"""
    try:
        req = urllib.request.Request(f"{base}/v1/models", headers=auth or {})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except (OSError, json.JSONDecodeError, UnicodeDecodeError):
        return []
    out = []
    for it in (data.get("data") or []):
        if isinstance(it, dict):
            mid = it.get("id") or it.get("model") or it.get("name")
            if mid:
                out.append(str(mid))
    return out


# ------------------------------------------------------------------ 外部 API ターゲット

def normalize_external_base(url: str) -> str:
    """外部 API の base URL を /v1 無し形に正規化 (pi 配線が /v1 を付加する)。
    https://api.openai.com/v1 → https://api.openai.com"""
    u = (url or "").strip().rstrip("/")
    if u.endswith("/v1"):
        u = u[:-3]
    return u


def mask_secret(key: str) -> str:
    """API キーの履歴保存用マスク (先頭 4 文字 + 末尾 2 文字だけ残す)。"""
    k = (key or "").strip()
    if not k:
        return ""
    if len(k) <= 8:
        return k[:2] + "…"
    return k[:4] + "…" + k[-2:]


def external_probe(base: str, auth: dict[str, str] | None = None,
                   timeout: float = 15.0) -> dict[str, Any]:
    """外部 API の /v1/models を叩いて (ok, models, error, status) を返す。
    接続テストと起動待ちの両方で使う。認証エラーは即失敗扱い。"""
    try:
        req = urllib.request.Request(f"{base}/v1/models", headers=auth or {})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        models = [str(it.get("id") or it.get("model") or it.get("name") or "")
                  for it in (data.get("data") or []) if isinstance(it, dict)]
        return {"ok": True, "models": [m for m in models if m], "error": "", "status": 200}
    except urllib.error.HTTPError as e:
        detail = ""
        with contextlib.suppress(Exception):
            detail = e.read().decode("utf-8", "replace")[:300]
        return {"ok": False, "models": [], "status": e.code,
                "error": f"HTTP {e.code}: {detail or e.reason}"}
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as e:
        return {"ok": False, "models": [], "status": 0, "error": str(e)}


# ------------------------------------------------------------------ マネージャー


class CodingManager:
    """コーディングベンチマークの実行・履歴・プレビュー管理。"""

    def __init__(self, root: Path, store: JsonStore, profiles_store: JsonStore,
                 manager: Any, assets: CodingAssets, pi: PiTool):
        self.root = root
        self.store = store
        self.profiles = profiles_store
        self.manager = manager          # ProcessManager
        self.assets = assets
        self.pi = pi
        self.exclusive: Any = None      # bench.ExclusiveLock (attach_exclusive で共有)
        self.active_id: str | None = None
        self._tasks: dict[str, asyncio.Task] = {}
        self._cancel: set[str] = set()
        self._dead: set[str] = set()
        self._last_save: dict[str, float] = {}
        self._previews: dict[str, dict[str, Any]] = {}   # rid -> {proc, port, mode, ...}
        # 外部 API キーは履歴 (runs.json) に残さず実行中だけメモリで持つ
        self._ext_keys: dict[str, str] = {}

    def attach_exclusive(self, lock: Any) -> None:
        """ベンチ / キャリブレーションと同じ実走排他ロックを共有する。"""
        self.exclusive = lock

    @property
    def exclusive_owner(self) -> str:
        return self.exclusive.owner if self.exclusive else ""

    # ------------------------------------------------------------ lifecycle

    def on_startup(self) -> list[str]:
        """launcher 再起動で死んだ実行中ジョブを lost に回復 (calibrate と同じ)。"""
        lost: list[str] = []
        changed = False
        for r in self.store.load():
            if r.get("status") in ("queued", "running"):
                r["status"] = "lost"
                r["error"] = (r.get("error") or "") + " / launcher の再起動で中断されました"
                r["finished_at"] = r.get("finished_at") or now()
                lost.append(r["id"])
                changed = True
            if r.get("preview"):
                r["preview"] = None     # die-with-parent で既に死んでいる
                changed = True
        if changed:
            self.store.save()
        docker_cleanup_stale()
        return lost

    async def shutdown(self) -> None:
        for rid in list(self._tasks):
            self._cancel.add(rid)
            self._tasks[rid].cancel()
        for rid in list(self._previews):
            with contextlib.suppress(Exception):
                await self.preview_stop(rid)

    # ------------------------------------------------------------ runs CRUD

    def list_runs(self) -> list[dict[str, Any]]:
        return sorted(self.store.load(), key=lambda r: r.get("created_at") or 0, reverse=True)

    def get(self, rid: str) -> dict[str, Any] | None:
        return next((r for r in self.store.load() if r.get("id") == rid), None)

    def run_dir(self, rid: str) -> Path:
        return self.assets.runs_dir / rid

    async def start(self, profile: dict[str, Any] | None, params: dict[str, Any],
                    backend_name: str) -> dict[str, Any]:
        """profile=None + params["external"] で外部 API ターゲットの実行。
        外部実行は GPU を使わないため実走排他ロックを取らない (ベンチ / キャリブと共存可)。"""
        ext = params.get("external")
        if self.active_id:
            raise CodingError("他のコーディングベンチマークが実行中です。完了を待つか取り消してください")
        if not ext and self.exclusive is not None and not self.exclusive.acquire(
                f"coding:{(profile or {}).get('id', '')}"):
            raise CodingError("ベンチマーク / キャリブレーションが実行中です。完了を待つか中断してください"
                             " (同じ GPU を使うため同時に実走できません)")
        if params.get("sandbox", True):
            backend = sandbox_backend(self.pi.settings.load())
            if not backend:
                not ext and self.exclusive and self.exclusive.release()
                info = sandbox_info(self.pi.settings.load())
                raise CodingError(
                    "サンドボックス (bubblewrap / Docker) が使えません。"
                    + (f" 対処: {info['hint']}" if info["hint"] else "")
                    + " / サンドボックス OFF でも実行できます")
        if not self.pi.available():
            not ext and self.exclusive and self.exclusive.release()
            raise CodingError("pi が見つかりません。コーディングベンチ画面の「pi をインストール」を押すか、"
                              "npm install -g --ignore-scripts " + PI_PKG + " を実行してください")
        if ext:
            label = ext.get("name") or ext.get("model") or ext.get("base_url", "external")
            default_name = f"{label} {time.strftime('%m/%d %H:%M')}"
        else:
            default_name = f"{(profile or {}).get('name', '')} {time.strftime('%m/%d %H:%M')}"
        name = str(params.get("name") or "").strip() or default_name
        run = {
            "id": new_id("cb"),
            "name": name[:120],
            "created_at": now(),
            "started_at": None,
            "finished_at": None,
            "status": "queued",
            "error": "",
            "profile_id": (profile or {}).get("id", ""),
            "profile_name": (profile or {}).get("name", "") or (ext or {}).get("name", ""),
            "backend": (profile or {}).get("backend", "") or "external",
            "backend_name": backend_name,
            "model": params.get("model") or (ext or {}).get("model", ""),
            "session_id": None,
            "port": None,
            "auto_started": False,
            "stopped_after": False,
            "stop_error": "",
            "params": params,
            "progress": {"phase": "queued", "turns": 0, "tool_calls": 0, "tool_errors": 0,
                         "last_tool": "", "gen_avg": None,
                         "tokens_in": 0, "tokens_out": 0, "elapsed": 0, "text": ""},
            "phases": {},
            "files": {"count": 0, "bytes": 0},
            "preview": None,
        }
        if ext:
            run["external"] = {"base_url": ext.get("base_url", ""),
                               "model": ext.get("model", ""),
                               "name": ext.get("name", ""),
                               "api_key": mask_secret(ext.get("api_key", ""))}
            self._ext_keys[run["id"]] = ext.get("api_key", "")
            params["external"] = {**ext, "api_key": mask_secret(ext.get("api_key", ""))}
        try:
            self.store.mutate(lambda lst: lst.append(run))
            self._trim()
            self.active_id = run["id"]
            self._tasks[run["id"]] = asyncio.create_task(self._job(run, profile, params))
        except BaseException:
            not ext and self.exclusive and self.exclusive.release()
            self._ext_keys.pop(run["id"], None)
            raise
        return run

    def cancel(self, rid: str) -> None:
        run = self.get(rid)
        if not run:
            raise KeyError(rid)
        if run.get("status") not in ("queued", "running"):
            raise CodingError("この実行は取り消せません (完了済み)")
        self._cancel.add(rid)

    def delete(self, rid: str) -> None:
        run = self.get(rid)
        if not run:
            raise KeyError(rid)
        if run.get("status") in ("queued", "running"):
            raise CodingError("実行中のベンチマークは削除できません")
        if self.preview_info(rid):
            raise CodingError("プレビューが起動中です。停止してから削除してください")

        def _d(lst: list) -> None:
            lst[:] = [r for r in lst if r.get("id") != rid]
        self._dead.add(rid)
        self._last_save.pop(rid, None)
        self.store.mutate(_d)
        shutil.rmtree(self.run_dir(rid), ignore_errors=True)

    def clear_all(self) -> int:
        removed: list[str] = []

        def _c(lst: list) -> None:
            keep: list = []
            for r in lst:
                rid = r.get("id")
                if r.get("status") in ("queued", "running") or rid == self.active_id:
                    keep.append(r)
                else:
                    removed.append(rid)
            lst[:] = keep
            self._dead.update(removed)
            for rid in removed:
                self._last_save.pop(rid, None)
        self.store.mutate(_c)
        for rid in removed:
            if not self.preview_info(rid):
                shutil.rmtree(self.run_dir(rid), ignore_errors=True)
        return len(removed)

    def _trim(self) -> None:
        def _t(lst: list) -> None:
            if len(lst) > MAX_RUNS:
                keep = {r["id"] for r in sorted(lst, key=lambda r: r.get("created_at") or 0,
                                                reverse=True)[:MAX_RUNS]}
                keep |= {r["id"] for r in lst if r.get("status") in ("queued", "running")}
                gone = [r["id"] for r in lst if r["id"] not in keep and r["id"] not in self._previews]
                lst[:] = [r for r in lst if r["id"] in keep or r["id"] in self._previews]
                self._dead.update(gone)
                for rid in gone:
                    self._last_save.pop(rid, None)
                    shutil.rmtree(self.run_dir(rid), ignore_errors=True)
        self.store.mutate(_t)

    def _save(self, run: dict[str, Any], force: bool = False) -> None:
        rid = run["id"]
        if rid in self._dead:
            return
        t = time.monotonic()
        if not force and t - self._last_save.get(rid, 0) < SAVE_THROTTLE:
            return
        self._last_save[rid] = t

        def _patch(lst: list) -> None:
            for i, r in enumerate(lst):
                if r.get("id") == rid:
                    lst[i] = run
                    return
        self.store.mutate(_patch)

    # ------------------------------------------------------------ job

    async def _job(self, run: dict[str, Any], profile: dict[str, Any],
                   params: dict[str, Any]) -> None:
        try:
            await self._run(run, profile, params)
            run["status"] = "cancelled" if run["id"] in self._cancel else "completed"
        except _Cancelled:
            run["status"] = "cancelled"
        except asyncio.CancelledError:
            run["status"] = "cancelled"
        except CodingError as e:
            run["status"] = "failed"
            run["error"] = str(e)
        except Exception as e:  # noqa: BLE001
            run["status"] = "failed"
            run["error"] = f"{type(e).__name__}: {e}"
        finally:
            run["finished_at"] = now()
            run["progress"]["phase"] = "done"
            self._save(run, force=True)
            self.active_id = None
            self._cancel.discard(run["id"])
            self._tasks.pop(run["id"], None)
            if not run.get("external"):
                self.exclusive and self.exclusive.release()
            if run.get("external"):
                self._ext_keys.pop(run["id"], None)
                self._scrub_pi_home(self.run_dir(run["id"]))
            if ((params.get("stop_after") or run.get("status") in ("failed", "cancelled"))
                    and run.get("auto_started") and run.get("session_id")
                    and not run.get("stopped_after")):
                try:
                    await self.manager.stop(run["session_id"])
                    run["stopped_after"] = True
                except Exception as e:  # noqa: BLE001
                    run["stop_error"] = f"{type(e).__name__}: {e}"
                self._save(run, force=True)

    async def _run(self, run: dict[str, Any], profile: dict[str, Any] | None,
                   params: dict[str, Any]) -> None:
        run["started_at"] = now()
        run["status"] = "running"
        self._save(run, force=True)
        ext = params.get("external")
        if ext:
            # 外部 API ターゲット: ローカルセッション不要。/v1/models が応答するまで待つ
            base = ext.get("base_url", "")
            key = self._ext_keys.get(run["id"], "")
            auth = {"Authorization": f"Bearer {key}"} if key else {}
            run["progress"]["phase"] = "waiting"
            self._save(run, force=True)
            await self._wait_ready_external(run, base, auth, int(params["ready_timeout"]))
            model = ext.get("model") or params.get("model") or await self._resolve_model(base, auth)
            if not model:
                raise CodingError("外部 API からモデル名を解決できません (/v1/models が空です)。"
                                  "モデル名を明示指定してください")
            efforts: list[str] = []
        else:
            sess = await self._resolve_session(run, profile, params)
            port = sess.get("port")
            if not port:
                raise CodingError("このセッションはポートを解決できていません "
                                  "(プロファイルの port 設定を確認してください)")
            base = f"http://127.0.0.1:{port}"
            auth = {"Authorization": f"Bearer {api_key_from_argv(sess.get('argv') or [])}"} \
                if api_key_from_argv(sess.get("argv") or []) else {}
            run["port"] = port
            run["progress"]["phase"] = "waiting"
            self._save(run, force=True)
            await self._wait_ready(run, base, sess.get("health_path") or "/health",
                                   int(params["ready_timeout"]), sess["id"], auth)
            model = params.get("model") or await self._resolve_model(base, auth)
            if not model:
                raise CodingError("サーバーからモデル名を解決できません (/v1/models が空です)。"
                                  "モデル名を明示指定してください")
            key = api_key_from_argv(sess.get("argv") or [])
            efforts, _ = await asyncio.to_thread(model_efforts, base, auth)
        run["model"] = model
        rd = self.run_dir(run["id"])
        ws = rd / "workspace"
        ws.mkdir(parents=True, exist_ok=True)
        (rd / "home").mkdir(exist_ok=True)
        (rd / "sessions").mkdir(exist_ok=True)
        (rd / "logs").mkdir(exist_ok=True)
        self._prepare_pi_home(rd, base, model, key, params, efforts, external=bool(ext))
        # 選択された AGENTS.md を workspace にコピー (pi が cwd から自動発見する)
        agents_src = Path(params["agents_path"])
        shutil.copyfile(agents_src, ws / "AGENTS.md")
        skill_dirs = self._skill_dirs(Path(params["skills_dir"]))
        t_start = time.monotonic()
        for phase in ("backend", "frontend"):
            self._check_cancel(run)
            prompt = str(params.get(f"{phase}_prompt") or "").strip()
            if not prompt:
                raise CodingError(f"{phase} プロンプトが空です")
            run["progress"]["phase"] = phase
            run["progress"]["turns"] = 0
            run["progress"]["tool_calls"] = 0
            run["progress"]["last_tool"] = ""
            run["progress"]["text"] = ""
            self._save(run, force=True)
            stats = await self._run_pi(run, phase, prompt, rd, ws, skill_dirs, params, t_start)
            run["phases"][phase] = stats
            self._save(run, force=True)
        run["progress"]["phase"] = "scanning"
        run["files"] = await asyncio.to_thread(scan_workspace, ws)
        # 結果の正直な注記: 実装ファイルが無い / ツール引数エラーが多い / 長さ上限打ち切り
        warns: list[str] = []
        generated = [f for f in file_tree(ws) if f["path"] != "AGENTS.md"]
        if not generated:
            warns.append("実装ファイルが生成されませんでした (モデルがツールを正しく使えなかった可能性)")
        terr = sum(int((run["phases"].get(ph) or {}).get("tool_errors") or 0)
                   for ph in ("backend", "frontend"))
        if terr:
            warns.append(f"ツール引数の検証エラー {terr} 回 (空の引数など)")
        if any((run["phases"].get(ph) or {}).get("stop_reason") == "length"
               for ph in ("backend", "frontend")):
            warns.append("出力トークン上限 (max_tokens) で打ち切られたターンがあります")
        run["warning"] = " / ".join(warns)
        self._save(run, force=True)

    async def _resolve_session(self, run: dict[str, Any], profile: dict[str, Any],
                               params: dict[str, Any]) -> dict[str, Any]:
        stopping = next((s for s in self.manager.sessions.values()
                         if s["profile_id"] == profile["id"] and s["status"] == "stopping"), None)
        if stopping:
            raise CodingError("サーバーが停止中です。少し待ってから再実行してください")
        live = next((s for s in self.manager.sessions.values()
                     if s["profile_id"] == profile["id"] and s["status"] in ("running", "starting")),
                    None)
        if not live:
            if not params.get("auto_start", True):
                raise CodingError("プロファイルが起動していません。起動タブでスタートするか、"
                                  "「起動済みでない場合は自動起動」を ON にしてください")
            run["progress"]["phase"] = "launching"
            self._save(run, force=True)
            try:
                live = await self.manager.start(profile, via=VIA_CODING)
                run["auto_started"] = True
                run["session_id"] = live["id"]
            except Exception as e:  # noqa: BLE001 — MemoryGuardError 等も理由ごと出す
                raise CodingError(f"サーバーの起動に失敗しました: {e}") from e
        run["session_id"] = live["id"]
        return live

    async def _wait_ready(self, run: dict[str, Any], base: str, health: str,
                          timeout: int, sid: str, auth: dict[str, str]) -> None:
        from .bench import _health_ok
        t0 = time.monotonic()
        while True:
            self._check_cancel(run)
            s = self.manager.sessions.get(sid)
            if s and s.get("status") not in ("running", "starting"):
                raise CodingError("サーバーが起動途中で終了しました。サーバータブのログを確認してください")
            if await asyncio.to_thread(_health_ok, f"{base}{health}", auth):
                return
            if time.monotonic() - t0 > timeout:
                raise CodingError(f"{timeout}s 経ってもサーバーが health に応答しません。"
                                  "サーバータブのログを確認してください")
            await asyncio.sleep(1.5)

    async def _resolve_model(self, base: str, auth: dict[str, str]) -> str:
        from .bench import _first_model
        for _ in range(3):
            mid = await asyncio.to_thread(_first_model, base, auth)
            if mid:
                return mid
            await asyncio.sleep(1.0)
        return ""

    async def _wait_ready_external(self, run: dict[str, Any], base: str,
                                   auth: dict[str, str], timeout: int) -> None:
        """外部 API の /v1/models が 200 を返すまで待つ。認証エラー (401/403) は即失敗。"""
        t0 = time.monotonic()
        while True:
            self._check_cancel(run)
            probe = await asyncio.to_thread(external_probe, base, auth, 15.0)
            if probe["ok"]:
                return
            if probe["status"] in (401, 403):
                raise CodingError(f"外部 API の認証に失敗しました: {probe['error']}")
            if time.monotonic() - t0 > timeout:
                raise CodingError(f"{timeout}s 経っても外部 API が応答しません: {probe['error']}")
            await asyncio.sleep(2.0)

    def _scrub_pi_home(self, rd: Path) -> None:
        """外部 API 実行後、pi の models.json から実キーを消す (実行中だけ必要)。"""
        p = rd / "pi-home" / "models.json"
        try:
            cfg = json.loads(p.read_text(encoding="utf-8"))
            for prov in (cfg.get("providers") or {}).values():
                if isinstance(prov, dict) and prov.get("apiKey") not in ("", "local", None):
                    prov["apiKey"] = "(removed-after-run)"
            p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass

    def _prepare_pi_home(self, rd: Path, base: str, model: str, key: str,
                         params: dict[str, Any], efforts: list[str] | None = None,
                         external: bool = False) -> None:
        """pi の models.json: launcher が起動したサーバーを OpenAI 互換プロバイダとして登録。

        Qwen3 系は enable_thinking の ON/OFF が実体なので、thinkingFormat を
        qwen-chat-template に固定し、off で enable_thinking=false が送られるようにする
        (テンプレート既定の thinking ON を確実に切る)。レベルは off/high のみ提示。

        effort 語彙を公開する Qwen (Qwen3.8-Flash-Next の xhigh/medium/low) は
        トグルと段階が共存する: qwen-chat-template は enable_thinking しか送れない
        ため chat-template に替え、chat_template_kwargs で enable_thinking と
        reasoning_effort の両方を送る (off は enable_thinking=false かつ
        reasoning_effort を omit — テンプレート既定の xhigh が漏れないように)。

        effort 語彙 ("none" 含む) を公開する非 Qwen モデル (Inkling-2) は pi 既定の
        reasoning_effort 配線を使う: thinkingLevelMap.off を "none" に割り当て、
        off でも reasoning=True にして pi が reasoning_effort="none" を送るようにする
        (テンプレート既定の effort 0.9 を確実に切る)。

        外部 API (external=True) はサーバー側テンプレートが未知なので、OpenAI 標準の
        reasoning_effort 配線だけを使う (chat_template_kwargs は送らない)。
        off は reasoning=False で何も送らない ("none" は OpenAI が拒否するため)。"""
        pi_home = rd / "pi-home"
        pi_home.mkdir(exist_ok=True)
        thinking = str(params.get("thinking") or "off")
        qwen = bool(QWEN_THINKING_RE.search(model or "")) and not external
        effort_wired = bool(efforts) and "none" in efforts and not qwen
        ctx = int((params.get("external") or {}).get("context_window") or 128000) \
            if external else 128000
        entry: dict[str, Any] = {
            "id": model,
            "name": model,
            "reasoning": True if (qwen or effort_wired) else thinking != "off",
            "input": ["text"],
            "contextWindow": ctx,
            # 思考 ON では thinking が出力枠を食う。8192 固定だと thinking=max で
            # 思考だけで枠を使い切り length 打ち切り → ツール呼び出しの無いターンで
            # pi が終了し、ファイルが 1 つも生成されない (2026-11-24 Inkling 実測)。
            # 思考時はエンジンの既定 max_output_tokens (32768) まで枠を広げる。
            "maxTokens": 8192 if thinking == "off" else 32768,
            "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0},
        }
        if external:
            entry["compat"] = {"supportsReasoningEffort": True, "supportsDeveloperRole": False}
            entry["thinkingLevelMap"] = {
                name: name for name in ("minimal", "low", "medium", "high", "xhigh", "max")}
        elif qwen and efforts:
            # 語彙はサーバー probe 済み (例: xhigh/medium/low)。pi の extended
            # レベル (xhigh/max) は明示 map が無いと unsupported 扱いなので、
            # 語彙の名前は全部明示する。high は旧 "on" の読み替え先として
            # 語彙に無ければ最大段階 (xhigh) へ繋ぐ (サーバーの量子化と同じ)。
            level_map: dict[str, Any] = {
                name: name for name in efforts
                if name in ("minimal", "low", "medium", "high", "xhigh", "max")}
            for name in ("minimal", "low", "medium", "high", "xhigh", "max"):
                level_map.setdefault(name, None)
            if level_map["high"] is None and level_map["xhigh"] is not None:
                level_map["high"] = "xhigh"
            entry["compat"] = {
                "thinkingFormat": "chat-template",
                "chatTemplateKwargs": {
                    "enable_thinking": {"$var": "thinking.enabled"},
                    "preserve_thinking": True,
                    "reasoning_effort": {"$var": "thinking.effort", "omitWhenOff": True},
                },
            }
            entry["thinkingLevelMap"] = level_map
        elif qwen:
            entry["compat"] = {"thinkingFormat": "qwen-chat-template"}
            entry["thinkingLevelMap"] = {"minimal": None, "low": None, "medium": None}
        elif effort_wired:
            level_map: dict[str, Any] = {"off": "none"}
            for name in efforts:
                if name in ("minimal", "low", "medium", "high", "xhigh", "max"):
                    level_map[name] = name
            # supportsDeveloperRole=False: pi は reasoning=True だと系統プロンプトを
            # developer ロールで送るが、Inkling-2 のテンプレートは system しか知らず
            # 400 (Unknown message role: developer) になるため system に固定する
            entry["compat"] = {"supportsReasoningEffort": True, "supportsDeveloperRole": False}
            entry["thinkingLevelMap"] = level_map
        cfg = {
            "providers": {
                PI_PROVIDER: {
                    "name": "External API" if external else "LLM Launcher",
                    "baseUrl": f"{base}/v1",
                    "api": "openai-completions",
                    "apiKey": key or "local",
                    "authHeader": True,
                    "models": [entry],
                },
            },
        }
        out = pi_home / "models.json"
        out.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        if external:
            out.chmod(0o600)   # 実キーを含むため所有者のみ (実行後に _scrub_pi_home で消去)

    def _skill_dirs(self, folder: Path) -> list[Path]:
        """--skill に渡すディレクトリ列挙 (SKILL.md を持つものだけ)。"""
        if (folder / "SKILL.md").is_file():
            return [folder]
        out = [p.parent for p in sorted(folder.rglob("SKILL.md"))
               if not any(part in SKIP_DIRS for part in p.parts)]
        return out[:100]

    def _pi_argv(self, run: dict[str, Any], phase: str, prompt: str, rd: Path,
                 ws: Path, skill_dirs: list[Path], params: dict[str, Any]
                 ) -> tuple[list[str], dict[str, str], str]:
        """(実行 argv, CLI に渡す環境, docker コンテナ名 or "") を組み立てる。

        DSH 風ツール拡張 (dsh-tools.ts) があれば -e で明示ロードし、read / edit /
        write / grep / glob を拡張版に差し替える (--no-extensions は自動発見だけを
        止め、明示 -e は有効)。無いときは pi 組み込みツールにフォールバック。"""
        pi_cmd = self.pi.command()
        ext = self.assets.extension_path
        use_ext = ext.is_file()
        tools = "read,bash,edit,write,grep,glob" if use_ext \
            else "read,bash,edit,write,grep,find,ls"
        inner = [pi_cmd, "--mode", "json",
                 "--model", f"{PI_PROVIDER}/{run['model']}",
                 "--thinking", str(params.get("thinking") or "off"),
                 "--tools", tools,
                 "--no-skills", "--no-extensions",
                 "--no-prompt-templates", "--no-themes", "--approve",
                 "--session-dir", str(rd / "sessions"),
                 "--name", run["name"]]
        # backend は新規セッションを id 指定なしで作る。--session-id を渡すと pi が
        # 既存セッションを見つけられず "No project session found with id 'coding';
        # creating a new session with that id." を毎回 stderr に出すため (2026-11-24)。
        # frontend は --continue で backend のセッションを継続 (同一 cwd / session-dir)。
        if phase == "frontend":
            inner += ["--continue"]
        if use_ext:
            inner += ["--extension", str(ext)]
        for d in skill_dirs:
            inner += ["--skill", str(d)]
        inner += ["--", prompt]
        env = sandbox_env(rd / "home", rd / "pi-home", rd / "sessions")
        ro_binds = skill_dirs + [Path(params["agents_path"])]
        if use_ext:
            ro_binds.append(ext)
        if not params.get("sandbox", True):
            return inner, env, ""
        backend = sandbox_backend(self.pi.settings.load())
        if backend == "docker":
            name = f"{CONTAINER_PREFIX}{run['id']}-{phase}"
            # ランチャ symlink (/usr/local/bin/pi 等) はコンテナに無い。パッケージ
            # mount 内の解決済み実パスを実行する (ESM 相対 import はパッケージ構造が要る)
            inner = [str(Path(pi_cmd).resolve()), *inner[1:]]
            argv = docker_argv(rd, ws, ro_binds=ro_binds, env=env,
                               image=effective_sandbox_image(self.pi.settings.load()), name=name,
                               pi_mounts=pi_runtime_mounts(pi_cmd)) + inner
            return argv, dict(os.environ), name
        # bwrap (動くことは start() で確認済み)
        return sandbox_argv(rd, ws, ro_binds=ro_binds) + inner, env, ""

    async def _run_pi(self, run: dict[str, Any], phase: str, prompt: str, rd: Path,
                      ws: Path, skill_dirs: list[Path], params: dict[str, Any],
                      t_start: float) -> dict[str, Any]:
        argv, env, container = self._pi_argv(run, phase, prompt, rd, ws, skill_dirs, params)
        logs = rd / "logs"
        raw_path = logs / f"{phase}.jsonl"
        digest_path = logs / f"{phase}.log"
        err_path = logs / f"{phase}.stderr"
        stats: dict[str, Any] = {"status": "running", "started_at": now(), "finished_at": None,
                                 "secs": 0.0, "exit_code": None, "turns": 0, "tool_calls": 0,
                                 "tool_errors": 0, "stop_reason": "",
                                 "tokens_in": 0, "tokens_out": 0, "error": "",
                                 # ターン別時間計測の積算 (平均は finalize で算出)
                                 "turn_secs_sum": 0.0, "turn_n": 0,
                                 "gen_secs_sum": 0.0, "gen_n": 0,
                                 "ttft_sum": 0.0, "ttft_n": 0}
        run["phases"][phase] = stats
        timeout = float(params.get("phase_timeout", 1800))
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *argv, cwd=str(ws), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            start_new_session=True, limit=32 * 1024 * 1024)
        raw_fh = open(raw_path, "wb")
        digest_fh = open(digest_path, "w", encoding="utf-8")
        err_fh = open(err_path, "wb")

        async def pump_err() -> None:
            assert proc.stderr is not None
            while True:
                chunk = await proc.stderr.read(65536)
                if not chunk:
                    break
                err_fh.write(chunk)
                err_fh.flush()

        err_task = asyncio.create_task(pump_err())
        deadline = t0 + timeout
        try:
            assert proc.stdout is not None
            while True:
                if run["id"] in self._cancel:
                    raise _Cancelled()
                if time.monotonic() > deadline:
                    raise CodingError(f"pi ({phase}) が {int(timeout)}s でタイムアウトしました")
                try:
                    line = await asyncio.wait_for(proc.stdout.readline(), timeout=1.0)
                except asyncio.TimeoutError:
                    run["progress"]["elapsed"] = round(time.monotonic() - t_start, 1)
                    self._save(run)
                    continue
                if not line:
                    break
                raw_fh.write(line)
                raw_fh.flush()
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                self._consume_event(run, phase, ev, digest_fh, stats)
                run["progress"]["elapsed"] = round(time.monotonic() - t_start, 1)
                self._save(run)
            rc = await asyncio.wait_for(proc.wait(), timeout=30)
        except (_Cancelled, CodingError):
            await self._kill_proc(proc, container)
            stats["status"] = "cancelled" if run["id"] in self._cancel else "failed"
            stats["error"] = "タイムアウト" if run["id"] not in self._cancel else ""
            raise
        except asyncio.CancelledError:
            await self._kill_proc(proc, container)
            raise
        finally:
            err_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await err_task
            for fh in (raw_fh, digest_fh, err_fh):
                with contextlib.suppress(OSError):
                    fh.close()
            stats["finished_at"] = now()
            stats["secs"] = round(time.monotonic() - t0, 2)
            stats["exit_code"] = proc.returncode
            # ターン別時間の平均を確定 (一時キーは畳む)
            tn, gn, fn = stats.pop("turn_n", 0), stats.pop("gen_n", 0), stats.pop("ttft_n", 0)
            ts, gs, fs = stats.pop("turn_secs_sum", 0.0), stats.pop("gen_secs_sum", 0.0), stats.pop("ttft_sum", 0.0)
            stats["turn_n"], stats["gen_n"], stats["ttft_n"] = tn, gn, fn
            stats["turn_secs_sum"], stats["gen_secs_sum"], stats["ttft_sum"] = \
                round(ts, 3), round(gs, 3), round(fs, 3)
            stats["turn_secs_avg"] = round(ts / tn, 3) if tn else None
            stats["gen_secs_avg"] = round(gs / gn, 3) if gn else None
            stats["ttft_avg"] = round(fs / fn, 3) if fn else None
            stats["decode_tps"] = round(stats["tokens_out"] / gs, 2) if gs > 0 else None
            for k in ("_turn_t0", "_msg_t0", "_msg_first"):
                stats.pop(k, None)
            self._save(run, force=True)
        if rc != 0:
            err_tail = ""
            with contextlib.suppress(OSError):
                err_tail = err_path.read_text("utf-8", "replace")[-600:]
            stats["status"] = "failed"
            stats["error"] = f"pi exit {rc}"
            raise CodingError(f"pi ({phase}) が exit {rc} で終了しました。"
                              f"{' ' + err_tail.strip() if err_tail.strip() else ''}")
        stats["status"] = "completed"
        return stats

    def _consume_event(self, run: dict[str, Any], phase: str, ev: dict[str, Any],
                       digest_fh: Any, stats: dict[str, Any]) -> None:
        """pi の JSONL イベントをライブ進捗 + ダイジェストログに反映する。"""
        t = ev.get("type")
        prog = run["progress"]
        if t == "turn_start":
            stats["turns"] += 1
            prog["turns"] = stats["turns"]
            stats["_turn_t0"] = time.monotonic()
        elif t == "turn_end":
            t0 = stats.pop("_turn_t0", None)
            if t0 is not None:
                stats["turn_secs_sum"] += time.monotonic() - t0
                stats["turn_n"] += 1
        elif t == "message_start":
            if (ev.get("message") or {}).get("role") == "assistant":
                stats["_msg_t0"] = time.monotonic()
                stats["_msg_first"] = None
        elif t == "tool_execution_start":
            stats["tool_calls"] += 1
            prog["tool_calls"] = stats["tool_calls"]
            tool = str(ev.get("toolName") or "")
            prog["last_tool"] = tool
            args = ev.get("args") or {}
            brief = str(args.get("command") or args.get("path") or args.get("pattern")
                        or args.get("file_path") or "")[:160]
            digest_fh.write(f"[tool] {tool}: {brief}\n")
            digest_fh.flush()
        elif t == "tool_execution_end":
            if ev.get("isError"):
                stats["tool_errors"] = stats.get("tool_errors", 0) + 1
                prog["tool_errors"] = stats["tool_errors"]
                res = ev.get("result") or {}
                parts = res.get("content") if isinstance(res, dict) else None
                msg = ""
                if isinstance(parts, list):
                    msg = " ".join(str(b.get("text", "")) for b in parts
                                   if isinstance(b, dict))[:160]
                digest_fh.write(f"[tool-error] {ev.get('toolName')}: {msg}\n")
                digest_fh.flush()
        elif t == "message_update":
            # 最初の delta 到着 = TTFT (アシスタント応答の生成開始まで)
            if stats.get("_msg_t0") is not None and stats.get("_msg_first") is None:
                stats["_msg_first"] = time.monotonic()
            # 進行中レスポンスの暫定 usage (確定値は message_end で積む)
            usage = ev.get("usage") or {}
            if isinstance(usage, dict):
                prog["tokens_in"] = stats["tokens_in"] + int(usage.get("input") or 0)
                prog["tokens_out"] = stats["tokens_out"] + int(usage.get("output") or 0)
        elif t == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                mt0 = stats.pop("_msg_t0", None)
                if mt0 is not None:
                    stats["gen_secs_sum"] += time.monotonic() - mt0
                    stats["gen_n"] += 1
                    first = stats.pop("_msg_first", None)
                    if first is not None:
                        stats["ttft_sum"] += first - mt0
                        stats["ttft_n"] += 1
                    prog["gen_avg"] = round(stats["gen_secs_sum"] / stats["gen_n"], 2)
                if msg.get("stopReason"):
                    stats["stop_reason"] = str(msg["stopReason"])
                usage = msg.get("usage") or {}
                if isinstance(usage, dict) and usage:
                    stats["tokens_in"] += int(usage.get("input") or 0)
                    stats["tokens_out"] += int(usage.get("output") or 0)
                    prog["tokens_in"] = stats["tokens_in"]
                    prog["tokens_out"] = stats["tokens_out"]
                content = msg.get("content")
                texts: list[str] = []
                if isinstance(content, str):
                    texts = [content]
                elif isinstance(content, list):
                    texts = [b.get("text", "") for b in content
                             if isinstance(b, dict) and b.get("type") == "text"]
                text = "\n".join(x for x in texts if x).strip()
                if text:
                    prog["text"] = text[-300:]
                    digest_fh.write(f"[assistant] {text[:2000]}\n")
                    digest_fh.flush()
        elif t == "auto_retry_start":
            digest_fh.write(f"[retry] attempt {ev.get('attempt')}: "
                            f"{str(ev.get('errorMessage') or '')[:200]}\n")
            digest_fh.flush()

    async def _kill_proc(self, proc: asyncio.subprocess.Process,
                         container: str = "") -> None:
        """プロセスグループごと止める (bwrap --die-with-parent で内側も道連れ)。
        docker は CLI を止めてもコンテナが残るため名前指定で強制削除する。"""
        if container:
            await asyncio.to_thread(docker_rm, container)
        if proc.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.killpg(proc.pid, signal.SIGKILL)
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=5)

    def _check_cancel(self, run: dict[str, Any]) -> None:
        if run["id"] in self._cancel:
            raise _Cancelled()

    # ------------------------------------------------------------ preview (ビューアー用)

    def preview_info(self, rid: str) -> dict[str, Any] | None:
        p = self._previews.get(rid)
        if not p:
            return None
        proc = p["proc"]
        if proc.returncode is not None:
            return None
        return {"port": p["port"], "mode": p["mode"], "command": p["command"],
                "started_at": p["started_at"], "pid": proc.pid}

    def detect_preview(self, ws: Path) -> dict[str, Any]:
        """生成アプリの起動方法を決める: python エントリ → app / 無ければ静的配信。"""
        for rel in PREVIEW_CANDIDATES:
            if (ws / rel).is_file():
                return {"mode": "app", "entry": rel}
        for rel in ("frontend/index.html", "index.html"):
            if (ws / rel).is_file():
                return {"mode": "static", "root": str(Path(rel).parent)}
        raise CodingError("プレビューできる実装が見つかりません "
                          "(backend/server.py 等のエントリも frontend/index.html も無い)")

    async def preview_start(self, rid: str, command: str = "") -> dict[str, Any]:
        run = self.get(rid)
        if not run:
            raise KeyError(rid)
        if run.get("status") in ("queued", "running"):
            raise CodingError("実行中のベンチマークはプレビューできません (完了を待ってください)")
        if rid in self._previews and self._previews[rid]["proc"].returncode is None:
            return self.preview_info(rid) or {}
        ws = self.run_dir(rid) / "workspace"
        if not ws.is_dir():
            raise CodingError("ワークスペースが見つかりません (削除された実行かもしれません)")
        det = self.detect_preview(ws)
        if det["mode"] == "static" and not command:
            run["preview"] = {"mode": "static", "root": det["root"], "port": None,
                              "started_at": now()}
            self._save(run, force=True)
            return run["preview"]
        # app モード: 空きポートを確保してサンドボックス内で起動
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        rd = self.run_dir(rid)
        settings = self.pi.settings.load()
        backend = sandbox_backend(settings) if run.get("params", {}).get("sandbox", True) else ""
        container = ""
        if backend == "docker":
            # コンテナ内の python3 (PATH 上) を使う。生成アプリは標準ライブラリのみ前提
            python = "python3"
        else:
            python = shutil.which("python3") or "/usr/bin/python3"
        if command:
            cmd_argv = ["sh", "-c", command]
            cmd_str = command
        else:
            cmd_argv = [python, det["entry"]]
            cmd_str = f"python3 {det['entry']}"
        env = sandbox_env(rd / "home", rd / "pi-home", rd / "sessions")
        env = {**env, "PORT": str(port), "PYTHONUNBUFFERED": "1"}
        if backend == "docker":
            container = f"{CONTAINER_PREFIX}pv-{rid}"
            docker_rm(container)                      # 前回の残骸があれば消す
            argv = docker_argv(rd, ws, env=env, image=effective_sandbox_image(settings),
                               name=container) + cmd_argv
            cli_env = dict(os.environ)
        elif backend == "bwrap":
            argv = sandbox_argv(rd, ws) + cmd_argv
            cli_env = env
        else:
            argv = cmd_argv
            cli_env = {k: os.environ[k] for k in ("PATH", "HOME", "LANG") if k in os.environ}
            cli_env = {**cli_env, "PORT": str(port), "PYTHONUNBUFFERED": "1"}
        (rd / "logs").mkdir(parents=True, exist_ok=True)
        log_fh = open(rd / "logs" / "preview.log", "ab")
        try:
            proc = await asyncio.create_subprocess_exec(
                *argv, cwd=str(ws), env=cli_env,
                stdout=log_fh, stderr=asyncio.subprocess.STDOUT,
                start_new_session=True)
        finally:
            log_fh.close()
        self._previews[rid] = {"proc": proc, "port": port, "mode": "app",
                               "command": cmd_str, "started_at": now(),
                               "container": container}
        # 起動待ち: 何らかの HTTP 応答が返れば OK (404 でもサーバーは生きている)
        import urllib.error
        import urllib.request
        t0 = time.monotonic()
        ready = False
        while time.monotonic() - t0 < PREVIEW_READY_TIMEOUT:
            if proc.returncode is not None:
                tail = ""
                with contextlib.suppress(OSError):
                    tail = (rd / "logs" / "preview.log").read_text("utf-8", "replace")[-500:]
                self._previews.pop(rid, None)
                raise CodingError(f"プレビューアプリが起動直後に終了しました (exit {proc.returncode})。"
                                  f"{' ' + tail.strip() if tail.strip() else ''}")
            try:
                req = urllib.request.Request(f"http://127.0.0.1:{port}/")
                await asyncio.to_thread(urllib.request.urlopen, req, timeout=1.5)
                ready = True
                break
            except urllib.error.HTTPError:
                ready = True
                break
            except OSError:
                await asyncio.sleep(0.4)
        if not ready:
            await self._kill_proc(proc, container)
            self._previews.pop(rid, None)
            raise CodingError(f"プレビューアプリが {int(PREVIEW_READY_TIMEOUT)}s 以内に応答しません。"
                              "logs/preview.log を確認してください")
        run["preview"] = {"mode": "app", "port": port, "command": cmd_str,
                          "started_at": now(), "pid": proc.pid}
        self._save(run, force=True)
        return run["preview"]

    async def preview_stop(self, rid: str) -> None:
        p = self._previews.pop(rid, None)
        if p:
            await self._kill_proc(p["proc"], p.get("container", ""))
        run = self.get(rid)
        if run and run.get("preview"):
            run["preview"] = None
            self._save(run, force=True)

    # ------------------------------------------------------------ files / log

    def files(self, rid: str) -> list[dict[str, Any]]:
        ws = self.run_dir(rid) / "workspace"
        if not ws.is_dir():
            raise CodingError("ワークスペースが見つかりません")
        return file_tree(ws)

    def read_file(self, rid: str, rel: str) -> dict[str, Any]:
        ws = self.run_dir(rid) / "workspace"
        p = safe_join(ws, rel)
        if not p.is_file():
            raise CodingError("ファイルが見つかりません: " + rel)
        size = p.stat().st_size
        with open(p, "rb") as fh:
            raw = fh.read(FILE_READ_MAX + 1)
        truncated = len(raw) > FILE_READ_MAX
        raw = raw[:FILE_READ_MAX]
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise CodingError("テキストとして読めないファイルです (バイナリ)")
        return {"path": rel, "text": text, "size": size, "truncated": truncated}

    def log_tail(self, rid: str, phase: str) -> dict[str, Any]:
        if phase not in ("backend", "frontend", "preview"):
            raise CodingError("phase は backend / frontend / preview です")
        logs = self.run_dir(rid) / "logs"

        def tail(p: Path) -> str:
            try:
                with open(p, "rb") as fh:
                    fh.seek(0, os.SEEK_END)
                    size = fh.tell()
                    fh.seek(max(0, size - LOG_TAIL_MAX))
                    return fh.read().decode("utf-8", "replace")
            except OSError:
                return ""
        return {"phase": phase, "log": tail(logs / f"{phase}.log"),
                "stderr": tail(logs / f"{phase}.stderr"),
                "preview": tail(logs / "preview.log") if phase == "preview" else ""}

    def transcript(self, rid: str, phase: str) -> dict[str, Any]:
        """モデル出力履歴: pi の生 JSONL からアシスタントの確定出力を取り出す。

        message_end の content (text / thinking / toolCall) がモデルが実際に出力した
        トークン内容。ツール呼び出しの引数 (write の本文など) も出力の一部なので含める。
        本文の総量が TRANSCRIPT_MAX_BYTES を超えたら打ち切り (件数は最後まで数える)。
        実行中のフェーズでもその時点までの出力を返す (ライブ閲覧)。"""
        if phase not in ("backend", "frontend"):
            raise CodingError("phase は backend / frontend です")
        p = self.run_dir(rid) / "logs" / f"{phase}.jsonl"
        messages: list[dict[str, Any]] = []
        total = 0
        truncated = False
        budget = TRANSCRIPT_MAX_BYTES
        try:
            fh = open(p, "rb")
        except OSError:
            return {"phase": phase, "messages": [], "total": 0,
                    "truncated": False, "missing": True}
        with fh:
            for line in fh:
                try:
                    ev = json.loads(line.decode("utf-8", "replace"))
                except json.JSONDecodeError:
                    continue
                if ev.get("type") != "message_end":
                    continue
                msg = ev.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                total += 1
                texts: list[str] = []
                thinking: list[str] = []
                tools: list[dict[str, Any]] = []
                content = msg.get("content")
                if isinstance(content, str):
                    texts = [content]
                elif isinstance(content, list):
                    for b in content:
                        if not isinstance(b, dict):
                            continue
                        bt = b.get("type")
                        if bt == "text" and b.get("text"):
                            texts.append(str(b["text"]))
                        elif bt == "thinking" and b.get("thinking"):
                            thinking.append(str(b["thinking"]))
                        elif bt == "toolCall":
                            tools.append({"name": str(b.get("name") or ""),
                                          "args": b.get("arguments")})
                usage = msg.get("usage") or {}
                item: dict[str, Any] = {
                    "i": total,
                    "ts": msg.get("timestamp"),
                    "text": "\n".join(texts),
                    "thinking": "\n".join(thinking),
                    "tools": tools,
                    "tokens_in": int(usage.get("input") or 0),
                    "tokens_out": int(usage.get("output") or 0),
                    "stop_reason": str(msg.get("stopReason") or ""),
                    "error": str(msg.get("errorMessage") or ""),
                }
                size = (len(item["text"].encode("utf-8"))
                        + len(item["thinking"].encode("utf-8"))
                        + sum(len(json.dumps(t["args"], ensure_ascii=False).encode("utf-8"))
                              for t in tools))
                if size <= budget:
                    budget -= size
                    messages.append(item)
                else:
                    truncated = True
                    if not messages:      # 先頭 1 件が巨大でも冒頭だけは見せる
                        item["text"] = item["text"].encode("utf-8")[:TRANSCRIPT_MAX_BYTES] \
                            .decode("utf-8", "ignore")
                        item["thinking"] = ""
                        item["tools"] = []
                        item["clipped"] = True
                        messages.append(item)
                        budget = 0
        return {"phase": phase, "messages": messages, "total": total,
                "truncated": truncated, "missing": False}