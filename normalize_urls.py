#!/usr/bin/env python3
"""購入URLを正規の形へ均す（cron から定期実行）。

貼られるURLは、短縮リンク（amzn.asia/d/xxxx）か、追跡パラメータで膨れた
長いURLになる。何の商品かURLから分からないし、短縮リンクはいつ切れるか
分からないので、定期的に正規の形へ直しておく。

  文字列だけで直せるもの（追跡パラメータ落とし・/dp/<ASIN> への短縮）は
  server.normalize_url が保存時にその場で行う。ここが受け持つのは
  **ネットワークが要る短縮リンクの解決**——リダイレクト先の Location だけを
  読んで /dp/<ASIN> を取り出す。本文は取りに行かない（VPS の IP からだと
  Amazon の bot 検知に 503 で弾かれるが、リダイレクトは素通しで返ってくる）。

書き込みは必ず HTTP API 経由で行う（データファイルを直接書くと、動いている
サーバが持っている状態と食い違う）。サーバが止まっていれば何もせず終わる。

    python3 normalize_urls.py [--dry-run] [--base http://127.0.0.1:8689]
"""
import argparse
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import server  # noqa: E402  (normalize_url を共有する)

DEFAULT_BASE = "http://127.0.0.1:8689"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TIMEOUT = 20

# 解決しに行く短縮リンクのホスト。ここに無いホストへは触りに行かない
SHORTENERS = ("amzn.asia", "amzn.to", "a.co", "amzn.eu", "r10.to")

LOCATION_RE = re.compile(r"^Location:\s*(\S+)", re.I | re.M)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """リダイレクトを追わない。Location を読めればそれで用は足りる。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def resolve(url: str) -> str:
    """短縮リンクの行き先を1段だけ読む。分からなければ元のURLを返す。"""
    opener = urllib.request.build_opener(NoRedirect)
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with opener.open(req, timeout=TIMEOUT) as res:
            location = res.headers.get("Location")
    except urllib.error.HTTPError as e:
        location = e.headers.get("Location")
    except (urllib.error.URLError, OSError):
        return url
    return location or url


def fetch_state(base: str) -> dict:
    with urllib.request.urlopen(base + "/api/state", timeout=TIMEOUT) as res:
        return json.load(res)


def patch_url(base: str, item_id: str, url: str) -> None:
    req = urllib.request.Request(
        f"{base}/api/items/{item_id}",
        data=json.dumps({"url": url}).encode(),
        headers={"content-type": "application/json"},
        method="POST",
    )
    urllib.request.urlopen(req, timeout=TIMEOUT).read()


def plan(items: list) -> list:
    """(item, 新しいURL) の一覧。変わらないものは含めない。"""
    todo = []
    for item in items:
        url = (item.get("url") or "").strip()
        if not url:
            continue
        new = url
        if any(h in url for h in SHORTENERS):
            new = resolve(url)
        new = server.normalize_url(new)
        if new and new != url:
            todo.append((item, new))
    return todo


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=DEFAULT_BASE)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        state = fetch_state(args.base)
    except (urllib.error.URLError, OSError) as e:
        print(f"サーバに繋がらないので何もしない: {e}")
        return 0

    todo = plan(state["items"])
    if not todo:
        print("URLは全て正規の形。変更なし")
        return 0

    for item, new in todo:
        print(f"{item['name']}: {item['url']} -> {new}")
        if not args.dry_run:
            patch_url(args.base, item["id"], new)
    print(f"{'（--dry-run のため書いていない）' if args.dry_run else '書き換えた'}: {len(todo)}件")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
