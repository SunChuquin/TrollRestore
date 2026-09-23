#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kline 复权差分重灌包生成器（PC 侧）：**新库 tdx.db vs 旧库基线 tdx.db** → `patch_<seq>.db`

为什么要差分，而不是重推全量
------------------------------
主库里存的是**前复权**K线，前复权因子来自通达信 gbbq 股本变迁。每新增一次除权除息，
该标的的**全部历史**价格会被重新缩放 —— 所以"只追加最新一根"不够，必须把被改写的
历史行也同步给设备。主库 1.4 GB / 1400 万行日线，全量推送要 2~4 分钟且要重启 App；
差分包通常只有几 MB、秒级。

产出与既有分片**完全同构**（表结构照抄 `src/live_db_builder.py` 的 `build_bucket_file`）：
    bkt_meta(file TEXT PRIMARY KEY, code TEXT, name TEXT, type TEXT, updated_at INTEGER)
    bkt_daily / bkt_weekly / bkt_monthly(file, date, open, high, low, close, vol, amo,
                                          PRIMARY KEY(file, date))
这样设备侧 `LiveDataStore.mergeBucket(atPath:)` 可以原样复用 —— 它对包内日期**不做任何
校验**（ATTACH 后直接 `INSERT OR REPLACE`），所以跨年历史行也能被正确覆盖，
且不受 `KEEP_BUCKETS=30` 限制。

与分片的差别（**只有这一处**）：
  · 分片是"一天一片"，包内所有行的 date == 该片日期；补丁包 MAY 含任意多个日期；
  · 因此 `bkt_meta.updated_at` 取**包内最新日期**的 UTC 零点 epoch 秒（分片取的是该片日期
    的 UTC 零点，两者都是"确定性时间戳"，刻意不用墙钟，保证同内容包字节一致）。
  · `bkt_quarterly` / `bkt_yearly` **不在契约里**，两库都有这两张表时**只统计并打印**差异，
    不入包（设备侧合并逻辑没有对应表，硬塞会破坏契约）。

关键口径（改错会把设备上的K线写歪）
------------------------------------
1. **关联键只用 `file`（如 SH#600519），不比较任何 `meta_id`**。注意这**不是**因为"id 会变"：
   `tdx_parser.py:673-680` 本身保证同谱系内 id 稳定（按 `file` 命中就复用 `exist_meta[file]['id']`，
   `--rebuild` 同理）。不采用 `meta_id` 的理由是：它只是 parser 的记账细节、跨谱系不可比，
   且**实测存在"库内 meta 与数据表不同号"的脏库**（见第 5 条）。`file` 则是端到端契约键，
   从生成端到设备一路不改、唯一且带市场前缀。
2. 差异判定覆盖 `open/high/low/close/vol/amo` **任一字段**变化（复权改写的典型表现是
   全历史价格变、量额不变），并覆盖"新库有、基线没有"的 `(file, date)`（缺口补齐）。
3. 浮点比较用容差 `abs(a-b) > FLOAT_TOL`（默认 **1e-6**，可用 `--tol` 调整）：
   主库价格是 `REAL`，同一次写入的同一数值应当逐位相同；1e-6 只用于吸收 SQLite
   在 INT/REAL 混存与读写往返时的表示噪声（例如 10 vs 10.0、1e-7 级舍入），
   相对前复权改写（动辄千分之几到几成）小 3~5 个数量级，**不会漏掉真实改写**。
   判定式写成 `(n.x IS b.x)=0 OR abs(n.x-b.x)>tol`，`IS` 是 null-safe 相等，
   于是"一侧为 NULL、另一侧有值"也会被判为变化。
4. 基线里存在、新库里没有的 `(file, date)`（通常是退市/删除标的）**只统计并报告**，
   **本次不生成删除指令**（是否删除设备上的对应行见报告与 spec，需要单独决策）。
5. **出包前先验基库自洽**：`file → meta_id` 是各库**自己的 meta 表**给出的，所以某库一旦
   出现"meta 表与数据表用了两个不同 id 空间"（悬空 meta_id、first/last_date 与数据范围
   系统性不符），按 file 关联就会拿 A 标的的价格比 B 标的 → 产出上千万行假差异。
   本脚本用 `check_meta_consistency` 把这种库拦下来（`--allow-inconsistent-db` 可强行绕过）。
6. **差异扫描必须「逐 file 局部核对」**（性能红线，别改回覆盖全库的一条大 JOIN）：
   大 JOIN 会对 1400 万行各做一次**随机**索引探查，实测 daily 单周期 208s；
   而 `WHERE n.meta_id = ?` 把两侧探查都限制在同一个 file 的连续页内（命中缓存），
   实测 **0.005s/只 → 3611 只 19.8s**。这是本脚本 400s → 约 40s 的唯一原因（见 `scan_period`）。

候选集裁剪（`--old-txt-dir` / `--new-txt-dir`，省掉 110.5s 的全量比对）
----------------------------------------------------------------------
导入端 `tdx_parser.py` 现在按「新旧两份 txt 目录」判定哪些标的**真变了**（唯一一份实现：
`src/txt_changes.py`）。本脚本复用同一份判定，把差异扫描的 `pairs` 直接裁成候选 file 集，
只对"txt 真变过的标的"逐 file 局部核对，其余整批跳过。
  · **为什么能省 110 秒**：全量比对是「3611 只 × 5 周期」逐文件局部核对，实测 110.5s
    （daily 67.5 / weekly 25.0 / monthly 12.1 / 季 4.5 / 年 1.4）。而一次增量里真正被复权改写
    的标的**常态只有 20~30 只**，裁掉 3587 只后差异扫描降到秒级，全跑 **138.4s → ~30s**。
  · 判定三档（均来自 `txt_changes`，实测）：`stat` 比 size+mtime **~0.1s/3637 文件**
    （够用——复权改写会改 mtime）；`content` sha256 全量 **~30s / 701MB×2**（12.5s+17.1s）；
    `bc` Beyond Compare 5 CLI `criteria crc` **26.8s（热）/ 29.5s（冷）**，精确命中 24 个
    「同尺寸改写」文件；BC 缺失或调用失败时**自动退回 content** 并写明原因。
  · 候选为空 → 直接走既有「无差异」分支，**不做任何行级扫描**。
  · `--no-prune`：强制全量比对（排查用，忽略候选集裁剪）。
  · 裁剪只影响"扫哪些 file"，并把自洽性检查的**逐 file 校验收窄到候选集**（见下节）；
    **不改**差异判定口径、出包结构与完整性校验方式。

固定开销优化（`--verify-hashes` / 自洽性收窄）
---------------------------------------------
候选集裁剪解决"扫多少"，本节解决"固定开销"：
  · **完整性校验默认只比 `(size, mtime)`**（`--verify-hashes` 打开才跑 sha256）。两库都是
    只读打开 + `PRAGMA query_only=1`，内容被改写机制上不可能，sha256 属额外保险；
    默认省掉 **4 遍读 1.4GB（~35s）**。日志明确写 `mode=size+mtime` 还是 `mode=sha256`，
    受影响 file 清单头部也按模式打印 `(size+mtime)` 或 `sha256=` 摘要，**绝不打假/空 sha256**。
  · **自洽性检查按范围收窄**：裁剪生效时只对**候选 file** 逐 file 核对 file↔meta_id
    （`check_meta_consistency(conn, schema, cand)`）；全局悬空 id 检查
    （`SELECT DISTINCT meta_id FROM daily EXCEPT SELECT id FROM meta`）两种范围都照跑，
    作廉价兜底。**拒绝语义不放松**：dangling 必须为 0；候选范围 first/last_date **零容忍**
    （候选 file 就是会被比对的 file，1 个不符即 exit 3），未裁剪时保持原全量 5% 容差。

性能（2026-09-22 实测，1.4GB 库对，**热缓存**、默认无 `--verify-hashes`）
----------------------------------------------------------------------------
| 环节 | 本轮前（固定开销未动） | 现状（默认：size+mtime） |
| :--- | ---: | ---: |
| 基线校验 | mode=sha256 ×2：16.3s | **mode=size+mtime：0.0s** |
| 候选判定（stat） | 0.7s | 0.1s |
| 自洽性检查 | 全量 3611 只：63.9s | **候选范围**：7.1s（0 候选）/ 6.6s（4 候选） |
| 差异扫描 + 出包 | 候选 0 只：0.0s | 0.0s |
| 完整性复核 | mode=sha256 ×2：19.2s | **mode=size+mtime：0.0s** |
| **总耗时** | **100.2s** | **7.3s（0 候选）/ 6.8s（有候选）** |
参考：`--no-prune` 全量扫描实测 daily 311.6s + 周 78.1 + 月 19.4 + 季 7.9 + 年 2.2 ≈ **419.2s**。
`--verify-hashes` 打开时恢复 sha256：基线 13.5s + 复核 13.4s（0 候选总 **57.4s**）。
现状瓶颈是自洽性里的 `DISTINCT meta_id EXCEPT` 索引扫描（~3.6s/库，纯磁盘 I/O）：
**冷缓存**下这一步会放大到 ~35s/库（总 ~70s），热缓存 ~3.6s/库。

安全（硬约束，与 `build_live_buckets_pc.py` 同风格）
----------------------------------------------------
两个库都用 `file:...?mode=ro` **只读打开**，连接上再加 `PRAGMA query_only=1` 双保险，
脚本里**不存在任何写这两个库的代码路径**；跑完自动核对两库的 `(size, mtime)` 一字未变
（默认只比 size+mtime，`--verify-hashes` 时叠 sha256），变了就大声报错。
注意 SQLite 读 WAL 库时可能在库旁生成 `-shm`/`-wal`，那是读侧副作用，库文件本身不会被改写。

用法
----
    python TrollRestore/diff_live_patch.py                       # 用默认两库出补丁包
    python TrollRestore/diff_live_patch.py --dry-run             # 只统计不落盘
    python TrollRestore/diff_live_patch.py --seq 3               # 指定补丁序号
    python TrollRestore/diff_live_patch.py --tol 1e-9            # 收紧容差
    python TrollRestore/diff_live_patch.py --old-txt-dir <旧txt目录>   # 候选集裁剪（推荐，~30s）
    python TrollRestore/diff_live_patch.py --old-txt-dir <旧> --txt-compare bc   # 用 BC 做内容判定
    python TrollRestore/diff_live_patch.py --no-prune            # 强制全量比对（排查用）
    python TrollRestore/diff_live_patch.py --verify-hashes       # 额外跑 sha256 完整性校验（默认关）
"""

import argparse
import datetime
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
KLINE_REPO = r"c:\Users\sunck\home\projects\ios\Kline"
DEFAULT_NEW_DB = r"C:\Users\sunck\home\tdx.db"
DEFAULT_BASE_DB = r"C:\Users\sunck\home\projects\tdx_project\tdx.db"
DEFAULT_OUT = os.path.join(KLINE_REPO, "build_logs", "patches")
DEFAULT_TXT_DIR = r"C:\Users\sunck\home\tdx_data"     # 新 txt 目录（候选集裁剪用）

# 复用生成端唯一一份实现（只读 URI / sha256 / 日期口径 / 分片落盘）
sys.path.insert(0, os.path.join(KLINE_REPO, "src"))
sys.path.insert(0, HERE)

import live_db_builder as L        # noqa: E402
import txt_changes                 # noqa: E402  新旧 txt 目录变更判定（全项目唯一一份）

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

FLOAT_TOL = 1e-6                        # 浮点容差（见 docstring 口径 3）
FIELDS = ("open", "high", "low", "close", "vol", "amo")
PATCH_PREFIX = "patch_"
# 包内可承载的周期：与分片契约完全一致（bkt_daily / bkt_weekly / bkt_monthly）
PATCH_PERIODS = tuple(L.PERIODS)
# 契约里没有 bkt_quarterly / bkt_yearly → 只统计报告，不入包
REPORT_ONLY_PERIODS = ("quarterly", "yearly")
BIG_PATCH_WARN_ROWS = 2_000_000         # 差异行数超过这个量级大概率不是"日常差分"，给个提示


# ---------------------------------------------------------------------------
# 只读 / 完整性核对
# ---------------------------------------------------------------------------

def shield_sha256(path):
    """库完整性核对用的 sha256（分块读，1.6GB 约几秒）。"""
    return L.sha256_file(path)


def hash_mode(verify_hashes):
    return "sha256" if verify_hashes else "size+mtime"


def db_state(path, verify_hashes=False):
    """抓某库的状态快照。

    `--verify-hashes` 关（默认）时**完全不算 sha256**，只用 (size, mtime_ns)：
    两库都是 `mode=ro` + `PRAGMA query_only=1` 打开的，任何写操作都会被 SQLite 拒绝，
    所以"内容被改写"在机制上不可能发生，sha256 只是额外保险（内部阶段可放宽，省掉 4 遍读 1.4GB）。
    """
    st = os.stat(path)
    state = {"mtime": st.st_mtime, "mtime_ns": st.st_mtime_ns, "size": st.st_size}
    if verify_hashes:
        state["sha256"] = shield_sha256(path)
    return state


def state_desc(st):
    """摘要串：有 sha256（--verify-hashes）就打 sha256，否则打 (size+mtime)。绝不打假/空 sha256。"""
    ts = datetime.datetime.fromtimestamp(st["mtime"]).strftime("%Y-%m-%d %H:%M:%S")
    if "sha256" in st:
        return "sha256=%s…  %d B  mtime=%s" % (st["sha256"][:16], st["size"], ts)
    return "(size+mtime)=%d B  %s" % (st["size"], ts)


def verify_unchanged(name, path, before, after, verify_hashes=False):
    """前后状态比对：默认比 (size, mtime_ns)；--verify-hashes 时再叠 sha256。"""
    size_same = (after["size"], after["mtime_ns"]) == (before["size"], before["mtime_ns"])
    if verify_hashes:
        sha_same = after.get("sha256") == before.get("sha256")
        same = sha_same and size_same
        print("%s完整性（mode=sha256）: sha256 %s  mtime/size %s  %s"
              % (name, "一致" if sha_same else "不一致!",
                 "一致" if size_same else "不一致!",
                 "✅ 未被修改" if same else "❌ 被修改了，请立即排查！"))
        return same
    same = size_same
    print("%s完整性（mode=size+mtime）: size/mtime %s  %s"
          % (name, "一致" if size_same else "不一致!",
             "✅ 未被修改" if same else "❌ 被修改了，请立即排查！"))
    return same


def integrity_recheck(new_db, base_db, before_new, before_base, verify_hashes=False):
    """跑完的两库完整性复核（硬约束：内容不得被改写；默认用 size+mtime，--verify-hashes 叠 sha256）。"""
    print("-" * 78)
    t0 = time.time()
    print("完整性复核（mode=%s）:" % hash_mode(verify_hashes))
    ok_new = verify_unchanged("新库  ", new_db, before_new, db_state(new_db, verify_hashes), verify_hashes)
    ok_base = verify_unchanged("基线库", base_db, before_base, db_state(base_db, verify_hashes), verify_hashes)
    print("（完整性复核耗时 %.1fs）" % (time.time() - t0))
    return ok_new and ok_base


def table_names(conn, schema):
    """某库里现存的表名集合（main / base）。"""
    return {r[0] for r in conn.execute(
        "SELECT name FROM %s.sqlite_master WHERE type='table'" % schema)}


# ---------------------------------------------------------------------------
# 基库自洽性检查（**出包前的硬门槛**）
# ---------------------------------------------------------------------------

def check_meta_consistency(conn, schema, cand_files=None, period="daily"):
    """核对某库「meta.id ↔ 数据表 meta_id」是否指向同一 `file`（判据只用 daily）。

    为什么必须有这一步：本脚本按 spec 用 `file` 关联，而 `file → meta_id` 是**各库自己的
    meta 表**给出来的。若某库的 meta 与数据表用了**不同的 id 空间**，那么"按 file 关联"会拿
    A 标的的价格去比 B 标的，产出上千万行假差异 —— 最坏情况是给设备灌一个 GB 级、内容全错的补丁包。
    实测 `C:\\Users\\sunck\\home\\projects\\tdx_project\\tdx.db` 就是这样一份库：它的**数据表**与当前主库
    同号（按 (meta_id,date) 对齐 1400 万行值差异 = 0），但 **meta 表被重新编号**为连续 1..3611，
    而数据表是 3..3637（含 24 个空洞）→ 内部错位（茅台在该库 meta 里是 732，真值却在 daily 的 756）。
    注：`meta.id ∈ [1,3611]` 是「重新编号」，不是「按字母序重排」。

    **范围收窄（候选集裁剪生效时）**：这个门槛的目的只是"保证**被比对的 file** 的
    file↔meta_id 映射可信"。裁剪后真正会被拿去出包的 file 全在候选集里（常态 20~30 只），
    所以把逐 file 校验收窄到候选集**不降低保护强度**，却省掉对 3611 只的全表聚合。
      · 全局悬空检查两种范围都保留（`SELECT DISTINCT meta_id … EXCEPT SELECT id …` 是纯索引
        扫描，实测 ~3.4s/库，比原 LEFT JOIN 的 5.1s 更快），作为廉价兜底；
      · 逐 file 的 first/last_date 校验：候选范围只查候选 file 的 meta_id（量级几十行，
        走 (meta_id,date) 索引）；全量范围（未裁剪）保持原 GROUP BY 行为不变。

    判据（**只用 daily**：`meta.first_date/last_date` 记录的是**日线**首末交易日，
    周/月线的 MIN/MAX(date) 天然与它不同 —— 实测好库的 weekly/monthly 会 3611/3611 不符，
    那是口径差异不是错误，所以不能拿周/月线做判据）：
      · dangling: 数据表引用、但 meta 里不存在的 meta_id 的行数（**应为 0**）；
      · mismatch: first_date/last_date 与数据实际 MIN/MAX(date) 对不上的 file 数。
    **拒绝语义不放松**：两种情况 dangling 都必须为 0；候选范围对 mismatch **零容忍**
    （`mism == 0`，候选 file 就是会被比对的 file），全量范围沿用原 5% 容差公式。
    返回 {"dangling", "total", "nodata", "mismatch", "ok", "scope"}。
    """
    # 全局悬空 id：EXCEPT 纯索引扫描，便宜；只对返回的悬空 id 再数行数
    dang_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT meta_id FROM %s.%s EXCEPT SELECT id FROM %s.meta"
        % (schema, period, schema))]
    dangling = 0
    if dang_ids:
        dangling = conn.execute("SELECT COUNT(*) FROM %s.%s WHERE meta_id IN (%s)"
                                % (schema, period, ",".join("?" * len(dang_ids))),
                                tuple(dang_ids)).fetchone()[0]

    if cand_files is None:
        total, nodata, mism = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN d.mn IS NULL THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN d.mn IS NOT NULL AND ((m.first_date IS NOT NULL AND d.mn<>m.first_date) "
            "     OR (m.last_date IS NOT NULL AND d.mx<>m.last_date)) THEN 1 ELSE 0 END) "
            "FROM %s.meta m LEFT JOIN (SELECT meta_id, MIN(date) mn, MAX(date) mx FROM %s.%s "
            "GROUP BY meta_id) d ON d.meta_id=m.id" % (schema, schema, period)).fetchone()
        total, nodata, mism = total or 0, nodata or 0, mism or 0
        # 5% 容差：允许极少数标的的历史首末行有出入（如停牌/退市边缘），但不允许整体错位
        ok = (dangling == 0) and (mism <= max(1, int(total * 0.05)))
        scope = "all(%d 只)" % total
    else:
        files = sorted(cand_files)
        if not files:
            return {"dangling": dangling, "total": 0, "nodata": 0, "mismatch": 0,
                    "ok": dangling == 0, "scope": "candidate(0 只)"}
        mrows = conn.execute("SELECT file,id,first_date,last_date FROM %s.meta WHERE file IN (%s)"
                             % (schema, ",".join("?" * len(files))), tuple(files)).fetchall()
        ids = [r[1] for r in mrows]
        agg = {}
        if ids:
            for mid, mn, mx in conn.execute(
                    "SELECT meta_id, MIN(date), MAX(date) FROM %s.%s WHERE meta_id IN (%s) "
                    "GROUP BY meta_id" % (schema, period, ",".join("?" * len(ids))), tuple(ids)):
                agg[mid] = (mn, mx)
        total = len(mrows)
        nodata = mism = 0
        for _file, mid, fd, ld in mrows:
            mn, mx = agg.get(mid, (None, None))
            if mn is None:
                nodata += 1
            elif (fd is not None and mn != fd) or (ld is not None and mx != ld):
                mism += 1
        ok = (dangling == 0) and (mism == 0)          # 候选范围：零容忍
        scope = "candidate(%d 只)" % total
    return {"dangling": dangling, "total": total, "nodata": nodata,
            "mismatch": mism, "ok": ok, "scope": scope}


def print_consistency(name, st, period="daily"):
    print("  %-6s %-6s 范围=%-16s meta %d 行 · 无数据 %d · 悬空 meta_id %d 行 · "
          "first/last_date 不符 %d file  → %s"
          % (name, period, st.get("scope", "all"), st["total"], st["nodata"], st["dangling"],
             st["mismatch"], "✅ 自洽" if st["ok"] else "❌ 不自洽"))


# ---------------------------------------------------------------------------
# 差异 SQL：全程用 file 关联（绝不碰 meta_id 的"值"，只用它做索引查找）
# ---------------------------------------------------------------------------

def change_predicate(tol):
    """任一字段变化的判定式（null-safe + 容差，见 docstring 口径 3）。"""
    return " OR ".join("(n.%s IS b.%s)=0 OR abs(n.%s-b.%s)>%s" % (f, f, f, f, repr(tol))
                       for f in FIELDS)


# 逐 file 核对的 SQL（**性能关键**：不要改回覆盖全库的一条大 JOIN，理由见 docstring 性能一节）

def sql_changed_or_new(period, tol):
    """该 file 的「改写 + 主库新增」行 —— 一条 SQL 同时拿两类。

    参数顺序 `(base_id, main_id)`；返回第 8 列 1 = 主库新增（基线没有该 date）、0 = 改写。
    """
    return ("SELECT n.date, n.open, n.high, n.low, n.close, n.vol, n.amo, (b.date IS NULL) "
            "FROM main.%s n LEFT JOIN base.%s b ON b.meta_id = ? AND b.date = n.date "
            "WHERE n.meta_id = ? AND (b.date IS NULL OR %s)"
            % (period, period, change_predicate(tol)))


def sql_main_only_rows(period):
    """仅新库有的 file（基线里没有它）→ 该 file 全部行都算「新增」。参数：`main_id`。"""
    return "SELECT date, open, high, low, close, vol, amo FROM main.%s WHERE meta_id = ?" % period


def sql_period_counts(period):
    """该 file 在两库该周期的行数（走 (meta_id,date) 索引，index-only，极快）。

    参数 `(main_id, base_id)`；`base_id` 传 -1 即得 0（用于「仅新库有的 file」）。
    """
    return ("SELECT (SELECT COUNT(*) FROM main.%s WHERE meta_id = ?), "
            "(SELECT COUNT(*) FROM base.%s WHERE meta_id = ?)" % (period, period))


def file_id_pairs(conn):
    """`file → (main_id, base_id)`。

    关联仍以 **`file` 为契约键**（见口径 1）——两库各自的 id 只用来做**定位**，
    各自从自己的 `meta` 里按 `file` 查出来，所以不依赖「两库 id 空间一致」这个前提。
    仅新库有的 file 给 `base_id=None`（其全部行按「新增」处理）。
    """
    main_map = {f: i for i, f in conn.execute("SELECT id, file FROM main.meta")}
    base_map = {f: i for i, f in conn.execute("SELECT id, file FROM base.meta")}
    return [(f, main_map[f], base_map.get(f)) for f in sorted(main_map)]


def sql_files_only_side(left, right):
    """只在 left 库出现、right 库没有的 file（用于报告新上市 / 退市标的）。"""
    return ("SELECT l.file FROM %s.meta l LEFT JOIN %s.meta r ON r.file = l.file "
            "WHERE r.id IS NULL" % (left, right))


def _f(x):
    return 0.0 if x is None else float(x)


# ---------------------------------------------------------------------------
# 扫描：每个周期一遍
# ---------------------------------------------------------------------------

def scan_period(conn, period, tol, gather, rows, totals, per_file, pairs):
    """扫一个周期：**逐 file 局部核对**，一条 SQL 同时拿「改写」与「主库新增」。

    `base_only`（基线独有）由行数差算出：`base_only = base_cnt - (main_cnt - new_only)`
    —— 「两边都有的行数」= `main_cnt - new_only`，所以不必再扫一遍基线。

    为什么不是一条覆盖全库的大 JOIN：那会对 1400 万行各做一次**随机**索引探查，
    实测 daily 单周期 208s；而 `WHERE n.meta_id = ?` 把两侧探查都限制在同一个 file 的连续页内
    （全部命中缓存），实测 0.005s/只 → 3611 只 19.8s。关联键仍是 `file`，语义不变。

    返回该周期的耗时（秒）。
    """
    t0 = time.time()
    sql_pair = sql_changed_or_new(period, tol)
    sql_main_only = sql_main_only_rows(period)
    sql_cnt = sql_period_counts(period)
    changed = new_only = base_only = 0
    for f, mid, bid in pairs:
        n_chg = n_new = 0
        if bid is None:
            # 仅新库有的 file：基线里没有它，故该 file 全部行都是「新增」
            for r in conn.execute(sql_main_only, (mid,)):
                n_new += 1
                if gather:
                    rows[period].append((f, int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]),
                                         _f(r[4]), _f(r[5]), _f(r[6])))
        else:
            for r in conn.execute(sql_pair, (bid, mid)):
                if r[7]:
                    n_new += 1
                else:
                    n_chg += 1
                if gather:
                    rows[period].append((f, int(r[0]), _f(r[1]), _f(r[2]), _f(r[3]),
                                         _f(r[4]), _f(r[5]), _f(r[6])))
        main_cnt, base_cnt = conn.execute(sql_cnt,
                                         (mid, bid if bid is not None else -1)).fetchone()
        bo = base_cnt - (main_cnt - n_new)
        changed += n_chg
        new_only += n_new
        base_only += bo
        if n_chg or n_new or bo:
            rec = per_file.setdefault(f, {})
            if n_chg or n_new:
                rec[period] = rec.get(period, 0) + n_chg + n_new
            if bo:
                rec["base_only"] = rec.get("base_only", 0) + bo
    totals[period]["changed"] = changed
    totals[period]["new_only"] = new_only
    totals[period]["base_only"] = base_only
    el = time.time() - t0
    print("    %-9s 改写 %8d / 新增 %8d / 基线独有 %8d 行  %.1fs"
          % (period, changed, new_only, base_only, el))
    return el


def check_patch_db(path, rows, patch_periods, expect_meta):
    """写出后的轻量自检：各表行数与内存 rows 一致、bkt_meta 无重复（读回核对）。

    不能复用 `L.validate_bucket`：它假定"包内所有行 date == 分片日期"，补丁包刻意不是。
    """
    conn = sqlite3.connect(L.ro_uri(path), uri=True)
    try:
        problems = []
        got = conn.execute("SELECT COUNT(*) FROM bkt_meta").fetchone()[0]
        if got != expect_meta:
            problems.append("bkt_meta 行数不一致: 实际 %d / 应为 %d" % (got, expect_meta))
        dup = conn.execute("SELECT COUNT(*) FROM (SELECT file FROM bkt_meta "
                           "GROUP BY file HAVING COUNT(*)>1)").fetchone()[0]
        if dup:
            problems.append("bkt_meta 存在 %d 个重复 file" % dup)
        for period in patch_periods:
            cnt = conn.execute("SELECT COUNT(*) FROM bkt_%s" % period).fetchone()[0]
            if cnt != len(rows.get(period) or []):
                problems.append("bkt_%s 行数不一致: 实际 %d / 声明 %d"
                                % (period, cnt, len(rows.get(period) or [])))
        if problems:
            raise AssertionError("补丁包校验失败:\n  - %s" % "\n  - ".join(problems))
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# 报告落盘
# ---------------------------------------------------------------------------

def write_patch_report(path, header_lines, columns, per_file):
    """落一份可复查的「受影响 file 清单」（UTF-8，TAB 分隔）。"""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        for line in header_lines:
            fh.write("# %s\n" % line)
        fh.write("# 列: file\t%s\n" % "\t".join(columns))
        for f in sorted(per_file):
            rec = per_file[f]
            fh.write("%s\t%s\n" % (f, "\t".join(str(int(rec.get(c, 0))) for c in columns)))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def next_seq(out_dir):
    """扫描输出目录里已有的 patch_<seq>.db，返回下一个可用序号。"""
    seqs = []
    if os.path.isdir(out_dir):
        for name in os.listdir(out_dir):
            if name.startswith(PATCH_PREFIX) and name.endswith(".db"):
                body = name[len(PATCH_PREFIX):-len(".db")]
                if body.isdigit():
                    seqs.append(int(body))
    return max(seqs) + 1 if seqs else 1


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Kline 复权差分重灌包生成器：新库 vs 旧库基线 → patch_<seq>.db（两库只读）")
    ap.add_argument("--new-db", dest="new_db", default=DEFAULT_NEW_DB,
                    help="新库路径（默认 %s，**只读打开**）" % DEFAULT_NEW_DB)
    ap.add_argument("--base-db", dest="base_db", default=DEFAULT_BASE_DB,
                    help="旧库基线路径（默认 %s，**只读打开**）" % DEFAULT_BASE_DB)
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="输出目录（默认 %s，放 patch_<seq>.db 与清单 txt）" % DEFAULT_OUT)
    ap.add_argument("--seq", type=int, default=None,
                    help="补丁序号，命名 patch_<seq>.db（默认自动取输出目录里已有序号 +1）")
    ap.add_argument("--dry-run", dest="dry_run", action="store_true",
                    help="只统计不落盘（不产出 patch_*.db，仍打印摘要与受影响 file 清单）")
    ap.add_argument("--tol", type=float, default=FLOAT_TOL,
                    help="浮点容差，判定 abs(a-b)>tol 为变化（默认 %g，见脚本文档）" % FLOAT_TOL)
    ap.add_argument("--allow-inconsistent-db", dest="allow_bad_db", action="store_true",
                    help="【危险】即使某库的 meta 与数据表 id 空间不自洽也强行出包（结果不可信，"
                         "只在确认过该库确实是这样的历史库时才用）")
    ap.add_argument("--old-txt-dir", dest="old_txt_dir", default=None,
                    help="旧 txt 目录：与 --new-txt-dir 做变更判定，把差异扫描裁成候选 file 集"
                         "（不给=不裁剪、全量比对 3611 只；见 docstring「候选集裁剪」）")
    ap.add_argument("--new-txt-dir", dest="new_txt_dir", default=DEFAULT_TXT_DIR,
                    help="新 txt 目录（默认 %s）" % DEFAULT_TXT_DIR)
    ap.add_argument("--txt-compare", dest="txt_compare", default="stat",
                    choices=["stat", "content", "bc"],
                    help="新旧目录变更判定方式（默认 stat：比 size+mtime ~0.1s；content=sha256 ~30s；"
                         "bc=Beyond Compare CLI ~27s，失败自动退回 content）")
    ap.add_argument("--bc-exe", dest="bc_exe", default=None,
                    help="BCompare.exe 路径（默认自动探测 Program Files）")
    ap.add_argument("--no-prune", dest="no_prune", action="store_true",
                    help="强制全量比对（忽略候选集裁剪，排查用）")
    ap.add_argument("--verify-hashes", dest="verify_hashes", action="store_true",
                    help="跑前后对两库各算 sha256 并按 sha256 校验（默认关：只比 size+mtime，"
                         "省掉 4 遍读 1.4GB；两库只读打开，内容被改在机制上不可能）")
    args = ap.parse_args(argv)

    t_all = time.time()

    # ---- 路径与基线检查 ----
    if not os.path.exists(args.new_db):
        raise SystemExit("新库不存在: %s" % args.new_db)
    if not os.path.exists(args.base_db):
        raise SystemExit(
            "❌ 基线库不存在: %s\n"
            "   差分必须拿「旧库基线」当参照，本脚本**不会**静默退化成全库级大包。请先建立基线：\n"
            "     ① 确认某个 tdx.db 是「设备上已同步过的那一版」（按需备份留存）；\n"
            "     ② 把它整份复制到基线路径，例如：\n"
            "        copy \"C:\\Users\\sunck\\home\\tdx.db\" "
            "\"C:\\Users\\sunck\\home\\projects\\tdx_project\\tdx.db\"\n"
            "     ③ 之后每次重建 tdx.db 再跑本脚本，即可得到增量补丁包。"
            % args.base_db)

    out_dir = os.path.abspath(args.out)
    print("新库  : %s" % args.new_db)
    print("基线库: %s" % args.base_db)
    print("输出  : %s%s" % (out_dir, "（--dry-run：不落盘）" if args.dry_run else ""))

    # ---- 两库完整性基线（跑完必须一字不差）----
    t0 = time.time()
    before_new = db_state(args.new_db, args.verify_hashes)
    before_base = db_state(args.base_db, args.verify_hashes)
    print("基线校验（mode=%s）:" % hash_mode(args.verify_hashes))
    print("新库  : %s" % state_desc(before_new))
    print("基线库: %s" % state_desc(before_base))
    print("（基线校验耗时 %.1fs）" % (time.time() - t0))

    # ---- 只读打开 + ATTACH（两个库都有 meta 表 → 必须给别名并在 SQL 里加库前缀）----
    refuse = None
    conn = sqlite3.connect(L.ro_uri(args.new_db), uri=True)      # main = 新库，只读
    try:
        conn.execute("ATTACH DATABASE '%s' AS base" % L.ro_uri(args.base_db))   # 基线，只读
        conn.execute("PRAGMA query_only=1")      # 双保险：本连接上任何写操作都会被拒绝

        new_tables = table_names(conn, "main")
        base_tables = table_names(conn, "base")
        for schema, tabs in (("新库", new_tables), ("基线库", base_tables)):
            if "meta" not in tabs:
                raise SystemExit("%s 缺少 meta 表，无法差分" % schema)

        both = [p for p in PATCH_PERIODS + REPORT_ONLY_PERIODS
                if p in new_tables and p in base_tables]
        patch_periods = [p for p in PATCH_PERIODS if p in both]
        report_periods = [p for p in REPORT_ONLY_PERIODS if p in both]
        for p in PATCH_PERIODS + REPORT_ONLY_PERIODS:
            if p not in both:
                print("[info] 周期 %s 未在两库中同时存在（新库 %s / 基线 %s），跳过"
                      % (p, p in new_tables, p in base_tables))
        if not patch_periods:
            raise SystemExit("daily/weekly/monthly 未在两库中同时存在，无法出包")

        # ---- 候选集裁剪：先算候选 file 集（同时供下面自洽性收窄与差异扫描使用）----
        cand = None            # None = 不裁剪（全量比对）
        cres = None
        cmp_el = 0.0
        if args.old_txt_dir and not args.no_prune:
            t_cmp = time.time()
            cres = txt_changes.changed_files(args.old_txt_dir, args.new_txt_dir,
                                             mode=args.txt_compare, bc_exe=args.bc_exe)
            cmp_el = time.time() - t_cmp
            cand = {os.path.splitext(n)[0] for n in cres["changed"]}
            print("候选集判定（mode=%s，%.1fs）· 候选 %d 只（%s）"
                  % (cres["mode"], cmp_el, len(cand), cres["detail"]))

        # ---- 基库自洽性门槛：meta.id 与数据表 meta_id 必须指向同一 file（见函数注释）----
        print("自洽性检查（file ↔ meta_id 映射是否可信）:")
        print("  校验范围: %s"
              % ("candidate(%d 只)" % len(cand) if cand is not None else "all(全量)"))
        t0 = time.time()
        cons = {"新库": check_meta_consistency(conn, "main", cand),
                "基线库": check_meta_consistency(conn, "base", cand)}
        for name, st in cons.items():
            print_consistency(name, st)
        print("  （自洽性检查耗时 %.1fs）" % (time.time() - t0))
        bad = {n: st for n, st in cons.items() if not st["ok"]}
        if bad and not args.allow_bad_db:
            refuse = ["❌ %s 的 meta 与数据表**不是同一个 id 空间**，" % "、".join(sorted(bad))
                      + "按 file 关联会拿 A 标的的价格去比 B 标的，产出上千万行「假差异」。"]
            for n, st in sorted(bad.items()):
                refuse.append("   %s: 悬空 meta_id %d 行、first/last_date 不符 %d/%d 个 file、"
                              "无数据 file %d 个" % (n, st["dangling"], st["mismatch"],
                                                 st["total"], st["nodata"]))
            refuse += [
                "   本脚本已拦住，**不会**产出这种 GB 级垃圾包。处理建议：",
                "     ① 用「设备上已同步过的那一版主库」的**整份文件拷贝**当基线（同一个库文件，天然自洽）；",
                "     ② 或把当前可信的主库复制到基线路径，作为下一次差分的基线；",
                "     ③ 确知该库就是这样、且只想要个「包长什么样」的样本时，才加 --allow-inconsistent-db。",
            ]
        if refuse is None and bad:
            print("[warn] --allow-inconsistent-db：无视上面的不自洽继续出包，**结果不可信**")
        if refuse is not None:
            # 拒绝出包：立刻停在这里（**绝不进入下面的差异扫描**），完整性复核照跑
            print("-" * 78)
            print("\n".join(refuse))
            conn.close()                    # 先关连接，再复核（复核要重新读文件）
            ok = integrity_recheck(args.new_db, args.base_db, before_new, before_base,
                                   args.verify_hashes)
            print("总耗时 %.1fs" % (time.time() - t_all))
            return 3 if ok else 1

        # ---- 只在单侧出现的 file（新上市 / 退市）----
        new_only_files = {r[0] for r in conn.execute(sql_files_only_side("main", "base"))}
        base_only_files = {r[0] for r in conn.execute(sql_files_only_side("base", "main"))}
        if new_only_files:
            print("[info] 仅新库有 %d 只（新上市/新覆盖）: %s"
                  % (len(new_only_files), ", ".join(sorted(new_only_files)[:10])))
        if base_only_files:
            print("[info] 仅基线有 %d 只（退市/已从新库移除）: %s"
                  % (len(base_only_files), ", ".join(sorted(base_only_files)[:10])))

        # ---- 逐周期扫描 ----
        rows = {"meta": []}
        for p in patch_periods:
            rows[p] = []
        totals = {p: {"changed": 0, "new_only": 0, "base_only": 0} for p in both}
        per_file = {}
        # file → (main_id, base_id)：关联键仍是 file，id 只是各自库内的定位（见 file_id_pairs）
        pairs = file_id_pairs(conn)
        # ---- 候选集裁剪：把 pairs 裁到"txt 真变过的标的"（候选集已在自洽性检查前算好）----
        if cand is not None:
            full = len(pairs)
            pairs = [p for p in pairs if p[0] in cand]
            print("候选集裁剪：生效（mode=%s，%.1fs）· 候选 %d / 全量 %d 只 · 跳过 %d 只"
                  % (cres["mode"], cmp_el, len(pairs), full, full - len(pairs)))
            print("  （旧txt %d 文件 / 新txt %d 文件，仅旧目录 %d 个、仅新目录 %d 个）"
                  % (cres["old_count"], cres["new_count"],
                     len(cres["only_old"]), len(cres["only_new"])))
            if not pairs:
                print("  候选集为空 → 直接走「无差异」分支，不做任何行级扫描")
        elif args.old_txt_dir and args.no_prune:
            print("候选集裁剪：已跳过（--no-prune：强制全量比对 %d 只）" % len(pairs))
        print("差异扫描（逐 file 局部核对，关联键=file，容差 abs(a-b)>%g）:" % args.tol)
        for period in both:
            el = scan_period(conn, period, args.tol, (not args.dry_run) and period in patch_periods,
                             rows, totals, per_file, pairs)
            print("    %-9s 小计 %.1fs" % (period, el))

        # ---- 包内 file（= 有差异行的 file）与 meta（name/code/type 取**新库** meta）----
        patch_period_rows = {p: totals[p]["changed"] + totals[p]["new_only"]
                             for p in patch_periods}
        patch_changed = sum(patch_period_rows.values())
        patch_files = sorted(f for f, rec in per_file.items()
                             if any(rec.get(p) for p in patch_periods))
        if patch_changed and not args.dry_run:
            meta_src = {r[0]: r for r in conn.execute(
                "SELECT file,code,name,type FROM main.meta")}
            miss = [f for f in patch_files if f not in meta_src]
            if miss:
                print("[warn] 有 %d 个 file 不在新库 meta（bkt_meta 将缺失）: %s"
                      % (len(miss), ", ".join(miss[:10])))
            rows["meta"] = [(f, meta_src[f][1], meta_src[f][2], meta_src[f][3])
                            for f in patch_files if f in meta_src]
    finally:
        conn.close()

    # ---- 无差异：不产空包（判据是**差异行数**，不是"内存里有没有攒行"：--dry-run 也走这里）----
    if patch_changed == 0:
        base_only_total = sum(totals[p]["base_only"] for p in both)
        print("-" * 78)
        print("无差异：新库与基线库在 %s 上逐行一致（容差 %g），无需产出补丁包。"
              % (" / ".join(patch_periods), args.tol))
        for p in both:
            print("  %-9s 改写=%d 新增=%d 基线独有=%d"
                  % (p, totals[p]["changed"], totals[p]["new_only"], totals[p]["base_only"]))
        if base_only_total:
            print("  （基线独有 %d 行**仅统计、未删除**；是否清理设备侧对应行需另行决策）"
                  % base_only_total)
        ok = integrity_recheck(args.new_db, args.base_db, before_new, before_base,
                               args.verify_hashes)
        print("总耗时 %.1fs" % (time.time() - t_all))
        return 0 if ok else 1

    if patch_changed > BIG_PATCH_WARN_ROWS:
        print("[warn] 差异行数 %d 已达「全量级」，请确认基线是否过期（例如基线其实是很久以前的库）"
              % patch_changed)

    # ---- 包内最新日期 → updated_at（确定性时间戳，见 docstring；--dry-run 不落盘故不统计）----
    max_date = updated_at = None
    if not args.dry_run:
        max_date = max(r[1] for p in patch_periods for r in rows[p])
        updated_at = L.date_int_to_epoch_utc(max_date)

    seq = args.seq if args.seq is not None else next_seq(out_dir)
    out_path = os.path.join(out_dir, "%s%d.db" % (PATCH_PREFIX, seq))
    report_path = os.path.join(out_dir, "%s%d_files.txt" % (PATCH_PREFIX, seq))
    size = sha = None

    if args.dry_run:
        print("-" * 78)
        print("--dry-run：不落盘（本应写入 %s）" % out_path)
    else:
        os.makedirs(out_dir, exist_ok=True)
        tmp_path = out_path + ".tmp"
        t0 = time.time()
        # 表结构 / 字段名 / 写入方式与分片**同一份实现**（bkt_meta + bkt_daily/weekly/monthly）
        L.build_bucket_file(tmp_path, rows, updated_at)
        try:
            check_patch_db(tmp_path, rows, patch_periods, len(rows["meta"]))
        except Exception:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
            raise
        existed = os.path.exists(out_path)
        os.replace(tmp_path, out_path)
        size = os.path.getsize(out_path)
        sha = shield_sha256(out_path)
        print("-" * 78)
        print("%s: %s  %d B (%.3f MB)  sha256=%s…  耗时 %.1fs"
              % ("已更新" if existed else "已生成", out_path, size, size / 1048576.0,
                 sha[:16], time.time() - t0))

    # ---- 受影响 file 清单（可复查）----
    columns = patch_periods + report_periods + ["base_only"]
    header = [
        "Kline 复权差分重灌包 · 受影响 file 清单",
        "生成时间: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "新库  : %s  %s" % (args.new_db, state_desc(before_new)),
        "基线库: %s  %s" % (args.base_db, state_desc(before_base)),
        "容差  : abs(a-b) > %g（字段 %s 任一变化即计入）" % (args.tol, "/".join(FIELDS)),
        "周期差异行数: %s" % "  ".join(
            "%s: 改写=%d 新增=%d 基线独有=%d"
            % (p, totals[p]["changed"], totals[p]["new_only"], totals[p]["base_only"])
            for p in both),
        "受影响 file 数: %d（其中仅新库 %d / 仅基线 %d）"
        % (len(per_file), len(new_only_files), len(base_only_files)),
        "包文件: %s" % (out_path if not args.dry_run else "（--dry-run 未落盘）"),
        "说明: base_only = 基线有、新库没有的 (file,date)，**本次不生成删除指令**",
    ]
    if max_date is not None:
        header.insert(5, "包内最新日期: %s   updated_at(UTC epoch)=%d" % (max_date, updated_at))
    if size is not None:      # manifest.patches 需要 file/id/bytes/rows/sha256，这里一并留下
        header.insert(6, "包 sha256: %s   bytes=%d   rows=%s" % (
            sha, size, " ".join("%s=%d" % (p, patch_period_rows[p]) for p in patch_periods)))
    if args.dry_run:
        os.makedirs(out_dir, exist_ok=True)
    write_patch_report(report_path, header, columns, per_file)
    print("受影响 file 清单: %s（%d 个 file）" % (report_path, len(per_file)))

    # ---- 差异摘要 ----
    print("-" * 78)
    print("差异摘要（关联键=file）")
    for p in both:
        print("  %-9s 改写=%-8d 新增=%-8d 基线独有=%-8d%s"
              % (p, totals[p]["changed"], totals[p]["new_only"], totals[p]["base_only"],
                 "   [仅统计，契约无 bkt_%s，不入包]" % p if p in report_periods else ""))
    rows_desc = "  ".join("%s=%d" % (p, patch_period_rows[p]) for p in patch_periods)
    print("  包内 file 数: %d（bkt_meta）  受影响 file 数: %d（含仅基线独有的 file）"
          % (len(patch_files), len(per_file)))
    print("  包内行数: %s   总计 %d 行" % (rows_desc, patch_changed))
    if size is not None:
        print("  包体积: %d B (%.2f KB / %.3f MB)" % (size, size / 1024.0, size / 1048576.0))
    print("  耗时: 扫描+出包 %.1fs" % (time.time() - t_all))

    # ---- 两库完整性复核（硬约束）----
    ok = integrity_recheck(args.new_db, args.base_db, before_new, before_base,
                           args.verify_hashes)
    print("总耗时 %.1fs" % (time.time() - t_all))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())