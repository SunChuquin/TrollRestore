#!/usr/bin/env python3
"""迁移 Xcode 旧版 Kline 的完整行情库（tdx.db）到 TrollStore 版可读位置。

背景：TrollStore 版（System 态）因 data vault 容器隔离，读不了 Xcode 版（User 态）
容器的 tdx.db（no-sandbox 也绕不过）。故经电脑中转：
  1. house_arrest 拉取 Xcode 版（bundle id: com.sunck.Kline.4G3V8W86TN）Documents/tdx.db
     → 本地（~1.35GB，约 1-2 分钟，13MB/s）
  2. usbmux forward 5051 → Kline 内嵌 HTTP 服务器（需前台运行）
  3. POST /upload?name=tdx.db 流式上传到公共 Downloads（Kline no-sandbox 可写，USB 高速）
  4. 用户在 Kline 点「导入 tdx.db」拷贝进容器（或只读挂载公共区）

用法：
    python migrate_tdxdb.py                 # 拉取 Xcode 版并上传
    python migrate_tdxdb.py --local db.tdx  # 用本地已有文件上传（跳过拉取）
"""
import argparse
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PM = Path(r"c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe")
BUNDLE_ID = "com.sunck.Kline.4G3V8W86TN"  # Xcode 旧版（Team ID 后缀）
LOCAL_PORT = 5051
LOCAL_DB = Path(r"c:\Users\sunck\home\projects\ios\artifacts\xcode_tdx.db")


def pull_xcode_db(local: Path) -> bool:
    print(f"▶ 拉取 Xcode 旧版行情库（house_arrest，~1.35GB）...")
    r = subprocess.run(
        [str(PM), "apps", "pull", BUNDLE_ID, "Documents/tdx.db", str(local)],
        capture_output=True, text=True,
    )
    if r.returncode != 0 or not local.exists():
        print(f"❌ 拉取失败: {(r.stderr or r.stdout)[-500:]}")
        return False
    print(f"✅ 已拉取: {local} ({local.stat().st_size:,} bytes)")
    return True


def upload_stream(local: Path) -> bool:
    print(f"▶ 启动 USB 转发 {LOCAL_PORT}...")
    fwd = subprocess.Popen(
        [str(PM), "usbmux", "forward", str(LOCAL_PORT), str(LOCAL_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    try:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{LOCAL_PORT}/", timeout=5) as r:
                print(f"✅ Kline 在线: {r.read().decode()}")
        except Exception as e:
            print(f"❌ Kline 不在前台（{e}）。请在 iPad 上打开 Kline（TrollStore 版）后重试。")
            return False

        import requests
        size = local.stat().st_size
        print(f"▶ 流式上传 {size:,} bytes -> Downloads/tdx.db（约 2-4 分钟）...")
        with open(local, "rb") as f:
            r = requests.post(
                f"http://127.0.0.1:{LOCAL_PORT}/upload?name=tdx.db",
                data=f,
                headers={"Content-Length": str(size)},
                timeout=900,
            )
        print(f"   HTTP {r.status_code}: {r.text[:200]}")
        return r.status_code == 200
    finally:
        fwd.terminate()


def main() -> int:
    ap = argparse.ArgumentParser(description="迁移 Xcode 旧版行情库到 TrollStore 版可读位置")
    ap.add_argument("--local", default="", help="本地已有 tdx.db 路径（跳过拉取）")
    ap.add_argument("--force", action="store_true", help="强制重新拉取（忽略本地已有文件）")
    args = ap.parse_args()

    if args.local:
        src = Path(args.local)
    elif LOCAL_DB.exists() and not args.force:
        print(f"ℹ 本地已有 {LOCAL_DB}（{LOCAL_DB.stat().st_size:,} bytes），直接上传。加 --force 重新拉取。")
        src = LOCAL_DB
    else:
        src = LOCAL_DB
        if not pull_xcode_db(src):
            return 1

    if not src.exists() or src.stat().st_size == 0:
        print(f"❌ 源文件无效: {src}")
        return 1

    print(f"📦 待上传: {src} ({src.stat().st_size:,} bytes)")
    if not upload_stream(src):
        return 1

    print()
    print("✅ 上传完成（公共 Downloads/tdx.db）。接下来：")
    print("   1. iPad 上打开 Kline → 个人中心")
    print("   2. 点「导入 tdx.db（从 Downloads）」→ 提示成功后完全退出重开 Kline")
    return 0


if __name__ == "__main__":
    sys.exit(main())
