"""スマートフォン幅でカードの枠がページ端からはみ出さないための契約。

暗黙の grid トラックは min-content を下限に取るため、長いプロファイル名 / パス /
操作ボタン群がカード自身を 390px 画面で 450〜500px まで広げていた。カードを
画面幅に従わせる CSS (minmax(0,1fr) + min-width:0) と、その内部の
.kvs / .m-hd の縮小・折返しを固定する。
"""
import re
from pathlib import Path

CSS = (Path(__file__).resolve().parents[1] / "src" / "llm_launcher" / "static"
       / "css" / "app.css").read_text(encoding="utf-8")


def test_grid_cards_can_shrink_to_the_page_width():
    # カードを抱える grid は暗黙の min-content トラックにせず、画面幅 1 列を
    # 0 下限で張らせる (.grid / .dash-2col はホーム・起動・プロファイル・設定画面)
    assert re.search(
        r"\.grid\s*,\s*\.dash-2col\s*\{[^}]*grid-template-columns:\s*minmax\(0,\s*1fr\)",
        CSS)
    # その項目自身も min-content 幅でトラックを押し広げない
    assert re.search(
        r"\.grid>\*\s*,\s*\.dash-2col>\*\s*\{[^}]*min-width:\s*0",
        CSS)


def test_kvs_value_column_does_not_force_card_width():
    # カード内の「モデル / ポート / 備考」2 列値。値の長さがカード幅を決めて
    # ページ端を越えないよう 0 下限 + min-width:0 の両方を守る
    assert re.search(r"\.kvs\s*\{[^}]*grid-template-columns:\s*auto\s+minmax\(0,\s*1fr\)", CSS)
    assert re.search(r"\.kvs\s*\.v\s*\{[^}]*min-width:\s*0", CSS)


def test_meter_header_wraps_on_a_narrow_screen():
    # OOM カードの「42GB 空き / 予約 8GB · 使用 66%" は nowrap の右寄せ値。
    # 320px ではラベルと値が 1 行に収まらず、m-hd が折り返せる必要がある
    assert re.search(r"\.m-hd\s*\{[^}]*flex-wrap:\s*wrap", CSS)
