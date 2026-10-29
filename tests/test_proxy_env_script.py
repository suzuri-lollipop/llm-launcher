"""プロキシの systemd サービスへの受け渡し (scripts/proxy-env-from-bashrc.sh)。

systemd サービスは .bashrc を読まない (非ログイン / 非対話)。そのため
systemd-install.sh / oom-protect-install.sh はインストール時に .bashrc の
export *_PROXY を EnvironmentFile へ抽出して Unit に EnvironmentFile= で結ぶ。
抽出器の書式 (両形 / 後勝ち / コメント除去) と、両インストーラの実配線を Here に固定する。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
EXTRACTOR = SCRIPTS / "proxy-env-from-bashrc.sh"


def extract(tmp_path: Path, bashrc: str) -> str:
    src = tmp_path / "bashrc"
    src.write_text(bashrc, "utf-8")
    r = subprocess.run(["bash", str(EXTRACTOR), str(src)],
                       capture_output=True, text=True, timeout=15, check=False)
    assert r.returncode == 0, r.stderr
    return r.stdout


def test_exports_extracted_in_both_cases(tmp_path: Path):
    out = extract(tmp_path, 'export HTTP_PROXY="http://proxy.local:3128"\n')
    lines = out.splitlines()
    assert "http_proxy=http://proxy.local:3128" in lines      # 小文字形
    assert "HTTP_PROXY=http://proxy.local:3128" in lines      # 大文字形も両形
    assert 'export' not in out and '"' not in out             # quote は剥がれる


def test_no_proxy_with_spaces_and_quotes(tmp_path: Path):
    out = extract(tmp_path, "export no_proxy='localhost, 127.0.0.1, .internal'\n")
    assert "NO_PROXY=localhost, 127.0.0.1, .internal" in out.splitlines()
    assert "NO_BOGUS" not in out


def test_last_line_wins_and_empty_untsets(tmp_path: Path):
    out = extract(tmp_path, (
        'export https_proxy="http://first:3128"\n'
        'export https_proxy="http://second:3128"\n'
    ))
    assert out.count("https_proxy=") == 1 and "second" in out
    out2 = extract(tmp_path, (
        'export https_proxy="http://first:3128"\n'
        'export https_proxy=""\n'                             # 最後の空値 = 未設定
    ))
    assert out2 == ""


def test_comments_and_non_exports_ignored(tmp_path: Path):
    out = extract(tmp_path, (
        "# export HTTPS_PROXY=http://commented\n"
        "export ALL_PROXY=socks5://10.0.0.1:1080 # trailing comment\n"
        "HTTPS_PROXY=http://plain-assignment\n"               # export でない行は対象外
        'export PATH="/usr/local/bin:$PATH"\n'                # *_proxy 以外は素通し
    ))
    assert out.count("ALL_PROXY=") == 1 and "# trailing" not in out
    assert "commented" not in out and "plain-assignment" not in out and "PATH" not in out


def test_dollar_expansion_warned_and_skipped(tmp_path: Path):
    """EnvironmentFile は $VAR を展開しないので、展開依存の値は警告付きで見送る。"""
    src = tmp_path / "bashrc"
    src.write_text('export HTTP_PROXY="http://$CORP_USER@proxy.local:3128"\n'
                   'export NO_PROXY=localhost\n', "utf-8")
    r = subprocess.run(["bash", str(EXTRACTOR), str(src)],
                       capture_output=True, text=True, timeout=15, check=False)
    assert r.returncode == 0
    assert "HTTP_PROXY" not in r.stdout and "スキップ" in r.stderr
    assert "no_proxy=localhost" in r.stdout                   # 無関係な行は普通に抽出


def test_missing_file_is_quiet_failure(tmp_path: Path):
    r = subprocess.run(["bash", str(EXTRACTOR), str(tmp_path / "nope")],
                       capture_output=True, text=True, timeout=15, check=False)
    assert r.returncode == 0 and r.stdout == "" and r.stderr  # 警告のみ / 空出力


@pytest.mark.parametrize("script", ["systemd-install.sh", "oom-protect-install.sh"])
def test_installers_wire_env_file_into_unit(script: str):
    """両インストーラが抽出器を実行し、ユニットに EnvironmentFile= で結んでいること。"""
    src = (SCRIPTS / script).read_text("utf-8")
    assert "proxy-env-from-bashrc.sh" in src                  # .bashrc から抽出して…
    assert "EnvironmentFile=-$ENV_FILE" in src                # …Unit に差し込む (- = 無しも許容)
    assert '"$UNIT" "$ENV_FILE"' in src                       # uninstall で env ファイルも消す
