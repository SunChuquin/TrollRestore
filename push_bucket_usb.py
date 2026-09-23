#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""电脑侧直推"当天分片/补丁"到设备：PUT 到沙盒 live/ → 触发 App 合并。

两种模式：
  · 默认（增量库路径）：PUT live/<name> → POST /sync/merge-bucket（合并进**增量库** tdx_live.db）
  · --apply-main（主库路径）：PUT live/<name>
        → POST /sync/apply-patch?name=&sha256=（按行 UPSERT 进**主库** tdx.db）
        → DELETE /sandbox/tdx_live.db（清增量库）→ POST /sync/reload

用法：
    python TrollRestore/push_bucket_usb.py <bucket.db> [bucket2.db ...]
    python TrollRestore/push_bucket_usb.py --apply-main <patch_<seq>.db>
示例：
    python TrollRestore/push_bucket_usb.py Kline/build_logs/live_eastmoney/bucket_20718.db
    python TrollRestore/push_bucket_usb.py --apply-main Kline/build_logs/patches/patch_4.db
"""
import argparse
import hashlib
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import push_live_usb as P      # noqa: E402
import sandbox_cli             # noqa: E402


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def http_request(method, path, timeout=120):
    """对设备 5051 发一个请求，返回 (status, body)。

    非 2xx **不抛异常**：HTTPError 也读出 body 一并返回，便于打印原因。
    """
    req = urllib.request.Request(P.BASE + path,
                                 data=(b"" if method in ("POST", "PUT") else None),
                                 method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace")


def merge_bucket(name, sha):
    """增量库路径（默认）：POST /sync/merge-bucket，合并进 tdx_live.db。"""
    t0 = time.time()
    status, body = http_request("POST", "/sync/merge-bucket?name=%s&sha256=%s" % (name, sha), timeout=60)
    print("MERGE %-21s HTTP %s  %.1fs  %s" % (name, status, time.time() - t0, body[:160]))


def apply_main(name, sha):
    """主库路径（--apply-main）：apply-patch → 清增量库 → reload。返回 True=已写进主库。

    任一步失败都给出「下一步」提示，不静默继续。
    """
    # 1) 按行 UPSERT 进主库 tdx.db（不经过增量库）
    t0 = time.time()
    status, body = http_request("POST", "/sync/apply-patch?name=%s&sha256=%s" % (name, sha), timeout=300)
    print("APPLY %-21s HTTP %s  %.1fs  %s" % (name, status, time.time() - t0, body[:200]))
    if not (200 <= status < 300):
        print("  ❌ apply-patch 失败 → 主库未变（未写入任何行），可修正后重试；")
        print("     本轮**不清理增量库**（主库尚无新值，清掉会丢历史数据）。")
        return False

    # 2) 清增量库 Documents/tdx_live.db —— **正确性必需**（不是可选）：
    #    App 查询规则「同一 date 以增量库为准」，而增量库里有历史遗留的旧值
    #    （旧 patch 推入的旧历史行 / 东财快照的原始价），会**遮蔽**刚写进主库的新值 → 界面显示错值。
    #    主库更新后已含全部数据，故清掉是安全且必要的（增量库缺失时 App 正常、全部查询走主库、不报错）。
    t0 = time.time()
    status, body = http_request("DELETE", "/sandbox/tdx_live.db", timeout=30)
    print("DEL   %-21s HTTP %s  %.1fs  %s" % ("tdx_live.db", status, time.time() - t0, body[:80]))
    if not (200 <= status < 300):
        print("  ⚠️ 删除增量库返回 HTTP %s（可能本就不存在）→ 继续；"
              "若它仍在且含旧值会遮蔽主库，请人工核对。" % status)

    # 3) 触发 App 重载（主库 last_date 已变 → 重读 metaList + 热刷新 dataVersion）
    t0 = time.time()
    status, body = http_request("POST", "/sync/reload", timeout=30)
    print("RELOAD %-20s HTTP %s  %.1fs  %s" % ("", status, time.time() - t0, body[:80]))
    if not (200 <= status < 300):
        print("  ⚠️ reload 失败 → 主库已更新但界面可能未刷新，可再手动触发 POST /sync/reload。")
    return True


def main():
    ap = argparse.ArgumentParser(description="电脑侧直推分片/补丁到设备（PUT live/ → 合并）")
    ap.add_argument("--apply-main", action="store_true",
                    help="走主库路径：apply-patch 按行 UPSERT 进 tdx.db 并清增量库"
                         "（默认走 merge-bucket 增量库）")
    ap.add_argument("files", nargs="*", help="要推送的 .db 分片/补丁文件")
    args = ap.parse_args()

    buckets = [p for p in args.files if os.path.isfile(p)]
    if not buckets:
        raise SystemExit("用法: python push_bucket_usb.py [--apply-main] <bucket.db> [...]")
    fwd = sandbox_cli.forward()
    try:
        if not sandbox_cli.check_online():
            raise SystemExit("Kline 不在前台（5051 无响应）。请打开 Kline 并解锁后重试。")
        print("模式  : %s" % ("主库(apply-patch)" if args.apply_main else "增量库(merge-bucket)"))
        print("before:", P.http_get("/sync/status"))
        for path in buckets:
            name = os.path.basename(path)
            sha = sha256_file(path)
            # 0) 分片落沙盒 Documents/live/（两种模式相同）
            t0 = time.time()
            status, body = P.http_put("live/" + name, Path(path))
            print("PUT  %-22s HTTP %s  %8d B  %.1fs  %s"
                  % (name, status, os.path.getsize(path), time.time() - t0, body[:40]))
            if status != 200:
                print("  ⚠️ PUT 失败（HTTP %s）→ 跳过该文件；请确认 Kline 前台、沙盒可写。" % status)
                continue
            if args.apply_main:
                apply_main(name, sha)
            else:
                merge_bucket(name, sha)
        time.sleep(2)
        print("after :", P.http_get("/sync/status"))
    finally:
        fwd.terminate()


if __name__ == "__main__":
    main()
