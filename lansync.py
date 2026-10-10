#!/usr/bin/env python3
r"""Kline 局域网联机同步 · PC 端实现（部署助手扩展，零 Kline 代码改动）。

Kline 的联机同步是「仅拉取」模型（见 Kline/Infrastructure/LANSyncTransfer.swift 文件头），
线上契约 = LANSyncModels.swift（字段名即线上格式）。本模块用 Python 完整复刻两端：

· **PC 作为被拉取方（暴露）**：zeroconf 广播 `_klinesync._tcp`（TXT `id`=本机唯一 ID）
  + HTTP 服务（默认 0.0.0.0:5054），实现对端拉取所需的 4 个端点：
      GET  /sync/status            设备信息 + 6 类内容清单（与 LANSyncSupport.buildSyncInventory 同构）
      POST /sync/request-pair      暴露中 → 200 {"token":...}；未暴露 → 403
      GET  /sync/sha?path=<rel>    文件 sha256（完整性核对）
      GET  /sandbox/<rel>          文件下载（流式，Content-Length 齐备）
  iPad 的 Kline「联机同步 → 扫描一次」即可发现 PC 并按类别拉取（配置类拉完自动热重载）。

· **PC 作为拉取方**：`pull_from_kline()` 按同样 4 步把设备内容拉进模拟沙盒目录
  （配对 → 逐文件下载 → sha256 核对 → 原子替换）。

· **PC 作为推送方**：`push_to_kline()` 把模拟沙盒目录内容推到设备：
  配对 → POST /sync/backup（设备侧先备份将被覆盖的文件）→ PUT /sandbox/<rel> 逐文件上传
  → POST /sync/reload-config（配置类热重载；live 等设备指纹监视/重启生效；main 需重启 App）。

沙盒模拟目录（默认 C:\Users\sunck\home\KlineSandbox，不进 git）：
  结构与设备 Documents 一致：Favorites/ Simulation/ Layouts/ indicator/ formula/
  tdx_live.db(+tdx_live.manifest.json) tdx.db
  首次启动从「内置数据」目录（TrollRestore/sandbox_builtin/）播种；「重置」按钮恢复内置数据，
  「存为内置」把当前沙盒内容固化为新的内置数据。
"""
import hashlib
import json
import os
import shutil
import socket
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import requests
from zeroconf import ServiceInfo, Zeroconf

_HERE = Path(__file__).resolve().parent

# ============ 配置 ============
SANDBOX_DIR = Path(r"C:\Users\sunck\home\KlineSandbox")     # 模拟沙盒（资源管理器直接编辑）
BUILTIN_DIR = _HERE / "sandbox_builtin"                     # 内置数据（重置源，不进 git）
SYNC_PORT = 5054            # PC 暴露服务端口（避开 5051 设备/转发、5052 助手通知、5053 IPA）
SYNC_SERVICE = "_klinesync._tcp.local."
DEVICE_NAME = "PC-KlineSync"
APP_NAME = "Kline-Deploy"
CHUNK = 1 << 20             # 流式读写块 1MB（1.4GB 主库内存平稳）

# 6 类内容（与 LANSyncCategory.rawValues 一致；顺序即 UI 展示顺序）
CATEGORIES = ["favorites", "sim", "layouts", "indicators", "live", "main"]
CATEGORY_TITLES = {
    "favorites": "自选", "sim": "模拟交易", "layouts": "页面布局",
    "indicators": "指标公式", "live": "增量库", "main": "主库",
}
# 拉取/推送后设备侧可热重载的配置类
CONFIG_CATEGORIES = ["favorites", "sim", "layouts", "indicators"]

# 本机唯一 ID（防 Kline 自扫；持久化，重启不变）
_DEVID_FILE = _HERE / "lansync_devid.txt"


def device_unique_id() -> str:
    try:
        if _DEVID_FILE.exists():
            v = _DEVID_FILE.read_text(encoding="utf-8").strip()
            if v:
                return v
        devid = f"pc-{uuid.uuid4().hex[:16]}"
        _DEVID_FILE.write_text(devid, encoding="utf-8")
        return devid
    except Exception:
        return "pc-unknown"


# ============ 沙盒模拟目录管理 ============

def ensure_sandbox_seeded(log=print) -> None:
    """沙盒目录不存在/为空 → 从内置数据播种（内置缺失则只建空目录，不报错）"""
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)
    if any(SANDBOX_DIR.iterdir()):
        return
    if BUILTIN_DIR.is_dir() and any(BUILTIN_DIR.iterdir()):
        shutil.copytree(BUILTIN_DIR, SANDBOX_DIR, dirs_exist_ok=True)
        log(f"✅ 沙盒目录已从内置数据播种: {SANDBOX_DIR}")
    else:
        log(f"ℹ 沙盒目录为空且无内置数据，先「从 Kline 拉取」或「存当前为内置」: {SANDBOX_DIR}")


def reset_to_builtin(log=print) -> None:
    """清空沙盒目录并恢复为内置数据"""
    if not BUILTIN_DIR.is_dir() or not any(BUILTIN_DIR.iterdir()):
        raise RuntimeError(f"内置数据目录为空: {BUILTIN_DIR}")
    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
    shutil.copytree(BUILTIN_DIR, SANDBOX_DIR)
    log(f"✅ 沙盒目录已重置为内置数据: {SANDBOX_DIR}")


def save_as_builtin(log=print) -> None:
    """把当前沙盒内容固化为新的内置数据（下次「重置」恢复到这份）"""
    if not SANDBOX_DIR.is_dir() or not any(SANDBOX_DIR.iterdir()):
        raise RuntimeError(f"沙盒目录为空，无可固化内容: {SANDBOX_DIR}")
    if BUILTIN_DIR.exists():
        shutil.rmtree(BUILTIN_DIR)
    shutil.copytree(SANDBOX_DIR, BUILTIN_DIR,
                    ignore=shutil.ignore_patterns("*.part"))
    log(f"✅ 当前沙盒已固化为内置数据: {BUILTIN_DIR}")


# ============ 内容清单（与 LANSyncSupport.buildSyncInventory 同构） ============

def _entry(sandbox: Path, rel: str):
    p = sandbox / rel
    if not p.is_file():
        return None
    return {"path": rel, "size": p.stat().st_size, "mod": int(p.stat().st_mtime)}


def _dir_entries(sandbox: Path, root: str, ext: str, recursive: bool) -> list:
    base = sandbox / root
    if not base.is_dir():
        return []
    out = []
    it = base.rglob(f"*.{ext}") if recursive else base.glob(f"*.{ext}")
    for p in it:
        if p.is_file():
            out.append({"path": p.relative_to(sandbox).as_posix(),
                        "size": p.stat().st_size, "mod": int(p.stat().st_mtime)})
    return out


def _favorites_children(sandbox: Path):
    """favorites.json 分组子项（解析失败/缺失 → None，对端退回整类语义）"""
    p = sandbox / "Favorites" / "favorites.json"
    if not p.is_file():
        return None
    try:
        root = json.loads(p.read_text(encoding="utf-8"))
        groups = root.get("groups") or []
        return [{"key": g.get("name", ""), "name": g.get("name", ""), "size": 0,
                 "count": len(g.get("manualFiles") or g.get("manualMetaIDs") or [])}
                for g in groups]
    except Exception:
        return None


def build_inventory(sandbox: Path = SANDBOX_DIR) -> list:
    """6 类清单；结构/字段名与 LANSyncItem Codable 强一致（key/files/children）"""
    items = []

    fav = _entry(sandbox, "Favorites/favorites.json")
    items.append({"key": "favorites", "files": [fav] if fav else [],
                  "children": _favorites_children(sandbox)})

    sim = _entry(sandbox, "Simulation/sim.json")
    items.append({"key": "sim", "files": [sim] if sim else [], "children": None})

    lay = _dir_entries(sandbox, "Layouts", "json", recursive=False)
    items.append({"key": "layouts", "files": lay,
                  "children": [{"key": e["path"],
                                "name": Path(e["path"]).stem,
                                "size": e["size"], "count": None} for e in lay]})

    ind = (_dir_entries(sandbox, "indicator", "tdx", recursive=True)
           + _dir_entries(sandbox, "formula/picker", "tdx", recursive=False)
           + _dir_entries(sandbox, "formula/strategy", "tdx", recursive=False))
    ind.sort(key=lambda e: e["path"])
    items.append({"key": "indicators", "files": ind,
                  "children": [{"key": e["path"],
                                "name": "/".join(Path(e["path"]).parts[1:]),
                                "size": e["size"], "count": None} for e in ind]})

    live = [e for e in (_entry(sandbox, "tdx_live.db"),
                        _entry(sandbox, "tdx_live.manifest.json")) if e]
    items.append({"key": "live", "files": live, "children": None})

    main = _entry(sandbox, "tdx.db")
    items.append({"key": "main", "files": [main] if main else [], "children": None})
    return items


# ============ 通用 HTTP 细节 ============

def _encode_rel(rel: str) -> str:
    """相对路径逐段 percent 编码（RFC 3986 unreserved），"/" 不编码——与 LANSyncTransfer 一致"""
    allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
    return "/".join("".join(c if c in allowed else
                            "".join(f"%{b:02X}" for b in c.encode("utf-8"))
                            for c in seg)
                    for seg in rel.split("/"))


def stream_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _safe_resolve(sandbox: Path, rel: str):
    """把 rel 解析到沙盒内绝对路径（防穿越）；返回 Path 或 None"""
    try:
        root = sandbox.resolve()
        target = (root / rel).resolve()
        if target == root or root in target.parents:
            return target
    except Exception:
        pass
    return None


# ============ PC 暴露服务（被拉取方） ============

class _ExposeHandler(BaseHTTPRequestHandler):
    # 类属性（start_server 里注入）
    state = None   # ExposeState

    # -- 工具 --
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self) -> bool:
        t = self.headers.get("X-Kline-Pair", "")
        return bool(t) and self.state.has_token(t)

    def log_message(self, *args):
        pass

    # -- 路由 --
    def do_GET(self):
        st = self.state
        u = urlparse(self.path)
        if u.path == "/":
            self._json({"device": st.device_info(), "exposed": st.exposed})
        elif u.path == "/sync/status":
            self._json({"device": st.device_info(),
                        "items": build_inventory(st.sandbox),
                        "exposed": st.exposed})
        elif u.path == "/sync/sha":
            if not self._token_ok():
                self._json({"error": "not exposed"}, 403)
                return
            rel = (parse_qs(u.query).get("path") or [""])[0]
            target = _safe_resolve(st.sandbox, rel) if rel else None
            if not target or not target.is_file():
                self._json({"error": "not found"}, 404)
                return
            self._json({"path": rel, "sha256": stream_sha256(target)})
        elif u.path in ("/sandbox", "/sandbox/"):
            if not self._token_ok():
                self._json({"error": "not exposed"}, 403)
                return
            self._json([{"name": p.name, "size": p.stat().st_size, "dir": p.is_dir()}
                        for p in sorted(st.sandbox.iterdir())] if st.sandbox.is_dir() else [])
        elif u.path.startswith("/sandbox/"):
            if not self._token_ok():
                self._json({"error": "not exposed"}, 403)
                return
            from urllib.parse import unquote
            rel = unquote(u.path[len("/sandbox/"):])
            target = _safe_resolve(st.sandbox, rel)
            if not target or not target.is_file():
                self._json({"error": "not found"}, 404)
                return
            size = target.stat().st_size
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(size))
            self.end_headers()
            with open(target, "rb") as f:
                while True:
                    chunk = f.read(CHUNK)
                    if not chunk:
                        break
                    self.wfile.write(chunk)
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        st = self.state
        u = urlparse(self.path)
        if u.path == "/sync/request-pair":
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                from_name = str(body.get("from", "")).strip()
            except Exception:
                from_name = ""
            if not st.exposed:
                st.log(f"[-] 配对被拒（未暴露）: {from_name or '未知来源'}")
                self._json({"error": "denied"}, 403)
                return
            token = st.issue_token()
            st.log(f"[+] 配对成功: {from_name or '未知来源'}（已签发会话 token）")
            self._json({"token": token})
        else:
            self._json({"error": "not found"}, 404)


class ExposeState:
    """暴露服务的共享状态：暴露开关 / 会话 token / 日志回调"""

    def __init__(self, sandbox: Path, log=print):
        self.sandbox = sandbox
        self.log = log
        self.exposed = False
        self._tokens = set()
        self._lock = threading.Lock()

    def device_info(self) -> dict:
        return {"name": DEVICE_NAME, "appVersion": "1.0", "app": APP_NAME,
                "id": device_unique_id()}

    def has_token(self, t: str) -> bool:
        with self._lock:
            return t in self._tokens

    def issue_token(self) -> str:
        t = uuid.uuid4().hex
        with self._lock:
            self._tokens.add(t)
        return t

    def revoke_all(self):
        with self._lock:
            self._tokens.clear()


class ExposeServer:
    """暴露服务 = HTTP(5054) + mDNS 广播；expose(True/False) 由助手「暴露」开关驱动"""

    def __init__(self, log=print):
        self.log = log
        self.state = ExposeState(SANDBOX_DIR, log)
        self.httpd = None
        self.zc = None
        self.info = None
        self._thread = None

    def start(self):
        if self.httpd is not None:
            return
        handler = type("H", (_ExposeHandler,), {"state": self.state})
        try:
            self.httpd = ThreadingHTTPServer(("0.0.0.0", SYNC_PORT), handler)
        except OSError as e:
            self.log(f"❌ 联机同步服务启动失败（端口 {SYNC_PORT} 被占）: {e}")
            self.httpd = None
            return
        self._thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self._thread.start()
        self.log(f"✅ 联机同步服务已启动: http://{_lan_ip()}:{SYNC_PORT}/（手动直连填 IP:5054）")

    def expose(self, on: bool):
        """开/关暴露（mDNS 广播 + 授权语义与 Kline「暴露即授权」一致；关 = 吊销全部 token）"""
        if on:
            self.start()
            if self.httpd is None:
                return
            if self.zc is None:
                self.zc = Zeroconf()
            ip = _lan_ip()
            self.info = ServiceInfo(
                SYNC_SERVICE,
                name=f"{DEVICE_NAME}.{SYNC_SERVICE}",
                port=SYNC_PORT,
                properties={"id": device_unique_id()},
                parsed_addresses=[ip],
            )
            try:
                self.zc.register_service(self.info)
                self.state.exposed = True
                self.log(f"📡 已暴露: {DEVICE_NAME} @ {ip}:{SYNC_PORT}"
                         f"（iPad 联机同步 → 扫描一次 即可发现）")
            except Exception as e:
                self.state.exposed = False
                self.log(f"❌ mDNS 广播失败（仍可手动直连 IP:{SYNC_PORT}）: {e}")
        else:
            self.state.exposed = False
            self.state.revoke_all()
            if self.zc is not None and self.info is not None:
                try:
                    self.zc.unregister_service(self.info)
                except Exception:
                    pass
            self.log("⏸ 已取消暴露（广播停止，会话 token 全部吊销）")


def _lan_ip() -> str:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


# ============ 拉取客户端（PC ← Kline） ============

def _peer_session():
    s = requests.Session()
    s.trust_env = False          # 禁系统代理：局域网直连（与 Kline 端 connectionProxyDictionary=[:] 同理）
    return s


def fetch_peer_status(host: str, port: int, timeout=6) -> dict:
    """GET /sync/status；非 Kline（无 device/items 键）抛错"""
    s = _peer_session()
    r = s.get(f"http://{host}:{port}/sync/status", timeout=timeout)
    r.raise_for_status()
    obj = r.json()
    if "device" not in obj or "items" not in obj:
        raise RuntimeError("对端不是可同步的 Kline（/sync/status 缺 device/items）")
    return obj


def pull_from_kline(host: str, port: int, categories: list,
                    log=print, pipeline: bool = False) -> dict:
    """把设备上选中类别的内容拉进模拟沙盒目录。

    pipeline=True：带 X-Kline-Client: pipeline 头走 USB 转发通道（首次播种用，跳过配对）；
    返回 {category: {"files": n, "bytes": b, "seconds": s}}；全部失败抛异常。
    """
    t_all = time.time()
    status = fetch_peer_status(host, port)
    if not status.get("exposed", True) and not pipeline:
        raise RuntimeError("对端已取消暴露，无法拉取（在 Kline 联机同步页重新打开暴露）")
    dev = status.get("device", {}).get("name", "?")
    items = {it["key"]: it for it in status.get("items", [])}

    # 计划：类别 → 文件清单（对端该类为空则注明跳过）
    plan = {}
    for cat in categories:
        files = (items.get(cat) or {}).get("files") or []
        if files:
            plan[cat] = files
    skipped = [c for c in categories if c not in plan]
    total = sum(f["size"] for c in plan for f in plan[c])
    log(f"计划: 拉取 {sum(len(f) for f in plan.values())} 文件 / {total / 1048576:.1f} MB ← {dev}"
        + (f"（对端无此内容: {','.join(skipped)}）" if skipped else ""))

    headers = {}
    if pipeline:
        headers["X-Kline-Client"] = "pipeline"
    else:
        req = {"from": DEVICE_NAME, "items": categories}
        r = _peer_session().post(f"http://{host}:{port}/sync/request-pair",
                                 json=req, timeout=70)
        if r.status_code == 403:
            raise RuntimeError("对端未开启暴露，无法配对")
        if r.status_code != 200:
            raise RuntimeError(f"配对失败 HTTP {r.status_code}")
        token = r.json().get("token", "")
        if not token:
            raise RuntimeError("配对响应格式异常")
        headers["X-Kline-Pair"] = token

    s = _peer_session()
    result = {}
    for cat, files in plan.items():
        t0 = time.time()
        n = bytes_done = 0
        for f in files:
            rel, size = f["path"], f["size"]
            dst = SANDBOX_DIR / rel
            part = dst.with_suffix(dst.suffix + ".part")
            dst.parent.mkdir(parents=True, exist_ok=True)
            try:
                _pull_one(s, host, port, rel, part, headers, log)
                if stream_sha256(part) != _peer_sha(s, host, port, rel, headers):
                    raise RuntimeError(f"sha256 校验不符: {rel}")
                os.replace(part, dst)
            finally:
                if part.exists():
                    try:
                        part.unlink()
                    except OSError:
                        pass
            bytes_done += size
            n += 1
            log(f"  ⬇ [{CATEGORY_TITLES.get(cat, cat)}] {rel} ({size / 1048576:.1f} MB)")
        result[cat] = {"files": n, "bytes": bytes_done, "seconds": time.time() - t0}
        speed = bytes_done / result[cat]["seconds"] / 1048576 if result[cat]["seconds"] else 0
        log(f"✅ [{CATEGORY_TITLES.get(cat, cat)}] {n} 文件 / "
            f"{bytes_done / 1048576:.1f} MB / {result[cat]['seconds']:.1f}s（{speed:.1f} MB/s）")
    log(f"拉取完成，总耗时 {time.time() - t_all:.1f}s → {SANDBOX_DIR}")
    return result


def _pull_one(s: requests.Session, host: str, port: int, rel: str, part: Path,
              headers: dict, log) -> None:
    """流式下载单文件到 .part（失败重试 ≤2 次，指数退避 1s/2s——与 LANSyncTransfer 同策略）"""
    url = f"http://{host}:{port}/sandbox/{_encode_rel(rel)}"
    last = None
    for attempt in range(3):
        if attempt:
            time.sleep(1 * attempt)
            log(f"  ↻ 重试 {attempt}/2: {rel}")
        try:
            if part.exists():
                part.unlink()
        except OSError:
            pass
        try:
            with s.get(url, headers=headers, stream=True, timeout=(10, 120)) as r:
                if r.status_code != 200:
                    raise RuntimeError(f"HTTP {r.status_code}")
                with open(part, "wb") as f:
                    for chunk in r.iter_content(CHUNK):
                        if chunk:
                            f.write(chunk)
            return
        except Exception as e:
            last = e
    raise RuntimeError(f"下载失败: {rel}: {last}")


def _peer_sha(s: requests.Session, host: str, port: int, rel: str, headers: dict) -> str:
    r = s.get(f"http://{host}:{port}/sync/sha", params={"path": rel},
              headers=headers, timeout=(10, 300))
    if r.status_code != 200:
        raise RuntimeError(f"对端 sha 查询失败 HTTP {r.status_code}: {rel}")
    sha = r.json().get("sha256", "")
    if len(sha) != 64:
        raise RuntimeError(f"对端 sha 响应格式异常: {rel}")
    return sha.lower()


# ============ 推送客户端（PC → Kline） ============

def push_to_kline(host: str, port: int, categories: list, log=print) -> dict:
    """把模拟沙盒目录中选中类别的内容推到设备（备份 → 逐文件 PUT → 配置类热重载）。

    前提：设备 Kline 前台且已打开「暴露」（配对签发 token）。
    返回 {category: {"files": n, "bytes": b, "seconds": s}}。
    """
    t_all = time.time()
    status = fetch_peer_status(host, port)
    dev = status.get("device", {}).get("name", "?")
    if not status.get("exposed", False):
        raise RuntimeError("对端未开启暴露，无法推送（在 Kline 联机同步页打开「暴露」）")

    # 计划：沙盒里选中类别的现有文件（路径口径与 inventory 一致）
    inv = {it["key"]: it for it in build_inventory(SANDBOX_DIR)}
    plan = {}
    for cat in categories:
        files = [e["path"] for e in (inv.get(cat) or {}).get("files") or []]
        if files:
            plan[cat] = files
    if not plan:
        raise RuntimeError("沙盒目录里选中类别没有任何文件可推")
    total = sum((SANDBOX_DIR / rel).stat().st_size for c in plan for rel in plan[c])
    log(f"计划: 推送 {sum(len(v) for v in plan.values())} 文件 / {total / 1048576:.1f} MB → {dev}")

    r = _peer_session().post(f"http://{host}:{port}/sync/request-pair",
                             json={"from": DEVICE_NAME, "items": categories}, timeout=70)
    if r.status_code == 403:
        raise RuntimeError("对端未开启暴露，无法配对")
    if r.status_code != 200:
        raise RuntimeError(f"配对失败 HTTP {r.status_code}")
    token = r.json().get("token", "")
    if not token:
        raise RuntimeError("配对响应格式异常")
    headers = {"X-Kline-Pair": token, "X-Kline-Client": "pipeline"}

    # ① 设备侧覆盖前备份（白名单：Favorites/Simulation/Layouts/ 前缀 + 三张库文件 + indicator/formula 目录）
    paths, dirs = [], []
    for cat, files in plan.items():
        if cat == "indicators":
            dirs += ["indicator", "formula"]
        else:
            paths += files
    paths, dirs = sorted(set(paths)), sorted(set(dirs))
    if paths or dirs:
        r = _peer_session().post(f"http://{host}:{port}/sync/backup",
                                 json={"paths": paths, "dirs": dirs},
                                 headers=headers, timeout=300)
        if r.status_code == 200:
            log(f"✅ 设备侧已备份原文件: {r.json().get('backupDir', '?')}（{r.json().get('backedUp', 0)} 项）")
        else:
            log(f"⚠ 设备侧备份失败 HTTP {r.status_code}（继续推送，设备旧文件将直接被覆盖）")

    # ② 逐文件 PUT /sandbox/<rel>
    s = _peer_session()
    result = {}
    for cat, files in plan.items():
        t0 = time.time()
        n = bytes_done = 0
        for rel in files:
            src = SANDBOX_DIR / rel
            size = src.stat().st_size
            url = f"http://{host}:{port}/sandbox/{_encode_rel(rel)}"
            with open(src, "rb") as f:
                r = s.put(url, data=f, headers={**headers, "Content-Length": str(size)},
                          timeout=(10, 600))
            if r.status_code != 200:
                raise RuntimeError(f"上传失败 HTTP {r.status_code}: {rel}: {r.text[:120]}")
            bytes_done += size
            n += 1
            log(f"  ⬆ [{CATEGORY_TITLES.get(cat, cat)}] {rel} ({size / 1048576:.1f} MB)")
        result[cat] = {"files": n, "bytes": bytes_done, "seconds": time.time() - t0}
        speed = bytes_done / result[cat]["seconds"] / 1048576 if result[cat]["seconds"] else 0
        log(f"✅ [{CATEGORY_TITLES.get(cat, cat)}] {n} 文件 / "
            f"{bytes_done / 1048576:.1f} MB / {result[cat]['seconds']:.1f}s（{speed:.1f} MB/s）")

    # ③ 配置类热重载（设备侧 /sync/reload-config；live 由设备指纹监视/重启生效；main 需重启 App）
    reload_scopes = [c for c in categories if c in CONFIG_CATEGORIES and c in plan]
    if reload_scopes:
        r = s.post(f"http://{host}:{port}/sync/reload-config",
                   json={"scopes": reload_scopes}, headers=headers, timeout=60)
        if r.status_code == 200:
            log(f"✅ 设备侧配置已热重载: {','.join(reload_scopes)}")
        else:
            log(f"⚠ 热重载失败 HTTP {r.status_code}（重启 App 后生效）")
    if "live" in plan:
        log("ℹ 增量库将由设备指纹监视（≤5 分钟）或重启 App 后生效")
    if "main" in plan:
        log("ℹ 主库已替换，需重启设备上的 App 生效")
    log(f"推送完成，总耗时 {time.time() - t_all:.1f}s")
    return result
