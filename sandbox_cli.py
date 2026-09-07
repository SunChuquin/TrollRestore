#!/usr/bin/env python3
"""Kline-TS 沙盒直连 CLI（经 usbmux forward + KlineHTTP /sandbox 端点）。

电脑像访问本地目录一样读写 Kline-TS（TrollStore 版）的 Documents 沙盒，
无需公共目录中转。需要 Kline 前台运行。

用法：
    python sandbox_cli.py ls [path]            # 列出沙盒目录（默认根）
    python sandbox_cli.py get <remote> <local> # 拉取沙盒文件到本地
    python sandbox_cli.py put <local> <remote> # 上传本地文件到沙盒（流式）
    python sandbox_cli.py rm <remote>          # 删除沙盒文件
    python sandbox_cli.py cat <remote>         # 打印沙盒文件内容（小文件）
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

PM = Path(r"c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe")
PORT = 5051
BASE = f"http://127.0.0.1:{PORT}"


def enc(path: str) -> str:
    """URL 百分号编码沙盒相对路径（保留 / 分隔符，支持中文）"""
    return quote(path, safe="/")


def kill_port(port: int) -> None:
    """杀掉占用指定本地端口的进程（避免 OSError 10048）"""
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    except Exception:
        pass


def forward() -> subprocess.Popen:
    kill_port(PORT)
    time.sleep(1)
    fwd = subprocess.Popen(
        [str(PM), "usbmux", "forward", str(PORT), str(PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return fwd


def check_online(retries: int = 3) -> bool:
    for _ in range(retries):
        try:
            with urllib.request.urlopen(f"{BASE}/", timeout=4) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(2)
    return False


def fmt_size(n):
    return f"{n/1024/1024:.1f}MB" if n > 1048576 else f"{n/1024:.1f}KB"


def cmd_ls(path: str) -> int:
    url = f"{BASE}/sandbox/{enc(path)}" if path else f"{BASE}/sandbox/"
    with urllib.request.urlopen(url, timeout=10) as r:
        items = json.loads(r.read())
    for it in items:
        kind = "📁" if it["dir"] else "  "
        size = "" if it["dir"] else f"  {fmt_size(it['size'])}"
        print(f"{kind} {it['name']}{size}")
    print(f"共 {len(items)} 项")
    return 0


def cmd_get(remote: str, local: str) -> int:
    url = f"{BASE}/sandbox/{enc(remote)}"
    with urllib.request.urlopen(url, timeout=60) as r:
        data = r.read()
    Path(local).write_bytes(data)
    print(f"✅ 已保存 {local} ({len(data):,} bytes)")
    return 0


def cmd_put(local: str, remote: str) -> int:
    import requests
    size = Path(local).stat().st_size
    url = f"{BASE}/sandbox/{enc(remote)}"
    print(f"▶ 上传 {local} ({size:,} bytes) -> 沙盒/{remote}")
    with open(local, "rb") as f:
        r = requests.put(url, data=f, headers={"Content-Length": str(size)}, timeout=900)
    print(f"   HTTP {r.status_code}: {r.text[:200]}")
    return 0 if r.status_code == 200 else 1


def cmd_rm(remote: str) -> int:
    import requests
    r = requests.delete(f"{BASE}/sandbox/{enc(remote)}", timeout=15)
    print(f"   HTTP {r.status_code}: {r.text[:200]}")
    return 0 if r.status_code == 200 else 1


def cmd_cat(remote: str) -> int:
    url = f"{BASE}/sandbox/{enc(remote)}"
    with urllib.request.urlopen(url, timeout=15) as r:
        print(r.read().decode("utf-8", errors="replace"))
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="Kline-TS 沙盒直连 CLI")
    ap.add_argument("cmd", choices=["ls", "get", "put", "rm", "cat"])
    ap.add_argument("args", nargs="*")
    a = ap.parse_args()

    fwd = forward()
    try:
        if not check_online():
            print("❌ Kline-TS 不在前台（HTTP 服务器未响应）。请打开 Kline 后重试。")
            return 1
        if a.cmd == "ls":
            return cmd_ls(a.args[0] if a.args else "")
        if a.cmd == "get":
            return cmd_get(a.args[0], a.args[1])
        if a.cmd == "put":
            return cmd_put(a.args[0], a.args[1])
        if a.cmd == "rm":
            return cmd_rm(a.args[0])
        if a.cmd == "cat":
            return cmd_cat(a.args[0])
        return 1
    finally:
        fwd.terminate()


if __name__ == "__main__":
    sys.exit(main())
