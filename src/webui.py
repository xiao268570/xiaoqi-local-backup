"""
本地设置界面：一个只监听 127.0.0.1 的极简 HTTP 服务 + 单页设置界面。
用浏览器打开即可，不用装任何界面库，后续想换界面也只改 HTML。
"""
from __future__ import annotations

import json
import logging
import os
import socket
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

from config import APP_VERSION, config_path, logs_dir, recent_logs

log = logging.getLogger("webui")


def _ui_file() -> Path:
    """界面文件位置。打包成 exe 后资源被解到 _MEIPASS。"""
    base = getattr(sys, "_MEIPASS", None)
    if base:
        p = Path(base) / "ui" / "index.html"
        if p.exists():
            return p
    return Path(__file__).resolve().parent / "ui" / "index.html"
_pick_lock = threading.Lock()


# ---------------------------------------------------------------- 文件夹选择框
#
# 为什么不用 tkinter：本机托管的 Python **没有带 tkinter**（没有 tkinter 目录、也没有 _tkinter 模块），
# 所以 `import tkinter` 必然失败 —— 这个功能曾经因此一直静默失效。
# 现在直接用 Win32 的 SHBrowseForFolderW，系统自带、打包进 exe 也一定可用。

def _pick_folder_win32(initial: str = "") -> str:
    """弹出系统「选择文件夹」对话框。返回选中路径；取消或失败返回空串。"""
    if os.name != "nt":
        return ""
    try:
        import ctypes
        from ctypes import wintypes
    except Exception as e:                             # noqa: BLE001
        log.warning("无法加载 ctypes：%s", e)
        return ""

    class BROWSEINFOW(ctypes.Structure):
        _fields_ = [
            ("hwndOwner", wintypes.HWND),
            ("pidlRoot", ctypes.c_void_p),
            ("pszDisplayName", ctypes.c_wchar_p),
            ("lpszTitle", ctypes.c_wchar_p),
            ("ulFlags", ctypes.c_uint),
            ("lpfn", ctypes.c_void_p),
            ("lParam", ctypes.c_void_p),
            ("iImage", ctypes.c_int),
        ]

    BIF_RETURNONLYFSDIRS = 0x0001
    BIF_EDITBOX = 0x0010
    BIF_NEWDIALOGSTYLE = 0x0040
    BFFM_INITIALIZED = 1
    WM_CLOSE = 0x0010

    shell32 = ctypes.windll.shell32
    ole32 = ctypes.windll.ole32
    user32 = ctypes.windll.user32
    shell32.SHBrowseForFolderW.restype = ctypes.c_void_p
    shell32.SHBrowseForFolderW.argtypes = [ctypes.POINTER(BROWSEINFOW)]
    shell32.SHGetPathFromIDListW.restype = wintypes.BOOL
    shell32.SHGetPathFromIDListW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p]
    ole32.CoTaskMemFree.argtypes = [ctypes.c_void_p]

    # 对话框初始化时把它提到最前面：否则从浏览器点出来的框可能藏在浏览器后面，
    # 用户就会以为"点了没反应"。
    @ctypes.WINFUNCTYPE(ctypes.c_int, wintypes.HWND, ctypes.c_uint,
                        ctypes.c_void_p, ctypes.c_void_p)
    def _on_init(hwnd, msg, lparam, lpdata):           # noqa: ANN001
        if msg == BFFM_INITIALIZED:
            try:
                user32.SetForegroundWindow(hwnd)
            except Exception:
                pass
        return 0

    hr = 2
    try:
        hr = ole32.CoInitialize(None)                  # S_OK=0 / S_FALSE=1
        display = ctypes.create_unicode_buffer(260)
        bi = BROWSEINFOW()
        bi.hwndOwner = None
        bi.pidlRoot = None
        bi.pszDisplayName = ctypes.cast(display, ctypes.c_wchar_p)
        bi.lpszTitle = "选择文件夹"
        bi.ulFlags = BIF_RETURNONLYFSDIRS | BIF_EDITBOX | BIF_NEWDIALOGSTYLE
        bi.lpfn = ctypes.cast(_on_init, ctypes.c_void_p)
        bi.lParam = None

        pidl = shell32.SHBrowseForFolderW(ctypes.byref(bi))
        if not pidl:
            log.info("文件夹选择框：用户取消了")
            return ""
        buf = ctypes.create_unicode_buffer(1024)
        ok = shell32.SHGetPathFromIDListW(ctypes.c_void_p(pidl), buf)
        ole32.CoTaskMemFree(ctypes.c_void_p(pidl))
        path = buf.value if ok else ""
        log.info("文件夹选择框：选中 %s", path or "(空)")
        return path
    except Exception as e:                             # noqa: BLE001
        log.warning("文件夹选择框出错：%s", e)
        return ""
    finally:
        if hr in (0, 1):
            try:
                ole32.CoUninitialize()
            except Exception:
                pass


def pick_folder(initial: str = "") -> str:
    """弹出系统原生「选择文件夹」对话框。失败时返回空字符串（界面提示改用直接粘贴路径）。

    注意：这个函数是**阻塞**的，要等用户选完或取消。调用方是 HTTP 处理线程，不影响其它请求。
    """
    with _pick_lock:
        return _pick_folder_win32(initial)


# ---------------------------------------------------------------- 端口

def _free_port(preferred: int, tries: int = 25) -> int:
    for i in range(tries):
        p = preferred + i
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ---------------------------------------------------------------- HTTP 处理器

class _Handler(BaseHTTPRequestHandler):
    controller = None                                  # 由 serve() 注入
    # HTTP 头只能是 latin-1，这里不能出现中文
    server_version = f"XiaoQiBackup/{APP_VERSION}"
    sys_version = ""

    def log_message(self, fmt, *args):                 # noqa: ANN001
        return                                         # 静音，避免刷日志

    # ------------------------------------------------ 工具

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _guard(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._json({"ok": False, "error": "forbidden"}, 403)
            return False
        return True

    # ------------------------------------------------ 路由

    def do_GET(self) -> None:                          # noqa: N802
        if not self._guard():
            return
        u = urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            try:
                html = _ui_file().read_text(encoding="utf-8")
            except Exception as e:                     # noqa: BLE001
                self._send(500, f"界面文件缺失：{e}".encode("utf-8"), "text/plain; charset=utf-8")
                return
            self._send(200, html.encode("utf-8"), "text/html; charset=utf-8")
            return

        if path == "/api/state":
            self._json(self.controller.snapshot())
            return

        if path == "/api/log":
            qs = parse_qs(u.query)
            limit = int((qs.get("limit") or ["200"])[0])
            self._json({"lines": recent_logs(min(max(limit, 1), 400))})
            return

        if path == "/api/backups":
            qs = parse_qs(u.query)
            job_id = (qs.get("id") or [""])[0]
            name = (qs.get("name") or [""])[0]
            if name:
                self._json(self.controller.backup_tree(job_id, name))
            else:
                self._json(self.controller.list_backups(job_id))
            return

        if path == "/api/pick":
            qs = parse_qs(u.query)
            initial = (qs.get("initial") or [""])[0]
            picked = pick_folder(initial)
            # 没选到东西时必须让界面知道，不能让它静默什么都不做
            self._json({
                "path": picked,
                "error": "" if picked else "没有选择文件夹（可能取消了，或系统对话框打不开）",
            })
            return

        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self) -> None:                         # noqa: N802
        if not self._guard():
            return
        path = urlparse(self.path).path
        body = self._read_json()

        if path == "/api/save":
            try:
                self.controller.save_config(body)
                self._json({"ok": True})
            except Exception as e:                     # noqa: BLE001
                log.exception("保存配置失败")
                self._json({"ok": False, "error": str(e)}, 500)
            return

        if path == "/api/run":
            job_id = str(body.get("id") or "")
            mode = str(body.get("mode") or "full")
            self.controller.run_now(job_id, mode, trigger="手动")
            self._json({"ok": True})
            return

        if path == "/api/restore":
            job_id = str(body.get("id") or "")
            name = str(body.get("name") or "")
            rels = body.get("rels")
            if not job_id or not name:
                self._json({"ok": False, "error": "缺少任务或备份名"}, 400)
                return
            rels_list = [str(x) for x in rels] if isinstance(rels, list) and rels else None
            self._json(self.controller.restore(job_id, name, rels_list))
            return

        if path == "/api/open":
            target = str(body.get("path") or "")
            ok, err = self.controller.open_path(target)
            self._json({"ok": ok, "error": err})
            return

        if path == "/api/autostart":
            enabled = bool(body.get("enabled"))
            ok, err = self.controller.set_autostart(enabled)
            self._json({"ok": ok, "error": err})
            return

        if path == "/api/pause":
            self.controller.set_watch_paused(bool(body.get("paused")))
            self._json({"ok": True})
            return

        if path == "/api/quit":
            self._json({"ok": True})
            threading.Thread(target=self.controller.quit, daemon=True).start()
            return

        self._json({"ok": False, "error": "not found"}, 404)


# ---------------------------------------------------------------- 启动

def serve(controller, preferred_port: int = 45700) -> tuple[ThreadingHTTPServer, str]:
    port = _free_port(int(preferred_port or 45700))
    handler = type("BoundHandler", (_Handler,), {"controller": controller})
    httpd = ThreadingHTTPServer(("127.0.0.1", port), handler)
    httpd.daemon_threads = True
    t = threading.Thread(target=httpd.serve_forever, name="webui", daemon=True)
    t.start()
    url = f"http://127.0.0.1:{port}/"
    log.info("设置界面已启动：%s", url)
    log.info("配置文件：%s", config_path())
    log.info("日志目录：%s", logs_dir())
    return httpd, url


def open_in_browser(url: str) -> None:
    try:
        webbrowser.open(url)
    except Exception as e:                             # noqa: BLE001
        log.warning("打开浏览器失败：%s", e)
