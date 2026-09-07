"""Upload IPA to ES文件浏览器 via its local HTTP upload endpoint.

ES file browser's web UI uses jQuery File Upload plugin:
  POST http://<ipad_ip>:5050/upload
  multipart/form-data fields:
    files[]: <file content>  (filename=Kline.ipa)
    path:    "/"             (target directory, _path from JS)
  Returns JSON.
"""
import sys
import requests

IPA_PATH = r"c:\Users\sunck\home\projects\ios\artifacts\Kline.ipa"
BASE = "http://192.168.137.52:5050"
TARGET_PATH = "/"

def main():
    print(f"Uploading {IPA_PATH} ({__import__('os').path.getsize(IPA_PATH):,} bytes)")
    print(f"  -> {BASE}/upload  (path={TARGET_PATH})")

    with open(IPA_PATH, "rb") as f:
        files = {"files[]": ("Kline.ipa", f, "application/octet-stream")}
        data = {"path": TARGET_PATH}
        try:
            r = requests.post(f"{BASE}/upload", files=files, data=data, timeout=300)
        except requests.RequestException as e:
            print(f"NETWORK ERROR: {e}")
            sys.exit(1)

    print(f"HTTP {r.status_code}")
    print(f"Response body: {r.text[:1000]}")

    try:
        j = r.json()
        print(f"JSON: {j}")
    except Exception:
        pass

    if r.status_code == 200:
        print("\nUpload OK. Now on iPad open ES文件浏览器 -> 孙楚昆的iPad -> Kline.ipa -> Share -> TrollStore")
    else:
        print("\nUpload FAILED.")

if __name__ == "__main__":
    main()
