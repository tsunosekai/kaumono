#!/usr/bin/env python3
"""kaumono — 買うものリスト（日用品・食料の在庫台帳）+ NFCタップ受け。

「切れた」を、切れたその瞬間・その場で記録するための小さなサーバ。
記録の入口は2つある:

  1. NFCタグ（NTAG215 など）に `/t/<tag>` を書いておき、スマホでかざす
     → 「欲しい」が立って確認画面が出る（Android はかざすと既定ブラウザが
       勝手に開くので、実質「かざす」だけで完了する）
  2. 一覧ページのボタン（タグを用意できない・貼れない品目用）

タグは「貼ってから紐付ける」方式。未知の tag に来ても 404 にせず紐付け画面を
出し、その場で既存品目に結び付けるか新規登録できる。何十枚ものタグを事前に
採番して書き分ける手間を無くすため（タグには /t/01〜/t/99 のような適当な
連番を書いておけばよい）。

認証は持たない。家庭内・VPN内（Tailscale など）に置く前提で、公開範囲は
ネットワーク層で絞る。タグに書く URL はシール表面から誰でも読めるので、
秘密を置く場所ではない。

データは $KAUMONO_DATA/stock.json（既定 ~/kaumono-data）。書き込みは必ず
save_state() 経由（tmp + os.replace の原子的置換）で行う。
"""
import json
import os
import re
import secrets
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from flask import Flask, abort, jsonify, request, send_file

BASE = Path(__file__).resolve().parent
STATIC_INDEX = BASE / "static" / "index.html"

STOCK_DIR = Path(os.environ.get("KAUMONO_DATA") or (Path.home() / "kaumono-data"))
STOCK_FILE = STOCK_DIR / "stock.json"

JST = timezone(timedelta(hours=9))

# 在庫の状態は2値（数が増えるほど判断が重くなるため）。数量は管理しない。
# 旧3値（ok/low/out）のデータは load_state で want へ寄せる
STATUSES = ("ok", "want")
LEGACY_STATUS = {"low": "want", "out": "want"}

# 買い方。複数持てる（醤油はスーパーでもネットでも買える）
CHANNELS = ("super", "conbini", "drug", "net", "other")

# 履歴は品目ごとに直近この件数だけ持つ（無限に伸ばすと JSON が膨らむだけで読まない）
HISTORY_LIMIT = 20

_lock = threading.Lock()

app = Flask(__name__)


def now_iso() -> str:
    return datetime.now(JST).isoformat(timespec="seconds")


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(4)}"


def slugify(name: str) -> str:
    """ジャンルIDの種。日本語は残らないので、空になったら乱数に落とす。"""
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return s or secrets.token_hex(3)


def empty_state() -> dict:
    return {"areas": [], "genres": [], "items": []}


def load_state() -> dict:
    if not STOCK_FILE.exists():
        return empty_state()
    try:
        state = json.loads(STOCK_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return empty_state()
    state.setdefault("areas", [])
    state.setdefault("genres", [])
    state.setdefault("items", [])
    for item in state["items"]:
        item["status"] = LEGACY_STATUS.get(item.get("status"), item.get("status", "ok"))
        # 購入日は list（bought、新しい順）が正。単発の last_bought しか無い旧データは寄せる
        if "bought" not in item:
            item["bought"] = [item["last_bought"]] if item.get("last_bought") else []
    return state


def save_state(state: dict) -> None:
    STOCK_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STOCK_FILE.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    os.replace(tmp, STOCK_FILE)


def find_item(state: dict, item_id: str):
    return next((i for i in state["items"] if i["id"] == item_id), None)


def find_by_tag(state: dict, tag: str):
    return next((i for i in state["items"] if i.get("tag") == tag), None)


# 買い物カゴに入れる前に貼るURLは、短縮リンクか、追跡パラメータで膨れた長いURLになる。
# 保存する時点で削れるものは削る（何の商品か一目で分かる形にしておくため）。
# ここは文字列だけで完結する処理に限る——短縮リンクの解決はネットワークが要るので
# normalize_urls.py（定期実行）が受け持つ。

AMAZON_ASIN_RE = re.compile(r"/(?:dp|gp/product|gp/aws/d)/([A-Z0-9]{10})")
TRACKING_PARAMS = {
    "ref", "ref_", "tag", "linkcode", "linkid", "creative", "creativeasin",
    "ascsubtag", "psc", "th", "smid", "qid", "sr", "keywords", "crid", "sprefix",
    "gad_source", "gad_campaignid", "gclid", "gbraid", "wbraid", "fbclid", "mcid",
    "hvadid", "hvpos", "hvnetw", "hvrand", "hvpone", "hvptwo", "hvqmt", "hvdev",
    "hvdvcmdl", "hvlocint", "hvlocphy", "hvtargid", "hvocijid", "hvexpln",
    "s-id", "social_share", "_encoding", "content-id", "pd_rd_w", "pd_rd_r",
    "pd_rd_wg", "pf_rd_p", "pf_rd_r", "dib", "dib_tag",
} | {f"utm_{k}" for k in ("source", "medium", "campaign", "term", "content", "id")}


def normalize_url(url: str) -> str:
    """商品URLを短く・安定した形に直す。分からない形はそのまま返す（壊さないことを優先）。"""
    url = (url or "").strip()
    if not url.startswith(("http://", "https://")):
        return url
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    host = parts.netloc.lower()

    # Amazon は ASIN が分かれば /dp/<ASIN> だけで足りる（スラッグも追跡パラメータも捨てる）
    if host.endswith("amazon.co.jp") or host.endswith("amazon.com"):
        m = AMAZON_ASIN_RE.search(parts.path)
        if m:
            site = "amazon.co.jp" if host.endswith("amazon.co.jp") else "amazon.com"
            return f"https://www.{site}/dp/{m.group(1)}"

    # それ以外は追跡パラメータだけ落とす
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if k.lower() not in TRACKING_PARAMS]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def normalize_channels(value) -> list:
    """買い方は list で持つ。文字列1つで来ても受ける（UI/seed の揺れを吸収）。"""
    if value is None:
        return []
    if isinstance(value, str):
        value = [value]
    return [c for c in value if c in CHANNELS]


def set_status(item: dict, status: str, source: str) -> dict:
    """状態を変えて履歴を1件積む。同じ状態への再設定は履歴を積まない
    （NFCの二度読み・ブラウザのリロードで履歴が汚れるのを防ぐ）。

    購入の記録は set_status ではなく api_item_bought が行う（状態を戻すのと
    「買った」は別の出来事——間違って「ある」に戻したときに購入日が増えては困る）。
    """
    changed = item.get("status") != status
    item["status"] = status
    item["updated_at"] = now_iso()
    if changed:
        history = item.setdefault("history", [])
        history.insert(0, {"at": item["updated_at"], "status": status, "src": source})
        del history[HISTORY_LIMIT:]
    return {"changed": changed}


# ---------------------------------------------------------------- API: 参照

@app.get("/api/state")
def api_state():
    state = load_state()
    return jsonify({**state, "statuses": list(STATUSES), "channels": list(CHANNELS)})


# ------------------------------------------------------------ API: 並べ替え

# 並び順は配列の順そのものが正（order フィールドは持たない——2つあると必ずズレる）。
# ids は画面に出ている部分集合でよい: その要素が今占めている位置だけを詰め替えるので、
# 表示していない要素（別ジャンルの品目など）の位置は動かない。

REORDERABLE = ("areas", "genres", "items")


@app.post("/api/reorder")
def api_reorder():
    body = request.get_json(silent=True) or {}
    kind = body.get("kind")
    ids = body.get("ids") or []
    if kind not in REORDERABLE:
        return jsonify({"error": "unknown kind"}), 400
    with _lock:
        state = load_state()
        coll = state[kind]
        wanted_ids = set(ids)
        slots = [k for k, x in enumerate(coll) if x["id"] in wanted_ids]
        by_id = {x["id"]: x for x in coll}
        try:
            ordered = [by_id[i] for i in ids]
        except KeyError:
            return jsonify({"error": "unknown id"}), 400
        if len(slots) != len(ordered):
            return jsonify({"error": "id list mismatch"}), 400
        for slot, element in zip(slots, ordered):
            coll[slot] = element
        save_state(state)
    return jsonify({"ok": True, "count": len(ordered)})


# ------------------------------------------------------------ API: エリア（場所）

# エリアは「家のどこに置いてあるか」。ジャンル（何か）とは直交させてある——
# 同じ「掃除」でもドメストはトイレ、スクラビングバブルは風呂に置いてあり、
# ジャンルの上位階層にすると破綻するため。
# タグを貼る動線・棚卸しの動線はこのエリア単位になる。

@app.post("/api/areas")
def api_area_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with _lock:
        state = load_state()
        if any(a["name"] == name for a in state["areas"]):
            return jsonify({"error": "already exists"}), 409
        aid = slugify(name)
        while any(a["id"] == aid for a in state["areas"]):
            aid = f"{aid}-{secrets.token_hex(2)}"
        area = {"id": aid, "name": name}
        state["areas"].append(area)
        save_state(state)
    return jsonify(area)


@app.post("/api/areas/<aid>")
def api_area_patch(aid):
    body = request.get_json(silent=True) or {}
    with _lock:
        state = load_state()
        area = next((a for a in state["areas"] if a["id"] == aid), None)
        if area is None:
            abort(404)
        if "name" in body:
            area["name"] = (body["name"] or "").strip() or area["name"]
        save_state(state)
    return jsonify(area)


@app.delete("/api/areas/<aid>")
def api_area_delete(aid):
    with _lock:
        state = load_state()
        if any(i.get("area") == aid for i in state["items"]):
            return jsonify({"error": "area not empty"}), 409
        before = len(state["areas"])
        state["areas"] = [a for a in state["areas"] if a["id"] != aid]
        if len(state["areas"]) == before:
            abort(404)
        save_state(state)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API: ジャンル

@app.post("/api/genres")
def api_genre_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    if not name:
        return jsonify({"error": "name required"}), 400
    with _lock:
        state = load_state()
        if any(g["name"] == name for g in state["genres"]):
            return jsonify({"error": "already exists"}), 409
        gid = slugify(name)
        while any(g["id"] == gid for g in state["genres"]):
            gid = f"{gid}-{secrets.token_hex(2)}"
        genre = {"id": gid, "name": name}
        state["genres"].append(genre)
        save_state(state)
    return jsonify(genre)


@app.post("/api/genres/<gid>")
def api_genre_patch(gid):
    body = request.get_json(silent=True) or {}
    with _lock:
        state = load_state()
        genre = next((g for g in state["genres"] if g["id"] == gid), None)
        if genre is None:
            abort(404)
        if "name" in body:
            genre["name"] = (body["name"] or "").strip() or genre["name"]
        save_state(state)
    return jsonify(genre)


@app.delete("/api/genres/<gid>")
def api_genre_delete(gid):
    """品目が残っているジャンルは消させない（品目が迷子になるため）。"""
    with _lock:
        state = load_state()
        if any(i["genre"] == gid for i in state["items"]):
            return jsonify({"error": "genre not empty"}), 409
        before = len(state["genres"])
        state["genres"] = [g for g in state["genres"] if g["id"] != gid]
        if len(state["genres"]) == before:
            abort(404)
        save_state(state)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API: 品目

def apply_item_fields(item: dict, body: dict) -> None:
    for key in ("name", "product", "url", "note", "genre", "area"):
        if key in body:
            item[key] = (body.get(key) or "").strip()
    if "url" in body:
        item["url"] = normalize_url(item["url"])
    if "channels" in body:
        item["channels"] = normalize_channels(body["channels"])
    if "tag" in body:
        tag = (body.get("tag") or "").strip()
        item["tag"] = tag or None


@app.post("/api/items")
def api_item_add():
    body = request.get_json(silent=True) or {}
    name = (body.get("name") or "").strip()
    genre = (body.get("genre") or "").strip()
    if not name or not genre:
        return jsonify({"error": "name and genre required"}), 400
    with _lock:
        state = load_state()
        if not any(g["id"] == genre for g in state["genres"]):
            return jsonify({"error": "unknown genre"}), 400
        tag = (body.get("tag") or "").strip() or None
        if tag and find_by_tag(state, tag):
            return jsonify({"error": "tag already bound"}), 409
        item = {
            "id": new_id("itm"),
            "genre": genre,
            "area": (body.get("area") or "").strip(),
            "name": name,
            "product": (body.get("product") or "").strip(),
            "url": normalize_url(body.get("url") or ""),
            "channels": normalize_channels(body.get("channels")),
            "status": body.get("status") if body.get("status") in STATUSES else "ok",
            "tag": tag,
            "note": (body.get("note") or "").strip(),
            "updated_at": now_iso(),
            "last_bought": None,
            "bought": [],
            "history": [],
        }
        state["items"].append(item)
        save_state(state)
    return jsonify(item)


@app.post("/api/items/<item_id>")
def api_item_patch(item_id):
    body = request.get_json(silent=True) or {}
    with _lock:
        state = load_state()
        item = find_item(state, item_id)
        if item is None:
            abort(404)
        tag = (body.get("tag") or "").strip() if "tag" in body else None
        if tag:
            other = find_by_tag(state, tag)
            if other is not None and other["id"] != item_id:
                return jsonify({"error": "tag already bound"}), 409
        apply_item_fields(item, body)
        if body.get("status") in STATUSES:
            set_status(item, body["status"], body.get("src") or "web")
        else:
            item["updated_at"] = now_iso()
        save_state(state)
    return jsonify(item)


@app.post("/api/bought")
def api_bought_bulk():
    """まとめて「買った」。通販サイト単位で片づけるための口。

    /api/items/<id>/bought と同じことを ids の数だけ行う。1件ずつ叩いても
    同じだが、途中で失敗して半端に終わるのを避けたいので1回の保存にまとめる。
    """
    body = request.get_json(silent=True) or {}
    ids = body.get("ids") or []
    with _lock:
        state = load_state()
        targets = [i for i in state["items"] if i["id"] in set(ids)]
        if len(targets) != len(set(ids)):
            return jsonify({"error": "unknown id"}), 400
        for item in targets:
            bought = item.setdefault("bought", [])
            bought.insert(0, now_iso())
            del bought[HISTORY_LIMIT:]
            set_status(item, "ok", "bought")
            item["last_bought"] = bought[0]
        save_state(state)
    return jsonify({"ok": True, "count": len(targets)})


@app.post("/api/items/<item_id>/bought")
def api_item_bought(item_id):
    """買った。購入日を1件積んで「ある」に戻す。

    履歴が溜まれば消費周期（前回から何日か）が出る——タグを触り忘れても
    「そろそろのはず」を言えるようにするための材料。
    取り消せるように undo=true で直近の1件を消す口も同じ所に置く。
    """
    body = request.get_json(silent=True) or {}
    with _lock:
        state = load_state()
        item = find_item(state, item_id)
        if item is None:
            abort(404)
        bought = item.setdefault("bought", [])
        if body.get("undo"):
            if bought:
                bought.pop(0)
        else:
            bought.insert(0, now_iso())
            del bought[HISTORY_LIMIT:]
            set_status(item, "ok", "bought")
        item["last_bought"] = bought[0] if bought else None
        save_state(state)
    return jsonify(item)


@app.delete("/api/items/<item_id>")
def api_item_delete(item_id):
    with _lock:
        state = load_state()
        before = len(state["items"])
        state["items"] = [i for i in state["items"] if i["id"] != item_id]
        if len(state["items"]) == before:
            abort(404)
        save_state(state)
    return jsonify({"ok": True})


# ---------------------------------------------------------------- API: タグ

@app.get("/api/tags/<tag>")
def api_tag_get(tag):
    """タグの引き当てだけ（副作用なし）。紐付け画面が使う。"""
    state = load_state()
    item = find_by_tag(state, tag)
    return jsonify({"tag": tag, "item": item})


@app.post("/api/tags/<tag>/bind")
def api_tag_bind(tag):
    body = request.get_json(silent=True) or {}
    item_id = (body.get("item_id") or "").strip()
    with _lock:
        state = load_state()
        if find_by_tag(state, tag):
            return jsonify({"error": "tag already bound"}), 409
        item = find_item(state, item_id)
        if item is None:
            abort(404)
        item["tag"] = tag
        item["updated_at"] = now_iso()
        save_state(state)
    return jsonify(item)


@app.post("/api/tags/<tag>/tap")
def api_tag_tap(tag):
    """タップ本体。紐付け済みなら out にする。

    GET /t/<tag> は画面を返すだけで副作用を持たせず、実際の記録は画面から
    この POST を打つ。ブラウザの先読み・履歴からの再訪で「切れた」が
    勝手に立つのを避けるため。
    """
    with _lock:
        state = load_state()
        item = find_by_tag(state, tag)
        if item is None:
            return jsonify({"tag": tag, "item": None, "bound": False}), 404
        result = set_status(item, "want", "nfc")
        save_state(state)
    return jsonify({"tag": tag, "item": item, "bound": True, **result})


# ---------------------------------------------------------------- 画面

def index(**_path_params):
    """全ページで同じ HTML を返し、振り分けはブラウザ側の route() が行う。
    パス変数（gid / tag）はサーバでは使わないので捨てる。"""
    return send_file(STATIC_INDEX)


app.add_url_rule("/", "index", index)
app.add_url_rule("/a/<path:aid>", "index_area", index)
app.add_url_rule("/g/<path:gid>", "index_genre", index)
app.add_url_rule("/t/<path:tag>", "index_tag", index)
app.add_url_rule("/tags", "index_tags", index)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8689)
