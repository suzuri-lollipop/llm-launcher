"""モデルダウンロード専用の子プロセス (installer.py が起動する)。

snapshot_download を本体プロセス (スレッド) で走らせない理由:
スレッドは殺せないため、中断は tqdm フックの協調フラグ頼みになるが、
その確認は「バイトが到着した瞬間」にしか来ない。転送が詰まっていると
いつまでも返ってこず、「中断しても止まらない」原因になっていた。
子プロセスなら親が SIGTERM → (猶予経過で) SIGKILL をプロセスグループへ
送って確実に止められる。ここが強制中断の実装そのもの。

契約 (installer.py との NDJSON プロトコル):
  起動   : python dlchild.py <config.json>
  stdout : "#@ " で始まる行だけがイベント。他行は親がログとして拾う
    {"ev":"prog","done":B,"file_total":B,"file":str,"files_done":int}
    {"ev":"done","path":str}   成功 (終了コード 0)
    {"ev":"err","msg":str}     失敗 (終了コード 1)
  stderr : そのまま親のログに流れる。
  SIGTERM : 既定動作 (プロセス即消滅) に任せる。フック側の協調停止より
    確実なのでハンドラは張らない。

速度上限 (config.max_kbps) はこのプロセス内でトークンバケット適用。
hf_transfer / Xet は Rust 側で直接受けて Python を素通りするため
スロットルが効かない → 上限指定時は両者を無効化する (親も env を送るが、
単体起動でも効くようにここでも念押しする)。
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any


def emit(obj: dict[str, Any]) -> None:
    sys.stdout.write("#@ " + json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


class _Throttle:
    """update() で眠って平均速度を上限以下に抑える。スレッド間でロックし、
    睡眠中は全 worker を止めるので合計スループットが上限に収束する。"""

    def __init__(self, kbps: int) -> None:
        self.bps = max(1024, kbps * 1024)
        self.t0 = time.monotonic()
        self.received = 0
        self._lock = threading.Lock()

    def consume(self, n: int) -> None:
        if n <= 0:
            return
        with self._lock:
            self.received += n
            # 超過分を返すまで次のチャンクを通さない。睡眠は長くても 2s に
            # 刻む (刻んでもロックは持ち続けるので worker 全体が止まる)。
            # 1 回 5s 上限では 10MB チャンクに対し追いつけず実速が膨らむ。
            while True:
                over = self.received - (time.monotonic() - self.t0) * self.bps
                if over <= 0:
                    return
                time.sleep(min(2.0, over / self.bps))


def _make_hook(throttle: "_Throttle | None", state: dict[str, Any]) -> type:
    """snapshot_download に渡す tqdm サブクラス (進捗 emit とスロットル)。
    disable=True で描画を殺す。huggingface_hub 2.x は集約バー
    (name=...transfer) を先に作り子バーの update がそこへ流れるので、
    集約バーが見えたらそこだけ数える (二重計数回避)。"""
    from tqdm.auto import tqdm

    agg_name = "huggingface_hub.snapshot_download.transfer"
    rec_name = "huggingface_hub.snapshot_download"
    skip_descs = ("Downloading bucket files",)  # xet の再構成側バー (受信量と重複する)
    last = [0.0]

    def push(force: bool = False) -> None:
        t = time.monotonic()
        if not force and t - last[0] < 0.4:
            return
        last[0] = t
        emit({"ev": "prog", **state})

    class _Hook(tqdm):  # type: ignore[misc,valid-type]
        _saw_agg = False  # クラス属性: 集約転送バーが登場したら True

        def __init__(self, *args: Any, **kw: Any) -> None:  # noqa: D107
            # disable=True だと unit/desc が未設定になるので引数から控えておく
            self._hook_unit = kw.get("unit", "it")
            self._hook_desc = kw.get("desc") or ""
            self._hook_agg = (kw.get("name") or "") == agg_name
            if self._hook_agg:
                type(self)._saw_agg = True
            kw["disable"] = True
            super().__init__(*args, **kw)

        def _counts(self) -> bool:
            if self._hook_unit != "B":
                return False
            if _Hook._saw_agg:
                return self._hook_agg  # 集約バーのみ (子バーの転送分で二重計数しない)
            d = self._hook_desc
            return d not in skip_descs and not d.startswith("Reconstructing")

        def update(self, n: float = 1) -> None:  # noqa: D102
            super().update(n)
            if self._counts() and n:
                state["done"] += int(n)
                if throttle:
                    throttle.consume(int(n))
                if self.total:
                    state["file_total"] = int(self.total)
                state["file"] = getattr(self, "desc", "") or self._hook_desc
                push()

        def set_description(self, desc: str = "", refresh: bool = True) -> None:  # noqa: D102
            self._hook_desc = desc or ""

        def close(self) -> None:  # noqa: D102
            if not getattr(self, "_hook_counted", False) and self._hook_unit == "B" \
                    and not self._hook_agg and self._hook_desc != rec_name:
                self._hook_counted = True
                state["files_done"] += 1
            super().close()
            push(True)  # close は取りこぼしやすいので必ず送り込む

    return _Hook


def main(argv: list[str]) -> int:
    with open(argv[1], encoding="utf-8") as f:
        cfg = json.load(f)
    kb = int(cfg.get("max_kbps") or 0)
    if kb > 0:
        # スロットルは Python の受信経路でしか効かない → バイパスを落とす
        os.environ["HF_HUB_ENABLE_HF_TRANSFER"] = "0"
        os.environ["HF_HUB_DISABLE_XET"] = "1"
    try:
        from huggingface_hub import snapshot_download
        if kb > 0:
            # http_get の受信チャンク既定は 10MB。スロットルはチャンク境界で
            # しか効かないので刻みを落とし、バーストを実速の 1 秒分に近づける
            import huggingface_hub.constants as _hfc
            _hfc.DOWNLOAD_CHUNK_SIZE = max(4096, min(262144, kb * 1024))
    except Exception as e:  # noqa: BLE001
        emit({"ev": "err", "msg": f"huggingface_hub が使えません: {e}"})
        return 1
    state = {"done": 0, "file_total": 0, "file": "", "files_done": 0}
    try:
        path = snapshot_download(
            repo_id=cfg["repo_id"],
            revision=cfg.get("revision") or None,
            allow_patterns=cfg.get("allow_patterns") or None,
            local_dir=cfg.get("local_dir") or None,
            tqdm_class=_make_hook(_Throttle(kb) if kb > 0 else None, state),
            max_workers=8,
        )
        emit({"ev": "done", "path": str(path)})
        return 0
    except KeyboardInterrupt:
        return 130
    except BaseException as e:  # noqa: BLE001
        emit({"ev": "err", "msg": f"{type(e).__name__}: {e}"})
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
