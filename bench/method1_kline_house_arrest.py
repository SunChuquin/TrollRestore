"""Benchmark & validation: USB HouseArrest push IPA to Kline sandbox.

This is the most ideal USB-only path because:
  - Kline is a permanent TrollStore-installed app with UIFileSharingEnabled,
    HouseArrestService should be able to connect (confirmed in earlier probes).
  - User can then open Files App → On My iPad → Kline → Kline.ipa
    → Share → TrollStore → Install.  This is the "permanent drop zone" pattern.
"""
import asyncio
import os
import time

IPA_PATH = r"c:\Users\sunck\home\projects\ios\artifacts\Kline.ipa"
BUNDLE_ID = "com.sunck.Kline"

async def main():
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.house_arrest import HouseArrestService

    if not os.path.exists(IPA_PATH):
        raise SystemExit(f"IPA missing: {IPA_PATH}")
    size = os.path.getsize(IPA_PATH)
    print(f"[M1] Target: HouseArrest {BUNDLE_ID}")
    print(f"[M1] IPA: {IPA_PATH} ({size:,} bytes)")

    t0 = time.time()

    print("[M1] Connecting lockdown...")
    lockdown = await create_using_usbmux()
    print(f"[M1]   Device: {lockdown.product_type} iOS {lockdown.product_version} ({time.time()-t0:.2f}s)")

    print(f"[M1] Creating HouseArrest for {BUNDLE_ID}...")
    t_conn = time.time()
    try:
        afc = await HouseArrestService.create(
            lockdown=lockdown, bundle_id=BUNDLE_ID, documents_only=False
        )
    except Exception as e:
        print(f"[M1]   FAIL: HouseArrestService.create -> {e!r}")
        return
    print(f"[M1]   Connected ({time.time()-t_conn:.2f}s)")

    # List Documents (current state)
    try:
        docs = await afc.listdir("/Documents")
        print(f"[M1] /Documents has {len(docs)} items")
    except Exception as e:
        print(f"[M1]   /Documents missing or error: {e!r}, creating")
        await afc.makedirs("/Documents")

    # Push IPA
    t_push = time.time()
    with open(IPA_PATH, "rb") as f:
        data = f.read()
    print(f"[M1] Pushing {len(data):,} bytes to /Documents/Kline.ipa ...")
    await afc.set_file_contents("/Documents/Kline.ipa", data)
    t_done = time.time()
    print(f"[M1]   Push took {t_done-t_push:.2f}s  (throughput: {len(data)/1024/(t_done-t_push):.0f} KB/s)")

    # Verify
    docs = await afc.listdir("/Documents")
    ok = "Kline.ipa" in docs
    print(f"[M1] Verify: Kline.ipa in /Documents -> {ok}")

    # Get file size from remote (stat if possible, fallback to download first bytes)
    try:
        info = await afc.stat("/Documents/Kline.ipa")
        if isinstance(info, dict):
            remote_size = info.get("st_size", "?")
            match = (remote_size == len(data)) if isinstance(remote_size, int) else "?"
            print(f"[M1] Remote size: {remote_size}  (local {len(data)} -> match={match})")
        else:
            print(f"[M1] stat result type {type(info).__name__}: {str(info)[:120]}")
    except Exception as e:
        print(f"[M1] stat not available: {e!r}")

    try:
        await afc.aclose()
    except Exception:
        pass

    total = time.time() - t0
    print(f"\n[M1] TOTAL: {total:.2f}s")
    if ok:
        print("[M1] ✅ SUCCESS. User action: Files App -> On My iPad -> Kline -> Kline.ipa -> Share -> TrollStore")
    else:
        print("[M1] ❌ FAILED")

if __name__ == "__main__":
    asyncio.run(main())
