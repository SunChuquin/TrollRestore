#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""设备主库全量核对（**手动工具**：流水线 `sync_pipeline.py` **不会**调用它）。

用途（spec `.trae/specs/pipeline-shard-patch-sync/` Task 7.2 / Task 9.3）
--------------------------------------------------------------------
把设备上的主库 `Documents/tdx.db`（~1.4GB）经 usbmux forward **流式**拉到本地，
然后核对「补丁是否真的把设备主库改对了」：
  1. 五张周期表 `daily/weekly/monthly/quarterly/yearly` 的 `MAX(date)` 与行数；
  2. 全部标的的日线末日是否都 == `--expect-date`（默认 20260922）；
  3. 缺口检查：抽样若干标的（含 `SH#999999` / `SZ#399001` / `27#HSI` 等指数），
     把库内该标的的日期集合与 `--new-txt-dir` 里对应 txt 的日期集合逐一比对；
  4. 取值抽查：对 2~3 只标的（含 1 只 rewrite 标的，如 `SH#600519`）逐行比对库内值与 txt。

⚠ 关键：本机 `HTTP_PROXY=127.0.0.1:10808` 且 `NO_PROXY` 为空 —— 直接 `urllib.urlopen`
会被系统代理拦截、对 127.0.0.1:5051 的请求返回 **503 假象**（`build_and_deploy.py:107` 已注明）。
故本脚本**一律用 no-proxy opener**（`ProxyHandler({})`），且大文件**分块流式写盘**，
绝不整份读进内存。

本脚本**只读设备**（只发 GET），只在本地写一份临时库文件：默认放 `Kline/build_logs/`，
加 `--keep` 保留、否则核对完自动删除。

用法
----
    python TrollRestore/verify_main_db.py                       # 默认期望 20260922
    python TrollRestore/verify_main_db.py --expect-date 20260922 --keep
    python TrollRestore/verify_main_db.py --new-txt-dir C:\\Users\\sunck\\home\\tdx_data
"""
import argparse
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
KLINE_REPO = r"c:\Users\sunck\home\projects\ios\Kline"

DEFAULT_NEW_TXT = r"C:\Users\sunck\home\tdx_data"
DEFAULT_LOCAL = os.path.join(KLINE_REPO, "build_logs", "tdx_device.db")
REMOTE_DB = "tdx.db"                       # Documents/tdx.db
PERIODS = ("daily", "weekly", "monthly", "quarterly", "yearly")
FLOAT_TOL = 1e-6
CHUNK = 1 << 20                            # 1 MiB 分块写盘

# 本机回环调用必须绕过系统代理（理由见模块 docstring / build_and_deploy.py:107）。
# 实测：带代理时对 127.0.0.1:5051 的请求会拿到 **HTTP 503**；装 no-proxy opener 后才是真实连接。
# 这里**全局安装**，使本进程内 `sandbox_cli.check_online()` 等所有 urlopen 一并绕过代理。
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
urllib.request.install_opener(_OPENER)

# 抽样标的：缺口检查（含指数）
SAMPLE_GAP = ["SH#999999", "SZ#399001", "27#HSI", "SH#600519"]
# 取值抽查（含 1 只 rewrite 标的 SH#600519）
SAMPLE_VALUE = ["SH#600519", "SZ#399001", "27#HSI"]

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print(msg, flush=True)


# ---------------------------------------------------------------------------
# 下载（no-proxy + 流式）
# ---------------------------------------------------------------------------

def download_stream(url, dest, timeout=1800):
    """no-proxy GET，分块写盘。返回 (bytes, content_length, elapsed)。"""
    req = urllib.request.Request(url, method="GET")
    t0 = time.time()
    got = 0
    last_report = 0
    tmp = dest + ".tmp"
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            total = int(r.headers.get("Content-Length") or 0)
            with open(tmp, "wb") as fh:
                while True:
                    chunk = r.read(CHUNK)
                    if not chunk:
                        break
                    fh.write(chunk)
                    got += len(chunk)
                    mb = got // (50 << 20)
                    if mb != last_report:
                        last_report = mb
                        log("  ...已下载 %6.1f MB / %s  (%.1fs)"
                            % (got / 1048576.0,
                               ("%.1f MB" % (total / 1048576.0)) if total else "?",
                               time.time() - t0))
                fh.flush()
                os.fsync(fh.fileno())
        os.replace(tmp, dest)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)      # 半截文件绝不留下
            except OSError:
                pass
        raise
    return got, total, time.time() - t0


# ---------------------------------------------------------------------------
# txt 解析（口径与 txt_patch_builder.parse_daily 一致：GBK / `;` 分隔 / date int）
# ---------------------------------------------------------------------------

def read_txt_rows(path):
    """txt → {date_int: (open, high, low, close, vol, amo)}（表头/列名/页脚行自然跳过）。"""
    with open(path, "rb") as fh:
        text = fh.read().decode("gbk", "replace")
    rows = {}
    for line in text.splitlines():
        p = line.split(";")
        if len(p) < 7:
            continue
        p[-1] = p[-1].strip()
        try:
            d = int(p[0])
            if d <= 19000101:
                continue
            rows[d] = (float(p[1]), float(p[2]), float(p[3]), float(p[4]),
                       float(p[5]) if p[5] else 0.0, float(p[6]) if p[6] else 0.0)
        except (ValueError, IndexError):
            continue
    return rows


# ---------------------------------------------------------------------------
# 核对
# ---------------------------------------------------------------------------

def open_db(path):
    """只读打开本地下载的库副本（Windows 路径要转正斜杠并转义 `?`/`#`，同 live_db_builder.ro_uri）。"""
    uri = "file:%s?mode=ro" % path.replace("\\", "/").replace("?", "%3f").replace("#", "%23")
    conn = sqlite3.connect(uri, uri=True)
    conn.execute("PRAGMA query_only=1")
    return conn


def table_names(conn):
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def check_tables(conn):
    """五张表的 (行数, MAX(date))。"""
    log("-" * 78)
    log("① 五张周期表 行数 / MAX(date)")
    tabs = table_names(conn)
    out = {}
    for p in PERIODS:
        if p not in tabs:
            log("  %-10s ❌ 表不存在" % p)
            out[p] = None
            continue
        cnt, mx = conn.execute("SELECT COUNT(*), MAX(date) FROM %s" % p).fetchone()
        out[p] = (cnt, mx)
        log("  %-10s 行数=%10d   MAX(date)=%s" % (p, cnt, mx))
    return out


def check_last_date(conn, expect):
    """全部标的日线末日是否 == expect。"""
    log("-" * 78)
    log("② 日线末日核对（期望 %s）" % expect)
    total = conn.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
    rows = conn.execute(
        "SELECT m.file, MAX(d.date) FROM meta m LEFT JOIN daily d ON d.meta_id=m.id "
        "GROUP BY m.id").fetchall()
    bad = [(f, md) for f, md in rows if md != expect]
    log("  标的数=%d   末日==%s 的 %d 只   不符 %d 只"
        % (total, expect, total - len(bad), len(bad)))
    for f, md in bad[:20]:
        log("    ✗ %s  MAX(date)=%s" % (f, md))
    if len(bad) > 20:
        log("    ...（其余 %d 只略）" % (len(bad) - 20))
    return total, len(bad)


def db_dates_for(conn, file):
    """某标的库内日线日期集合（按 meta.file → meta_id）。"""
    r = conn.execute("SELECT id FROM meta WHERE file=?", (file,)).fetchone()
    if not r:
        return None
    ids = r[0]
    return {row[0] for row in conn.execute("SELECT date FROM daily WHERE meta_id=?", (ids,))}


def check_gaps(conn, new_txt_dir, symbols):
    """缺口：库内日期集合 vs txt 日期集合。"""
    log("-" * 78)
    log("③ 缺口检查（库内日线日期集合 vs txt 日期集合）")
    for f in symbols:
        txt_path = os.path.join(new_txt_dir, f + ".txt")
        if not os.path.exists(txt_path):
            log("  %-12s ⚠️ txt 不存在，跳过（%s）" % (f, txt_path))
            continue
        db_dates = db_dates_for(conn, f)
        if db_dates is None:
            log("  %-12s ⚠️ 主库 meta 无此 file，跳过" % f)
            continue
        txt_dates = set(read_txt_rows(txt_path).keys())
        miss = sorted(txt_dates - db_dates)      # txt 有、库内无（缺口）
        extra = sorted(db_dates - txt_dates)     # 库内有、txt 无（多余）
        status = "✅ 一致" if not miss and not extra else "❌ 不一致"
        log("  %-12s 库内 %5d / txt %5d 日 · 缺 %d · 多 %d  → %s"
            % (f, len(db_dates), len(txt_dates), len(miss), len(extra), status))
        if miss:
            log("      缺（txt 有、库内无）: %s%s"
                % (miss[:10], " ..." if len(miss) > 10 else ""))
        if extra:
            log("      多（库内有、txt 无）: %s%s"
                % (extra[:10], " ..." if len(extra) > 10 else ""))
    return True


def check_values(conn, new_txt_dir, symbols, tol=FLOAT_TOL):
    """逐行比对库内值与 txt（open/high/low/close/vol/amo）。"""
    log("-" * 78)
    log("④ 取值抽查（逐行比对库内值 vs txt，容差 %g）" % tol)
    fields = ("open", "high", "low", "close", "vol", "amo")
    for f in symbols:
        txt_path = os.path.join(new_txt_dir, f + ".txt")
        if not os.path.exists(txt_path):
            log("  %-12s ⚠️ txt 不存在，跳过" % f)
            continue
        r = conn.execute("SELECT id FROM meta WHERE file=?", (f,)).fetchone()
        if not r:
            log("  %-12s ⚠️ 主库 meta 无此 file，跳过" % f)
            continue
        mid = r[0]
        db_rows = {row[0]: row[1:] for row in conn.execute(
            "SELECT date, open, high, low, close, vol, amo FROM daily WHERE meta_id=?", (mid,))}
        txt_rows = read_txt_rows(txt_path)
        common = sorted(set(db_rows) & set(txt_rows))
        diff = 0
        first_diff = None
        for d in common:
            dv, tv = db_rows[d], txt_rows[d]
            for i, name in enumerate(fields):
                a, b = dv[i], tv[i]
                if a is None:
                    a = 0.0
                if abs(float(a) - float(b)) > tol:
                    diff += 1
                    if first_diff is None:
                        first_diff = (d, name, a, b)
                    break
        status = "✅ 一致" if diff == 0 else "❌ %d 行不一致" % diff
        log("  %-12s 比对 %d 行（库 %d / txt %d）  → %s"
            % (f, len(common), len(db_rows), len(txt_rows), status))
        if first_diff:
            d, name, a, b = first_diff
            log("      首个差异: date=%s  %s  库=%s  txt=%s" % (d, name, a, b))
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="设备主库全量核对（**手动工具**，流水线不会调用；只读设备、只写本地临时文件）")
    ap.add_argument("--expect-date", dest="expect_date", type=int, default=20260922,
                    help="期望的日线末日（YYYYMMDD，默认 20260922）")
    ap.add_argument("--new-txt-dir", dest="new_txt_dir", default=DEFAULT_NEW_TXT,
                    help="新 txt 目录（缺口与取值比对用，默认 %s）" % DEFAULT_NEW_TXT)
    ap.add_argument("--local", default=DEFAULT_LOCAL,
                    help="本地落盘路径（默认 %s）" % DEFAULT_LOCAL)
    ap.add_argument("--remote", default=REMOTE_DB,
                    help="设备沙盒内的主库相对路径（默认 %s）" % REMOTE_DB)
    ap.add_argument("--keep", action="store_true",
                    help="核对完**保留**本地下载的库文件（默认跑完删除，省 1.4GB）")
    args = ap.parse_args(argv)

    if not os.path.isdir(args.new_txt_dir):
        raise SystemExit("新 txt 目录不存在: %s" % args.new_txt_dir)

    import sandbox_cli  # noqa: E402  复用 usbmux forward / 在线探测

    local = os.path.abspath(args.local)
    os.makedirs(os.path.dirname(local), exist_ok=True)
    url = "%s/sandbox/%s" % (sandbox_cli.BASE, urllib.parse.quote(args.remote, safe="/"))

    log("=" * 78)
    log("设备主库全量核对（手动工具）")
    log("=" * 78)
    log("远程   : %s" % url)
    log("本地   : %s" % local)
    log("期望   : daily.MAX(date) == %s" % args.expect_date)
    log("提示   : 请让 Kline 保持前台、设备保持解锁（否则 5051 无响应）")
    log("-" * 78)

    fwd = sandbox_cli.forward()
    try:
        if not sandbox_cli.check_online():
            log("❌ 设备离线 / 未解锁（本地 HTTP 服务器未响应）。请在设备上打开 Kline 后重试。")
            return 1

        # ---- 1) 流式下载（no-proxy）----
        log("▶ 拉取设备主库（流式，no-proxy）…")
        try:
            got, total, el = download_stream(url, local)
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")[:200]
            log("❌ 下载失败 HTTP %s: %s" % (e.code, body))
            return 1
        log("✅ 已下载 %d B (%.1f MB)%s  耗时 %.1fs"
            % (got, got / 1048576.0,
               ("  / Content-Length %.1f MB" % (total / 1048576.0)) if total else "",
               el))
        if total and got != total:
            log("⚠️ 下载字节数与 Content-Length 不符（可能被截断）")

        # ---- 2) 核对 ----
        conn = open_db(local)
        try:
            check_tables(conn)
            total_sym, bad = check_last_date(conn, args.expect_date)
            check_gaps(conn, args.new_txt_dir, SAMPLE_GAP)
            check_values(conn, args.new_txt_dir, SAMPLE_VALUE)
        finally:
            conn.close()

        log("=" * 78)
        ok = (bad == 0)
        log("核对结论: 五表 MAX(date) 见 ①；末日不符 %d/%d 只 → %s"
            % (bad, total_sym, "✅ 通过" if ok else "❌ 未通过"))
        log("（这是**手动**核对工具，流水线不调用；日志只打印标量，不含大块库内容）")
        return 0 if ok else 1
    finally:
        fwd.terminate()
        if not args.keep and os.path.exists(local):
            try:
                os.remove(local)
                log("已删除本地临时库（--keep 可保留）: %s" % local)
            except OSError as exc:
                log("⚠️ 删除本地临时库失败: %s" % exc)


if __name__ == "__main__":
    sys.exit(main())
