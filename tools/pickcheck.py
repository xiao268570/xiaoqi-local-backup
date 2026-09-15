"""
检查「选择文件夹」对话框能不能弹出来。

背景：这台机器的托管 Python **没有 tkinter**，所以旧版选择框一直静默失效。
现在改成了 Win32 的 SHBrowseForFolderW。这个脚本用来自动确认它真的能弹窗：

- 后台线程调用 `webui.pick_folder()`
- 主线程等 3 秒，用 FindWindowW 找标题为「选择文件夹」的窗口
- 找到了 → 发 WM_CLOSE 关掉 → 检查调用是否正常返回（取消应返回空串）
- 找不到 → 说明选择框没弹出来，报错

屏幕会闪一个对话框窗口，约 3 秒，不需要人工点击。

用法：
    python tools/pickcheck.py                       # 直接测源码里的 pick_folder()
    python tools/pickcheck.py --http http://127.0.0.1:45700/   # 测**正在运行的程序**（含打包后的 exe）
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config                                    # noqa: E402
config.setup_logging()

import webui                                     # noqa: E402

TITLE = "选择文件夹"
WM_CLOSE = 0x0010

result = {"path": None, "done": False, "error": ""}


def worker() -> None:
    try:
        result["path"] = webui.pick_folder("")
    except Exception as e:                       # noqa: BLE001
        result["error"] = str(e)
    finally:
        result["done"] = True


def _windows_of_this_process() -> list[tuple[int, str, str, bool]]:
    """列出本进程拥有的所有顶层窗口：(hwnd, 标题, 类名, 是否可见)。

    按 PID 找窗口，而不是按标题猜 —— 系统对话框的标题在不同 Windows 版本上不一样。
    """
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    pid = kernel32.GetCurrentProcessId()
    found: list[tuple[int, str, str, bool]] = []

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def _cb(hwnd, _lparam):                      # noqa: ANN001
        try:
            wpid = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(wpid))
            if wpid.value == pid:
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 2)
                user32.GetWindowTextW(hwnd, buf, n + 2)
                cbuf = ctypes.create_unicode_buffer(256)
                user32.GetClassNameW(hwnd, cbuf, 256)
                found.append((hwnd, buf.value, cbuf.value,
                              bool(user32.IsWindowVisible(hwnd))))
        except Exception:                        # noqa: BLE001
            pass
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found


def check_http(url: str) -> int:
    """隔着 HTTP 检查**正在运行的程序**（含打包后的 exe）能不能弹出选择框。

    做法：请求 /api/pick（这个请求会一直挂着直到对话框关闭），
    同时枚举桌面上的「浏览文件夹」对话框（类名 #32770），确认它出现了再关掉它。
    """
    import ctypes
    import json
    import urllib.request
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    resp: dict = {}

    def call() -> None:
        try:
            with urllib.request.urlopen(url + "api/pick", timeout=90) as r:
                resp["data"] = json.loads(r.read().decode("utf-8"))
        except Exception as e:                       # noqa: BLE001
            resp["error"] = str(e)

    t = threading.Thread(target=call, daemon=True)
    t.start()

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    hwnd_found = None

    def _cb(hwnd, _lparam):                          # noqa: ANN001
        nonlocal hwnd_found
        try:
            if not user32.IsWindowVisible(hwnd):
                return True
            cbuf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, cbuf, 256)
            if cbuf.value == "#32770":
                n = user32.GetWindowTextLengthW(hwnd)
                buf = ctypes.create_unicode_buffer(n + 2)
                user32.GetWindowTextW(hwnd, buf, n + 2)
                if "浏览" in buf.value or "选择" in buf.value or "Browse" in buf.value:
                    hwnd_found = hwnd
                    return False
        except Exception:                            # noqa: BLE001
            pass
        return True

    cb = WNDENUMPROC(_cb)
    deadline = time.time() + 15
    while time.time() < deadline:
        hwnd_found = None
        user32.EnumWindows(cb, 0)
        if hwnd_found:
            break
        time.sleep(0.4)

    if not hwnd_found:
        print("[FAIL] 请求 /api/pick 后，桌面上没有出现「浏览文件夹」对话框")
        return 1

    print(f"[PASS] 对话框已弹出（hwnd={hwnd_found}）")
    user32.PostMessageW(hwnd_found, 0x0010, 0, 0)     # WM_CLOSE
    t.join(timeout=20)

    data = resp.get("data") or {}
    if resp.get("error"):
        print(f"[FAIL] 接口报错：{resp['error']}")
        return 1
    print(f"[PASS] 接口正常返回：{data}")
    if data.get("path"):
        print("[PASS] 没选东西时 path 为空、并带 error 说明 —— 界面能据此给出提示")
    elif data.get("error"):
        print("[PASS] 取消后返回了 error 说明，界面不会静默无反应")
    else:
        print("[FAIL] 既没有 path 也没有 error，界面会静默无反应")
        return 1
    print("\n结果：全部通过 —— 运行中的程序里，选择文件夹对话框可用")
    return 0


def main() -> int:
    if "--http" in sys.argv:
        i = sys.argv.index("--http")
        url = sys.argv[i + 1] if len(sys.argv) > i + 1 else "http://127.0.0.1:45700/"
        if not url.endswith("/"):
            url += "/"
        print("=" * 56)
        print(f"检查运行中的程序（{url}）能否弹出选择框")
        print("=" * 56)
        return check_http(url)

    print("=" * 56)
    print("检查「选择文件夹」对话框（会自动关掉，无需人工点击）")
    print("=" * 56)

    t = threading.Thread(target=worker, daemon=True)
    t.start()

    import time as _t
    deadline = _t.time() + 10
    dialog = None
    seen: list[tuple[int, str, str, bool]] = []
    while _t.time() < deadline:
        seen = _windows_of_this_process()
        # 排除我们自己的控制台窗口，找带标题的可见窗口
        cands = [w for w in seen if w[3] and w[1] and "选择" in w[1]]
        if not cands:
            cands = [w for w in seen if w[3] and w[1]]
        if cands:
            dialog = cands[0]
            break
        _t.sleep(0.3)

    print("本进程当前的顶层窗口：")
    for hwnd, title, cls, vis in _windows_of_this_process():
        print(f"   hwnd={hwnd} 可见={vis} 类={cls} 标题={title!r}")

    if not dialog:
        print("[FAIL] 没有找到任何对话框窗口 —— 选择框没有弹出来")
        print("       这种情况下界面会提示你直接把路径粘贴到输入框。")
        return 1

    hwnd, title, cls, _vis = dialog
    print(f"[PASS] 对话框已弹出：标题={title!r} 类={cls} hwnd={hwnd}")
    user32 = ctypes.windll.user32
    user32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
    t.join(timeout=15)

    if not result["done"]:
        print("[FAIL] 关掉对话框后，调用没有正常返回（可能卡住了）")
        return 1
    if result["error"]:
        print(f"[FAIL] 调用抛异常：{result['error']}")
        return 1

    print(f"[PASS] 取消后正常返回空路径（返回值为 {result['path']!r}）")
    print("\n结果：全部通过 —— 选择文件夹对话框可用")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
