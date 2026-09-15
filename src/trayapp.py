"""
控制器 + 系统托盘。
托盘菜单里做完所有日常操作；设置界面走本地网页。
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time

import pystray
from PIL import Image, ImageDraw
from pystray import Menu, MenuItem

from config import (
    APP_ID, APP_TITLE, APP_VERSION,
    ConfigStore, config_path, launch_command, logs_dir, new_job,
)
from engine import (
    MODE_FULL, MODE_LABEL, MODE_MIRROR, JobResult,
    list_backup_tree, list_full_backups, restore_backup, run_job,
)
from triggers import TriggerManager

log = logging.getLogger("app")

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
MODES = (MODE_FULL, MODE_MIRROR)


def _make_icon(paused: bool = False) -> Image.Image:
    """动态画一个托盘图标，避免外挂资源文件。"""
    size = 64
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    bg = (150, 158, 168, 255) if paused else (47, 111, 237, 255)
    # 外框
    d.rounded_rectangle((4, 4, size - 5, size - 5), radius=14, fill=bg)
    # 文件夹
    d.rounded_rectangle((14, 22, size - 15, size - 17), radius=6, fill=(255, 255, 255, 255))
    d.rounded_rectangle((14, 16, 30, 24), radius=4, fill=(255, 255, 255, 255))
    # 时钟指针（表示定时）
    d.line((32, 34, 32, 43), fill=bg, width=3)
    d.line((32, 38, 39, 41), fill=bg, width=3)
    return img


class Controller:
    def __init__(self) -> None:
        self.store = ConfigStore()
        self.results: dict[str, dict] = {}       # "任务id:模式" → 最近一次结果
        self.restores: dict[str, dict] = {}      # 任务id → 最近一次恢复结果
        self.running: set[str] = set()           # "任务id:模式"
        self._running_lock = threading.Lock()
        self.url = ""
        self.tray: pystray.Icon | None = None
        self._quitting = False
        self.triggers = TriggerManager(self.store, self._on_fire)

    # ------------------------------------------------ 启停

    def start(self, url: str) -> None:
        self.url = url
        self.triggers.start()
        self.tray = pystray.Icon(
            APP_ID,
            _make_icon(False),
            f"{APP_TITLE} v{APP_VERSION}",
            menu=self._build_menu(),
        )
        log.info("托盘已就绪")
        self.tray.run()                       # 阻塞在主线程

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        log.info("正在退出…")
        try:
            self.triggers.stop()
        except Exception:
            pass
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass

    # ------------------------------------------------ 托盘菜单

    def _build_menu(self) -> Menu:
        items = [
            MenuItem("打开设置", self._act_open_settings, default=True),
            MenuItem("立即备份全部", self._act_run_all),
            Menu.SEPARATOR,
        ]
        jobs = [j for j in self.store.jobs()]
        if jobs:
            sub = []
            for j in jobs:
                label = str(j.get("name") or "未命名")
                sub.append(MenuItem(label, self._make_open_target(j)))
            items.append(MenuItem("打开备份文件夹", Menu(*sub)))
        items += [
            MenuItem("打开日志文件夹", lambda: self.open_path(str(logs_dir()))),
            Menu.SEPARATOR,
            MenuItem(
                "恢复变动监听",
                self._act_toggle_pause,
                checked=lambda item: not self.triggers.watch_paused,
                radio=False,
            ),
            Menu.SEPARATOR,
            MenuItem("关于 / 版本信息", self._act_about),
            MenuItem("退出", lambda: self.quit()),
        ]
        return Menu(*items)

    def _refresh_menu(self) -> None:
        if not self.tray:
            return
        try:
            self.tray.menu = self._build_menu()
            self.tray.update_menu()
        except Exception:
            log.exception("刷新托盘菜单失败")

    def _make_open_target(self, job: dict):
        def _open(icon=None, item=None):               # noqa: ANN001
            self.open_path(str(job.get("target") or ""))
        return _open

    def _act_open_settings(self, icon=None, item=None) -> None:   # noqa: ANN001
        if self.url:
            try:
                os.startfile(self.url)                 # noqa: S606
            except Exception:
                subprocess.Popen(["cmd", "/c", "start", "", self.url], shell=False)

    def _act_run_all(self, icon=None, item=None) -> None:         # noqa: ANN001
        for job in self.store.jobs():
            for mode in MODES:
                if (job.get(mode) or {}).get("enabled"):
                    self.run_now(str(job.get("id")), mode, trigger="手动")

    def _act_toggle_pause(self, icon=None, item=None) -> None:    # noqa: ANN001
        self.set_watch_paused(not self.triggers.watch_paused)
        self._refresh_menu()
        self._notify(
            "变动监听已恢复" if not self.triggers.watch_paused else "变动监听已暂停",
            "定时备份不受影响" if self.triggers.watch_paused else "",
        )

    def _act_about(self, icon=None, item=None) -> None:           # noqa: ANN001
        self._notify(
            f"{APP_TITLE} v{APP_VERSION}",
            f"配置文件：{config_path()}",
        )

    def _notify(self, title: str, message: str = "") -> None:
        try:
            if self.tray:
                self.tray.notify(message or title, title)
        except Exception:
            pass

    # ------------------------------------------------ 对外操作

    def _on_fire(self, job: dict, mode: str, trigger: str) -> None:
        self.run_now(str(job.get("id")), mode, trigger=trigger)

    def run_now(self, job_id: str, mode: str = MODE_FULL, trigger: str = "手动") -> None:
        job = self.store.get_job(job_id)
        if not job:
            return
        mode = MODE_MIRROR if mode == MODE_MIRROR else MODE_FULL
        key = f"{job_id}:{mode}"
        with self._running_lock:
            self.running.add(key)

        def _worker() -> None:
            try:
                res = run_job(job, mode, trigger=trigger)
                self.results[key] = res.to_dict()
                self.triggers.mark_run(job, mode)
                if not res.ok and res.message != "已有备份正在进行，本次触发已合并":
                    self._notify(f"{MODE_LABEL[mode]}失败：{job.get('name')}", res.summary())
            except Exception as e:                     # noqa: BLE001
                log.exception("执行任务异常")
                self.results[key] = JobResult(
                    job_id=job_id, job_name=str(job.get("name")), mode=mode,
                    trigger=trigger, ok=False, message=str(e), finished_at=time.time(),
                ).to_dict()
            finally:
                with self._running_lock:
                    self.running.discard(key)

        threading.Thread(target=_worker, name=f"job-{key}", daemon=True).start()

    def save_config(self, payload: dict) -> None:
        self.store.replace_all(payload)
        self.triggers.reload()
        self._refresh_menu()
        log.info("配置已保存，监听已重建")

    def open_path(self, path: str) -> tuple[bool, str]:
        p = str(path or "").strip()
        if not p:
            return False, "路径为空"
        if not os.path.exists(p):
            try:
                os.makedirs(p, exist_ok=True)
            except Exception as e:                     # noqa: BLE001
                return False, f"路径不存在且无法创建：{e}"
        if os.environ.get("XQB_NO_OPEN"):
            return True, ""                            # 自测时别真去弹资源管理器
        try:
            os.startfile(p)                            # noqa: S606
            return True, ""
        except Exception as e:                         # noqa: BLE001
            return False, str(e)

    # ------------------------------------------------ 恢复

    def list_backups(self, job_id: str) -> dict:
        """列出这个任务的「完整备份」，新的在前，供恢复界面选日期。"""
        try:
            job = self.store.get_job(job_id)
            if not job:
                return {"ok": False, "error": "找不到这个任务"}
            target = str(job.get("target") or "")
            if not target.strip():
                return {"ok": False, "error": "这个任务还没有设置备份文件夹"}
            return {"ok": True, "target": target, "backups": list_full_backups(target)}
        except Exception as e:                             # noqa: BLE001
            log.exception("列出备份失败")
            return {"ok": False, "error": str(e)}

    def backup_tree(self, job_id: str, name: str) -> dict:
        """某一份备份里的文件树（文件夹可展开）。"""
        try:
            job = self.store.get_job(job_id)
            if not job:
                return {"ok": False, "error": "找不到这个任务"}
            return list_backup_tree(str(job.get("target") or ""), name)
        except Exception as e:                             # noqa: BLE001
            return {"ok": False, "error": str(e)}

    def restore(self, job_id: str, name: str, rels: list[str] | None = None) -> dict:
        """恢复。

        rels 为空 → 整批恢复：整个备份文件夹复制到源文件夹旁边（不覆盖任何东西）。
        rels 有值 → 逐项恢复：放回源文件夹里原来的位置，重名自动改名另存。
        恢复完自动打开结果所在文件夹。
        """
        job = self.store.get_job(job_id)
        if not job:
            return {"ok": False, "error": "找不到这个任务"}
        res = restore_backup(job, name, rels=rels or None)
        data = res.to_dict()
        self.restores[job_id] = data
        if res.ok and res.dest:
            ok, err = self.open_path(res.dest)
            if not ok:
                data["open_error"] = err
        if res.ok:
            self._notify(f"恢复完成：{job.get('name')}", res.summary())
        else:
            self._notify(f"恢复失败：{job.get('name')}", res.message or res.summary())
        return data

    def set_watch_paused(self, paused: bool) -> None:
        self.triggers.watch_paused = paused
        self.store.patch_app(watch_enabled=not paused)
        try:
            if self.tray:
                self.tray.icon = _make_icon(paused)
        except Exception:
            pass

    def set_autostart(self, enabled: bool) -> tuple[bool, str]:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                                winreg.KEY_SET_VALUE) as k:
                if enabled:
                    winreg.SetValueEx(k, APP_ID, 0, winreg.REG_SZ, launch_command())
                else:
                    try:
                        winreg.DeleteValue(k, APP_ID)
                    except FileNotFoundError:
                        pass
            self.store.patch_app(autostart=bool(enabled))
            log.info("开机自启已%s", "开启" if enabled else "关闭")
            return True, ""
        except Exception as e:                         # noqa: BLE001
            log.exception("设置开机自启失败")
            return False, str(e)

    def autostart_enabled(self) -> bool:
        try:
            import winreg
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as k:
                winreg.QueryValueEx(k, APP_ID)
                return True
        except Exception:
            return False

    # ------------------------------------------------ 状态快照

    def snapshot(self) -> dict:
        with self._running_lock:
            running = list(self.running)
        return {
            "app": self.store.app,
            "jobs": self.store.jobs(),
            "runtime": {
                "version": APP_VERSION,
                "url": self.url,
                "config_path": str(config_path()),
                "logs_dir": str(logs_dir()),
                "autostart": self.autostart_enabled(),
                "watch_paused": self.triggers.watch_paused,
                "running": running,
                "results": self.results,
                "restores": self.restores,
                "status": self.triggers.status(),
                "server_time": time.time(),
            },
        }

    # ------------------------------------------------ 首次运行

    def ensure_example_job(self) -> None:
        if self.store.jobs():
            return
        if not self.store.app.get("first_run_done"):
            self.store.patch_app(first_run_done=True)
        log.info("当前没有任务，等你在设置界面里添加")


def make_blank_job() -> dict:
    return new_job()
