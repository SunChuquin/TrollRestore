#!/usr/bin/env python3
"""Kline 增量行情库 —— USB 一键推送（通道 A）

把本机生成的 `tdx_live.db`（+ `tdx_live.manifest.json`）经数据线推入 Kline 沙盒
`Documents/`，推送完成后立即通知 App 热刷新（无需重启、无需等 5 分钟指纹检查）。

与局域网通道（`push_live_lan.py`）共用同一套流程，区别只是连接方式：
本脚本走 `usbmux forward 5051`（需数据线 + 已信任本机），它能在**设备没有外网 / 不在同一 Wi-Fi**
时依然工作，是"全量兜底"最可靠的一条通道。

前置条件：
  1. 数据线已连接，且首次已在设备上点过「信任此电脑」；
  2. **Kline 处于前台且未锁屏**（iOS 无后台能力，App 切后台后 5051 监听会被冻结）。

用法：
    # 先本机生成（联网取公开行情），再推入设备
    python src/live_db_builder.py --out build_logs/live
    python TrollRestore/push_live_usb.py

    # 一步到位：自动先生成再推送
    python TrollRestore/push_live_usb.py --build

    # 推送指定文件
    python TrollRestore/push_live_usb.py --db <path> [--manifest <path>]

    # 只推不通知（排查用）
    python TrollRestore/push_live_usb.py --no-reload
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
TROLL = Path(__file__).resolve().parent                # ...\ios\TrollRestore
PORT = 5051
BASE = f"http://127.0.0.1:{PORT}"

# 默认产物位置（与 src/live_db_builder.py --out build_logs/live 约定一致）
DEFAULT_DB = REPO / "build_logs" / "live" / "tdx_live.db"
DEFAULT_MANIFEST = REPO / "build_logs" / "live" / "tdx_live.manifest.json"

sys.path.insert(0, str(TROLL))


def http_put(path: str, file_path: Path, timeout: int = 900) -> tuple:
    """流式 PUT 到沙盒（stdlib，避免依赖 requests）。返回 (status, body)"""
    size = file_path.stat().st_size
    req = urllib.request.Request(
        f"{BASE}/sandbox/{path}",
        data=file_path.open("rb"),
        method="PUT",
        headers={"Content-Length": str(size), "Content-Type": "application/octet-stream"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, r.read().decode("utf-8", errors="replace")


def http_get(path: str, timeout: int = 15) -> str:
    with urllib.request.urlopen(f"{BASE}{path}", timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def http_post(path: str, timeout: int = 15) -> str:
    req = urllib.request.Request(f"{BASE}{path}", data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", errors="replace")


def build_artifacts(out_dir: Path) -> None:
    """调用生成端产出增量库（联网取公开行情）"""
    builder = REPO / "src" / "live_db_builder.py"
    print(f"▶ 生成增量库 → {out_dir}")
    r = subprocess.run([sys.executable, str(builder), "--out", str(out_dir)],
                       cwd=str(REPO))
    if r.returncode != 0:
        raise SystemExit("❌ 生成失败（未推送任何文件）")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ap = argparse.ArgumentParser(description="Kline 增量行情库 USB 一键推送（通道 A）")
    ap.add_argument("--db", default=str(DEFAULT_DB), help=f"增量库文件（默认 {DEFAULT_DB}）")
    ap.add_argument("--manifest", default=str(DEFAULT_MANIFEST), help="manifest 文件（可选）")
    ap.add_argument("--build", action="store_true", help="推送前先本机生成一次")
    ap.add_argument("--no-reload", action="store_true", help="不通知 App 立即热刷新")
    args = ap.parse_args()

    out_dir = Path(args.db).parent
    if args.build:
        build_artifacts(out_dir)

    db_path = Path(args.db)
    if not db_path.exists():
        print(f"❌ 找不到增量库：{db_path}")
        print("   先执行：python src/live_db_builder.py --out build_logs/live")
        return 1
    manifest_path = Path(args.manifest)
    manifest = None
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except Exception:
            manifest = None

    print("=" * 72)
    print("Kline 增量行情库 · USB 一键推送")
    print("=" * 72)
    print(f"库文件   : {db_path}  ({db_path.stat().st_size:,} bytes)")
    if manifest:
        print(f"manifest : v{manifest.get('version')} trade_date={manifest.get('trade_date')} "
              f"覆盖={manifest.get('symbols')}只 daily={manifest.get('rows', {}).get('daily')}行")
    print("提示     : 请让 Kline 保持前台、设备保持解锁（否则 5051 无响应）")
    print("-" * 72)

    # 1) 建端口转发 + 确认 App 在线（复用 sandbox_cli 的转发与探测）
    import sandbox_cli  # noqa: E402  （同目录工具，复用 usbmux forward / 在线探测）

    fwd = sandbox_cli.forward()
    try:
        if not sandbox_cli.check_online():
            print("❌ Kline 不在前台（本地 HTTP 服务器未响应）。")
            print("   请在设备上打开 Kline、保持解锁后重试。")
            return 1

        # 2) 推送
        for name, path in (("tdx_live.db", db_path),
                           ("tdx_live.manifest.json", manifest_path if manifest else None)):
            if path is None or not path.exists():
                continue
            t0 = time.time()
            status, body = http_put(name, path)
            ok = status == 200
            print(f"{'✅' if ok else '❌'} 推送 {name} → HTTP {status}  "
                  f"{path.stat().st_size:,} bytes  {time.time() - t0:.1f}s  {body[:80]}")

        # 3) 通知 App 立即热刷新（免等 5 分钟指纹轮询）
        if not args.no_reload:
            try:
                print(f"🔄 通知热刷新 → {http_post('/sync/reload')}")
            except Exception as exc:
                print(f"⚠️ 热刷新通知失败（不影响文件已写入）：{exc}")

        # 4) 回读状态，给出可核对的结论
        time.sleep(1.5)
        try:
            st = json.loads(http_get("/sync/status"))
            print("-" * 72)
            print(f"设备侧状态：可用={st.get('available')} 覆盖={st.get('metaCount')}只 "
                  f"日线={st.get('dailyCount')}行 最新交易日={st.get('latestDate')} "
                  f"重载次数={st.get('reloadCount')}")
            if manifest and str(st.get("latestDate")) == str(manifest.get("trade_date")):
                print("✅ 设备侧最新交易日与推送的 manifest 一致")
            else:
                print("⚠️ 设备侧最新交易日与 manifest 不一致：确认热刷新是否生效（见设备日志 [Live]）")
        except Exception as exc:
            print(f"⚠️ 读取设备状态失败：{exc}")
        return 0
    finally:
        fwd.terminate()


if __name__ == "__main__":
    sys.exit(main())