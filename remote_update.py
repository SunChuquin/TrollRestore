#!/usr/bin/env python3
"""Kline 一键远程更新脚本（🥈 方案）。

原理：
    - iPad 上的 Kline（TrollStore 版）内嵌轻量 HTTP 服务器（KlineHTTPServer.swift，监听 0.0.0.0:5051）
    - 本脚本通过 USB 端口转发（usbmux forward）把 iPad 的 5051 端口映射到电脑 127.0.0.1:5051
    - 电脑 POST http://127.0.0.1:5051/install {"url": "<IPA下载URL>"} 给 Kline
    - Kline 收到后调用 UIApplication.shared.open("apple-magnifier://install?url=<URL>") 拉起 TrollStore
    - 用户在 iPad 上依次点「打开」确认 → TrollStore 下载安装

前置条件：
    - iPad 通过 USB 连接（pymobiledevice3 usbmux list 能看到）
    - iPad 上 Kline 处于**前台运行**（HTTP 服务器前台监听，切后台会被系统冻结）
    - Kline 已装含 KlineHTTPServer 的版本

用法：
    # 安装指定 URL 的 IPA（Gitee/GitHub 均可，需公网可下载）
    python remote_update.py --url "https://gitee.com/user/repo/releases/download/tag/Kline.ipa"

    # 安装 GitHub Release 的 IPA（临时测试用，需梯子）
    python remote_update.py --url "https://github.com/SunChuquin/Kline/releases/download/test/Kline.ipa"

    # 只探测 Kline 是否在线（健康检查）
    python remote_update.py --check

    # 安装本地 IPA（把本地文件先推到 iPad Downloads，再走本地安装）
    python remote_update.py --local "Kline.ipa"

退出码：0=成功触发；1=前置检查失败；2=POST 失败
"""
import argparse
import json
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PM_EXE = Path(r"c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe")
LOCAL_PORT = 5051
DEVICE_PORT = 5051


def run_pm(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run([str(PM_EXE), *args], capture_output=True, text=True)


def check_device() -> bool:
    """确认 iPad 通过 USB 连接"""
    r = run_pm("usbmux", "list")
    if r.returncode != 0 or "iPad" not in r.stdout:
        print("❌ 未检测到 USB 连接的 iPad。请插好 USB 并解锁设备。")
        return False
    return True


def start_forward() -> subprocess.Popen:
    """usbmux forward：把 iPad 的 5051 端口转发到电脑 127.0.0.1:5051"""
    proc = subprocess.Popen(
        [str(PM_EXE), "usbmux", "forward", str(LOCAL_PORT), str(DEVICE_PORT)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(2)
    return proc


def http_get(path: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{LOCAL_PORT}{path}", timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def http_post(path: str, payload: dict, timeout: float = 5.0) -> tuple[int, str]:
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"http://127.0.0.1:{LOCAL_PORT}{path}",
        data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, str(e)


def wait_for_kline_online(timeout: float = 90.0) -> bool:
    """轮询等待 Kline HTTP 服务器在线（即 Kline 前台运行）。

    Kline 切后台后监听 socket 会被系统冻结，此时健康检查失败。
    脚本自动提示用户打开 Kline 并重试，不再需要人工在电脑端反复确认。
    """
    print("   ⏳ 检测到 Kline 不在前台（HTTP 服务器未响应）...")
    deadline = time.time() + timeout
    while time.time() < deadline:
        status, _ = http_get("/")
        if status == 200:
            return True
        remaining = int(deadline - time.time())
        print(f"   ⏳ 请在 iPad 上打开 Kline 并保持前台（剩余 {remaining}s，每 3s 自动重试）...")
        time.sleep(3)
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description="Kline 一键远程更新（🥈 方案，USB 直连）")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--url", help="IPA 下载 URL（http/https，TrollStore 会下载它）")
    group.add_argument("--local", help="本地安装：Downloads 下的 IPA 文件名（如 Kline.ipa），走本地 HTTP 服务安装")
    group.add_argument("--check", action="store_true", help="只做健康检查，探测 Kline HTTP 服务器是否在线")
    args = parser.parse_args()

    print("== Kline 远程更新（🥈）==")

    if not check_device():
        return 1

    print(f"启动 USB 端口转发: 127.0.0.1:{LOCAL_PORT} -> iPad:{DEVICE_PORT} ...")
    fwd = start_forward()
    try:
        # 健康检查：Kline 是否前台运行（不在前台自动等待重试）
        status, body = http_get("/")
        if status != 200:
            if not wait_for_kline_online():
                print("❌ 等待超时：Kline 仍不在前台。请打开 Kline 后重新运行。")
                return 2
            status, body = http_get("/")
        print(f"✅ Kline 在线: {body}")

        if args.check:
            print("健康检查通过。")
            return 0

        if args.url:
            payload = {"url": args.url}
            status, body = http_post("/install", payload)
            if status != 200:
                print(f"❌ 触发安装失败（{status}）：{body}")
                return 2
            print(f"✅ 已发送安装指令: {args.url}")

        if args.local:
            payload = {"file": args.local}
            status, body = http_post("/install-local", payload)
            if status != 200:
                print(f"❌ 触发本地安装失败（{status}）：{body}")
                return 2
            print(f"✅ 已发送本地安装指令: Downloads/{args.local}")

        print()
        print("接下来请在 iPad 上操作：")
        print("  1. 如弹出「在 TrollStore 中打开？」→ 点「打开」")
        print("  2. TrollStore 下载完成后弹 Install 确认 → 点「Install」")
        return 0
    finally:
        fwd.terminate()


if __name__ == "__main__":
    sys.exit(main())
