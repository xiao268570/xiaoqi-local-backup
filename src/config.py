"""
配置与基础设施：路径、常量、配置读写、日志。
"""
from __future__ import annotations

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path
from logging.handlers import RotatingFileHandler

# ---------------------------------------------------------------- 基础常量

APP_ID = "XiaoQiBackup"          # 内部标识（注册表 / 目录名）
APP_TITLE = "小柒本地备份"        # 界面显示名
APP_VERSION = "0.2.0"            # 0.2.0：备份模型重做（完整备份 / 镜像备份）

# 备份文件夹里每个「我们建的」子文件夹都会放一个标记文件。
# 清理旧备份时**只删带标记的**，绝不碰用户自己放进去的东西。
MARKER_FULL = ".xqb-full.json"     # 完整备份文件夹的标记
MARKER_MIRROR = ".xqb-mirror.json"  # 镜像文件夹的标记

# 备份文件夹命名的后缀格式：<备份夹名><202609151043> / <备份夹名>-镜像<202609151043>
STAMP_FORMAT = "%Y%m%d%H%M"

# 默认排除规则（fnmatch 语法，大小写不敏感）
DEFAULT_EXCLUDES = [
    "~$*", "*.tmp", "*.temp", "*.bak", "*.swp", "*.crdownload", "*.part",
    "Thumbs.db", "desktop.ini", "$RECYCLE.BIN", "System Volume Information",
    ".git", ".svn", ".vs", "__pycache__", "node_modules",
]

DEFAULT_INTERVAL_MINUTES = 60      # 定时触发：每 N 分钟
DEFAULT_CHECK_MINUTES = 5          # 变动触发：每 N 分钟检查一次有没有变动
DEFAULT_KEEP_RECENT = 5            # 文件留存：保留最近 N 份完整备份
MIN_CHECK_MINUTES = 1
MAX_KEEP_RECENT = 999


# ---------------------------------------------------------------- 路径

def appdata_dir() -> Path:
    # 测试时可用环境变量把数据目录隔离到临时位置，避免污染正式配置
    override = os.environ.get("XQB_DATA_DIR")
    if override:
        d = Path(override)
        d.mkdir(parents=True, exist_ok=True)
        return d
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / APP_ID
    d.mkdir(parents=True, exist_ok=True)
    return d


def config_path() -> Path:
    return appdata_dir() / "config.json"


def logs_dir() -> Path:
    d = appdata_dir() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def is_frozen() -> bool:
    """是否已打包成 exe。"""
    return bool(getattr(sys, "frozen", False))


def launch_command() -> str:
    """开机自启要写入注册表的命令行。"""
    if is_frozen():
        return f'"{sys.executable}"'
    main_py = Path(__file__).resolve().parent / "main.py"
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    exe = pythonw if pythonw.exists() else Path(sys.executable)
    return f'"{exe}" "{main_py}"'


# ---------------------------------------------------------------- 配置

def default_config() -> dict:
    return {
        "app": {
            "version": APP_VERSION,
            "port": 45700,
            "autostart": False,
            "watch_enabled": True,     # 全局暂停开关
        },
        "jobs": [],
    }


def _new_mode(mirror: bool = False) -> dict:
    """一种备份模式的配置块。两种模式各有一套自己的触发设置。"""
    return {
        "enabled": not mirror,          # 默认只开完整备份
        "schedule": {
            "enabled": True,
            "type": "interval",         # interval=按间隔 ｜ daily=每天固定时刻
            "interval_minutes": DEFAULT_INTERVAL_MINUTES,
            "daily_time": "02:00",
        },
        "watch": {
            "enabled": True,
            "check_interval_minutes": DEFAULT_CHECK_MINUTES,   # 每 N 分钟检查一次有没有变动
        },
    }


def new_job(name: str = "新建任务") -> dict:
    full = _new_mode()
    full["keep_recent"] = DEFAULT_KEEP_RECENT      # 文件留存：只归完整备份
    mirror = _new_mode(mirror=True)
    mirror["folder"] = ""                          # 第一次执行时写入，之后一直复用这个名字
    return {
        "id": os.urandom(4).hex(),
        "name": name,
        "enabled": True,
        "source": "",
        "target": "",
        "exclude": list(DEFAULT_EXCLUDES),
        "full": full,
        "mirror": mirror,
    }


def migrate_job(job: dict) -> dict:
    """把 0.1.0 的旧任务结构升级成 0.2.0 的。

    旧结构：mode（mirror/copy）+ 顶层 schedule/watch/versioning（_Versions 那套已整套废弃）。
    迁移原则：不丢用户的路径和触发设置；老的「只增不删」没有对应物，一律按「完整备份」处理。
    """
    if not isinstance(job, dict) or "full" in job or "mirror" in job:
        return job
    new = new_job(str(job.get("name") or "新建任务"))
    new["id"] = job.get("id") or new["id"]
    new["enabled"] = bool(job.get("enabled", True))
    new["source"] = str(job.get("source") or "")
    new["target"] = str(job.get("target") or "")
    if isinstance(job.get("exclude"), list):
        new["exclude"] = list(job["exclude"])
    old_sc = job.get("schedule") or {}
    if isinstance(old_sc, dict):
        new["full"]["schedule"].update({
            "enabled": bool(old_sc.get("enabled", True)),
            "type": str(old_sc.get("type") or "interval"),
            "interval_minutes": int(old_sc.get("interval_minutes") or DEFAULT_INTERVAL_MINUTES),
            "daily_time": str(old_sc.get("daily_time") or "02:00"),
        })
    old_wa = job.get("watch") or {}
    if isinstance(old_wa, dict):
        new["full"]["watch"]["enabled"] = bool(old_wa.get("enabled", True))
    return new


class ConfigStore:
    """线程安全的配置读写。"""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._data: dict = default_config()
        self.load()

    # -------------------------------------------------- 读写

    def load(self) -> dict:
        p = config_path()
        with self._lock:
            if p.exists():
                try:
                    raw = json.loads(p.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        # 顶层缺失项补齐，避免旧配置缺字段直接崩
                        base = default_config()
                        base["app"].update(raw.get("app") or {})
                        jobs = raw.get("jobs")
                        # 旧结构在这里升级，不丢用户的路径与触发设置
                        base["jobs"] = [migrate_job(j) for j in jobs] if isinstance(jobs, list) else []
                        self._data = base
                except Exception:
                    logging.exception("配置文件解析失败，已回退到默认配置: %s", p)
                    self._data = default_config()
            else:
                self._data = default_config()
                self.save()
            return self._data

    def save(self) -> None:
        p = config_path()
        with self._lock:
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(
                json.dumps(self._data, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            os.replace(tmp, p)

    # -------------------------------------------------- 访问

    @property
    def app(self) -> dict:
        with self._lock:
            return self._data["app"]

    def jobs(self) -> list[dict]:
        with self._lock:
            return list(self._data["jobs"])

    def get_job(self, job_id: str) -> dict | None:
        with self._lock:
            for j in self._data["jobs"]:
                if j.get("id") == job_id:
                    return j
        return None

    def replace_all(self, incoming: dict) -> None:
        """整表替换（来自设置界面）。"""
        with self._lock:
            app_cfg = dict(self._data["app"])
            app_cfg.update(incoming.get("app") or {})
            jobs = incoming.get("jobs")
            self._data = {
                "app": app_cfg,
                "jobs": jobs if isinstance(jobs, list) else [],
            }
            self.save()

    def patch_app(self, **kwargs) -> None:
        with self._lock:
            self._data["app"].update(kwargs)
            self.save()


# ---------------------------------------------------------------- 日志

_LOG_INITED = False
LOG_BUFFER: list[str] = []
_LOG_BUFFER_MAX = 400
_BUFFER_LOCK = threading.Lock()


class _BufferHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record)
        except Exception:
            return
        with _BUFFER_LOCK:
            LOG_BUFFER.append(line)
            if len(LOG_BUFFER) > _LOG_BUFFER_MAX:
                del LOG_BUFFER[: len(LOG_BUFFER) - _LOG_BUFFER_MAX]


def setup_logging(level: int = logging.INFO) -> None:
    global _LOG_INITED
    if _LOG_INITED:
        return
    _LOG_INITED = True

    fmt = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s", datefmt="%Y-%m-%d %H:%M:%S"
    )
    root = logging.getLogger()
    root.setLevel(level)

    fh = RotatingFileHandler(
        logs_dir() / f"backup-{time.strftime('%Y%m%d')}.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=7,
        encoding="utf-8",
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    bh = _BufferHandler()
    bh.setFormatter(fmt)
    root.addHandler(bh)


def recent_logs(limit: int = 200) -> list[str]:
    with _BUFFER_LOCK:
        return LOG_BUFFER[-limit:]
