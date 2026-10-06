"""浏览器登录导入（CDP）。

流程：启动/连接一个带远程调试端口的浏览器 → 用户在窗口里用 Google/GitHub 登录
miora.design → 轮询读取 localStorage 的 ``authToken`` / ``refreshToken`` /
``miora__user_info`` → 导入账号池。

设计要点（与 meshy 的区别）：
  * Miora 的凭证**只在 localStorage**（cookie 是副本），所以读 localStorage 即可，
    不需要 meshy 那种 device-id 匹配。
  * 支持两种模式：
      - ``launch``：自己拉起一个独立 profile 的浏览器（干净，不碰用户日常号）
      - ``attach``：连接用户已开着的调试端口（如 9366/9367），在其中登录
  * **绝不读取密码**：只读登录后 Miora 自己写入 localStorage 的值。
"""

from __future__ import annotations

import base64
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import threading
import tempfile
import time
import httpx

import db
import miora_client as mc

# ---------------------------------------------------------------- 浏览器探测

IS_WIN = sys.platform.startswith("win")

CHROME_PATHS = {
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "~/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "~/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    ],
    "win32": [
        # 优先用户级安装（无需管理员权限），再试系统级
        r"~\AppData\Local\Google\Chrome\Application\chrome.exe",
        r"~\AppData\Local\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    ],
    "linux": [
        "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium", "/usr/bin/chromium-browser",
        "/snap/bin/chromium", "~/.local/bin/google-chrome",
    ],
}


def _norm(p: str) -> str:
    return os.path.expanduser(os.path.expandvars(p))


def port_in_use(port: int) -> bool:
    """端口是否已被占用（避免 launch 时撞车）。"""
    import socket as _s
    with _s.socket(_s.AF_INET, _s.SOCK_STREAM) as sk:
        sk.settimeout(0.4)
        return sk.connect_ex(("127.0.0.1", port)) == 0

START_URL = "https://miora.design/"


def find_browser(custom: str = "") -> str | None:
    if custom:
        p = _norm(custom)
        if os.path.exists(p):
            return p
    # 注意：os.name 在 macOS 上是 "posix"，必须用 sys.platform 区分 darwin
    plat = sys.platform
    key = "darwin" if plat.startswith("darwin") else ("win32" if plat.startswith("win") else "linux")
    for p in CHROME_PATHS.get(key, CHROME_PATHS["linux"]):
        p = _norm(p)
        if os.path.exists(p):
            return p
    return None


def cdp_version(port: int) -> dict | None:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/version", timeout=1.5)
        return r.json() if r.status_code == 200 else None
    except Exception:
        return None


def cdp_alive(port: int) -> bool:
    return cdp_version(port) is not None


# ---------------------------------------------------------------- 状态

# 一个进行中的登录会话：{ port, proc, profile_dir, status, accounts, started_at }
_SESSIONS: dict[str, dict] = {}
DEFAULT_TTL = 600  # 10 分钟未完成即视为过期


def _gc() -> None:
    now = time.time()
    for key in list(_SESSIONS):
        s = _SESSIONS[key]
        if now - s["started_at"] > DEFAULT_TTL:
            close_session(key)


def _new_profile_dir() -> str:
    return tempfile.mkdtemp(prefix="miora-login-profile-")


def launch(port: int = 9222, path: str = "", profile_dir: str = "") -> dict:
    """拉起一个独立 profile 的浏览器（干净，不影响用户日常号）。"""
    _gc()
    exe = find_browser(path)
    if not exe:
        raise RuntimeError("未找到 Chrome/Edge/Chromium，请手动填写浏览器路径")

    if port_in_use(port):
        # 该端口已经有东西在跑 —— 那就直接 attach 它
        v = cdp_version(port)
        if v:
            sid = f"attach-{port}"
            _SESSIONS[sid] = {
                "mode": "attach", "port": port, "proc": None,
                "profile_dir": "", "owned_profile": False,
                "started_at": time.time(), "status": "ready",
            }
            return {"session_id": sid, "status": "ready", "browser": v.get("Browser", ""),
                    "port": port, "start_url": "", "note": "端口已被占用，已改为 attach 现有浏览器"}
        raise RuntimeError(f"端口 {port} 已被其他程序占用（且不是浏览器调试端口）")

    profile_dir = profile_dir or _new_profile_dir()
    owned = not _SESSION_PROFILES.get(profile_dir)
    if owned:
        _SESSION_PROFILES[profile_dir] = True

    args = [
        exe,
        f"--remote-debugging-port={port}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run", "--no-default-browser-check",
        "--remote-allow-origins=*",
        START_URL,
    ]
    popen_kw = {"stdout": subprocess.DEVNULL, "stderr": subprocess.DEVNULL}
    if IS_WIN:
        # 避免每次拉起都闪一个黑色控制台窗口
        popen_kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    proc = subprocess.Popen(args, **popen_kw)

    sid = f"launch-{port}"
    _SESSIONS[sid] = {
        "mode": "launch", "port": port, "proc": proc,
        "profile_dir": profile_dir, "owned_profile": owned,
        "started_at": time.time(), "status": "waiting",
    }
    # 等端口起来
    for _ in range(40):
        if cdp_alive(port):
            _SESSIONS[sid]["status"] = "ready"
            break
        time.sleep(0.25)
    else:
        _SESSIONS[sid]["status"] = "error"
        _SESSIONS[sid]["error"] = "浏览器启动了但调试端口没起来"

    v = cdp_version(port) or {}
    return {
        "session_id": sid, "status": _SESSIONS[sid]["status"],
        "browser": v.get("Browser", ""), "port": port,
        "profile_dir": profile_dir, "start_url": START_URL,
    }


_SESSION_PROFILES: dict[str, bool] = {}
_WATCHERS: dict[str, dict] = {}


def attach(port: int) -> dict:
    """连接一个已经在运行的调试端口。"""
    _gc()
    if not cdp_alive(port):
        raise RuntimeError(f"端口 {port} 没有可连接的浏览器（需以 --remote-debugging-port={port} 启动）")
    sid = f"attach-{port}"
    _SESSIONS[sid] = {
        "mode": "attach", "port": port, "proc": None,
        "profile_dir": "", "owned_profile": False,
        "started_at": time.time(), "status": "ready",
    }
    v = cdp_version(port) or {}
    return {"session_id": sid, "status": "ready", "browser": v.get("Browser", ""),
            "port": port, "start_url": START_URL}


def close_session(session_id: str) -> dict:
    s = _SESSIONS.pop(session_id, None)
    if not s:
        return {"ok": False, "error": "会话不存在或已过期"}
    if s.get("proc"):
        try:
            s["proc"].terminate()
            s["proc"].wait(timeout=5)
        except Exception:
            try:
                s["proc"].kill()
            except Exception:
                pass
    if s.get("owned_profile") and s.get("profile_dir"):
        shutil.rmtree(s["profile_dir"], ignore_errors=True)
    return {"ok": True}


_LAST_READ_ERROR: str | None = None


# ---------------------------------------------------------------- 打开页面


def open_start_url(port: int, url: str = START_URL, new_tab: bool = True) -> dict:
    """在已连接的浏览器里打开 miora 页面。

    attach 模式只是"连接"，本身不会打开任何页面 —— 必须显式调这个，
    否则用户看不到要登录的页面。CDP 的 /json/new 已废弃，改用 HTTP
    ``PUT /json/new?url=``。
    """
    try:
        r = httpx.put(f"http://127.0.0.1:{port}/json/new?{url}", timeout=5.0)
        if r.status_code in (200, 201):
            return {"ok": True, "url": url}
    except Exception:
        pass
    # 回退：找一个已有 page target，用 Page.navigate
    for t in _cdp_targets(port):
        if t.get("type") == "page" and t.get("webSocketDebuggerUrl"):
            try:
                with _raw_ws(t["webSocketDebuggerUrl"]) as sock:
                    sock.send(_ws_frame(json.dumps({
                        "id": 1, "method": "Page.navigate", "params": {"url": url}
                    }).encode()))
                    sock.recv()
                return {"ok": True, "url": url, "via": "Page.navigate"}
            except Exception:
                continue
    return {"ok": False, "error": f"无法在端口 {port} 打开页面"}


# ---------------------------------------------------------------- 抓取凭证


def _cdp_targets(port: int) -> list[dict]:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/json/list", timeout=2.0)
        return r.json() if r.status_code == 200 else []
    except Exception:
        return []


def grab_credentials(port: int) -> dict | None:
    """在浏览器里找 miora.design 页面并读 localStorage。

    返回 {authToken, refreshToken, userInfo} 或 None（还没登录）。
    """
    # ★ 必须遍历**所有** miora 标签页，不能在第一个就 return：
    #   open_start_url() 刚新开的那个标签页可能还在加载，localStorage 是空的，
    #   直接 return None 会漏掉其它已登录的标签页。
    last_err = None
    for t in _cdp_targets(port):
        if t.get("type") != "page":
            continue
        if "miora.design" not in (t.get("url") or ""):
            continue
        ws_url = t.get("webSocketDebuggerUrl")
        if not ws_url:
            continue
        try:
            creds = _read_localstorage(ws_url)
            if creds:
                return creds
        except Exception as e:
            last_err = e
    global _LAST_READ_ERROR
    if last_err:
        _LAST_READ_ERROR = str(last_err)[:160]
    return None


def last_read_error() -> str | None:
    return _LAST_READ_ERROR


def _read_localstorage(ws_url: str) -> dict | None:
    """用最简 WebSocket 客户端跑 CDP Runtime.evaluate（不引第三方依赖）。"""
    with _raw_ws(ws_url) as sock:
        sock.send(_ws_frame(json.dumps({
            "id": 1, "method": "Runtime.evaluate",
            "params": {
                "expression": "JSON.stringify({a:localStorage.getItem('authToken'),"
                              "r:localStorage.getItem('refreshToken'),"
                              "u:localStorage.getItem('miora__user_info')})",
                "returnByValue": True,
            },
        }).encode()))

        for _ in range(12):
            try:
                payload = sock.recv()
            except Exception:
                return None
            if payload is None:
                return None
            try:
                msg = json.loads(payload)
            except Exception:
                continue
            if msg.get("id") != 1:
                continue
            res = (msg.get("result") or {}).get("result") or {}
            val = res.get("value")
            if not val:
                return None
            d = json.loads(val)
            if not d.get("a"):
                return None
            try:
                ui = json.loads(d.get("u") or "{}")
            except Exception:
                ui = {}
            return {"authToken": d["a"], "refreshToken": d.get("r"), "userInfo": ui}
    return None


class _raw_ws:
    """极简 WebSocket 客户端（只需 text frame + 掩码发送）。"""

    def __init__(self, url: str):
        self.url = url
        self.sock = None

    def __enter__(self):
        from urllib.parse import urlparse
        u = urlparse(self.url)
        s = socket.create_connection((u.hostname, u.port or 80), timeout=6)
        key = base64.b64encode(os.urandom(16)).decode()
        path = u.path + (("?" + u.query) if u.query else "")
        req = (
            f"GET {path} HTTP/1.1\r\nHost: {u.hostname}:{u.port}\r\n"
            "Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        )
        s.sendall(req.encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(1)
            if not chunk:
                raise RuntimeError("WebSocket 握手失败")
            buf += chunk
        self.sock = s
        return self

    def recv(self) -> bytes | None:
        """读一帧（服务端→客户端不加掩码）。"""
        hdr = _recv_exact(self.sock, 2)
        if not hdr:
            return None
        b2 = hdr[1]
        ln = b2 & 0x7F
        if ln == 126:
            h = _recv_exact(self.sock, 2)
            if not h:
                return None
            ln = struct.unpack(">H", h)[0]
        elif ln == 127:
            h = _recv_exact(self.sock, 8)
            if not h:
                return None
            ln = struct.unpack(">Q", h)[0]
        if b2 & 0x80:                       # 掩码帧
            m = _recv_exact(self.sock, 4)
            if not m:
                return None
        data = _recv_exact(self.sock, ln)
        return data or None

    def __exit__(self, *a):
        try:
            self.sock.close()
        except Exception:
            pass

    def send(self, frame: bytes):
        """发送一个**已编码**的帧（用 _ws_frame() 生成，勿重复掩码）。"""
        self.sock.sendall(frame)


def _recv_exact(sock, n: int) -> bytes:
    buf = b""
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return b""
        buf += chunk
    return buf


def _ws_frame(data: bytes) -> bytes:
    mask = os.urandom(4)
    n = len(data)
    if n < 126:
        hdr = struct.pack("!BB", 0x81, 0x80 | n)
    elif n < 65536:
        hdr = struct.pack("!BBH", 0x81, 0x80 | 126, n)
    else:
        hdr = struct.pack("!BBQ", 0x81, 0x80 | 127, n)
    return hdr + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data))


# ---------------------------------------------------------------- 导入


def import_from_browser(
    port: int,
    label: str = "",
    auto_import: bool = True,
    persist: bool = True,
) -> dict:
    """抓凭证 → 校验余额 → 写入账号池。"""
    creds = grab_credentials(port)
    if not creds:
        return {"ok": False, "error": "还没抓到凭证——请先在浏览器窗口里登录 miora.design"}

    ui = creds.get("userInfo") or {}
    uid = ui.get("userId")
    if not uid:
        return {"ok": False, "error": "抓到 authToken 但没有 userId（页面可能没完全加载）"}

    credits = None
    credit_total = None
    plan = None
    try:
        d = mc.get_credits(creds["authToken"])
        credit_total = float(d["total_amount"])
        credits = round(credit_total - float(d["used_amount"]), 4)
    except Exception as e:
        return {"ok": False,
                "error": f"凭证无效（余额查询失败）：{e}。可能 token 已过期或不是有效登录态"}

    name = ui.get("nickname") or ui.get("username") or ""
    email = ui.get("email") or ""
    default_label = f"{name} <{email}>".strip() or f"user_{uid}"

    if not persist:
        return {"ok": True, "persisted": False, "userId": uid, "label": label or default_label,
                "credits": credits, "credit_total": credit_total,
                "email": email, "country": (ui.get("settings") or {}).get("country")}

    db.upsert_account(uid, creds["authToken"], creds.get("refreshToken"),
                      label=label or default_label, credits=credits,
                      credit_total=credit_total, plan=plan,
                      country=(ui.get("settings") or {}).get("country"))
    db.update_account(uid, status="active")
    return {"ok": True, "persisted": True, "userId": uid,
            "label": label or default_label, "credits": credits,
            "credit_total": credit_total, "email": email,
            "country": (ui.get("settings") or {}).get("country")}


def watch_and_import(port: int, label: str = "", interval: float = 3.0,
                      timeout: float = 300.0) -> dict:
    """后台线程：轮询直到检测到登录态，自动导入。

    解决"登录完还要再点一次按钮"的麻烦。返回立即，导入在后台完成。
    同一端口重复调用不会重复导入。
    """
    key = f"watch:{port}"
    if key in _WATCHERS:
        return {"ok": True, "watching": True, "already": True,
                "message": f"端口 {port} 已在监听中"}

    _WATCHERS[key] = {"port": port, "label": label, "started": time.time(),
                      "state": "watching", "result": None}

    def _run():
        deadline = time.time() + timeout
        while time.time() < deadline:
            if key not in _WATCHERS:            # 被取消
                return
            try:
                if grab_credentials(port):
                    r = import_from_browser(port, label=label, persist=True)
                    if r.get("ok"):
                        _WATCHERS[key]["state"] = "imported"
                        _WATCHERS[key]["result"] = r
                        _WATCHERS[key]["finished"] = time.time()
                        return
            except Exception as e:
                _WATCHERS[key]["last_error"] = str(e)[:120]
            time.sleep(interval)
        _WATCHERS[key]["state"] = "timeout"
        _WATCHERS[key]["finished"] = time.time()

    threading.Thread(target=_run, daemon=True, name=f"bl-watch-{port}").start()
    return {"ok": True, "watching": True, "port": port,
            "message": f"已后台监听端口 {port}，登录成功会自动导入"}


def watch_status() -> dict:
    out = []
    for k, w in _WATCHERS.items():
        out.append({**w, "key": k,
                    "elapsed": int(time.time() - w["started"])})
    return {"watchers": out}


def cancel_watch(port: int) -> dict:
    k = f"watch:{port}"
    if k not in _WATCHERS:
        return {"ok": False, "error": "该端口没有在监听"}
    _WATCHERS.pop(k, None)
    return {"ok": True}


def status(session_id: str | None = None, port: int | None = None) -> dict:
    _gc()
    s = _SESSIONS.get(session_id) if session_id else None
    if s is None and port is not None:
        s = next((x for x in _SESSIONS.values() if x["port"] == port), None)
    out = {
        "sessions": [
            {"session_id": k, "mode": v["mode"], "port": v["port"],
             "status": v["status"], "age_sec": int(time.time() - v["started_at"])}
            for k, v in _SESSIONS.items()
        ],
        "default_port": 9222,
        "detected_path": find_browser(),
    }
    if s:
        creds = None
        try:
            creds = grab_credentials(s["port"])
        except Exception:
            pass
        out["logged_in"] = bool(creds and creds.get("userInfo", {}).get("userId"))
        out["user"] = (creds or {}).get("userInfo") or None
    return out