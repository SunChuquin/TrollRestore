"""One-click deploy Kline IPA to iPad, then prompt user for the single manual step.

Automates the FASTEST known delivery path (USB AFC push → /var/mobile/Media/Downloads),
then optionally runs WiFi ES HTTP upload as redundant parallel push.

Also emits a URL Scheme link that the user can paste into Safari if they prefer the
network path.  USB Web Inspector (M2) automation is NOT included for URL Scheme firing
because pymobiledevice3's `navigate_broswing_context(URL_Scheme)` blocks forever on
iOS 15 (see TrollRestore/bench/README.md for detailed comparison of 4 methods).

Usage:
  .venv-ios\Scripts\python.exe deploy_kline_to_ipad.py

Optional env vars:
  IPA_PATH=path\to\Kline.ipa      (defaults to ..\artifacts\Kline.ipa relative to this file)
  ES_URL=http://192.168.x.x:5050   (if set, also uploads via ES HTTP as fallback)
  ES_TARGET=/                     (directory inside ES, default root)
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

THIS_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = THIS_DIR.parent  # ios/
DEFAULT_IPA = PROJECT_ROOT / "artifacts" / "Kline.ipa"
PM = PROJECT_ROOT / ".venv-ios" / "Scripts" / "pymobiledevice3.exe"


def run(cmd: list[str], timeout: int = 300, label: str = "") -> tuple[int, str, str]:
    """Run command, return (exit_code, stdout, stderr)."""
    print(f"\n▶ {label}: {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.stdout:
        print(f"  STDOUT ({len(r.stdout)} chars): {r.stdout[:300].strip()}")
    if r.stderr:
        print(f"  STDERR ({len(r.stderr)} chars): {r.stderr[:300].strip()}")
    print(f"  exit {r.returncode}")
    return r.returncode, r.stdout, r.stderr


def check_device() -> bool:
    rc, out, err = run([str(PM), "usbmux", "list"], label="usbmux list")
    ok = (rc == 0) and ('"DeviceClass": "iPad"' in out or '"DeviceClass": "iPhone"' in out or '"DeviceName":' in out)
    if ok:
        print("✅ iPad detected via USB.")
    else:
        print("❌ iPad not found.  Plug in USB and tap 'Trust This Computer' on iPad.")
    return ok


def push_afc(ipa_path: Path) -> bool:
    """USB AFC push to /var/mobile/Media/Downloads/Kline.ipa.  ~714 KB/s, 3-6s total."""
    t0 = time.time()
    remote = "Downloads/Kline.ipa"
    rc, out, err = run(
        [str(PM), "afc", "push", str(ipa_path), remote],
        timeout=300,
        label="AFC push USB -> Downloads/",
    )
    if rc != 0:
        print(f"❌ AFC push FAILED: {err.strip() or out.strip()}")
        return False
    # Verify listing
    rc2, out2, _ = run([str(PM), "afc", "ls", "Downloads"], timeout=60, label="AFC ls verify")
    ok = (rc2 == 0) and ("Kline.ipa" in out2)
    print(f"{'✅' if ok else '❌'} AFC verify: {ok}  ({time.time()-t0:.2f}s)")
    if ok:
        print("   📁 用户操作1（文件App）：iPad→文件App→我的iPad→下载项→Kline.ipa→共享→TrollStore")
    return ok


def push_es_http(ipa_path: Path, base: str, target_dir: str = "/") -> bool:
    """WiFi HTTP upload via ES文件浏览器.  ~95 KB/s, 15-25s for ~2MB."""
    try:
        import requests  # type: ignore
    except ImportError:
        print("⚠ requests missing in venv; trying pip install first...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "requests"])
        import requests  # type: ignore

    t0 = time.time()
    url = f"{base.rstrip('/')}/upload"
    size = ipa_path.stat().st_size
    print(f"\n▶ ES HTTP upload {size:,} bytes -> {url} path={target_dir}")
    try:
        with open(ipa_path, "rb") as f:
            r = requests.post(
                url,
                files={"files[]": ("Kline.ipa", f, "application/octet-stream")},
                data={"path": target_dir},
                timeout=300,
            )
    except requests.RequestException as e:
        print(f"❌ ES HTTP NETWORK FAIL: {e}")
        print("   (ES文件浏览器未开启WiFi传输或iPad不在同一局域网？可忽略此失败，AFC那条已经够用了)")
        return False
    took = time.time() - t0
    ok = (r.status_code == 200)
    print(f"   HTTP {r.status_code}  took {took:.2f}s ({size/1024/max(took,1e-3):.0f} KB/s)")
    if not ok:
        print(f"   Response: {r.text[:200]}")
    else:
        print("   📂 用户操作2（ES）：iPad→ES文件浏览器→孙楚昆的iPad→Kline.ipa→分享→TrollStore")
    return ok


def print_url_scheme(ipa_direct_url: str) -> None:
    if not ipa_direct_url:
        return
    scheme = f"apple-magnifier://install?url={ipa_direct_url}"
    print(f"\n🌐 URL Scheme（Safari 粘贴直达）：")
    print(f"   {scheme}")
    print(f"   📱 用户操作3：iPad→Safari→粘贴上面的URL→回车→弹窗中「打开」→TrollStore→Install")


def pull_logs(local: Path | None = None) -> bool:
    """A1 日志双写：通过 AFC 拉取 Kline 的公共日志镜像（不经过 HouseArrest，无需 bundle 注册）。"""
    local = local or THIS_DIR / "kline_debug_log.txt"
    rc, out, err = run(
        [str(PM), "afc", "pull", "Downloads/KlineLogs/debug_log.txt", str(local)],
        timeout=120,
        label="AFC pull KlineLogs/debug_log.txt",
    )
    if rc != 0 or not Path(local).exists():
        print("❌ 拉取失败：公共日志不存在。")
        print("   请确认：1) iPad 上装的是含 A1 日志双写的 Kline（no-sandbox 版）；")
        print("            2) Kline 启动过至少一次（启动时会写公共日志）。")
        return False
    text = Path(local).read_text(encoding="utf-8", errors="replace")
    print(f"✅ 日志已拉取: {local} ({Path(local).stat().st_size:,} bytes, {len(text.splitlines())} 行)")
    print("   内容预览（末尾 2000 字符）：")
    print("   " + "-" * 60)
    print(text[-2000:])
    print("   " + "-" * 60)
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="Deploy ad-hoc Kline.ipa to iPad for TrollStore install.")
    ap.add_argument("--ipa", default=os.environ.get("IPA_PATH", str(DEFAULT_IPA)), help="Local Kline.ipa path")
    ap.add_argument("--es-url", default=os.environ.get("ES_URL", ""), help="ES文件浏览器 WiFi URL, e.g. http://192.168.137.52:5050")
    ap.add_argument("--es-path", default=os.environ.get("ES_TARGET", "/"), help="Target dir inside ES (default /)")
    ap.add_argument("--url-scheme", default=os.environ.get("IPA_DOWNLOAD_URL", ""), help="HTTPS direct IPA URL (for URL Scheme copy)")
    ap.add_argument("--skip-es", action="store_true", help="Skip ES HTTP upload (push AFC only)")
    ap.add_argument("--pull-logs", action="store_true", help="Pull Kline debug log via AFC (A1 日志双写), skip deploy")
    args = ap.parse_args()

    if args.pull_logs:
        if not check_device():
            return 1
        return 0 if pull_logs() else 1

    ipa = Path(args.ipa)
    if not ipa.exists():
        print(f"❌ IPA not found: {ipa}")
        print("   Run: gh run download <RUN_ID> --dir <ios_root>\\artifacts --name Kline-unsigned-ipa")
        return 2
    print(f"📦 IPA: {ipa} ({ipa.stat().st_size:,} bytes)")

    if not check_device():
        return 1

    ok_afc = push_afc(ipa)
    ok_es = None
    if args.es_url and not args.skip_es:
        ok_es = push_es_http(ipa, args.es_url, args.es_path)
    elif not args.skip_es:
        print(f"\nℹ Skip ES HTTP push (--es-url not set).  Set env ES_URL=http://<ipad>:5050 to enable fallback.")

    print_url_scheme(args.url_scheme)

    any_ok = ok_afc or (ok_es is True)
    if not any_ok:
        print("\n❌ All push methods failed.  See messages above.")
        return 1

    print("\n" + "=" * 64)
    print("📲 iPad 上只需做一步（任选其一）：")
    print("   A. 系统文件App → 下载项 → Kline.ipa → 共享 → TrollStore → Install")
    if ok_es:
        print("   B. ES文件浏览器 → 孙楚昆的iPad → Kline.ipa → 共享 → TrollStore → Install")
    if args.url_scheme:
        print("   C. Safari → 粘贴 URL Scheme → 回车 → 「打开」→ TrollStore → Install")
    print("=" * 64)
    print("✅ 文件已送达。安装完成后桌面出现 Kline 图标即为永久版（7天不过期）。")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrupted.")
        sys.exit(130)
