#!/usr/bin/env python3
"""Kline-TS 自动部署助手（PySide2 GUI）

工作方式：
  1. TRAE 推送代码并监控 GitHub Actions
  2. 构建成功生成 IPA 后，TRAE 只需 POST 通知本工具：
        curl -X POST http://127.0.0.1:5052/notify -H "Content-Type: application/json" -d "{\"run_id\":\"<RUN_ID>\"}"
  3. 工具自动执行流水线：
        a. gh run download 拉取 IPA 到固定路径
        b. 检查 Kline-TS 前台（不在前台则提示等待）
        c. 经 usbmux forward + KlineHTTP /sandbox 流式上传 IPA 到沙盒 Downloads/
        d. POST /install-local (scope=sandbox) 让 Kline 拉起 TrollStore 安装
  4. 用户在 iPad 上手动点「打开」+「Install」
  5. 安装完成后用户打开新版 Kline：助手自动检测新版上线（KlineHTTP 回报版本号）
     并从"等待 iPad 确认安装"自动转为"部署完成"

运行：.venv-ios\\Scripts\\python.exe deploy_gui.py
"""
import json
import plistlib
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from PySide2.QtCore import QObject, QThread, Signal, Qt
from PySide2.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPlainTextEdit, QPushButton, QLabel, QLineEdit, QGroupBox, QCheckBox,
)

# ============ 配置 ============
PM = Path(r"c:\Users\sunck\home\projects\ios\.venv-ios\Scripts\pymobiledevice3.exe")
REPO_DIR = Path(r"c:\Users\sunck\home\projects\ios\Kline")
ARTIFACTS_DIR = Path(r"c:\Users\sunck\home\projects\ios\artifacts")
LISTEN_PORT = 5052     # 本工具通知监听端口
DEVICE_PORT = 5051     # KlineHTTP 端口
IPA_PORT = 5053        # 本工具 IPA 下载服务端口（TrollStore 从此下载，不依赖 Kline 前台）
IPA_SUB = ["Kline-unsigned-ipa", "Kline.ipa"]
WAIT_KLINE_SECONDS = 180
WAIT_REOPEN_SECONDS = 300   # 触发安装后，等待新版 Kline 重新打开的最长时间
GH_POLL_SECONDS = 30        # 自动监控：轮询 GitHub Actions 最新 run 的间隔
STATE_FILE = Path(__file__).resolve().parent / "deploy_state.json"   # 记住上次已处理的 run id，避免重复部署

_status = "空闲"
_autopoll_on = True     # 自动监控开关状态（供 GET /status 查询；TRAE 可经 POST /autopoll 开启）


def set_autopoll_on(v: bool):
    global _autopoll_on
    _autopoll_on = v


def get_autopoll_on() -> bool:
    return _autopoll_on


def load_last_processed() -> str:
    """上次已处理的 GitHub run id（防止自动监控重复部署）"""
    try:
        return str(json.loads(STATE_FILE.read_text(encoding="utf-8")).get("last_run_id", ""))
    except Exception:
        return ""


def save_last_processed(run_id: str) -> None:
    try:
        STATE_FILE.write_text(json.dumps({"last_run_id": run_id}), encoding="utf-8")
    except Exception:
        pass


def set_status(s: str):
    global _status
    _status = s


def get_status() -> str:
    return _status


def get_lan_ip() -> str:
    """获取电脑局域网 IP（供 iPad 经 WiFi 访问本工具的 IPA 下载服务）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def gh(*args: str) -> str:
    r = subprocess.run(["gh", *args], capture_output=True, text=True, cwd=str(REPO_DIR))
    if r.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)} 失败: {(r.stderr or r.stdout)[-500:]}")
    return r.stdout


def kill_port(port: int) -> None:
    try:
        out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            if f":{port}" in line and "LISTENING" in line:
                pid = line.split()[-1]
                subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    except Exception:
        pass


def start_forward() -> subprocess.Popen:
    kill_port(DEVICE_PORT)
    time.sleep(1)
    fwd = subprocess.Popen(
        [str(PM), "usbmux", "forward", str(DEVICE_PORT), str(DEVICE_PORT)],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return fwd


def wait_online(timeout: float, log) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{DEVICE_PORT}/", timeout=4) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        remaining = int(deadline - time.time())
        log(f"⏳ Kline-TS 不在前台（HTTP 未响应）。请在 iPad 上打开 Kline 保持前台（剩余 {remaining}s）...")
        time.sleep(3)
    return False


def ipa_version(ipa: Path) -> str:
    """读取 IPA 内 Info.plist 的版本号（short + build），用于安装后校验新版 Kline 是否上线"""
    try:
        with zipfile.ZipFile(ipa) as z:
            with z.open("Payload/Kline.app/Info.plist") as f:
                pl = plistlib.load(f)
        return f"{pl.get('CFBundleShortVersionString', '?')} ({pl.get('CFBundleVersion', '?')})"
    except Exception:
        return "?"


def fetch_kline_status() -> dict:
    """GET KlineHTTP 根路径：新版 Kline 返回含 version 字段的 JSON；不可达返回 None"""
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{DEVICE_PORT}/", timeout=4) as r:
            if r.status == 200:
                try:
                    return json.loads(r.read().decode())
                except Exception:
                    return {}
    except Exception:
        pass
    return None


def upload_stream(local: Path, remote: str, log) -> None:
    import requests
    size = local.stat().st_size
    url = f"http://127.0.0.1:{DEVICE_PORT}/sandbox/{remote}"
    with open(local, "rb") as f:
        r = requests.put(url, data=f, headers={"Content-Length": str(size)}, timeout=1800)
    if r.status_code != 200:
        raise RuntimeError(f"沙盒上传失败 HTTP {r.status_code}: {r.text[:200]}")
    log(f"✅ 上传完成 {size:,} bytes")


# ============ 流水线工作线程 ============
class DeployWorker(QThread):
    log = Signal(str)
    status = Signal(str)
    poll_ctrl = Signal(bool)   # True=部署开始，关闭自动监控（下次 TRAE 推送构建时才重新开启）

    def __init__(self, run_id: str, parent=None):
        super().__init__(parent)
        self.run_id = run_id

    def run(self):
        try:
            self.poll_ctrl.emit(True)   # 下载 IPA 之前先关闭自动监控，避免部署期间重复触发
            self.log.emit(f"▶ 开始部署 run={self.run_id}")
            self.status.emit("下载 IPA...")

            # 1. gh run download 到固定路径
            self.log.emit("▶ 清理旧产物 ...")
            for p in ARTIFACTS_DIR.iterdir():
                if p.is_dir():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    try:
                        p.unlink()
                    except Exception:
                        pass
            self.log.emit("▶ gh run download ...")
            gh("run", "download", self.run_id, "--dir", str(ARTIFACTS_DIR))
            ipa = ARTIFACTS_DIR.joinpath(*IPA_SUB)
            if not ipa.exists():
                raise RuntimeError(f"未找到 IPA: {ipa}")
            self.log.emit(f"✅ IPA: {ipa} ({ipa.stat().st_size / 1048576:.1f}MB)")

            # 2. 沙盒传输（需 Kline 前台）
            self.status.emit("等待 Kline 前台...")
            self.log.emit("▶ 启动 USB 转发并检查 KlineHTTP ...")
            fwd = start_forward()
            try:
                if not wait_online(WAIT_KLINE_SECONDS, self.log.emit):
                    raise RuntimeError(f"Kline-TS 不在前台（{WAIT_KLINE_SECONDS}s 超时）。请打开 Kline 后手动重试。")
                self.log.emit("✅ Kline 在线")

                self.status.emit("传输 IPA 到沙盒...")
                self.log.emit(f"▶ 流式上传 -> 沙盒 Downloads/Kline.ipa ...")
                upload_stream(ipa, "Downloads/Kline.ipa", self.log.emit)

                # 3. 触发安装（TrollStore 从 KlineHTTP 本地下载 127.0.0.1，无外网/局域网依赖）
                #    注意：Kline 调 URL scheme 后会切后台，本地 HTTP 有短暂冻结窗口；
                #    2MB 下载极快，通常成功。若偶发失败，重新打开 Kline 后重试即可。
                self.status.emit("触发 TrollStore 安装...")
                self.log.emit("▶ POST /install-local (scope=sandbox, 本地下载)...")
                body = json.dumps({"file": "Downloads/Kline.ipa", "scope": "sandbox"}).encode("utf-8")
                req = urllib.request.Request(
                    f"http://127.0.0.1:{DEVICE_PORT}/install-local",
                    data=body, headers={"Content-Type": "application/json"}, method="POST",
                )
                with urllib.request.urlopen(req, timeout=8) as r:
                    self.log.emit(f"   HTTP {r.status}: {r.read().decode()}")
                expected = ipa_version(ipa)
                self.log.emit("✅ 已触发安装！请在 iPad 上操作：")
                self.log.emit("   1. 弹「在 TrollStore 中打开？」→ 点「打开」")
                self.log.emit("   2. TrollStore 下载 IPA → 弹 Install → 点「Install」")
                self.log.emit(f"   3. 安装完成后打开 Kline（期望新版本 v{expected}）——助手自动检测")

                # 4. 等待新版 Kline 重新打开（USB 转发保持不关）：
                #    新版 Kline 启动后 KlineHTTP 恢复在线并回报 version 字段，匹配即部署完成。
                self.status.emit("等待 iPad 确认安装（打开新版 Kline 后自动完成）")
                reopened = self.wait_kline_reopen(expected, WAIT_REOPEN_SECONDS)
                if reopened:
                    self.log.emit(f"🎉 部署完成：Kline 已重新打开并运行 v{expected}")
                    self.status.emit(f"✅ 部署完成：Kline v{expected}")
                else:
                    self.log.emit(
                        f"⚠ {WAIT_REOPEN_SECONDS}s 内未检测到新版 Kline 打开。"
                        "IPA 仍在沙盒 Downloads/Kline.ipa，打开新版 Kline 后再点一次「开始部署」即可。"
                    )
                    self.status.emit("部署超时：未检测到新版 Kline 打开")
            finally:
                fwd.terminate()
        except Exception as e:
            self.log.emit(f"❌ {e}")
            self.status.emit("部署失败")

    def wait_kline_reopen(self, expected: str, timeout: float) -> bool:
        """安装触发后等待新版 Kline 重新打开。
        新版 Kline 的 GET / 会返回 version 字段（旧版没有该字段），版本匹配即视为部署完成。
        """
        time.sleep(5)  # 让触发生效：Kline 切后台冻结 HTTP、TrollStore 接管
        deadline = time.time() + timeout
        warned_old = False
        while time.time() < deadline:
            st = fetch_kline_status()
            if st:
                got = st.get("version", "")
                if got == expected:
                    return True
                if not warned_old:
                    self.log.emit(
                        f"⚠ 当前在线的是旧版/未完成安装的 Kline（期望 v{expected}，实际 {got or '无版本字段'}）。"
                        "安装完成后请打开新版 Kline，助手会继续等待...")
                    warned_old = True
            remaining = int(deadline - time.time())
            if remaining > 0 and remaining % 20 == 0:
                self.log.emit(f"⏳ 等待新版 Kline 打开（剩余 {remaining}s）...")
            time.sleep(3)
        return False


# ============ 通知桥（HTTP 线程 → 主线程） ============
class Bridge(QObject):
    notified = Signal(str)
    autopoll = Signal(bool)   # TRAE 经 POST /autopoll 开启/关闭自动监控


# ============ 自动监控：轮询 GitHub Actions，发现新的成功构建自动部署 ============
class PollWorker(QThread):
    new_run = Signal(str)    # 发现新成功 run（未处理过）
    poll_log = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._running = True

    def stop(self):
        self._running = False

    def run(self):
        while self._running:
            try:
                out = gh("run", "list", "--limit", "1",
                         "--json", "databaseId,status,conclusion,headSha,displayTitle")
                runs = json.loads(out)
                if runs:
                    r = runs[0]
                    rid = str(r.get("databaseId", ""))
                    state = r.get("status", "")
                    concl = r.get("conclusion", "")
                    if (rid and state == "completed" and concl == "success"
                            and rid != load_last_processed()):
                        self.poll_log.emit(
                            f"📡 检测到新的成功构建 run={rid}（{r.get('displayTitle', '')[:40]}），自动部署")
                        save_last_processed(rid)   # 先记账：即使部署失败也不重复自动触发
                        self.new_run.emit(rid)
            except Exception as e:
                self.poll_log.emit(f"⚠ 自动监控轮询失败: {e}")
            for _ in range(max(1, int(GH_POLL_SECONDS / 0.5))):
                if not self._running:
                    return
                time.sleep(0.5)


# ============ IPA 下载服务（电脑侧，TrollStore 从此下载，不依赖 Kline 前台） ============
class IPAHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        ipa = ARTIFACTS_DIR.joinpath(*IPA_SUB)
        if self.path == "/Kline.ipa" and ipa.exists():
            size = ipa.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(ipa, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):
        pass


# ============ HTTP 通知服务 ============
class NotifyHandler(BaseHTTPRequestHandler):
    bridge = None

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            data = json.loads(raw or b"{}")
            run_id = str(data.get("run_id", "")).strip()
            if self.path == "/notify" and run_id:
                self.send_json({"ok": True, "status": get_status()})
                self.bridge.notified.emit(run_id)   # 队列投递到主线程
            elif self.path == "/autopoll" and isinstance(data.get("enabled"), bool):
                self.send_json({"ok": True, "autopoll": bool(data["enabled"])})
                self.bridge.autopoll.emit(bool(data["enabled"]))   # 主线程开启/关闭轮询
            else:
                self.send_json({"error": "bad request"}, 400)
        except Exception as e:
            self.send_json({"error": str(e)}, 500)

    def do_GET(self):
        if self.path == "/status":
            self.send_json({"status": get_status(), "listen_port": LISTEN_PORT,
                            "kline_port": DEVICE_PORT, "autopoll": get_autopoll_on()})
        else:
            self.send_json({"error": "not found"}, 404)

    def send_json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


# ============ 主窗口 ============
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Kline-TS 自动部署助手")
        self.resize(720, 520)

        self.bridge = Bridge()
        self.bridge.notified.connect(self.on_notify)
        self.bridge.autopoll.connect(self.set_autopoll)

        central = QWidget()
        self.setCentralWidget(central)
        lay = QVBoxLayout(central)

        # 状态
        self.status_label = QLabel("状态：空闲")
        self.status_label.setStyleSheet("font-size:16px; font-weight:bold; padding:4px;")
        lay.addWidget(self.status_label)

        # 通知地址说明
        info = QLabel(
            "自动监控已开启：每 30s 轮询 GitHub Actions，发现新的成功构建自动部署。\n"
            "（原手动方式仍可用：curl -X POST http://127.0.0.1:%d/notify -H \"Content-Type: application/json\" -d '{\"run_id\":\"<RUN_ID>\"}'）" % LISTEN_PORT
        )
        info.setWordWrap(True)
        info.setStyleSheet("color:#555; background:#f4f4f4; padding:6px; border-radius:4px;")
        lay.addWidget(info)

        # 自动监控开关
        self.chk_autopoll = QCheckBox(f"自动监控 GitHub 新构建（每 {GH_POLL_SECONDS}s 轮询）")
        self.chk_autopoll.setChecked(True)
        self.chk_autopoll.toggled.connect(self.toggle_autopoll)
        lay.addWidget(self.chk_autopoll)

        # 手动触发区
        gb = QGroupBox("手动部署")
        gl = QHBoxLayout(gb)
        self.run_id_edit = QLineEdit()
        self.run_id_edit.setPlaceholderText("GitHub Actions RUN ID")
        gl.addWidget(self.run_id_edit, 1)
        self.btn_deploy = QPushButton("开始部署")
        self.btn_deploy.clicked.connect(lambda: self.start_worker(self.run_id_edit.text().strip()))
        gl.addWidget(self.btn_deploy)
        self.btn_clear = QPushButton("清空日志")
        self.btn_clear.clicked.connect(lambda: self.log_view.clear())
        gl.addWidget(self.btn_clear)
        lay.addWidget(gb)

        # 日志区
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)
        lay.addWidget(self.log_view, 1)

        self.log("Kline-TS 自动部署助手已启动")
        self.log(f"通知监听: http://127.0.0.1:{LISTEN_PORT}/notify")
        self.log("使用方法：TRAE 构建成功后 POST /notify，或手动填入 RUN ID 点「开始部署」。")

        # HTTP 服务（后台线程）
        handler = type("H", (NotifyHandler,), {"bridge": self.bridge})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", LISTEN_PORT), handler)
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.log(f"✅ 通知服务已启动: 127.0.0.1:{LISTEN_PORT}")

        # IPA 下载服务（0.0.0.0，iPad 局域网可达；TrollStore 下载源，不依赖 Kline 前台）
        try:
            self.ipa_httpd = ThreadingHTTPServer(("0.0.0.0", IPA_PORT), IPAHandler)
            threading.Thread(target=self.ipa_httpd.serve_forever, daemon=True).start()
            self.log(f"✅ IPA 下载服务已启动: http://{get_lan_ip()}:{IPA_PORT}/Kline.ipa")
        except Exception as e:
            self.log(f"⚠ IPA 下载服务启动失败（{e}），iPad 与电脑需同一局域网才能安装")

        # 自动监控轮询线程
        self.poll_worker = PollWorker()
        self.poll_worker.new_run.connect(self.on_notify)
        self.poll_worker.poll_log.connect(self.log)
        if self.chk_autopoll.isChecked():
            self.poll_worker.start()
            self.log(f"✅ 自动监控已启动：每 {GH_POLL_SECONDS}s 轮询 GitHub 新构建"
                     f"（上次已处理 run={load_last_processed() or '无'}）")

    def log(self, text: str):
        self.log_view.appendPlainText(text)
        sb = self.log_view.verticalScrollBar()
        sb.setValue(sb.maximum())

    def toggle_autopoll(self, checked: bool):
        set_autopoll_on(checked)
        if checked:
            if not self.poll_worker.isRunning():
                self.poll_worker = PollWorker()
                self.poll_worker.new_run.connect(self.on_notify)
                self.poll_worker.poll_log.connect(self.log)
                self.poll_worker.start()
            self.log("✅ 自动监控已开启：每 30s 轮询 GitHub 新构建")
        else:
            self.poll_worker.stop()
            self.log("⏸ 自动监控已关闭（下次 TRAE 推送构建时自动重新开启）")

    def set_autopoll(self, enabled: bool):
        """供 POST /autopoll 与部署开始调用：同步勾选框并启停轮询线程"""
        self.chk_autopoll.setChecked(enabled)   # 触发 toggled → toggle_autopoll

    def on_poll_ctrl(self, deploy_started: bool):
        """部署开始：彻底关闭自动监控（下次 TRAE 推送构建时经 POST /autopoll 重新开启）"""
        if deploy_started and self.chk_autopoll.isChecked():
            self.set_autopoll(False)

    def on_notify(self, run_id: str):
        self.log(f"\n📨 收到通知 run_id={run_id}")
        self.start_worker(run_id)

    def start_worker(self, run_id: str):
        if not run_id:
            self.log("⚠ 未提供 RUN ID")
            return
        if hasattr(self, "worker") and self.worker.isRunning():
            self.log(f"⚠ 正在部署中，忽略新通知 run={run_id}")
            return
        save_last_processed(run_id)
        self.worker = DeployWorker(run_id)
        self.worker.log.connect(self.log)
        self.worker.status.connect(lambda s: (set_status(s), self.status_label.setText(s)))
        self.worker.poll_ctrl.connect(self.on_poll_ctrl)
        self.worker.start()


def main():
    app = QApplication(sys.argv)
    win = MainWindow()
    win.show()
    sys.exit(app.exec_())


if __name__ == "__main__":
    main()
