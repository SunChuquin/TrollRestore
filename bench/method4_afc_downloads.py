"""Benchmark: USB AFC push IPA to /var/mobile/Media/Downloads.

Pros: No bundle ID needed, any file manager / Files App can see it.
Cons: Files end up in system-wide shared Downloads folder, easy to clutter.
Kinda: 100% automated for the push, then manual share to TrollStore.
"""
import os
import subprocess
import sys
import time

IPA_PATH = r"c:\Users\sunck\home\projects\ios\artifacts\Kline.ipa"
PM = r"c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe"

def main():
    size = os.path.getsize(IPA_PATH)
    print(f"[M4] Target: AFC -> /var/mobile/Media/Downloads/Kline.ipa")
    print(f"[M4] IPA: {size:,} bytes")

    t0 = time.time()
    # Step 1: Push via `pymobiledevice3 afc push`
    t_push = time.time()
    r = subprocess.run(
        [PM, "afc", "push", IPA_PATH, "Downloads/Kline.ipa"],
        capture_output=True, text=True, timeout=300,
    )
    if r.returncode != 0:
        print(f"[M4] ❌ PUSH FAILED (code {r.returncode})")
        print(f"  STDOUT: {r.stdout[-500:]!r}")
        print(f"  STDERR: {r.stderr[-500:]!r}")
        return False
    t_done = time.time()
    print(f"[M4] Push took {t_done-t_push:.2f}s  (throughput: {size/1024/(t_done-t_push):.0f} KB/s)")

    # Step 2: Verify with `afc ls Downloads`
    r2 = subprocess.run(
        [PM, "afc", "ls", "Downloads"],
        capture_output=True, text=True, timeout=60,
    )
    ok = "Kline.ipa" in (r2.stdout or "")
    print(f"[M4] Verify: Kline.ipa listed -> {ok}")

    total = time.time() - t0
    print(f"[M4] TOTAL: {total:.2f}s")
    if ok:
        print("[M4] ✅ SUCCESS. User action: Files App -> Downloads -> Kline.ipa -> Share -> TrollStore")
    else:
        print("[M4] ❌ VERIFY FAILED:", r2.stdout[-300:], r2.stderr[-300:])
    return ok

if __name__ == "__main__":
    sys.exit(0 if main() else 1)
