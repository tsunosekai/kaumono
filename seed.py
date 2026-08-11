#!/usr/bin/env python3
"""初期データの投入（新しく建てたときの叩き台）。

$KAUMONO_DATA/stock.json が無いときだけ書き込む。既にあれば何もしない
（UI で編集したものを踏み潰さないため）。作り直すなら --force。

    python3 seed.py [--force] [--dry-run]

ここに並べてあるのは「置き場所（エリア）」「種類（ジャンル）」「品目」の
関係を掴むための最小限の例。自分の家に合わせて書き換えるか、空のまま
起動して UI から足していってよい。

エリア（どこに置いてあるか）とジャンル（何か）は直交させてある。同じ「掃除」
でも洗剤はトイレ、別の洗剤は風呂に置いてあるので、ジャンルの下にエリアを
ぶら下げると必ず破綻する。
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402  (STOCK_FILE / save_state を共有する)

AREAS = [
    ("kitchen", "キッチン"),
    ("fridge", "冷蔵庫"),
    ("pantry", "食料庫（常温）"),
    ("washroom", "洗面所"),
    ("bath", "風呂"),
    ("toilet", "トイレ"),
    ("laundry", "洗濯機まわり"),
    ("living", "リビング"),
]

# (ジャンルID, ジャンル名, [(一般名, 置き場所, 買い方), ...])
# ジャンルIDは URL（/g/<id>）に出るので ASCII を手で与える
# 買い方: super / conbini / drug / net / other
GENRES = [
    ("bath", "風呂・洗面", [
        ("シャンプー", "bath", ["net", "drug"]),
        ("ボディソープ", "bath", ["net", "drug"]),
        ("ハンドソープ", "washroom", ["net", "drug"]),
        ("歯磨き粉", "washroom", ["drug"]),
        ("歯ブラシ", "washroom", ["drug"]),
    ]),
    ("paper", "紙もの", [
        ("トイレットペーパー", "toilet", ["net"]),
        ("ティッシュ", "living", ["net"]),
        ("キッチンペーパー", "kitchen", ["net"]),
    ]),
    ("kitchen", "キッチン消耗品", [
        ("食器用洗剤", "kitchen", ["net", "drug"]),
        ("スポンジ", "kitchen", ["drug"]),
        ("ラップ", "kitchen", ["net", "super"]),
        ("ゴミ袋", "kitchen", ["super"]),
    ]),
    ("laundry", "洗濯", [
        ("洗濯洗剤", "laundry", ["net", "drug"]),
        ("柔軟剤", "laundry", ["net", "drug"]),
    ]),
    ("cleaning", "掃除", [
        ("トイレ用洗剤", "toilet", ["drug"]),
        ("風呂用洗剤", "bath", ["drug"]),
        ("掃除用ウェットシート", "living", ["net"]),
    ]),
    ("seasoning", "基本調味料", [
        ("醤油", "pantry", ["super"]),
        ("塩", "pantry", ["super"]),
        ("砂糖", "pantry", ["super"]),
        ("味噌", "fridge", ["super"]),
        ("みりん", "pantry", ["super"]),
        ("酢", "pantry", ["super"]),
        ("サラダ油", "pantry", ["super"]),
        ("ごま油", "pantry", ["super"]),
    ]),
    ("fridge", "冷蔵常備", [
        ("卵", "fridge", ["super"]),
        ("牛乳", "fridge", ["super"]),
        ("バター", "fridge", ["super"]),
    ]),
    ("staple", "主食", [
        ("米", "pantry", ["net", "super"]),
        ("パスタ", "pantry", ["super"]),
    ]),
]


def build() -> dict:
    state = server.empty_state()
    now = server.now_iso()

    for aid, name in AREAS:
        state["areas"].append({"id": aid, "name": name})

    for gid, gname, rows in GENRES:
        state["genres"].append({"id": gid, "name": gname})
        for name, area, channels in rows:
            state["items"].append({
                "id": server.new_id("itm"),
                "genre": gid, "area": area, "name": name,
                "product": "", "url": "", "channels": channels,
                "status": "ok", "tag": None, "note": "",
                "updated_at": now, "last_bought": None, "bought": [], "history": [],
            })
    return state


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="既存のデータを上書きする")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    state = build()
    print(f"genres={len(state['genres'])} items={len(state['items'])} -> {server.STOCK_FILE}")
    if args.dry_run:
        return 0
    if server.STOCK_FILE.exists() and not args.force:
        print("既にデータがあるので何もしない（上書きするなら --force）")
        return 0
    server.save_state(state)
    print("書き込んだ")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
