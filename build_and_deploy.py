#!/usr/bin/env python
# -*- coding: utf-8 -*-
r"""
build_and_deploy.py — Kline 端到端自动构建部署工作流（Windows，供 AI 一次性调用）

流程闭环：
  1. 接收 AI 传入的提交描述信息，git add + commit + push 触发 GitHub Actions
  2. 轮询监控本次推送（按 headSha 匹配）的 Actions run 直到结束
  3. 构建失败 → 拉取失败日志，保存到 build_logs 并摘录 error 行输出，供 AI 分析修复（退出码 1）
  4. 构建成功 → 检测自动部署助手（deploy_gui.py）：离线则自动后台拉起并等在线，
     然后 POST :5052/notify {"run_id"} 通知部署
  5. 助手自动执行：设备就绪门禁（下载 IPA 前 30s 探测 KlineHTTP，仅 Kline 前台时响应）
     → 下载 IPA → USB 沙盒推送 → TrollStore 安装 → opener 自动打开新版并校验版本。
     门禁不通过（iPad 锁屏 / Kline 未前台 / USB 断连）时助手进入"设备无人值守"终态、
     不下载不安装，本脚本据该终态以退出码 6 结束，等待人工解锁并打开 Kline 后重新通知续跑。
     默认等待助手部署终态并回报；--no-wait 可在通知后立即返回

用法：
  python build_and_deploy.py "修复编译错误：xxx"
  python build_and_deploy.py "新增指标COORD字段" --files Kline/KlineHTTPServer.swift
  python build_and_deploy.py "更新界面" --no-wait

输出约定：**成功路径只打印进度日志，不输出 RESULT 块**；仅失败时打印 RESULT JSON（含错误详情），
退出码（AI 依据退出码与 RESULT 块决策）：
  0 = 全链路成功（构建成功且部署完成/已通知）
  1 = 构建失败（编译日志已保存并摘录输出）
  2 = git 提交/推送失败（无可推送变更也归此类）
  3 = 自动部署助手无法自动启动（或启动后未在线，请检查 deploy_gui.py 窗口）
  4 = 等待超时（run 未出现 / 构建超时 / 部署未达终态）
  5 = 助手正忙（部署中，通知会被忽略，稍后重试）
  6 = 设备无人值守（构建已成功且已通知助手，但助手在下载 IPA 前的 30s 门禁窗口内探测不到
      KlineHTTP：iPad 锁屏 / Kline 未在前台 / USB 断连；助手未下载未安装）。等人工解锁
      iPad、打开 Kline 保持前台后，凭 RESULT 中的 run_id 重新 POST :5052/notify 续跑，
      无需重新构建
  7 = GitHub API 轮询连续失败（瞬时 TLS/网络超时：每次失败间隔 5s 自动重试，连试 3 次仍失败）。
      构建尚未结束/结论未知，非代码错误；先用 gh run view <run_id> / gh run watch <run_id>
      确认 run 结论——成功则按部署流程继续（重新运行本脚本或直接 POST :5052/notify）

依赖：Python 3.8+（仅标准库）、git、gh（已登录，repo 权限）。

已知契约（deploy_gui.py 5052）：POST /notify 先回响应、后经 Qt 信号启动部署，
故通知后助手 status 会短暂停留在**上一次的终态**（如"✅ 部署完成：旧build"）。
等待终态时必须先观察到 status 离开通知前快照（新部署真正开始），否则会把旧成功误判成本次完成。
"""

import argparse
import json
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

REPO_DIR = Path(r"c:\Users\sunck\home\projects\ios\Kline")
LOG_DIR = Path(r"c:\Users\sunck\home\projects\ios\build_logs")
ASSISTANT_BASE = "http://127.0.0.1:5052"   # deploy_gui.py 通知监听端口
DEPLOY_GUI = Path(r"c:\Users\sunck\home\projects\ios\TrollRestore\deploy_gui.py")
VENV_PYTHONW = Path(r"c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pythonw.exe")
ASSISTANT_START_TIMEOUT = 30  # 自动启动助手后等待其在线的最长时间（秒）

POLL_INTERVAL = 10        # 构建状态轮询间隔（秒）
RUN_APPEAR_TIMEOUT = 120  # 推送后等待 Actions run 出现的最长时间（秒）
DEPLOY_POLL_INTERVAL = 5  # 部署状态轮询间隔（秒）

# GitHub API 轮询可靠性：瞬时网络故障（TLS handshake timeout 等）自动重试，
# 连试 GH_POLL_RETRY 次仍失败则返回特殊退出码 GH_RETRY_EXIT（7），避免误判为 git 阶段失败（2）
GH_POLL_RETRY = 3           # 轮询失败最大重试次数
GH_POLL_RETRY_DELAY = 5     # 轮询失败后重试间隔（秒）
GH_RETRY_EXIT = 7           # GitHub API 轮询连续失败的特殊退出码

# deploy_gui.py 的终态 status 文案（部署等待循环据此判定）
DEPLOY_OK_KEY = "✅ 部署完成"
# "设备无人值守"为助手下载前 30s 门禁失败的终态（契约键须与 deploy_gui.py DEVICE_IDLE_KEY 一致）
DEVICE_IDLE_KEY = "设备无人值守"
DEPLOY_FAIL_KEYS = ("部署失败", "部署超时", DEVICE_IDLE_KEY)
ASSISTANT_IDLE_OK = ("空闲", DEPLOY_OK_KEY) + DEPLOY_FAIL_KEYS


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def sh(args, cwd=None, check=True, timeout=None):
    """执行命令并返回 CompletedProcess；check=True 时失败抛异常"""
    r = subprocess.run(
        [str(a) for a in args], cwd=str(cwd) if cwd else None,
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        timeout=timeout,
    )
    if check and r.returncode != 0:
        err = (r.stderr or r.stdout or "").strip()[-800:]
        raise RuntimeError(f"命令失败 (code={r.returncode}): {' '.join(str(a) for a in args)}\n{err}")
    return r


def gh(args, cwd, check=True):
    return sh(["gh"] + args, cwd=cwd, check=check)


# 本机回环调用必须绕过系统代理（本机环境变量设了 HTTP_PROXY=127.0.0.1:10808 且 NO_PROXY 为空，
# 否则对 127.0.0.1:5052 的请求会被代理拦截，返回 503 假象）
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def http_json(url, payload=None, timeout=5):
    """GET（payload=None）/ POST JSON，返回 (status_code, dict)；不经过系统代理"""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="GET" if data is None else "POST",
                                 headers={"Content-Type": "application/json"})
    with _OPENER.open(req, timeout=timeout) as r:
        return r.status, json.loads(r.read().decode("utf-8", "replace"))


class GhPollError(RuntimeError):
    """轮询 GitHub API 连续失败（瞬时网络超时等，已按 GH_POLL_RETRY 次重试耗尽），非代码错误"""


def list_runs(cwd, limit=15):
    """gh run list 并解析 JSON；瞬时网络故障按 GH_POLL_RETRY_DELAY 间隔自动重试，
    连试 GH_POLL_RETRY 次仍失败抛 GhPollError（main 映射为退出码 7）"""
    last = None
    for attempt in range(1, GH_POLL_RETRY + 1):
        try:
            out = gh(["run", "list", "--limit", str(limit),
                      "--json", "databaseId,headSha,status,conclusion,displayTitle,event"], cwd)
            return json.loads(out.stdout)
        except Exception as e:
            last = e
            if attempt < GH_POLL_RETRY:
                log(f"⚠ 轮询 GitHub API 失败（第 {attempt}/{GH_POLL_RETRY} 次）：{e}，"
                    f"{GH_POLL_RETRY_DELAY}s 后重试...")
                time.sleep(GH_POLL_RETRY_DELAY)
    raise GhPollError(f"连试 {GH_POLL_RETRY} 次轮询 GitHub API 均失败，最后错误：{last}")


def wait_for_run(sha, branch, cwd, timeout):
    """轮询等待本次推送（headSha==sha）的 run 出现，返回 run_id"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        for r in list_runs(cwd):
            if r.get("headSha") == sha:
                rid = str(r["databaseId"])
                # log(f"✅ 已捕获本次构建 run={rid}")
                return rid
        remaining = int(deadline - time.time())
        # log(f"⏳ 等待 Actions run 出现（按 commit {sha[:7]} 匹配，剩余 {remaining}s）...")
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"{timeout}s 内未发现 commit {sha[:7]} 对应的 Actions run")


def watch_run(rid, cwd, timeout):
    """轮询 run 直到 completed，返回 conclusion（如 success/failure/cancelled）"""
    deadline = time.time() + timeout
    last_state = ""
    while time.time() < deadline:
        runs = [r for r in list_runs(cwd) if str(r["databaseId"]) == rid]
        if not runs:
            raise RuntimeError(f"run={rid} 在 run list 中消失")
        r = runs[0]
        state = f"{r.get('status')}:{r.get('conclusion') or ''}"
        if state != last_state:   # 仅状态变化时输出，避免刷屏
            # log(f"⏳ run={rid} 状态：{r.get('status')} {r.get('conclusion') or ''}")
            last_state = state
        if r.get("status") == "completed":
            log(f"[5/5] 构建成功 run={rid} 状态")
            return r.get("conclusion") or "unknown"
        time.sleep(POLL_INTERVAL)
    raise TimeoutError(f"构建 {timeout}s 超时未结束，可稍后手动查看 run={rid}")


def extract_errors(log_text, max_lines=80):
    """从失败日志摘录 error 行及其后 2 行（xcodebuild 诊断格式：file:line:col: error: ...）"""
    lines = log_text.splitlines()
    hits, i = [], 0
    pat = re.compile(
        r"error:|fatal error|ARCHIVE FAILED|BUILD FAILED|EXPORT FAILED|no scheme"
        r"|Compiling failed|failed to|syntax error|##\[error\]"
        r"|\^~|\|\s*\^",  # \^~ 与 | ^ 为 xcodebuild 插入符行（指向出错列）
        re.I)
    while i < len(lines) and len(hits) < max_lines:
        if pat.search(lines[i]):
            j = i
            while j <= min(i + 2, len(lines) - 1) and len(hits) < max_lines:
                if lines[j] not in hits:
                    hits.append(lines[j])
                j += 1
            i = j
        else:
            i += 1
    return hits


def handle_failure(rid, cwd):
    """拉取并保存失败日志，摘录 error 行输出，返回日志文件路径"""
    r = gh(["run", "view", rid, "--log-failed"], cwd, check=False)
    log_text = r.stdout or r.stderr or "(未获取到日志)"
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"failed-run-{rid}.log"
    log_file.write_text(log_text, encoding="utf-8")
    log(f"❌ 构建失败，完整失败日志已保存：{log_file}")
    print("-" * 72, flush=True)
    hits = extract_errors(log_text)
    if hits:
        print("编译错误摘录（本地路径 = 日志路径去掉 /Users/runner/work/Kline/Kline/ 前缀）：", flush=True)
        for line in hits:
            print(f"  {line}", flush=True)
    else:
        print(log_text[-3000:], flush=True)
    print("-" * 72, flush=True)
    return str(log_file)


def assistant_status():
    try:
        _, data = http_json(f"{ASSISTANT_BASE}/status", timeout=3)
        return str(data.get("status", ""))
    except Exception:
        return None


def start_assistant():
    """自动启动 deploy_gui.py（后台 GUI，无控制台窗口），等待其 :5052 在线。返回 (ok, err)"""
    log("🚀 检测到自动部署助手未运行，正在自动启动 deploy_gui.py ...")
    pythonw = str(VENV_PYTHONW) if VENV_PYTHONW.exists() else "pythonw.exe"
    try:
        subprocess.Popen(
            [pythonw, str(DEPLOY_GUI)], cwd=str(DEPLOY_GUI.parent),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
    except FileNotFoundError:
        return False, f"找不到 pythonw.exe（{pythonw}），无法自动启动助手"
    deadline = time.time() + ASSISTANT_START_TIMEOUT
    while time.time() < deadline:
        if assistant_status() is not None:
            log(f"✅ 自动部署助手已启动并在线（解释器：{pythonw}）")
            return True, ""
        time.sleep(1)
    return False, f"deploy_gui.py 已尝试启动但 {ASSISTANT_START_TIMEOUT}s 内未在线（:5052 无响应），请检查助手窗口"


def notify_assistant(rid):
    """确保助手在线（离线则自动启动），检查空闲后 POST /notify 触发部署。
    返回 (ok, err, baseline)：baseline 为 POST 前的 status 快照（通常是上一次的终态），
    供 wait_deploy_done 区分"旧终态残留"与"本次部署的新终态"。"""
    if assistant_status() is None:
        ok, err = start_assistant()
        if not ok:
            return False, err, None
    st = assistant_status()
    if st and not any(k in st for k in ASSISTANT_IDLE_OK):
        return False, f"助手正忙（status={st}），本次通知会被忽略，请稍后重试", st
    baseline = st  # 通知前快照（可能是"空闲"或上一次的"✅ 部署完成：旧build"）
    http_json(f"{ASSISTANT_BASE}/notify", payload={"run_id": str(rid)}, timeout=5)
    # log(f"✅ 已通知自动部署助手部署 run={rid}（下载 IPA → USB 沙盒推送 → TrollStore 安装 → 自动打开校验）")
    return True, "", baseline


def wait_deploy_done(timeout, baseline=None):
    """轮询助手 status 直到**本次**部署终态，返回 (ok, final_status)。

    防竞态：/notify 先响应、助手后启动，且上一次成功状态会残留（含"✅ 部署完成"）。
    因此必须先观察到 status 离开 baseline 且进入非成功态（新部署真正开始，started=True），
    此后再出现的成功/失败键才算本次终态；在 started 之前读到的旧成功一律忽略。
    """
    deadline = time.time() + timeout
    last = ""
    started = False
    while time.time() < deadline:
        st = assistant_status()
        if st and st != last:
            # log(f"⏳ 助手：{st}")
            last = st
        if st is None or st == baseline:
            time.sleep(DEPLOY_POLL_INTERVAL)
            continue
        # 状态已离开通知前快照：
        # 1) 成功态 → 必为本次成功（旧成功文本 == baseline，已被 != 排除；兼容轮询间隙内部署秒完）
        if DEPLOY_OK_KEY in st:
            return True, st
        # 2) "设备无人值守"终态文本带 run_id，新旧实例不会相同，离开 baseline 即可采信
        #    （与成功态同等待遇，兼容门禁在轮询间隙内快速结束；不要求先观察到进行中文案）
        if DEVICE_IDLE_KEY in st:
            return False, st
        # 3) 其它失败态仅在观察到"进行中"之后才采信（"部署失败"文本不含 run 标识，
        #    可能与 baseline 残留同名，避免旧失败被当本次失败）
        if any(k in st for k in DEPLOY_FAIL_KEYS):
            if started:
                return False, st
        else:
            # 4) 其它（解析/门禁/下载/传输/等待前台…）= 本次部署已真正开始
            started = True
        time.sleep(DEPLOY_POLL_INTERVAL)
    return False, f"等待部署终态超时（{timeout}s，started={started}），助手最后状态：{last or assistant_status()}"


def result_block(**kw):
    print("\n========== RESULT ==========", flush=True)
    print(json.dumps(kw, ensure_ascii=False, indent=2), flush=True)
    print("============================", flush=True)


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Kline 端到端自动构建部署工作流")
    ap.add_argument("message", help="GitHub 提交描述信息")
    ap.add_argument("--files", nargs="*", default=None,
                    help="仅提交指定文件；缺省提交仓库内全部变更（git add -A）")
    ap.add_argument("--repo", default=str(REPO_DIR))
    ap.add_argument("--branch", default="main")
    ap.add_argument("--no-wait", action="store_true", help="通知助手后立即返回，不等待部署终态")
    ap.add_argument("--build-timeout", type=int, default=1200, help="构建监控超时秒数")
    ap.add_argument("--deploy-timeout", type=int, default=600, help="部署终态等待超时秒数")
    args = ap.parse_args()

    repo = Path(args.repo)
    if not (repo / ".git").exists():
        result_block(exit_code=2, stage="preflight", error=f"仓库不存在：{repo}")
        return 2

    common = {"run_id": None, "commit": None, "message": args.message}
    try:
        # ---- 1. 提交 ----
        log(f"[1/5] 提交变更到 {args.branch}")
        add_args = ["git", "add", *(args.files if args.files else ["-A"])]
        sh(add_args, cwd=repo)
        staged = sh(["git", "diff", "--cached", "--name-only"], cwd=repo).stdout.strip()
        if staged:
            sh(["git", "commit", "-m", args.message], cwd=repo)
            # log(f"   已提交 {len(staged.splitlines())} 个文件")
        else:
            log("   无暂存变更，跳过 commit")

        # ---- 2. 推送（含 non-fast-forward 自动 rebase 重试一次）----
        log("[2/5] 推送到 GitHub 触发 Actions ...")
        sh(["git", "fetch", "origin", args.branch], cwd=repo)
        head = sh(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        remote = sh(["git", "rev-parse", f"origin/{args.branch}"], cwd=repo, check=False).stdout.strip()
        if head == remote:
            msg = "无可推送变更（本地与远端一致），不会触发新构建。请先修改代码再调用。"
            log(f"❌ {msg}")
            result_block(exit_code=2, stage="push", error=msg, **common)
            return 2
        r = sh(["git", "push", "origin", args.branch], cwd=repo, check=False)
        if r.returncode != 0:
            log("   推送被拒，尝试 git pull --rebase 后重推 ...")
            sh(["git", "pull", "--rebase", "origin", args.branch], cwd=repo)
            sh(["git", "push", "origin", args.branch], cwd=repo)
        sha = sh(["git", "rev-parse", "HEAD"], cwd=repo).stdout.strip()
        common["commit"] = sha[:7]
        # log(f"✅ 已推送 {sha[:7]}")

        # ---- 3. 捕获本次 run 并监控 ----
        # log("[3/5] 捕获本次 Actions run ...")
        rid = wait_for_run(sha, args.branch, repo, RUN_APPEAR_TIMEOUT)
        common["run_id"] = rid
        # log(f"[4/5] 监控构建 run={rid}（典型耗时 40~85s，排队另计）...")
        conclusion = watch_run(rid, repo, args.build_timeout)

        # ---- 4. 失败 → 输出日志供 AI 修复；成功 → 通知助手 ----
        if conclusion != "success":
            failed_log = handle_failure(rid, repo)
            result_block(exit_code=1, stage="build", conclusion=conclusion,
                         failed_log=failed_log, **common)
            return 1
        # log(f"[5/5] 构建成功")

        # ---- 5. 通知助手（设备就绪门禁由助手在下载 IPA 前执行，30s）----
        # log("[5/5] 通知自动部署助手（其下载 IPA 前会先做 30s 设备就绪门禁）...")
        ok, err, baseline = notify_assistant(rid)
        if not ok:
            code = 3 if "离线" in err else 5
            log(f"❌ {err}")
            result_block(exit_code=code, stage="deploy-notify", error=err, **common)
            return code
        if args.no_wait:
            log("（--no-wait：不等待部署终态）已通知，退出码 0")
            return 0

        ok, final = wait_deploy_done(args.deploy_timeout, baseline=baseline)
        if ok:
            # 成功路径保持简洁：只打一行终态，不输出 RESULT JSON（RESULT 仅失败时打印）
            log(f"🎉 {final}")
            return 0
        # 助手下载前 30s 设备门禁未通过 = 人不在设备前（锁屏/未前台），区别于普通部署超时
        if DEVICE_IDLE_KEY in final:
            log(f"🔒 {final}")
            log("   构建已成功，助手未连接。")
            return 6
        log(f"❌ {final}")
        result_block(exit_code=4, stage="deploy-wait", assistant_status=final, **common)
        return 4

    except GhPollError as e:
        log(f"❌ {e}")
        return 7
    except TimeoutError as e:
        log(f"❌ {e}")
        result_block(exit_code=4, stage="timeout", error=str(e), **common)
        return 4
    except Exception as e:
        log(f"❌ {e}")
        result_block(exit_code=2, stage="git", error=str(e), **common)
        return 2


if __name__ == "__main__":
    sys.exit(main())
