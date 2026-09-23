#!/usr/bin/env python3
"""Kline 增量行情库 —— 局域网 WiFi 直推（通道 B，免数据线）

把本机生成的 `tdx_live.db` 经 Wi-Fi 直接 PUT 到 Kline 内嵌 HTTP 服务
（`http://<设备IP>:5051/sandbox/...`），推完立即通知 App 热刷新。

与 USB 通道（`push_live_usb.py`）的区别：
  - 不需要数据线，但**需要电脑与设备在同一 Wi-Fi**，且 Kline 前台未锁屏；
  - 设备要在「设置 → 本地更新 → 数据同步」里能看到本机局域网地址，填给 `--host`。

用法：
    # 先本机生成，再按 IP 推入
    python src/live_db_builder.py --out build_logs/live
    python TrollRestore/push_live_lan.py --host 192.168.1.23

    # 一步到位
    python TrollRestore/push_live_lan.py --host 192.168.1.23 --build

    # 不知道设备 IP 时，可先用 USB 通道读一次（U盘式兜底）
    python TrollRestore/push_live_usb.py --help
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Kline 仓库根（与 build_and_deploy.py 的 REPO_DIR 保持一致；TrollRestore 是它的同级目录，不是父目录）
REPO = Path(r"c:\Users\sunck\home\projects\ios\Kline")
PORT = 5051

DEFAULT_DB = REPO / "build_logs" / "live" / "tdx_live.db"
DEFAULT_MANIFEST = REPO / "build_logs" / "live" / "tdx_live.manifest.json"


def base_url(host: str) -> str:
    host = host.strip()
    if host.startswith("http://") or host.startswith("https://"):
        return host.rstrip("/")
    if ":" not in host:
        host = f"{host}:{PORT}"
    return f"http://{host}"


def http_put(base: str, path: str, file_path: Path, timeout: int = 900) -> tuple:
    size = file_path.stat().st_size
    body = file_path.read_bytes()
    req = urllib.request.Request(
        f"{base}/sandbox/{path}", data=body, method="PUT",
        headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def http_get(base: str, path: str, timeout: int = 15) -> str:
    with urllib.request.urlopen(f"{base}{path}", timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_post(base: str, path: str, timeout: int = 15) -> str:
    req = urllib.request.Request(f"{base}{path}", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Kline 增量行情库 局域网 WiFi 直推（通道 B）")
    ap.add_argument("--host", required=True, help="设备 IP（可带端口），如 192.168.1.23")
    ap.add_argument("--db", default=str(DEFAULT_DB))
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    ap.add_argument("--build", action="store_true", help="推送前先本机生成一次")
    ap.add_argument("--no-reload", action="store_true", help="不通知 App 立即热刷新")
    args = ap.parse_args()

    if args.build:
        out_dir = Path(args.db).parent
        print(f"▶ 生成增量库 → {out_dir}")
        r = subprocess.run([sys.executable, str(REPO / "src" / "live_db_builder.py"),
                            "--out", str(out_dir)], cwd=str(REPO))
        if r.returncode != 0:
            raise SystemExit("❌ 生成失败（未推送任何文件）")

    db_path, manifest_path = Path(args.db), Path(args.manifest)
    if not db_path.exists():
        print(f"❌ 找不到增量库：{db_path}")
        print("   先执行：python src/live_db_builder.py --out build_logs/live")
        return 1
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = None

    base = base_url(args.host)
    print("=" * 72)
    print("Kline 增量行情库 · 局域网 WiFi 直推")
    print("=" * 72)
    print(f"目标设备 : {base}")
    print(f"库文件   : {db_path}  ({db_path.stat().st_size:,} bytes)")
    if manifest:
        print(f"manifest : v{manifest.get('version')} trade_date={manifest.get('trade_date')} "
              f"覆盖={manifest.get('symbols')}只")
    print("提示     : 设备需与电脑同一 Wi-Fi，且 Kline 前台未锁屏")
    print("-" * 72)

    # 0) 先探活，给出可操作的失败提示（分不清"IP 错/IP 变了/App 不在前台"是最常见的坑）
    try:
        http_get(base, "/", timeout=6)
    except urllib.error.URLError as exc:
        print(f"❌ 连不上 {base}：{exc}")
        print("   逐项确认：① IP 是否正确（设置 → 数据同步 里的局域网地址）")
        print("             ② 设备与电脑是否同一 Wi-Fi（不是手机热点/访客网络）")
        print("             ③ Kline 是否在前台未锁屏")
        return 1
    except Exception as exc:
        print(f"❌ 连不上 {base}：{exc}")
        return 1

    for name, path in (("tdx_live.db", db_path),
                       ("tdx_live.manifest.json", manifest_path if manifest else None)):
        if path is None or not path.exists():
            continue
        t0 = time.time()
        status, body = http_put(base, name, path)
        print(f"{'✅' if status == 200 else '❌'} 推送 {name} → HTTP {status}  "
              f"{path.stat().st_size:,} bytes  {time.time() - t0:.1f}s  {body[:80]}")

    if not args.no_reload:
        try:
            print(f"🔄 通知热刷新 → {http_post(base, '/sync/reload')}")
        except Exception as exc:
            print(f"⚠️ 热刷新通知失败（文件已写入，等前台指纹轮询也会生效）：{exc}")

    time.sleep(1.5)
    try:
        st = json.loads(http_get(base, "/sync/status"))
        print("-" * 72)
        print(f"设备侧状态：可用={st.get('available')} 覆盖={st.get('metaCount')}只 "
              f"日线={st.get('dailyCount')}行 最新交易日={st.get('latestDate')} "
              f"重载次数={st.get('reloadCount')}")
    except Exception as exc:
        print(f"⚠️ 读取设备状态失败：{exc}")
    return 0


if __name__ == "__main__":
    sys.exit(main())