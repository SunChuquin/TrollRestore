#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kline 增量行情库 · **电脑侧生产者（首选）**：只读本机 tdx.db → 出日分片 → 推设备

为什么需要它（云端做不到的事）
------------------------------
云端（`Kline/src/live_db_builder.py`）只能覆盖 `universe.txt` 里能映射到公开行情接口的
3312 只；主库 meta 的 3611 只里有 **299 只是扩展行情指数**（`27#`/`62#`/`102#` 前缀，
恒生/AH/行业/主题指数），公开接口没有对应 secid，**只有本机能生产**。
本脚本读本机 `C:\\Users\\sunck\\home\\tdx.db` 的日线（**只读**），零网络、秒级，
覆盖率可达 3611/3611。

产出与云端**完全同构**：`bucket_<id>.db` + `manifest.json`（schema 3，`source: pc_tdx`）；
`bucket_id = (date - date(1970,1,1)).days`（UTC 日序），一天一片，保留最近 30 片。
表结构 / 日期口径 / 聚合实现与云端是同一份代码（本脚本直接 import 云端生成器的
`build_slots` / `emit_buckets` / `aggregate_full_periods` / `run_check`），不会两套漂移。

关键口径（与主库 tdx.db 对齐，改错会把合并回主库的数据写坏）
----------------------------------------------------------
· 键用 `file`（`SH#600000`），不用 `code`：主库 code 有 55 处重复（指数与股票同码）。
· 周/月线的 `date` = **该周期的第一个交易日**（实测 SH#600000 周线 20260824 覆盖 0824~0828）。
· 只发"整个周期都落在窗口内"的周/月线；主库的"进行中"周期线我们不发。
· 同一个 `date` 的所有 file（含 299 只扩展行情指数）都进该日分片。

安全（硬约束）
--------------
主库只用 `file:...?mode=ro` 打开，**绝不写入**；跑完自动核对主库 sha256 与 mtime 是否
变化，变了就大声报错（正常情况下永远不变）。注意 SQLite 读 WAL 库时可能在库旁生成
`tdx.db-shm` / `tdx.db-wal`，那是读侧副作用，主库文件本身不会被改写。

用法
----
    python TrollRestore/build_live_buckets_pc.py --days 30             # 出片到 --out
    python TrollRestore/build_live_buckets_pc.py --days 30 --check     # 只校验已有产物
    python TrollRestore/build_live_buckets_pc.py --days 30 --push-usb  # 出片后经数据线推设备
    python TrollRestore/build_live_buckets_pc.py --push-lan 192.168.1.23
"""

import argparse
import datetime
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KLINE_REPO = r"c:\Users\sunck\home\projects\ios\Kline"
DEFAULT_MASTER_DB = r"C:\Users\sunck\home\tdx.db"
DEFAULT_OUT = os.path.join(KLINE_REPO, "build_logs", "live")

# 复用云端生成器（唯一一份切分/聚合/校验实现）与 TrollRestore 里的推送逻辑
sys.path.insert(0, os.path.join(KLINE_REPO, "src"))
sys.path.insert(0, HERE)

import live_db_builder as L        # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

META_BATCH = 400        # 每次 IN 查询的 meta_id 个数（避开 SQLite 变量数上限）


# ---------------------------------------------------------------------------
# 读主库（只读）
# ---------------------------------------------------------------------------

def shield_sha256(path):
    """主库完整性核对用的 sha256（分块读，1.4GB 约几秒）。"""
    return L.sha256_file(path)


def load_window_daily(conn, entries, days):
    """取"最近 days 个交易日 × 全部 file"的日线。

    两步走（避免全表扫 1400 万行）：
      ① 用 `meta.last_date` 的最大值当锚点，往前放宽成日历跨度（交易日→日历日留足长假余量）；
      ② 按 `meta_id` 分批 IN 查 daily，走 `daily(meta_id,date)` 主键索引。
    说明：用 meta_id 过滤而不是 `m.file IN (...)`，因为 file↔meta_id 是 1:1（等价），
    而 3611 个占位符会撞 SQLite 的变量数上限；分批查询更快也更稳。

    返回 (bars_by_file, window_dates, files_not_in_db)。
    """
    anchor = conn.execute("SELECT MAX(last_date) FROM meta").fetchone()[0]
    if anchor is None:
        raise SystemExit("主库 meta 表为空（last_date 全空）")
    anchor = int(anchor)
    span = int(days * 1.7) + 10          # 30 个交易日 → 最多往回顾 61 个日历日，宽松覆盖长假
    guess = L.date_to_int(L.int_to_date(anchor) - datetime.timedelta(days=span))

    id_by_file, file_by_id = {}, {}
    for mid, file_str in conn.execute("SELECT id, file FROM meta"):
        id_by_file[file_str] = mid
        file_by_id[mid] = file_str

    wanted, not_in_db = [], []
    for e in entries:
        mid = id_by_file.get(e["file"])
        if mid is None:
            not_in_db.append(e["file"])
        else:
            wanted.append(mid)

    bars_by_file = {}
    for i in range(0, len(wanted), META_BATCH):
        chunk = wanted[i:i + META_BATCH]
        sql = ("SELECT meta_id,date,open,high,low,close,vol,amo FROM daily "
               "WHERE meta_id IN (%s) AND date>=?" % ",".join("?" * len(chunk)))
        for mid, date, o, h, l, c, v, a in conn.execute(sql, chunk + [guess]):
            bars_by_file.setdefault(file_by_id[mid], {})[int(date)] = {
                "date": int(date), "open": float(o), "high": float(h), "low": float(l),
                "close": float(c), "vol": float(v or 0), "amo": float(a or 0)}

    # 交易日历 = 所有 file 出现过的日期的并集；取最近 days 个作为发布窗口
    all_dates = sorted({d for bars in bars_by_file.values() for d in bars}, reverse=True)
    if not all_dates:
        raise SystemExit("主库 daily 里没有取到任何日线（date>=%s）" % guess)
    window_dates = sorted(all_dates[:days])
    start = window_dates[0]
    for f, bars in bars_by_file.items():                 # 裁到窗口内
        bars_by_file[f] = {d: b for d, b in bars.items() if d >= start}
    return bars_by_file, window_dates, not_in_db


# ---------------------------------------------------------------------------
# 推设备（复用 push_live_usb.py / push_live_lan.py）
# ---------------------------------------------------------------------------

def push_usb(items, no_reload):
    """通道 A：usbmux forward 5051 + PUT（需数据线、App 前台未锁屏）。"""
    import push_live_usb
    import sandbox_cli
    fwd = sandbox_cli.forward()
    try:
        if not sandbox_cli.check_online():
            raise SystemExit("❌ Kline 不在前台（本地 HTTP 服务器无响应）。"
                             "请在设备上打开 Kline、保持解锁后重试。")
        for name, path in items:
            t0 = time.time()
            status, body = push_live_usb.http_put(name, path)
            print("  %s PUT %-24s HTTP %s  %8d B  %.1fs  %s"
                  % ("✅" if status == 200 else "❌", name, status, os.path.getsize(path),
                     time.time() - t0, body[:60]))
        if not no_reload:
            print("  🔄 通知热刷新 → %s" % push_live_usb.http_post("/sync/reload"))
    finally:
        fwd.terminate()


def push_lan(host, items, no_reload):
    """通道 B：局域网 WiFi 直推（需同一 Wi-Fi、App 前台未锁屏）。"""
    import push_live_lan
    import urllib.error
    base = push_live_lan.base_url(host)
    try:
        push_live_lan.http_get(base, "/", timeout=6)      # 探活，给出可操作提示
    except urllib.error.URLError as exc:
        raise SystemExit("❌ 连不上 %s：%s\n   逐项确认：① IP 是否正确 ② 是否同一 Wi-Fi "
                         "③ Kline 是否前台未锁屏" % (base, exc))
    for name, path in items:
        t0 = time.time()
        status, body = push_live_lan.http_put(base, name, path)
        print("  %s PUT %-24s HTTP %s  %8d B  %.1fs  %s"
              % ("✅" if status == 200 else "❌", name, status, os.path.getsize(path),
                 time.time() - t0, body[:60]))
    if not no_reload:
        print("  🔄 通知热刷新 → %s" % push_live_lan.http_post(base, "/sync/reload"))


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Kline 增量行情库 · 电脑侧生产者（只读本机 tdx.db 出日分片）")
    ap.add_argument("--master-db", dest="master_db", default=DEFAULT_MASTER_DB,
                    help="主库路径（默认 %s，**只读打开，绝不写入**）" % DEFAULT_MASTER_DB)
    ap.add_argument("--universe", default=os.path.join(KLINE_REPO, "src", "data", "universe.txt"),
                    help="全市场清单（默认仓库内 src/data/universe.txt）")
    ap.add_argument("--days", type=int, default=30,
                    help="发布最近多少个交易日（默认 30；上限 = 保留片数 %d）" % L.KEEP_BUCKETS)
    ap.add_argument("--out", default=DEFAULT_OUT, help="输出目录（默认 %s）" % DEFAULT_OUT)
    ap.add_argument("--check", action="store_true", help="只校验已有产物，不重新生成")
    ap.add_argument("--force", action="store_true", help="即使判定'无变化'也强制重写 manifest")
    ap.add_argument("--push-usb", dest="push_usb", action="store_true",
                    help="出片后经数据线（usbmux forward 5051）逐片推设备 Documents/")
    ap.add_argument("--push-lan", dest="push_lan", default=None, metavar="HOST",
                    help="出片后经局域网推设备，如 --push-lan 192.168.1.23")
    ap.add_argument("--no-reload", dest="no_reload", action="store_true",
                    help="推送后不通知 App 热刷新")
    args = ap.parse_args(argv)

    out_dir = os.path.abspath(args.out)
    if args.check:
        return L.run_check(out_dir)

    t_all = time.time()
    os.makedirs(out_dir, exist_ok=True)
    for stale in [p for p in os.listdir(out_dir) if p.endswith(".db.tmp")]:
        os.remove(os.path.join(out_dir, stale))

    days = args.days
    if days > L.KEEP_BUCKETS:
        print("[warn] --days %d 超过保留片数 %d，已按 %d 处理" % (days, L.KEEP_BUCKETS, L.KEEP_BUCKETS))
        days = L.KEEP_BUCKETS
    if days < 1:
        raise SystemExit("--days 必须 >= 1")

    # ---- 主库完整性基线（跑完必须一字不差）----
    if not os.path.exists(args.master_db):
        raise SystemExit("主库不存在: %s" % args.master_db)
    before = (os.path.getmtime(args.master_db), os.path.getsize(args.master_db))
    t0 = time.time()
    sha_before = shield_sha256(args.master_db)
    print("主库: %s  %.1f MB  sha256=%s…  （基线校验耗时 %.1fs）"
          % (args.master_db, before[1] / 1048576.0, sha_before[:16], time.time() - t0))

    entries = L.parse_universe(args.universe)
    print("清单: %s -> %d 个 file" % (args.universe, len(entries)))
    meta_rows = [(e["file"], e["code"], e["name"], e["type"]) for e in entries]

    conn = sqlite3.connect(L.ro_uri(args.master_db), uri=True)      # 只读
    try:
        t0 = time.time()
        bars_by_file, window_dates, not_in_db = load_window_daily(conn, entries, days)
        print("读取主库日线: %d 个交易日（%s ~ %s）× %d 个 file，%d 行，耗时 %.1fs"
              % (len(window_dates), window_dates[0], window_dates[-1],
                 len(bars_by_file), sum(len(b) for b in bars_by_file.values()), time.time() - t0))
    finally:
        conn.close()

    if not_in_db:
        print("[warn] 清单里有 %d 个 file 不在主库 meta：%s"
              % (len(not_in_db), ", ".join(not_in_db[:10])))

    window_start, window_end = window_dates[0], window_dates[-1]
    kept = [L.bucket_id_for_date(d) for d in sorted(window_dates, reverse=True)]  # 新->旧
    slots = L.build_slots(bars_by_file, kept, meta_rows, window_start, window_end)
    kept = [bid for bid in kept if bid in slots]
    if not kept:
        raise SystemExit("没有任何非空分片，放弃生成")

    # ---- 只回写变化的分片（以**输出目录里已有的产物**当基线，天然幂等）----
    prev = L.read_prev_buckets(out_dir)
    generated_at = int(time.time())
    emitted = L.emit_buckets(out_dir, slots, kept, prev["files"], prev["shas"])

    # 覆盖口径与云端一致：分母 = universe 全部 file，covered = 最新那片里有日线的 file
    newest = L.date_for_bucket(kept[0])
    covered = {f for f, bars in bars_by_file.items() if newest in bars}
    uncovered = [e["file"] for e in entries if e["file"] not in covered]
    coverage = len(covered) / float(len(entries))

    manifest = {
        "schema": L.MANIFEST_SCHEMA,
        "generated_at": generated_at,
        "trade_date": newest,
        "source": "pc_tdx",
        "universe": len(entries),
        "covered": len(covered),
        "coverage": round(coverage, 6),
        "missing_sample": uncovered[:20],
        "latest_bucket": kept[0],
        "keep_buckets": L.KEEP_BUCKETS,
        "buckets": emitted["buckets"],            # 已按 id 从新到旧
    }
    mf_path = os.path.join(out_dir, L.MANIFEST_NAME)
    changed = emitted["changed"] or prev["manifest"] is None
    if changed or args.force:
        L.write_manifest_atomic(mf_path, manifest)
    elif not os.path.exists(mf_path):
        L.write_manifest_atomic(mf_path, manifest)

    total = sum(b["bytes"] for b in emitted["buckets"])
    print("-" * 78)
    print("产出交易日(%d 片): %s" % (len(emitted["buckets"]),
                                   ", ".join(str(L.date_for_bucket(b["id"])) for b in emitted["buckets"])))
    print("分片数=%d  总字节=%d (%.3f MB)  平均每片 %.1f KB  本次重写 %d 片"
          % (len(emitted["buckets"]), total, total / 1048576.0,
             total / max(1, len(emitted["buckets"])) / 1024.0, len(emitted["rewritten"])))
    print("覆盖率=%d/%d=%.4f  最新交易日=%s%s"
          % (len(covered), len(entries), coverage, newest,
             "  （无变化，未重写任何分片）" if not emitted["rewritten"] else ""))
    if uncovered:
        print("未覆盖样例: %s" % ", ".join(uncovered[:10]))
    print("manifest: %s  ✓ 已生成: %s / bucket_%d.db"
          % (mf_path, os.path.join(out_dir, L.MANIFEST_NAME), kept[0]))

    # ---- 推送（分片在前、manifest 在后：App 看到 manifest 时片一定已就位）----
    if args.push_usb or args.push_lan:
        items = [(b["file"], os.path.join(out_dir, b["file"])) for b in emitted["buckets"]]
        items.append((L.MANIFEST_NAME, mf_path))
        print("-" * 78)
        if args.push_usb:
            print("推送通道: USB（数据线）")
            push_usb(items, args.no_reload)
        else:
            print("推送通道: 局域网 %s" % args.push_lan)
            push_lan(args.push_lan, items, args.no_reload)

    # ---- 主库完整性复核（硬约束）----
    t0 = time.time()
    sha_after = shield_sha256(args.master_db)
    after = (os.path.getmtime(args.master_db), os.path.getsize(args.master_db))
    same = (sha_after == sha_before and after == before)
    print("-" * 78)
    print("主库完整性: sha256 %s  mtime/size %s  %s（复核耗时 %.1fs）"
          % ("一致" if sha_after == sha_before else "不一致!", "一致" if after == before else "不一致!",
             "✅ 主库未被修改" if same else "❌ 主库被修改了，请立即排查！", time.time() - t0))
    print("总耗时 %.1fs" % (time.time() - t_all))
    return 0 if same or args.check else 1


if __name__ == "__main__":
    sys.exit(main())