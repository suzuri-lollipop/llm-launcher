"""実行データ置き場の data/ 一本化 (レイアウト移行)。

llm-launcher の未追跡実行データ (モデル重み・起動 JSON/sh・HF キャッシュ) は
リポジトリの data/ (gitignore の /data/) に集約する。エンジン本体のコード /
.venv / build は module/ に残す (内部に絶対パスが焼き付いており移すと壊れる)。

置き場 (新):
  data/launch/          プロファイル起動 JSON + 同名 .sh / basalt・strata・maya 専用 config
  data/Freetoken-data/  FreeToken fork のモデル (<data>/models/<org>--<name>。setup.sh と同じ規約)
  data/Maya-data/       Maya のモデル (maya.py --models-dir がここを指す)
  data/Strata-data/     Strata のモデル (setup.py --data-dir / settings の data_dir)
  data/hf/              HF_HOME (hub キャッシュ = data/hf/hub)

migrate_data_layout() は起動時に旧置き場を data/ へ移す (冪等)。同一ファイル
システムなら rename で瞬時に終わり (bind mount 跨ぎ等の別デバイス間は
hardlink 保持コピー + 元削除)、プロファイル / config / 起動 JSON/sh の
絶対パス参照を新置き場へ書き換える。旧パスが消えて新パスが在る場合も参照だけ
書き換える (中断された移行の続きを毎回起動時に吸収する)。
"""
from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

# 起動 .sh が llm-launcher 生成物であることの目印 (_launch_sh_text のヘッダ)。
# 削除 / 移行で「ユーザーの手書きファイル」と生成物を区別するのに使う。
LAUNCH_SH_MARKER = "# llm-launcher 起動スクリプト"


def launch_dir(root: Path) -> Path:
    """プロファイル起動 JSON / 専用 config の置き場 (gitignore 済み)。"""
    return root / "data" / "launch"


def _remap_str(s: str, pairs: list[tuple[str, str]]) -> str:
    """旧パス接頭辞を新パスへ (ディレクトリ自身的一致も)。境界は / で判定。"""
    for old, new in pairs:
        if s == old:
            return new
        if s.startswith(old + os.sep):
            return new + s[len(old):]
    return s


def _remap_tree(obj: Any, pairs: list[tuple[str, str]]) -> tuple[Any, bool]:
    """JSON ツリー内の全文字列を _remap_str にかける (changed も返す)。
    辞書のキーも対象 (vision-memory.json はパス入りの合成キーで計測値を引く)。"""
    if isinstance(obj, str):
        r = _remap_str(obj, pairs)
        return r, r != obj
    if isinstance(obj, list):
        out = []
        changed = False
        for v in obj:
            r, c = _remap_tree(v, pairs)
            out.append(r)
            changed = changed or c
        return out, changed
    if isinstance(obj, dict):
        out_d = {}
        changed = False
        for k, v in obj.items():
            nk = _remap_str(k, pairs) if isinstance(k, str) else k
            r, c = _remap_tree(v, pairs)
            out_d[nk] = r
            changed = changed or c or nk != k
        return out_d, changed
    return obj, False


def _copy_tree(src: Path, dst: Path, seen: dict[tuple[int, int], Path]) -> None:
    """別デバイス間のコピー: hardlink / symlink / mtime を保つ。

    HF キャッシュの blobs↔snapshots のような hardlink を実体コピーすると
    容量が膨れるため、同一 inode は最初の 1 回だけコピーして残りは link する。"""
    dst.mkdir(parents=True, exist_ok=True)
    for child in sorted(src.iterdir()):
        target = dst / child.name
        st = child.lstat()
        if child.is_symlink():
            if not target.is_symlink():
                os.symlink(os.readlink(child), target)
        elif child.is_dir():
            _copy_tree(child, target, seen)
        else:
            key = (st.st_dev, st.st_ino)
            prev = seen.get(key)
            if prev is not None and prev.exists() and not target.exists():
                try:
                    os.link(prev, target)
                    continue
                except OSError:
                    pass
            shutil.copy2(child, target)
            seen[key] = target
    shutil.copystat(src, dst)


def _move_dir(src: Path, dst: Path) -> bool:
    """src を dst へ移す (同一 fs は rename で瞬時)。dst が在れば中身を合流。

    移したら True。rename できない別デバイス間 (bind mount 跨ぎ等) は
    hardlink 保持コピー + 元削除。コピー済みで元が残っている中断状態は
    サイズ + mtime 一致のファイルを元から消して再開する (再コピーしない)。
    内容の違うファイル名衝突は dst 側を尊重して src に残す (上書きしない)。"""
    if not src.is_dir() or src.is_symlink() or src.resolve() == dst.resolve():
        return False
    if not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.rename(src, dst)
            return True
        except OSError:
            pass
        _copy_tree(src, dst, {})
        shutil.rmtree(src)
        return True
    if not dst.is_dir():
        return False                    # ディレクトリ vs ファイルの衝突は触らない
    moved = False
    for child in sorted(src.iterdir()):
        target = dst / child.name
        if child.is_dir() and not child.is_symlink():
            moved = _move_dir(child, target) or moved
            continue
        if target.exists() or target.is_symlink():
            try:
                ss, ds = child.stat(), target.stat()
                same = ss.st_size == ds.st_size and int(ss.st_mtime) == int(ds.st_mtime)
            except OSError:
                same = False
            if same:
                child.unlink()          # コピー済みの中断残骸を掃除
                moved = True
            # 内容の違う衝突は dst を尊重 (src に残す)
        else:
            try:
                os.rename(child, target)
            except OSError:
                shutil.copy2(child, target)
                child.unlink()
            moved = True
    try:
        src.rmdir()                     # 空なら掃除 (残骸があれば残す)
    except OSError:
        pass
    return moved


def _strata_settings_file() -> Path:
    """純正 setup.py の settings_path と同じ解決 (STRATA_SETTINGS_FILE 優先)。"""
    raw = os.environ.get("STRATA_SETTINGS_FILE", "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "strata" / "settings.json"


def _old_strata_dirs(root: Path) -> list[Path]:
    """旧 Strata-data の候補 (settings の data_dir と clone 隣接の既定置き場)。"""
    cands: list[Path] = []
    try:
        st = json.loads(_strata_settings_file().read_text("utf-8"))
        dd = str(st.get("data_dir") or "").strip()
        if dd:
            cands.append(Path(dd).expanduser())
    except (OSError, ValueError):
        pass
    cands += [root / "module/Strata-data", root / "module/strata/Strata-data",
              root / "module/fork/strata/Strata-data", root / "module/fork/Strata/Strata-data"]
    out: list[Path] = []
    seen: set[Path] = set()
    data_r = (root / "data").resolve()
    for c in cands:
        try:
            r = c.resolve()
        except OSError:
            continue
        if r in seen or not c.is_dir() or data_r in r.parents or r == data_r:
            continue
        seen.add(r)
        out.append(c)
    return out


def _rewrite_json(path: Path, pairs: list[tuple[str, str]]) -> bool:
    """JSON ファイル内の旧パス参照を書き換える (changed ならアトミック書き込み)。"""
    try:
        doc = json.loads(path.read_text("utf-8", errors="replace"))
    except (OSError, ValueError):
        return False
    if not isinstance(doc, (dict, list)):
        return False
    new, changed = _remap_tree(doc, pairs)
    if not changed:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(new, ensure_ascii=False, indent=1) + "\n", "utf-8")
    tmp.replace(path)
    return True


def _rewrite_text(path: Path, pairs: list[tuple[str, str]]) -> bool:
    """テキストファイル (.sh 等) 内の旧パス参照を書き換える。

    境界は / かパス文字以外 (quote・空白・行末) — Freetoken-data2 のような
    別名を誤って書き換えない。"""
    import re
    try:
        text = path.read_text("utf-8")
    except OSError:
        return False
    new = text
    for old, repl in pairs:
        new = re.sub(re.escape(old) + r"(?![\w.-])", lambda _m: repl, new)
    if new == text:
        return False
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(new, "utf-8")
    tmp.replace(path)
    return True


def sh_owned(sh: Path) -> bool:
    """llm-launcher が生成した起動 .sh か (ヘッダの目印で判定)。"""
    try:
        return LAUNCH_SH_MARKER in sh.read_text("utf-8")[:200]
    except OSError:
        return False


def migrate_data_layout(root: Path, profiles: list[dict[str, Any]]) -> dict[str, int]:
    """旧置き場 (module 隣接の *-data / module 内の起動 JSON・sh / 既定 HF キャッシュ)
    を data/ へ移し、参照パスを書き換える。profiles は in-place で書き換える
    (保存は呼び出し側)。返り値は {dirs, pairs, files, profiles} の変更件数。

    冪等: 移行済みなら何もしない。旧ディレクトリが既に無くても参照だけ新パスへ
    書き換える (別マシンからコピーした config 等の吸収)。"""
    root = root.resolve()
    data = root / "data"
    data.mkdir(parents=True, exist_ok=True)
    stats = {"dirs": 0, "pairs": 0, "files": 0, "profiles": 0}
    pairs: list[tuple[str, str]] = []
    # root 外の置き場 (settings が指す Strata-data / 既定 HF キャッシュ) を動かして
    # 良いのは実際の launcher checkout だけ (find_root と同じ目印)。tmp root の
    # テスト等から実機のモデル置き場を移してしまわないための門。
    real_root = (root / "module").is_dir() and (root / "pyproject.toml").is_file()

    def plan(src: Path, dst: Path) -> None:
        """移動 (実在すれば) + 参照書き換え用の旧→新ペア登録。"""
        try:
            s, d = src.resolve(), dst.resolve()
        except OSError:
            return
        if s == d or data.resolve() in s.parents:
            return                  # 既に data/ 配下は対象外
        if _move_dir(src, dst):
            stats["dirs"] += 1
        pairs.append((str(s), str(d)))

    # 1) モデル / HF キャッシュのディレクトリ移動
    plan(root / "module/fork/Freetoken-data", data / "Freetoken-data")
    plan(root / "module/Maya-data", data / "Maya-data")
    for old in _old_strata_dirs(root):
        try:
            outside = root not in old.resolve().parents
        except OSError:
            continue
        if outside and not real_root:
            continue
        plan(old, data / "Strata-data")
    # HF キャッシュ: 明示 env (HF_HUB_CACHE / 別の HF_HOME) が無いときだけ
    # 既定 (~/.cache/huggingface) を data/hf へ移す。main() が HF_HOME=data/hf を
    # 既定設定するので、その場合もここを通る。
    hub_env = os.environ.get("HF_HUB_CACHE", "").strip()
    home_env = os.environ.get("HF_HOME", "").strip()
    if real_root and not hub_env and (not home_env
                                      or Path(home_env).expanduser().resolve() == (data / "hf").resolve()):
        xdg = os.environ.get("XDG_CACHE_HOME", "").strip()
        old_hf = Path(xdg).expanduser() / "huggingface" if xdg else Path.home() / ".cache/huggingface"
        plan(old_hf, data / "hf")

    # 2) 起動 JSON + 同名 .sh の一組を data/launch/ へ (旧: module/fork/freetoken)。
    #    プロファイルが参照しているか、.sh に生成目印があるものだけ移す
    #    (fork 純正の run-*.sh 等ユーザー / upstream のファイルは触らない)。
    launch = launch_dir(root)
    launch.mkdir(parents=True, exist_ok=True)
    refs: set[Path] = set()
    for p in profiles:
        raw = str((p.get("values") or {}).get("config") or "").strip()
        if raw:
            cp = Path(raw).expanduser()
            try:
                refs.add((cp if cp.is_absolute() else root / cp).resolve())
            except OSError:
                pass
    for mod in (root / "module/fork/freetoken", root / "module/freetoken"):
        if not mod.is_dir():
            continue
        for j in sorted(mod.glob("*.json")):
            sh = j.with_suffix(".sh")
            try:
                jr = j.resolve()
            except OSError:
                continue
            if jr not in refs and not sh_owned(sh):
                continue
            dest = launch / j.name
            pairs.append((str(jr), str(dest.resolve())))
            if sh.is_file():
                pairs.append((str(sh.resolve()), str(dest.with_suffix(".sh").resolve())))
            if dest.exists():
                continue            # 名前衝突は data/ 側を尊重 (参照だけ書き換わる)
            try:
                os.rename(j, dest)
            except OSError:
                shutil.move(str(j), str(dest))
            if sh.is_file():
                try:
                    os.rename(sh, dest.with_suffix(".sh"))
                except OSError:
                    shutil.move(str(sh), str(dest.with_suffix(".sh")))
            stats["pairs"] += 1

    if not pairs:
        return stats

    # 3) 参照書き換え: プロファイル (in-place) / 起動 JSON・sh / installer config /
    #    キャリブレーション / インストール記録 / Strata settings
    for p in profiles:
        new, changed = _remap_tree(p, pairs)
        if changed:
            p.clear()
            p.update(new)
            stats["profiles"] += 1
    json_files: list[Path] = []
    json_files += sorted(launch.glob("*.json"))
    json_files += sorted((data / "calibrations").glob("*.json"))
    json_files += [data / "calibrations.json", data / "installs.json",
                   data / "bench_profiles.json", data / "settings.json"]
    from . import strata_setup
    for mod in (root / "module/strata", root / "module/fork/strata",
                root / "module/fork/Strata"):
        if mod.is_dir():
            json_files += sorted(strata_setup.setup_dir(mod).glob("strata-*.json"))
    maya = root / "module/maya"
    if maya.is_dir():
        json_files += sorted(maya.glob("maya-*.json"))
    # fork の setup.sh 相当の起動 JSON テンプレート (installer.seed_launch_json)
    json_files += [root / "module/fork/freetoken/freetoken-launch.json",
                   root / "module/freetoken/freetoken-launch.json"]
    for f in json_files:
        if f.is_file() and _rewrite_json(f, pairs):
            stats["files"] += 1
    for f in sorted(launch.glob("*.sh")):
        if _rewrite_text(f, pairs):
            stats["files"] += 1
    st = _strata_settings_file()
    if st.is_file() and _rewrite_json(st, pairs):
        stats["files"] += 1
    return stats
