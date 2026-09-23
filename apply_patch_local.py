#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把设备侧已生效的补丁 `patch_<seq>.db` **应用到 PC 本地基线库**（行级 UPSERT）。

为什么需要（spec Task 5）
------------------------
补丁在设备侧生效后，PC 本地基线仍停在旧值；不更新的话，下一轮 txt 直出差分会把
**已同步的差异再算一遍**（白白多出几十 MB 与十几秒）。本脚本把同一补丁按行 UPSERT
进基线库，使本地基线与设备主库内容一致。该步骤**不计入端到端 60s 预算**（可另跑）。

与只读差分脚本（`diff_live_patch.py`）的关键区别
----------------------------------------------
本脚本是**写**操作，故安全约束相反：
  · **默认 --dry-run**：只报告「将写入多少行」，不落盘；要真写必须显式加 `--yes`。
  · 真写前先打印基线库路径 / size / mtime 与「本次将写入的行数」供确认。
  · **不做备份**（基线库 1.4GB，备份太占空间）；改为记录写入前的 `(size, mtime)` 并打印，
    写入失败则 `ROLLBACK` 并明确报错（库保持写入前状态）。
  · 写入时**不能**用只读 URI（`mode=ro`），必须可写打开；dry-run 才用 `mode=ro`。

覆盖补丁携带的**五张周期表**（`daily/weekly/monthly/quarterly/yearly`），与设备侧
`KlineHTTPServer.swift` 的 `applyPatch` / 会话 `apply` 用**同一份**集合式 SQL 口径
（映射键 = `file`，一条 `INSERT OR REPLACE ... SELECT ... JOIN meta`，**不在 Python 里逐行循环**；
主库缺某张表则该周期**优雅跳过**）。

用法
----
    python TrollRestore/apply_patch_local.py --patch <patch_<seq>.db>            # dry-run（默认）
    python TrollRestore/apply_patch_local.py --patch <patch_<seq>.db> --yes      # 真写
    python TrollRestore/apply_patch_local.py --patch <p.db> --base-db <基线库>    # 指定基线
"""
import argparse
import datetime
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KLINE_REPO = r"c:\Users\sunck\home\projects\ios\Kline"
DEFAULT_BASE_DB = r"C:\Users\sunck\home\tdx_baseline.db"
PERIODS = ("daily", "weekly", "monthly", "quarterly", "yearly")

# 复用生成端唯一一份实现（只读 URI；dry-run 与写前统计用）
sys.path.insert(0, os.path.join(KLINE_REPO, "src"))
sys.path.insert(0, HERE)

import live_db_builder as L        # noqa: E402

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

# 与设备侧 KlineHTTPServer.applyPatchLocked 逐字一致的集合式 SQL（main = 基线库，bkt = 补丁）
INSERT_SQL = ("INSERT OR REPLACE INTO %s(meta_id,date,open,high,low,close,vol,amo) "
              "SELECT m.id,b.date,b.open,b.high,b.low,b.close,b.vol,b.amo "
              "FROM bkt.bkt_%s b JOIN meta m ON m.file=b.file")
# last_date 只由日线决定；MAX(...) 兜底：补丁只含旧日期时**不让 last_date 回退**
UPDATE_META_SQL = ("UPDATE meta SET last_date = MAX(COALESCE(last_date,0), "
                   "COALESCE((SELECT MAX(b.date) FROM bkt.bkt_daily b WHERE b.file=meta.file),0)) "
                   "WHERE file IN (SELECT DISTINCT file FROM bkt.bkt_daily)")


def db_state(path):
    st = os.stat(path)
    return st.st_size, st.st_mtime


def fmt_state(path):
    size, mtime = db_state(path)
    return "%d B (%.1f MB)  mtime=%s" % (
        size, size / 1048576.0,
        datetime.datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"))


def table_exists(conn, schema, name):
    return conn.execute("SELECT 1 FROM %s.sqlite_master WHERE type='table' AND name=?"
                        % schema, (name,)).fetchone() is not None


def survey(conn, periods):
    """只读统计（conn 上已 `ATTACH <patch> AS bkt`，main = 基线库）。

    返回：各周期「将写入行数」（= 补丁行与基线 meta 按 file JOIN 的命中数）、补丁覆盖/跳过 file 数、
    基线 meta 行数与本次会更新 last_date 的 meta 行数、补丁 latestDate、基线各周期总行数与 meta 最大 last_date。
    """
    rows = {}
    base_counts = {}
    for p in periods:
        if not table_exists(conn, "main", p):
            rows[p] = None
            base_counts[p] = None
            continue
        base_counts[p] = conn.execute("SELECT COUNT(*) FROM main.%s" % p).fetchone()[0]
        if not table_exists(conn, "bkt", "bkt_" + p):
            rows[p] = None            # 补丁缺该周期 → 跳过
            continue
        rows[p] = conn.execute(
            "SELECT COUNT(*) FROM bkt.bkt_%s b JOIN main.meta m ON m.file=b.file" % p).fetchone()[0]
    patch_files = conn.execute("SELECT COUNT(DISTINCT file) FROM bkt.bkt_daily").fetchone()[0]
    covered = conn.execute(
        "SELECT COUNT(DISTINCT b.file) FROM bkt.bkt_daily b JOIN main.meta m ON m.file=b.file"
    ).fetchone()[0]
    meta_total = conn.execute("SELECT COUNT(*) FROM main.meta").fetchone()[0]
    meta_touched = conn.execute(
        "SELECT COUNT(*) FROM main.meta WHERE file IN (SELECT DISTINCT file FROM bkt.bkt_daily)"
    ).fetchone()[0]
    latest = conn.execute("SELECT MAX(date) FROM bkt.bkt_daily").fetchone()[0]
    max_last = conn.execute("SELECT MAX(last_date) FROM main.meta").fetchone()[0]
    return {"rows": rows, "base_counts": base_counts, "patch_files": patch_files,
            "covered": covered, "meta_total": meta_total, "meta_touched": meta_touched,
            "latest": latest, "max_last": max_last}


def print_survey(sv):
    print("补丁将写入的行数（按 file JOIN 基线 meta 命中数）:")
    for p in PERIODS:
        n = sv["rows"][p]
        print("  %-8s %s" % (p, "（任一侧缺表，跳过）" if n is None else "%d 行" % n))
    print("补丁覆盖 file: %d（其中基线 meta 命中 %d、跳过 %d）"
          % (sv["patch_files"], sv["covered"], sv["patch_files"] - sv["covered"]))
    print("基线 meta: 共 %d 行；本次将更新 last_date 的 %d 行" % (sv["meta_total"], sv["meta_touched"]))
    print("补丁 latestDate: %s   基线 meta 当前 MAX(last_date): %s" % (sv["latest"], sv["max_last"]))
    print("基线各周期总行数: %s"
          % "  ".join("%s=%s" % (p, sv["base_counts"][p]) for p in PERIODS))


def run(base_db, patch, periods, do_write):
    # ---- dry-run（默认）：只读打开基线 + 只读 ATTACH 补丁，纯统计、不落盘 ----
    if not do_write:
        conn = sqlite3.connect(L.ro_uri(base_db), uri=True)
        try:
            conn.execute("ATTACH DATABASE '%s' AS bkt" % L.ro_uri(patch))
            conn.execute("PRAGMA query_only=1")
            sv = survey(conn, periods)
        finally:
            conn.close()
        print_survey(sv)
        print("-" * 72)
        print("--dry-run（默认）：未写入任何数据。确认无误后加 --yes 真写。")
        return 0

    # ---- 真写 ----
    before = db_state(base_db)
    print("写入前基线库: %s" % fmt_state(base_db))
    t0 = time.time()
    conn = sqlite3.connect(base_db)             # 可写（绝不用 ro_uri）
    try:
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("ATTACH DATABASE '%s' AS bkt" % patch.replace("'", "''"))
        sv = survey(conn, periods)
        print_survey(sv)
        total = sum(n for n in sv["rows"].values() if n)
        print("→ 即将写入合计 %d 行" % total)
        conn.execute("BEGIN")
        try:
            for p in periods:
                if sv["rows"][p] is None:
                    continue
                conn.execute(INSERT_SQL % (p, p))
                print("  写入 %-8s 完成" % p)
            conn.execute(UPDATE_META_SQL)
            print("  更新 meta.last_date 完成")
            conn.execute("COMMIT")
        except Exception as exc:
            conn.execute("ROLLBACK")
            print("❌ 写入失败（已 ROLLBACK，基线库保持写入前状态）：%s" % exc)
            print("   写入前: size=%d B  mtime=%s" % before)
            return 1
    finally:
        try:
            conn.execute("DETACH DATABASE bkt")
        except Exception:
            pass
        conn.close()

    after = db_state(base_db)
    print("-" * 72)
    print("✅ 已应用补丁 → 基线库  %.1fs" % (time.time() - t0))
    print("写入前: size=%d B  mtime=%s" % before)
    print("写入后: size=%d B  mtime=%s" % after)

    # ---- 写后回读核对（只读）----
    conn = sqlite3.connect(L.ro_uri(base_db), uri=True)
    try:
        conn.execute("ATTACH DATABASE '%s' AS bkt" % L.ro_uri(patch))
        conn.execute("PRAGMA query_only=1")
        sv2 = survey(conn, periods)
    finally:
        conn.close()
    print("回读核对:")
    print("  基线各周期总行数: %s"
          % "  ".join("%s=%s" % (p, sv2["base_counts"][p]) for p in PERIODS))
    print("  基线 meta MAX(last_date): %s（补丁 latestDate=%s）" % (sv2["max_last"], sv2["latest"]))
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="把 patch_<seq>.db 按行 UPSERT 进 PC 本地基线库（默认 dry-run，--yes 才真写）")
    ap.add_argument("--patch", required=True,
                    help="补丁包路径（bkt_meta/bkt_daily/bkt_weekly/bkt_monthly/bkt_quarterly/bkt_yearly）")
    ap.add_argument("--base-db", dest="base_db", default=DEFAULT_BASE_DB,
                    help="本地基线库路径（默认 %s，--yes 时会被写入）" % DEFAULT_BASE_DB)
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只统计不落盘（**默认行为**，显式给出以图清晰）")
    ap.add_argument("--yes", action="store_true", help="确认真写（不加则等同 --dry-run）")
    args = ap.parse_args(argv)

    if not os.path.exists(args.patch):
        raise SystemExit("补丁不存在: %s" % args.patch)
    if not os.path.exists(args.base_db):
        raise SystemExit("基线库不存在: %s" % args.base_db)

    do_write = args.yes and not args.dry_run
    print("补丁  : %s  (%s)" % (args.patch, fmt_state(args.patch)))
    print("基线库: %s  (%s)" % (args.base_db, fmt_state(args.base_db)))
    print("模式  : %s" % ("真写（--yes）" if do_write else "dry-run（默认，不落盘）"))
    print("-" * 72)
    return run(args.base_db, args.patch, PERIODS, do_write)


if __name__ == "__main__":
    sys.exit(main())
