"""server.py のロジックテスト（ネットワーク・実データに触らない）。

$KAUMONO_DATA を一時ディレクトリへ向けてから import する必要がある
（server.py はモジュール読み込み時に STOCK_FILE を確定させるため）。

    python -m unittest discover -s tests
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.mkdtemp(prefix="kaumono-test-")
os.environ["KAUMONO_DATA"] = _TMP

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import server  # noqa: E402
import seed  # noqa: E402


class StockServerTest(unittest.TestCase):
    def setUp(self):
        server.save_state(server.empty_state())
        server.app.config["TESTING"] = True
        self.c = server.app.test_client()

    def state(self):
        return json.loads(server.STOCK_FILE.read_text(encoding="utf-8"))

    def add_genre(self, name="風呂"):
        return self.c.post("/api/genres", json={"name": name}).get_json()

    def add_area(self, name="洗面所"):
        return self.c.post("/api/areas", json={"name": name}).get_json()

    def add_item(self, gid, name="シャンプー", **kw):
        return self.c.post("/api/items", json={"genre": gid, "name": name, **kw}).get_json()

    # ---- ジャンル

    def test_genre_add_and_duplicate(self):
        g = self.add_genre()
        self.assertTrue(g["id"])
        self.assertEqual(self.c.post("/api/genres", json={"name": "風呂"}).status_code, 409)

    def test_genre_delete_requires_empty(self):
        g = self.add_genre()
        self.add_item(g["id"])
        self.assertEqual(self.c.delete(f"/api/genres/{g['id']}").status_code, 409)

    def test_japanese_name_gets_usable_id(self):
        """日本語名は slug が空になるため乱数IDに落ちる（URLに載る以上 ASCII が要る）。"""
        g = self.add_genre("薬味・チューブ")
        self.assertTrue(g["id"])
        self.assertTrue(all(ord(c) < 128 for c in g["id"]))

    # ---- 品目

    def test_item_defaults_and_channel_filtering(self):
        g = self.add_genre()
        item = self.add_item(g["id"], channels=["net", "でたらめ"])
        self.assertEqual(item["status"], "ok")
        self.assertEqual(item["channels"], ["net"])
        self.assertIsNone(item["tag"])

    def test_item_requires_known_genre(self):
        r = self.c.post("/api/items", json={"genre": "nope", "name": "x"})
        self.assertEqual(r.status_code, 400)

    def test_status_change_records_history_once(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.c.post(f"/api/items/{item['id']}", json={"status": "want"})
        self.c.post(f"/api/items/{item['id']}", json={"status": "want"})
        after = self.state()["items"][0]
        self.assertEqual(after["status"], "want")
        self.assertEqual(len(after["history"]), 1)

    # ---- タグ

    def test_tap_sets_out_and_is_idempotent(self):
        g = self.add_genre()
        item = self.add_item(g["id"], tag="07")
        first = self.c.post("/api/tags/07/tap").get_json()
        self.assertTrue(first["bound"])
        self.assertTrue(first["changed"])
        self.assertEqual(first["item"]["status"], "want")
        # NFC の二度読み・リロードで履歴が汚れないこと
        second = self.c.post("/api/tags/07/tap").get_json()
        self.assertFalse(second["changed"])
        self.assertEqual(len(self.state()["items"][0]["history"]), 1)

    def test_tap_unknown_tag_reports_unbound(self):
        r = self.c.post("/api/tags/zzz/tap")
        self.assertEqual(r.status_code, 404)
        self.assertFalse(r.get_json()["bound"])

    def test_tag_cannot_be_bound_twice(self):
        g = self.add_genre()
        self.add_item(g["id"], name="A", tag="07")
        b = self.add_item(g["id"], name="B")
        self.assertEqual(self.c.post("/api/tags/07/bind", json={"item_id": b["id"]}).status_code, 409)
        # 別品目への tag 付け替えも弾く
        self.assertEqual(self.c.post(f"/api/items/{b['id']}", json={"tag": "07"}).status_code, 409)

    def test_tap_by_item_id(self):
        """品目IDでも叩ける。タグに書くURLを品目側から配れるようにするため。"""
        g = self.add_genre()
        item = self.add_item(g["id"])
        got = self.c.post(f"/api/tags/{item['id']}/tap").get_json()
        self.assertTrue(got["bound"])
        self.assertEqual(got["item"]["id"], item["id"])
        self.assertEqual(got["item"]["status"], "want")

    def test_rename_does_not_break_the_id_url(self):
        """名前を変えても貼ったタグは生きている（URLの元がIDなので）。"""
        g = self.add_genre()
        item = self.add_item(g["id"], name="トイレットペーパー")
        self.c.post(f"/api/items/{item['id']}", json={"name": "トイレットペーパー（12ロール）"})
        got = self.c.post(f"/api/tags/{item['id']}/tap").get_json()
        self.assertEqual(got["item"]["name"], "トイレットペーパー（12ロール）")
        self.assertTrue(got["bound"])

    def test_tag_uniqueness_ignores_ids(self):
        """タグの重複判定はタグ文字列だけを見る（IDと混ざらない）。"""
        g = self.add_genre()
        a = self.add_item(g["id"], name="A")
        b = self.add_item(g["id"], name="B")
        r = self.c.post(f"/api/items/{b['id']}", json={"tag": a["id"]})
        self.assertEqual(r.status_code, 200)

    def test_bind_then_tap(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.c.post("/api/tags/12/bind", json={"item_id": item["id"]})
        self.assertTrue(self.c.post("/api/tags/12/tap").get_json()["bound"])

    # ---- エリア（置き場所）

    def test_area_add_and_delete_guard(self):
        a = self.add_area()
        g = self.add_genre()
        self.add_item(g["id"], area=a["id"])
        self.assertEqual(self.c.delete(f"/api/areas/{a['id']}").status_code, 409)

    def test_area_is_independent_of_genre(self):
        """同じ種類でも置き場所は別々に持てる（ドメスト=トイレ / スクバ=風呂）。"""
        toilet, bath = self.add_area("トイレ"), self.add_area("風呂")
        g = self.add_genre("掃除")
        self.add_item(g["id"], name="ドメスト", area=toilet["id"])
        self.add_item(g["id"], name="スクラビングバブル", area=bath["id"])
        got = {i["name"]: i["area"] for i in self.state()["items"]}
        self.assertEqual(got["ドメスト"], toilet["id"])
        self.assertEqual(got["スクラビングバブル"], bath["id"])

    # ---- URLの正規化

    def test_url_is_normalized_on_save(self):
        g = self.add_genre()
        item = self.add_item(
            g["id"],
            url="https://www.amazon.co.jp/ctunk-NFC/dp/B0GCM6222Q/ref=asc_df?tag=jpgo-22&th=1",
        )
        self.assertEqual(item["url"], "https://www.amazon.co.jp/dp/B0GCM6222Q")
        got = self.c.post(f"/api/items/{item['id']}",
                          json={"url": "https://item.rakuten.co.jp/a/b/?s-id=smt_top"}).get_json()
        self.assertEqual(got["url"], "https://item.rakuten.co.jp/a/b/")

    def test_unknown_url_shapes_are_left_alone(self):
        """分からない形は壊さずそのまま（短縮リンクの解決は normalize_urls.py の仕事）。"""
        for url in ("https://amzn.asia/d/07E9cMcZ", "https://example.com/x?size=L", "", "メモ"):
            self.assertEqual(server.normalize_url(url), url)

    # ---- 並べ替え

    def test_reorder_areas(self):
        ids = [self.add_area(n)["id"] for n in ("台所", "風呂場", "玄関")]
        self.c.post("/api/reorder", json={"kind": "areas", "ids": [ids[2], ids[0], ids[1]]})
        self.assertEqual([a["id"] for a in self.state()["areas"]], [ids[2], ids[0], ids[1]])

    def test_reorder_subset_leaves_others_in_place(self):
        """画面に出ている分だけ並べ替えても、映っていない品目の位置は動かない。"""
        g1, g2 = self.add_genre("掃除"), self.add_genre("洗濯")
        a = self.add_item(g1["id"], name="A")
        x = self.add_item(g2["id"], name="X")   # 別ジャンル（画面に出ていない）
        b = self.add_item(g1["id"], name="B")
        self.c.post("/api/reorder", json={"kind": "items", "ids": [b["id"], a["id"]]})
        self.assertEqual([i["name"] for i in self.state()["items"]], ["B", "X", "A"])
        self.assertEqual(self.state()["items"][1]["id"], x["id"])

    def test_reorder_rejects_bad_input(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.assertEqual(self.c.post("/api/reorder", json={"kind": "nope", "ids": []}).status_code, 400)
        self.assertEqual(
            self.c.post("/api/reorder", json={"kind": "items", "ids": [item["id"], "itm_ghost"]}).status_code,
            400,
        )
        # 重複した id で要素を取り違えないこと
        self.assertEqual(
            self.c.post("/api/reorder", json={"kind": "items", "ids": [item["id"], item["id"]]}).status_code,
            400,
        )

    # ---- 買った記録

    def test_bought_records_and_resets_status(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.c.post(f"/api/items/{item['id']}", json={"status": "want"})
        got = self.c.post(f"/api/items/{item['id']}/bought", json={}).get_json()
        self.assertEqual(got["status"], "ok")
        self.assertEqual(len(got["bought"]), 1)
        self.assertEqual(got["last_bought"], got["bought"][0])

    def test_status_change_does_not_record_purchase(self):
        """間違って「ある」に戻しただけで購入日が増えては困る。"""
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.c.post(f"/api/items/{item['id']}", json={"status": "want"})
        self.c.post(f"/api/items/{item['id']}", json={"status": "ok"})
        self.assertEqual(self.state()["items"][0]["bought"], [])

    def test_bought_bulk(self):
        """通販サイト単位の「全部買った」。まとめて1回で片づく。"""
        g = self.add_genre()
        ids = [self.add_item(g["id"], name=n)["id"] for n in ("A", "B", "C")]
        for i in ids:
            self.c.post(f"/api/items/{i}", json={"status": "want"})
        got = self.c.post("/api/bought", json={"ids": ids[:2]}).get_json()
        self.assertEqual(got["count"], 2)
        after = {i["name"]: i for i in self.state()["items"]}
        self.assertEqual([after[n]["status"] for n in ("A", "B", "C")], ["ok", "ok", "want"])
        self.assertEqual(len(after["A"]["bought"]), 1)
        self.assertEqual(after["C"]["bought"], [])

    def test_bought_bulk_rejects_unknown_id_without_writing(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.c.post(f"/api/items/{item['id']}", json={"status": "want"})
        r = self.c.post("/api/bought", json={"ids": [item["id"], "itm_ghost"]})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(self.state()["items"][0]["status"], "want")

    def test_bought_undo(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        self.c.post(f"/api/items/{item['id']}/bought", json={})
        self.c.post(f"/api/items/{item['id']}/bought", json={})
        self.assertEqual(len(self.state()["items"][0]["bought"]), 2)
        got = self.c.post(f"/api/items/{item['id']}/bought", json={"undo": True}).get_json()
        self.assertEqual(len(got["bought"]), 1)
        # 全部消えたら last_bought も空に戻る
        got = self.c.post(f"/api/items/{item['id']}/bought", json={"undo": True}).get_json()
        self.assertEqual(got["bought"], [])
        self.assertIsNone(got["last_bought"])

    def test_legacy_last_bought_becomes_list(self):
        g = self.add_genre()
        item = self.add_item(g["id"])
        raw = self.state()
        raw["items"][0].pop("bought", None)
        raw["items"][0]["last_bought"] = "2026-07-01T10:00:00+09:00"
        server.STOCK_FILE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(server.load_state()["items"][0]["bought"], ["2026-07-01T10:00:00+09:00"])
        self.assertTrue(item["id"])

    # ---- 保存

    def test_legacy_three_value_status_migrates(self):
        """旧 low/out のデータは読み込み時に want へ寄せる。"""
        g = self.add_genre()
        item = self.add_item(g["id"])
        raw = self.state()
        raw["items"][0]["status"] = "low"
        server.STOCK_FILE.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
        self.assertEqual(server.load_state()["items"][0]["status"], "want")
        self.assertEqual(item["status"], "ok")

    def test_state_survives_corrupt_file(self):
        server.STOCK_FILE.write_text("{壊れている", encoding="utf-8")
        self.assertEqual(server.load_state(), server.empty_state())

    def test_pages_are_served(self):
        for path in ("/", "/g/bath", "/a/kitchen", "/s/Amazon", "/c/super", "/t/07", "/tags", "/favicon.svg",
                     "/static/manifest.json", "/static/icon-192.png",
                     "/static/icon-512.png", "/static/icon-maskable-512.png",
                     "/static/apple-touch-icon.png"):
            self.assertEqual(self.c.get(path).status_code, 200, path)


class SeedTest(unittest.TestCase):
    def test_seed_is_consistent(self):
        state = seed.build()
        gids = {g["id"] for g in state["genres"]}
        aids = {a["id"] for a in state["areas"]}
        self.assertEqual(len(gids), len(state["genres"]), "ジャンルIDが重複している")
        self.assertEqual(len(aids), len(state["areas"]), "エリアIDが重複している")
        for item in state["items"]:
            self.assertIn(item["genre"], gids)
            self.assertIn(item["area"], aids, f"{item['name']} の置き場所が未定義")
            self.assertIn(item["status"], server.STATUSES)
            self.assertTrue(item["name"])
            for ch in item["channels"]:
                self.assertIn(ch, server.CHANNELS)
        # 品目名はジャンル内で一意（同名が並ぶとタグ紐付け時に選べない）
        for gid in gids:
            names = [i["name"] for i in state["items"] if i["genre"] == gid]
            self.assertEqual(len(names), len(set(names)), f"{gid} に同名の品目がある")


if __name__ == "__main__":
    unittest.main()
