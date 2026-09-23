#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kline 补丁流水线编排器（PC 分片并行出包 ⊕ 设备会话式单事务落库）。

目标（spec `.trae/specs/pipeline-shard-patch-sync/` Task 8）
---------------------------------------------------------
把原来**严格串行**的四步
`PC 出整包 25.4s → 建 USB 4.1s → PUT 5.9s → 设备 UPSERT 21.5s`（端到端 57.8s）
改成**重叠**执行：

    ┌─ PC 分片并行出包（--shards N --workers W）──────────────────────────┐
    │   片1 就绪 → PUT → APPLY  ┐                                          │
    │   片2 就绪 → PUT → APPLY  ├─ 设备落库与 PC 出包**完全重叠**           │
    │   ...                     ┘                                          │
    └──────────────────────────────────────────────────────────────────────┘
    全部片 apply 成功 → **一次** commit → 清增量库 + reload（各一次）

关键点
------
1. **一次** `sandbox_cli.forward()` 并**全程保持**（不每片重启）；且与 PC 首批出包
   **并发启动**：forward 在后台线程里建，主线程同时把出包进程拉起来。
2. PC 侧调 `src/txt_patch_builder.py` 分片并行出包。该脚本的 `--shards/--workers`
   **可能尚未就绪**（另一 agent 在收尾），故本编排器**运行时探测**其 `--help`：
     · 支持 `--shards` → 用 `--shards N --workers W`（单进程内部并行）；
     · 仅支持 `--only-shard` → 循环调用 `--only-shard i`；
     · 都不支持 → 退化为**单包骨架**（跑通时序，等待 `--shards` 就绪后自动切换）。
3. 分片产出后**立即 PUT + 立即 apply**（`POST /sync/patch-session/apply`），与 PC 后续分片重叠。
4. 全部片 apply 成功后**一次** `commit`；随后 `DELETE /sandbox/tdx_live.db`（失败不致命）
   + `POST /sync/reload`（各**一次**）。
5. 任一步失败 → `POST /sync/patch-session/rollback`，报「已成功 k/N 片」，**不**清增量库。
6. 逐份打印「出包 / PUT / APPLY」耗时与 HTTP 状态；末尾打印总耗时与分段汇总。

设备侧硬约束（已在 Swift 端实现，本脚本配合）
--------------------------------------------
· 会话内**不能** `DETACH`（`SQLITE_LOCKED`）→ 设备端每片用唯一别名 `bkt<seq>` 保持挂载；
  `SQLITE_MAX_ATTACHED=10` ⇒ **N 必须 ≤ 6**（本脚本校验并报错）。
· 看门狗 120s 无进展自动回滚：PC 单片出包 + 设备单片写入都远小于 120s，正常不会触发。

用法
----
    python TrollRestore/sync_pipeline.py --shards 4 --workers 4 \\
        --old-txt-dir C:\\Users\\sunck\\home\\tdx_data_old \\
        --new-txt-dir C:\\Users\\sunck\\home\\tdx_data \\
        --base-db C:\\Users\\sunck\\home\\tdx_baseline.db
"""
import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
KLINE_REPO = r"c:\Users\sunck\home\projects\ios\Kline"
BUILDER = os.path.join(KLINE_REPO, "src", "txt_patch_builder.py")
DEFAULT_OUT = os.path.join(KLINE_REPO, "build_logs", "patches")
DEFAULT_BASE_DB = r"C:\Users\sunck\home\tdx_baseline.db"
DEFAULT_NEW_TXT = r"C:\Users\sunck\home\tdx_data"

MAX_SHARDS = 6          # SQLITE_MAX_ATTACHED=10 → 留余量，N≤6（见 spec / Swift 注释）
STABLE_POLL = 0.3       # 分片发现轮询间隔（秒）
DRAIN_TIMEOUT = 30      # 出包结束后排空剩余分片的宽限（秒）

# 本机回环必须绕过系统代理：HTTP_PROXY 已设、NO_PROXY 为空 → 否则 127.0.0.1:5051 会 503 假象
# （build_and_deploy.py:107 已注明）。安装全局 opener 后，本进程所有 urlopen（含
# push_live_usb.http_put / push_bucket_usb.http_request / sandbox_cli.check_online）都不过代理。
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))

import sandbox_cli                    # noqa: E402
import push_live_usb as P             # noqa: E402  BASE / http_put
from push_bucket_usb import http_request   # noqa: E402  非 2xx 不抛异常，便于打印原因

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass


def log(msg):
    print("[%s] %s" % (time.strftime("%H:%M:%S"), msg), flush=True)


# ---------------------------------------------------------------------------
# 出包进程（后台线程，与 forward / 消费重叠）
# ---------------------------------------------------------------------------

class BuilderRunner(threading.Thread):
    """在后台线程里顺序执行 1..N 条出包命令；支持外部 abort（terminate 当前进程）。"""

    def __init__(self, commands):
        super().__init__(daemon=True)
        self.commands = commands
        self.returncode = None
        self._proc = None
        self._abort = False

    def run(self):
        rc = 0
        for cmd in self.commands:
            if self._abort:
                rc = -1
                break
            log("▶ PC 出包: %s" % " ".join(cmd[1:]))
            self._proc = subprocess.Popen(cmd)
            rc = self._proc.wait()
            self._proc = None
            if rc != 0:
                log("❌ 出包进程退出码 %d" % rc)
                break
        self.returncode = rc

    def abort(self):
        self._abort = True
        p = self._proc
        if p is not None and p.poll() is None:
            try:
                p.terminate()
            except Exception:
                pass


def probe_builder(builder):
    """探测 txt_patch_builder 是否已实现 --shards / --only-shard（**不改它**）。"""
    try:
        r = subprocess.run([sys.executable, builder, "--help"],
                           capture_output=True, text=True, timeout=60)
        text = (r.stdout or "") + (r.stderr or "")
    except Exception as exc:
        log("⚠️ 探测 %s --help 失败: %s" % (builder, exc))
        text = ""
    return {"shards": "--shards" in text, "only_shard": "--only-shard" in text}


# ---------------------------------------------------------------------------
# 分片发现 / PUT / APPLY
# ---------------------------------------------------------------------------

def discover(out_dir, baseline, t_start):
    """输出目录里「本轮新出现」的 patch_*.db（.tmp/清单 txt 不匹配 glob）。"""
    found = []
    if not os.path.isdir(out_dir):
        return found
    for p in Path(out_dir).glob("patch_*.db"):
        try:
            mtime = p.stat().st_mtime
        except OSError:
            continue
        # 快照里没有的 = 新文件；被本轮覆盖的（mtime 新）也算
        if p.name in baseline and mtime < t_start - 2:
            continue
        found.append(p)
    return sorted(found)


def put_and_apply(name, path):
    """PUT 到沙盒 live/（流式）→ POST /sync/patch-session/apply。

    返回 (ok, put_status, put_el, apply_status, apply_el, message)。
    """
    t0 = time.time()
    try:
        put_status, put_body = P.http_put("live/" + name, Path(path))
    except Exception as exc:
        return False, None, time.time() - t0, None, 0.0, "PUT 异常: %s" % exc
    put_el = time.time() - t0
    if put_status != 200:
        return False, put_status, put_el, None, 0.0, "PUT HTTP %s %s" % (put_status, put_body[:120])

    t0 = time.time()
    q = urllib.parse.quote(name, safe="")
    apply_status, apply_body = http_request(
        "POST", "/sync/patch-session/apply?name=" + q, timeout=600)
    apply_el = time.time() - t0
    ok = 200 <= apply_status < 300
    try:
        js = json.loads(apply_body)
        if not js.get("ok", True):
            ok = False
    except Exception:
        pass
    return ok, put_status, put_el, apply_status, apply_el, apply_body[:160]


def consume_once(out_dir, baseline, t_start, sizes, seen, applied, t_build_start):
    """发现→稳定→PUT+APPLY 一轮。返回 (consumed, unstable, error)。"""
    consumed = unstable = 0
    for p in discover(out_dir, baseline, t_start):
        if p.name in seen:
            continue
        try:
            sz = p.stat().st_size
        except OSError:
            continue
        if sizes.get(p.name) != sz or sz == 0:      # 等下一次轮询确认写盘完成
            sizes[p.name] = sz
            unstable += 1
            continue
        t_ready = time.time()
        log("▶ 分片就绪 %s  (%d B, 出包累计 %.1fs)"
            % (p.name, sz, t_ready - t_build_start))
        ok, put_status, put_el, apply_status, apply_el, msg = put_and_apply(p.name, str(p))
        log("  PUT   %-22s HTTP %s  %.1fs" % (p.name, put_status, put_el))
        log("  APPLY %-22s HTTP %s  %.1fs  %s" % (p.name, apply_status, apply_el, msg))
        if not ok:
            return consumed, unstable, "分片 %s 落库失败（PUT=%s APPLY=%s）" % (
                p.name, put_status, apply_status)
        seen.add(p.name)
        applied.append({"name": p.name, "t_ready": t_ready - t_build_start,
                        "put": put_el, "apply": apply_el,
                        "put_status": put_status, "apply_status": apply_status})
        consumed += 1
    return consumed, unstable, None


# ---------------------------------------------------------------------------
# 会话端点
# ---------------------------------------------------------------------------

def session_post(path, timeout=120):
    """POST 一个会话端点，返回 (status, dict)。"""
    status, body = http_request("POST", path, timeout=timeout)
    try:
        js = json.loads(body)
    except Exception:
        js = {"ok": 200 <= status < 300, "raw": body}
    return status, js


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Kline 补丁流水线编排器（PC 分片并行出包 ⊕ 设备会话式单事务落库）")
    ap.add_argument("--shards", type=int, default=4,
                    help="分片数 N（默认 4，**必须 ≤ %d**：SQLITE_MAX_ATTACHED=10）" % MAX_SHARDS)
    ap.add_argument("--workers", type=int, default=4, help="出包并行度 W（默认 4）")
    ap.add_argument("--old-txt-dir", dest="old_txt_dir", required=True,
                    help="旧 txt 目录（txt_patch_builder 的分类/append 参照）")
    ap.add_argument("--new-txt-dir", dest="new_txt_dir", default=DEFAULT_NEW_TXT,
                    help="新 txt 目录（默认 %s）" % DEFAULT_NEW_TXT)
    ap.add_argument("--base-db", dest="base_db", default=DEFAULT_BASE_DB,
                    help="基线库（只读打开，默认 %s）" % DEFAULT_BASE_DB)
    ap.add_argument("--seq", type=int, default=None,
                    help="补丁序号（命名 patch_<seq>.db；默认由出包器自增）")
    ap.add_argument("--out", default=DEFAULT_OUT,
                    help="出包输出目录（分片发现用，默认 %s）" % DEFAULT_OUT)
    args = ap.parse_args(argv)

    # ---- 参数校验（硬约束：N ≤ 6）----
    if not (1 <= args.shards <= MAX_SHARDS):
        raise SystemExit("❌ --shards 必须为 1..%d（SQLITE_MAX_ATTACHED=10，留余量），当前 %d"
                         % (MAX_SHARDS, args.shards))
    if args.workers < 1:
        raise SystemExit("❌ --workers 必须 ≥ 1，当前 %d" % args.workers)
    if not os.path.isdir(args.old_txt_dir):
        raise SystemExit("❌ 旧 txt 目录不存在: %s" % args.old_txt_dir)
    if not os.path.isdir(args.new_txt_dir):
        raise SystemExit("❌ 新 txt 目录不存在: %s" % args.new_txt_dir)
    if not os.path.exists(args.base_db):
        raise SystemExit("❌ 基线库不存在: %s" % args.base_db)
    if not os.path.exists(BUILDER):
        raise SystemExit("❌ 出包器不存在: %s" % BUILDER)

    caps = probe_builder(BUILDER)
    if caps["shards"]:
        mode = "shards"
    elif caps["only_shard"]:
        mode = "only_shard"
    else:
        mode = "single"

    base_cmd = [sys.executable, BUILDER,
                "--old-txt-dir", args.old_txt_dir,
                "--new-txt-dir", args.new_txt_dir,
                "--base-db", args.base_db,
                "--out", args.out]
    if args.seq is not None:
        base_cmd += ["--seq", str(args.seq)]

    if mode == "shards":
        base_cmd += ["--shards", str(args.shards), "--workers", str(args.workers)]
        commands = [base_cmd]
    elif mode == "only_shard":
        commands = [base_cmd + ["--only-shard", str(i)] for i in range(args.shards)]
    else:
        commands = [base_cmd]
    n_expected = 1 if mode == "single" else args.shards

    out_dir = os.path.abspath(args.out)
    baseline = ({p.name for p in Path(out_dir).glob("patch_*.db")}
                if os.path.isdir(out_dir) else set())

    log("=" * 78)
    log("Kline 补丁流水线（分片并行出包 ⊕ 会话式单事务落库）")
    log("=" * 78)
    log("出包模式 : %s" % {
        "shards": "--shards %d --workers %d（并行出包）" % (args.shards, args.workers),
        "only_shard": "循环 --only-shard 0..%d（%d 个子进程）" % (args.shards - 1, args.shards),
        "single": "单包（txt_patch_builder 尚未支持 --shards/--only-shard）",
    }[mode])
    if mode == "single":
        log("⚠️ 注意: src/txt_patch_builder.py **尚未支持 --shards/--only-shard** → 本次按单包跑通")
        log("        流水线骨架；待 `--shards` 就绪后本脚本会自动切换为分片并行。")
    log("输出目录 : %s" % out_dir)
    log("基线库   : %s" % args.base_db)
    log("-" * 78)

    t_all = time.time()

    # ---- 1) 后台线程建 forward（与 PC 出包**并发启动**）----
    fwd_box = {}

    def _start_forward():
        try:
            fwd_box["p"] = sandbox_cli.forward()
        except Exception as exc:      # noqa: BLE001
            fwd_box["err"] = exc

    fwd_thread = threading.Thread(target=_start_forward, daemon=True)
    fwd_thread.start()

    # ---- 2) 主线程立即开始出包 ----
    runner = BuilderRunner(commands)
    t_build_start = time.time()
    runner.start()

    # ---- 3) 等 forward 就绪（此前 PC 已在出包 → 重叠）----
    fwd_thread.join()
    fwd = fwd_box.get("p")
    if fwd is None:
        log("❌ 建立端口转发失败: %s" % fwd_box.get("err"))
        runner.abort()
        return 1
    if not sandbox_cli.check_online():
        log("❌ 设备离线 / 未解锁（本地 HTTP 服务器未响应）。请在设备上打开 Kline 后重试。")
        runner.abort()
        fwd.terminate()
        return 1
    log("✅ usbmux forward 已就绪（与 PC 出包并发启动，全程保持一次）")

    sizes, seen, applied = {}, set(), []
    commit_el = None          # commit 耗时（秒），未走到 commit 时为 None
    failed = None
    try:
        # ---- 4) 开会话（一条独立连接 + BEGIN IMMEDIATE）----
        status, js = session_post("/sync/patch-session/begin")
        if not js.get("ok"):
            log("❌ 会话 begin 失败（HTTP %s）: %s" % (status, js.get("message") or js))
            runner.abort()
            return 1
        log("✅ 会话已开启: %s" % (js.get("message", "")))

        # ---- 5) 消费分片：与 PC 出包**完全重叠** ----
        while runner.is_alive():
            consumed, unstable, err = consume_once(
                out_dir, baseline, t_build_start, sizes, seen, applied, t_build_start)
            if err:
                failed = err
                break
            time.sleep(STABLE_POLL)

        # 出包结束 → 排空剩余分片（给最后一片留稳定窗口）
        if failed is None:
            deadline = time.time() + DRAIN_TIMEOUT
            while time.time() < deadline:
                consumed, unstable, err = consume_once(
                    out_dir, baseline, t_build_start, sizes, seen, applied, t_build_start)
                if err:
                    failed = err
                    break
                if consumed == 0 and unstable == 0:
                    break
                time.sleep(STABLE_POLL)

        t_build_done = time.time()

        # ---- 6) 判定：出包失败 / 有分片未成功 → rollback（不清增量库）----
        if failed is None and runner.returncode not in (0, None):
            failed = "出包进程退出码 %d" % runner.returncode
        if failed is None and not applied:
            # 无差异：没有片可提交 → 回滚空会话
            session_post("/sync/patch-session/rollback")
            log("ℹ️ 本次无新分片（新 txt 与基线无差异）→ 已回滚空会话；**未清增量库**")
            _print_summary(t_all, t_build_start, t_build_done, applied, commit_el)
            return 0

        if failed is not None:
            session_post("/sync/patch-session/rollback")
            log("❌ 失败：%s" % failed)
            log("   已成功 %d/%d 片（会话已整体 ROLLBACK，**未清增量库**）"
                % (len(applied), n_expected))
            _print_summary(t_all, t_build_start, t_build_done, applied, commit_el)
            return 1

        # ---- 7) 全部片成功 → 一次 commit ----
        t0 = time.time()
        status, js = session_post("/sync/patch-session/commit", timeout=300)
        commit_el = time.time() - t0
        if not js.get("ok"):
            log("❌ commit 失败（HTTP %s, %.1fs）: %s" % (status, commit_el, js.get("message") or js))
            session_post("/sync/patch-session/rollback")
            log("   已成功 %d/%d 片（commit 失败，**未清增量库**）" % (len(applied), n_expected))
            _print_summary(t_all, t_build_start, t_build_done, applied, commit_el)
            return 1
        log("✅ commit 成功（%.1fs）: %s" % (commit_el, js.get("message", "")))
        log("   dailyRows=%s weeklyRows=%s monthlyRows=%s quarterlyRows=%s yearlyRows=%s "
            "coveredFiles=%s shards=%s latestDate=%s"
            % (js.get("dailyRows"), js.get("weeklyRows"), js.get("monthlyRows"),
               js.get("quarterlyRows"), js.get("yearlyRows"), js.get("coveredFiles"),
               js.get("shards"), js.get("latestDate")))

        # ---- 7b) commit 响应刚到 → 立刻从 **App 自身连接** 读回一只标的（校验 WAL 未回写时 App 也读到新值）----
        try:
            pq = urllib.parse.quote("SH#600519", safe="")
            st, body = http_request("GET", "/sync/probe?file=" + pq, timeout=60)
            log("🔎 App 侧读回探针（commit 响应后立即，HTTP %s, 距 commit %.1fs）: %s"
                % (st, time.time() - t0, body[:300]))
        except Exception as exc:      # noqa: BLE001
            log("⚠️ App 侧读回探针失败：%s" % exc)

        # ---- 8) 清增量库 + reload（各一次；清库失败不致命）----
        t0 = time.time()
        status, body = http_request("DELETE", "/sandbox/tdx_live.db", timeout=30)
        if 200 <= status < 300:
            log("✅ 清增量库 tdx_live.db（HTTP %s, %.1fs）" % (status, time.time() - t0))
        else:
            log("⚠️ 清增量库返回 HTTP %s（可能本就不存在）→ 继续（%.1fs）: %s"
                % (status, time.time() - t0, body[:80]))
        t0 = time.time()
        status, body = http_request("POST", "/sync/reload", timeout=30)
        log("%s reload（HTTP %s, %.1fs）"
            % ("✅" if 200 <= status < 300 else "⚠️", status, time.time() - t0))

        _print_summary(t_all, t_build_start, t_build_done, applied, commit_el)
        return 0
    finally:
        fwd.terminate()


def _print_summary(t_all, t_build_start, t_build_done, applied, commit_el=None):
    log("=" * 78)
    log("分段汇总")
    log("  出包（PC，%d 片）        : %.1fs" % (len(applied), t_build_done - t_build_start))
    for s in applied:
        log("    片 %-22s 就绪@+%5.1fs  PUT %5.1fs(HTTP %s)  APPLY %5.1fs(HTTP %s)"
            % (s["name"], s["t_ready"], s["put"], s["put_status"], s["apply"], s["apply_status"]))
    if applied:
        log("  设备 APPLY 合计          : %.1fs（与出包重叠）"
            % sum(s["apply"] for s in applied))
        log("  设备 PUT   合计          : %.1fs" % sum(s["put"] for s in applied))
    log("  设备 COMMIT              : %s"
        % ("%.1fs" % commit_el if commit_el is not None else "未执行"))
    log("总耗时                     : %.1fs" % (time.time() - t_all))
    log("（串行基线 57.8s；本流水线目标 ~25s）")


if __name__ == "__main__":
    sys.exit(main())
