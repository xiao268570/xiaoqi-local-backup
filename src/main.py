"""
入口：单实例检查 → 启动本地设置界面 → 进托盘常驻。

用法
----
双击启动（推荐）：  启动备份工具.bat        ← 会启动程序并**直接打开设置界面**
带控制台（调试用）：python  main.py
每次都要弹界面：    python  main.py --open-ui
开机自启（静默）：  pythonw main.py          ← 不带 --open-ui，只进托盘不打扰

说明：不带 `--open-ui` 时，只有**第一次**运行会自动弹界面（避免每次开机都弹浏览器）。
      双击 .bat 走的是带 `--open-ui` 的路径，所以每次双击都能看到设置界面。
"""
from __future__ import annotations

import ctypes
import logging
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import APP_ID, APP_TITLE, APP_VERSION, ConfigStore, setup_logging   # noqa: E402

log = logging.getLogger("main")

MUTEX_NAME = f"Local\\{APP_ID}_SingleInstance"
ERROR_ALREADY_EXISTS = 183


def _acquire_single_instance() -> bool:
    """返回 True 表示本进程是第一个实例。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        if not handle:
            log.warning("创建互斥体失败，跳过单实例检查")
            return True
        # 保留句柄引用，进程退出前不释放
        globals()["_mutex_handle"] = handle
        return ctypes.get_last_error() != ERROR_ALREADY_EXISTS
    except Exception as e:                              # noqa: BLE001
        log.warning("单实例检查出错：%s", e)
        return True


def _focus_existing() -> None:
    """已有实例在跑：直接把它的设置界面打开。"""
    try:
        store = ConfigStore()
        port = int((store.app or {}).get("port") or 45700)
    except Exception:
        port = 45700
    url = f"http://127.0.0.1:{port}/"
    try:
        os.startfile(url)                               # noqa: S606
    except Exception:
        pass


def main() -> int:
    setup_logging()
    log.info("=" * 56)
    log.info("%s v%s 启动", APP_TITLE, APP_VERSION)

    if not _acquire_single_instance():
        log.info("检测到已有实例在运行，打开它的设置界面后退出")
        _focus_existing()
        return 0

    from trayapp import Controller                          # noqa: E402
    from webui import serve, open_in_browser                # noqa: E402

    controller = Controller()
    controller.ensure_example_job()

    preferred = int((controller.store.app or {}).get("port") or 45700)
    httpd, url = serve(controller, preferred)
    controller.store.patch_app(port=int(url.rsplit(":", 1)[1].rstrip("/")))

    if bool(controller.store.app.get("watch_enabled", True)) is False:
        controller.triggers.watch_paused = True

    # 打开设置界面：
    #   --open-ui  → 每次都弹（双击 .bat 走这条，避免用户以为"点了没反应"）
    #   否则       → 只有第一次运行弹一次，之后静默进托盘（开机自启走这条，不打扰）
    want_ui = ("--open-ui" in sys.argv) or (not controller.store.app.get("ui_opened_once"))
    if not controller.store.app.get("ui_opened_once"):
        controller.store.patch_app(ui_opened_once=True)
    if want_ui and not os.environ.get("XQB_NO_BROWSER"):
        time.sleep(0.8)
        open_in_browser(url)

    try:
        controller.start(url)                                # 阻塞在托盘
    except KeyboardInterrupt:
        controller.quit()
    finally:
        try:
            httpd.shutdown()
        except Exception:
            pass
    log.info("已退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
