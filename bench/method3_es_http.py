"""Benchmark: WiFi HTTP upload to ES File Explorer (iPad Web端).

Requires: iPad running ES文件浏览器 with WiFi transfer enabled at 192.168.137.52:5050.

Pros: Directly lands in ES file browser where user can tap → share → TrollStore.
Cons: Requires same LAN + ES running in foreground on iPad.
Automation: 100% scriptable upload, user only clicks share/install on iPad.
"""
import os
import sys
import time

import requests

IPA_PATH = r"c:\Users\sunck\home\projects\ios\artifacts\Kline.ipa"
BASE = "http://192.168.137.52:5050"
TARGET_PATH = "/"  # 孙楚昆的iPad root

def main():
    size = os.path.getsize(IPA_PATH)
    print(f"[M3] Target: ES HTTP POST {BASE}/upload  -> {TARGET_PATH}")
    print(f"[M3] IPA: {size:,} bytes")

    t0 = time.time()
    try:
        with open(IPA_PATH, "rb") as f:
            files = {"files[]": ("Kline.ipa", f, "application/octet-stream")}
            data = {"path": TARGET_PATH}
            t_req = time.time()
            r = requests.post(f"{BASE}/upload", files=files, data=data, timeout=300)
            t_done = time.time()
    except requests.RequestException as e:
        print(f"[M3] ❌ NETWORK ERROR: {e}")
        return False

    print(f"[M3] HTTP {r.status_code}  (req took {t_done-t_req:.2f}s, throughput: {size/1024/(t_done-t_req):.0f} KB/s)")
    body = r.text[:200]
    print(f"[M3] Response: {body}")

    ok = (r.status_code == 200)
    if not ok:
        print(f"[M3] ❌ FAIL status {r.status_code}")

    total = time.time() - t0
    print(f"[M3] TOTAL: {total:.2f}s")
    if ok:
        print("[M3] ✅ SUCCESS. User action: ES文件浏览器 -> 孙楚昆的iPad -> Kline.ipa -> 分享 -> TrollStore")
    return ok

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
