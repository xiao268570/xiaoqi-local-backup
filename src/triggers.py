"""
触发机制（0.2.0）
================

两种触发，**每种备份模式各有一套**（完整备份、镜像备份互不干扰）：

1. 定时触发：每 N 分钟，或每天固定时刻。
2. 变动触发：**每 N 分钟检查一次源文件夹有没有变动** —— 把「相对路径 + 大小 + 修改时间」
   列成指纹跟上次比对，有变化才真去备份，没变化就什么都不做。

注意：变动触发**不再依赖文件监听组件（watchdog）**，就是一个定期扫描 + 比对。
这样少一层容易出问题的东西，打包 exe 也更好办。
代价：源文件夹特别大时，每次扫描要遍历一遍目录（只读元数据，不读内容）。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Callable

from config import appdata_dir, DEFAULT_INTERVAL_MINUTES, DEFAULT_CHECK_MINUTES, MIN_CHECK_MINUTES
from engine import MODE_FULL, MODE_MIRROR, fingerprint

log = logging.getLogger("triggers")

MODES = (MODE_FULL, MODE_MIRROR)
TICK_SECONDS = 15.0          # 内部检查节拍


# ---------------------------------------------------------------- 运行状态持久化

def _state_path() -> str:
    return str(appdata_dir() / "state.json")


class RunState:
    """记录每个「任务 + 模式」的上次执行时间，重启后不会立刻重跑一遍。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict[str, dict] = {}
        self.load()

    def load(self) -> None:
        p = _state_path()
        if os.path.exists(p):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict):
                    self._data = raw
            except Exception:
                log.warning("运行状态文件解析失败，忽略：%s", p)

    def save(self) -> None:
        try:
            with self._lock:
                tmp = _state_path() + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(self._data, f, ensure_ascii=False, indent=2)
                os.replace(tmp, _state_path())
        except Exception as e:                             # noqa: BLE001
            log.warning("运行状态保存失败：%s", e)

    def get(self, key: str) -> dict:
        with self._lock:
            return dict(self._data.get(key) or {})

    def set(self, key: str, **kwargs) -> None:
        with self._lock:
            d = self._data.setdefault(key, {})
            d.update(kwargs)
        self.save()


def _key(job_id: str, mode: str) -> str:
    return f"{job_id}:{mode}"


def _parse_hhmm(text: str) -> tuple[int, int] | None:
    try:
        h, m = str(text).strip().split(":")
        h, m = int(h), int(m)
        if 0 <= h <= 23 and 0 <= m <= 59:
            return h, m
    except Exception:
        pass
    return None


def _mode_cfg(job: dict, mode: str) -> dict:
    return (job.get(mode) or {}) if isinstance(job.get(mode), dict) else {}


def _mode_on(job: dict, mode: str) -> bool:
    return bool(job.get("enabled", True)) and bool(_mode_cfg(job, mode).get("enabled"))


# ---------------------------------------------------------------- 定时触发

class Scheduler:
    def __init__(self, store, state: RunState, on_fire: Callable[[dict, str, str], None]):
        self.store = store
        self.state = state
        self.on_fire = on_fire
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        log.info("定时触发器已启动（每 %.0f 秒检查一次）", TICK_SECONDS)
        while not self._stop.wait(TICK_SECONDS):
            try:
                self._check_once()
            except Exception:                              # noqa: BLE001
                log.exception("定时触发检查出错")

    def _check_once(self) -> None:
        now = time.time()
        today = datetime.now().strftime("%Y-%m-%d")
        for job in self.store.jobs():
            for mode in MODES:
                if not _mode_on(job, mode):
                    continue
                sc = _mode_cfg(job, mode).get("schedule") or {}
                if not sc.get("enabled", True):
                    continue
                key = _key(str(job.get("id")), mode)
                st = self.state.get(key)
                if not st:
                    # 新任务/新模式先登记当前时间，避免「刚建好就被判定为到点、立刻跑一次」
                    self.state.set(key, last_run=now)
                    continue
                if str(sc.get("type")) == "daily":
                    hm = _parse_hhmm(sc.get("daily_time") or "02:00")
                    if not hm:
                        continue
                    target_t = datetime.now().replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
                    if datetime.now() >= target_t and st.get("last_daily") != today:
                        log.info("定时（每天 %02d:%02d）触发 [%s]", hm[0], hm[1], job.get("name"))
                        self.state.set(key, last_daily=today, last_run=now)
                        self.on_fire(job, mode, "定时")
                else:
                    minutes = int(sc.get("interval_minutes") or 0)
                    if minutes <= 0:
                        continue
                    last = float(st.get("last_run") or 0)
                    if last == 0 or now - last >= minutes * 60:
                        log.info("定时（每 %d 分钟）触发 [%s]", minutes, job.get("name"))
                        self.state.set(key, last_run=now)
                        self.on_fire(job, mode, "定时")

    def next_run(self, job: dict, mode: str) -> float | None:
        if not _mode_on(job, mode):
            return None
        sc = _mode_cfg(job, mode).get("schedule") or {}
        if not sc.get("enabled", True):
            return None
        now = time.time()
        st = self.state.get(_key(str(job.get("id")), mode))
        if str(sc.get("type")) == "daily":
            hm = _parse_hhmm(sc.get("daily_time") or "02:00")
            if not hm:
                return None
            today = datetime.now().replace(hour=hm[0], minute=hm[1], second=0, microsecond=0)
            if datetime.now() >= today and st.get("last_daily") == datetime.now().strftime("%Y-%m-%d"):
                return (today + timedelta(days=1)).timestamp()
            if datetime.now() >= today:
                return now
            return today.timestamp()
        minutes = int(sc.get("interval_minutes") or 0)
        if minutes <= 0:
            return None
        last = float(st.get("last_run") or 0) or now
        return last + minutes * 60


# ---------------------------------------------------------------- 变动触发（定期扫描比对）

class ChangeScanner:
    """每 N 分钟扫一次源文件夹，比对指纹；有变化才触发。"""

    def __init__(self, store, on_fire: Callable[[dict, str, str], None]):
        self.store = store
        self.on_fire = on_fire
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.RLock()
        self._fp: dict[str, dict] = {}            # job_id → 指纹
        self._last_check: dict[str, float] = {}   # job_id → 上次扫描时间
        self._reseed: set[str] = set()            # 刚跑过备份的任务：下次只重新取样、不触发
        self.paused = False

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="scanner", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def mark_ran(self, job_id: str) -> None:
        """刚跑过一次备份：下次扫描只重新取样，不因为"和上次不同"再触发一次。"""
        with self._lock:
            self._reseed.add(job_id)

    def _watch_minutes(self, job: dict, mode: str) -> int:
        wa = _mode_cfg(job, mode).get("watch") or {}
        try:
            return max(MIN_CHECK_MINUTES, int(wa.get("check_interval_minutes") or DEFAULT_CHECK_MINUTES))
        except Exception:                                  # noqa: BLE001
            return DEFAULT_CHECK_MINUTES

    def _loop(self) -> None:
        log.info("变动触发（定期扫描）已启动：每 %.0f 秒检查一次是否该扫描", TICK_SECONDS)
        while not self._stop.wait(TICK_SECONDS):
            if self.paused:
                continue
            try:
                self._check_once()
            except Exception:                              # noqa: BLE001
                log.exception("变动扫描出错")

    def _check_once(self) -> None:
        now = time.time()
        for job in self.store.jobs():
            modes = [m for m in MODES if _mode_on(job, m)
                     and (_mode_cfg(job, m).get("watch") or {}).get("enabled", True)]
            if not modes:
                continue
            src = str(job.get("source") or "").strip()
            if not src or not os.path.isdir(src):
                continue
            job_id = str(job.get("id"))
            with self._lock:
                last = float(self._last_check.get(job_id) or 0)
                # 各模式取最密的那个间隔，一次扫描服务所有模式
                interval = min(self._watch_minutes(job, m) for m in modes)
            if last and now - last < interval * 60:
                continue
            try:
                fp = fingerprint(src, list(job.get("exclude") or []))
            except Exception as e:                         # noqa: BLE001
                log.warning("扫描源文件夹失败 %s：%s", src, e)
                continue
            with self._lock:
                old = self._fp.get(job_id)
                reseed = job_id in self._reseed
                self._fp[job_id] = fp
                self._last_check[job_id] = now
                if reseed:
                    self._reseed.discard(job_id)
            if old is None or reseed:
                continue                                   # 第一次取样：只记基线，不触发
            if fp != old:
                changed = len(set(fp) ^ set(old)) or sum(1 for k in fp if old.get(k) != fp[k])
                log.info("检测到源文件夹有变动（约 %d 处），触发备份 [%s]", changed, job.get("name"))
                for m in modes:
                    self.on_fire(job, m, "变动")

    def next_check(self, job: dict) -> float | None:
        modes = [m for m in MODES if _mode_on(job, m)
                 and (_mode_cfg(job, m).get("watch") or {}).get("enabled", True)]
        if not modes or self.paused:
            return None
        with self._lock:
            last = float(self._last_check.get(str(job.get("id"))) or 0)
        if not last:
            return None
        interval = min(self._watch_minutes(job, m) for m in modes)
        return last + interval * 60


# ---------------------------------------------------------------- 统一入口

class TriggerManager:
    def __init__(self, store, on_fire: Callable[[dict, str, str], None]):
        self.store = store
        self.state = RunState()
        self.scheduler = Scheduler(store, self.state, on_fire)
        self.scanner = ChangeScanner(store, on_fire)

    def start(self) -> None:
        self.scheduler.start()
        self.scanner.start()

    def stop(self) -> None:
        self.scheduler.stop()
        self.scanner.stop()

    def reload(self) -> None:
        """配置改了：把刚取消监听的任务状态清掉，避免留下过期基线。"""
        alive = {str(j.get("id")) for j in self.store.jobs()}
        with self.scanner._lock:                           # noqa: SLF001
            for jid in list(self.scanner._fp):
                if jid not in alive:
                    self.scanner._fp.pop(jid, None)
                    self.scanner._last_check.pop(jid, None)

    @property
    def watch_paused(self) -> bool:
        return self.scanner.paused

    @watch_paused.setter
    def watch_paused(self, value: bool) -> None:
        self.scanner.paused = bool(value)

    def mark_run(self, job: dict, mode: str) -> None:
        self.state.set(_key(str(job.get("id")), mode), last_run=time.time())
        self.scanner.mark_ran(str(job.get("id")))

    def status(self) -> dict:
        out: dict[str, dict] = {}
        for job in self.store.jobs():
            jid = str(job.get("id"))
            per: dict[str, dict] = {}
            for mode in MODES:
                key = _key(jid, mode)
                st = self.state.get(key)
                per[mode] = {
                    "enabled": _mode_on(job, mode),
                    "last_run": st.get("last_run"),
                    "next_run": self.scheduler.next_run(job, mode),
                    "watch_active": bool(
                        _mode_on(job, mode)
                        and (_mode_cfg(job, mode).get("watch") or {}).get("enabled", True)
                        and not self.scanner.paused
                    ),
                    "watch_check_interval": self.scanner._watch_minutes(job, mode),  # noqa: SLF001
                }
            out[jid] = per
        return out
