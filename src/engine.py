"""
备份引擎（0.2.0 模型）
=====================

两种模式，可以同时开，互不干扰：

1. 完整备份 —— 每次都是一份**完整、独立**的文件夹
   触发时在备份文件夹里新建 `<备份夹名><年月日时分>` → 把源里所有内容拷进去。
   好处：每一份都能直接打开用、能单独拷走、能回到过去某个时刻。
   代价：占空间（N 份 = 源大小的 N 倍），所以有「文件留存：保留最近 N 份」来清理旧的。

2. 镜像备份 —— 只保留**一份**镜像文件夹
   第一次建 `<备份夹名>-镜像<年月日时分>`，之后一直复用这个名字，只同步有变化的内容，
   并且把源里已经删掉的文件从镜像里也删掉（真镜像）。
   好处：省空间、始终和源一致。代价：没有历史。

安全约定（改动时不许破坏）
--------------------------
- **清理时只删带标记文件的文件夹**（`.xqb-full.json` / `.xqb-mirror.json`），
  绝不碰用户自己放进备份文件夹里的东西。
- **覆盖/删除镜像里的文件之前不丢内容**：完整备份那边已经有历史兜底；镜像本身的定位就是"和源一致"。
- 源与备份文件夹不能互相包含（在创建任何目录之前就拒绝）。
- 写文件走临时文件 + os.replace，中断不留半个文件。
"""
from __future__ import annotations

import fnmatch
import json
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from config import MARKER_FULL, MARKER_MIRROR, STAMP_FORMAT

log = logging.getLogger("engine")

MTIME_TOLERANCE = 2.0        # 秒。FAT / 网络盘的时间戳粒度可能是 2 秒
COPY_ATTEMPTS = 3
TMP_SUFFIX = ".xqbtmp"

FILE_ATTRIBUTE_REPARSE_POINT = 0x400

MODE_FULL = "full"
MODE_MIRROR = "mirror"
MODE_LABEL = {MODE_FULL: "完整备份", MODE_MIRROR: "镜像备份"}


# ---------------------------------------------------------------- 路径工具

def _lp(path: str) -> str:
    """超长路径（>=248 字符）加 \\\\?\\ 前缀，绕过 MAX_PATH 限制。"""
    try:
        p = os.path.abspath(path)
    except Exception:
        return path
    if os.name != "nt" or len(p) < 248 or p.startswith("\\\\?\\"):
        return p
    if p.startswith("\\\\"):
        return "\\\\?\\UNC\\" + p[2:]
    return "\\\\?\\" + p


def _is_reparse(entry: os.DirEntry) -> bool:
    """是否为符号链接 / 联接点（避免递归成环）。"""
    try:
        st = entry.stat(follow_symlinks=False)
        return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)
    except Exception:
        return False


def norm_dir(p: str) -> str:
    return os.path.normpath(os.path.abspath(p))


def is_subpath(child: str, parent: str) -> bool:
    try:
        return os.path.commonpath([norm_dir(child), norm_dir(parent)]) == norm_dir(parent)
    except Exception:
        return False


def match_exclude(name: str, rel: str, patterns: list[str]) -> bool:
    low_name = name.lower()
    low_rel = rel.lower().replace("\\", "/")
    for pat in patterns:
        p = (pat or "").strip().lower().replace("\\", "/")
        if not p:
            continue
        if "/" in p:
            if fnmatch.fnmatch(low_rel, p) or fnmatch.fnmatch(low_rel, "*/" + p):
                return True
        else:
            if fnmatch.fnmatch(low_name, p):
                return True
    return False


# ---------------------------------------------------------------- 结果

@dataclass
class JobResult:
    job_id: str = ""
    job_name: str = ""
    mode: str = MODE_FULL
    trigger: str = ""
    ok: bool = True
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    copied: int = 0            # 新增
    updated: int = 0           # 覆盖更新
    removed: int = 0           # 从镜像里移除（镜像模式）
    skipped: int = 0
    bytes_copied: int = 0
    folder: str = ""           # 这次产出/更新的备份文件夹（完整备份的名字 / 镜像的名字）
    pruned: int = 0            # 清理掉的旧完整备份份数
    errors: list[str] = field(default_factory=list)
    message: str = ""

    @property
    def duration(self) -> float:
        return max(0.0, (self.finished_at or time.time()) - self.started_at)

    @property
    def changed(self) -> bool:
        return bool(self.copied or self.updated or self.removed)

    def summary(self) -> str:
        if not self.ok:
            return f"失败：{self.message or '；'.join(self.errors[:2])}"
        if not self.changed:
            return "无变化"
        parts = []
        if self.copied:
            parts.append(f"新增 {self.copied}")
        if self.updated:
            parts.append(f"更新 {self.updated}")
        if self.removed:
            parts.append(f"删除 {self.removed}")
        if self.pruned:
            parts.append(f"清理旧备份 {self.pruned} 份")
        return "，".join(parts)

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "job_name": self.job_name,
            "mode": self.mode,
            "mode_label": MODE_LABEL.get(self.mode, self.mode),
            "trigger": self.trigger,
            "ok": self.ok,
            "copied": self.copied,
            "updated": self.updated,
            "removed": self.removed,
            "skipped": self.skipped,
            "bytes_copied": self.bytes_copied,
            "folder": self.folder,
            "pruned": self.pruned,
            "duration": round(self.duration, 2),
            "summary": self.summary(),
            "errors": self.errors[:10],
            "message": self.message,
            "finished_at": self.finished_at,
        }


@dataclass
class RestoreResult:
    ok: bool = True
    backup: str = ""
    restored: int = 0
    renamed: int = 0
    dest: str = ""             # 让界面恢复完自动打开这个文件夹
    errors: list[str] = field(default_factory=list)
    message: str = ""

    def summary(self) -> str:
        if not self.ok:
            return f"失败：{self.message or '；'.join(self.errors[:2])}"
        parts = [f"恢复 {self.restored} 项"]
        if self.renamed:
            parts.append(f"其中 {self.renamed} 项因重名已改名另存")
        return "，".join(parts)

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "backup": self.backup,
            "restored": self.restored,
            "renamed": self.renamed,
            "dest": self.dest,
            "errors": self.errors[:10],
            "message": self.message,
            "summary": self.summary(),
        }


# ---------------------------------------------------------------- 任务锁

_job_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_pending: set[tuple[str, str]] = set()


def _lock_for(job_id: str) -> threading.Lock:
    with _locks_guard:
        if job_id not in _job_locks:
            _job_locks[job_id] = threading.Lock()
        return _job_locks[job_id]


def mark_pending(job_id: str, mode: str) -> None:
    with _locks_guard:
        _pending.add((job_id, mode))


def _take_pending(job_id: str, mode: str) -> bool:
    with _locks_guard:
        key = (job_id, mode)
        if key in _pending:
            _pending.discard(key)
            return True
        return False


# ---------------------------------------------------------------- 文件操作

def _copy_file(src: str, dst: str) -> int:
    """原子复制：先写临时文件，再替换目标。返回复制的字节数。"""
    os.makedirs(_lp(os.path.dirname(dst)), exist_ok=True)
    tmp = dst + TMP_SUFFIX
    last_err: Exception | None = None
    for attempt in range(1, COPY_ATTEMPTS + 1):
        try:
            shutil.copy2(_lp(src), _lp(tmp))
            os.replace(_lp(tmp), _lp(dst))
            try:
                return os.path.getsize(_lp(dst))
            except OSError:
                return 0
        except Exception as e:                       # noqa: BLE001
            last_err = e
            try:
                if os.path.exists(_lp(tmp)):
                    os.remove(_lp(tmp))
            except Exception:
                pass
            if attempt < COPY_ATTEMPTS:
                time.sleep(0.4 * attempt)
    raise last_err if last_err else RuntimeError("复制失败")


def _needs_copy(src: str, dst: str) -> bool:
    if not os.path.exists(_lp(dst)):
        return True
    try:
        ss = os.stat(_lp(src))
        ds = os.stat(_lp(dst))
    except Exception:
        return True
    if ss.st_size != ds.st_size:
        return True
    return abs(ss.st_mtime - ds.st_mtime) > MTIME_TOLERANCE


def walk_files(root: str, excludes: list[str], skip_names: set[str] | None = None):
    """递归产 (绝对路径, 相对路径)。跳过排除项、联接点、指定的文件名。"""
    skip_names = skip_names or set()
    stack = [(root, "")]
    while stack:
        cur, rel = stack.pop()
        try:
            entries = list(os.scandir(_lp(cur)))
        except Exception as e:                        # noqa: BLE001
            log.warning("无法读取目录 %s：%s", cur, e)
            continue
        for entry in entries:
            name = entry.name
            child_rel = f"{rel}\\{name}" if rel else name
            if name in skip_names:
                continue
            try:
                if entry.is_dir(follow_symlinks=False):
                    if _is_reparse(entry):
                        continue
                    if match_exclude(name, child_rel, excludes):
                        continue
                    stack.append((entry.path, child_rel))
                elif entry.is_file(follow_symlinks=False):
                    if match_exclude(name, child_rel, excludes):
                        continue
                    yield entry.path, child_rel
            except Exception as e:                    # noqa: BLE001
                log.warning("扫描条目失败 %s：%s", entry.path, e)


def fingerprint(root: str, excludes: list[str]) -> dict[str, tuple[int, int]]:
    """给源文件夹做一份轻量指纹（相对路径 → 大小 + 修改时间），用于判断"有没有变动"。"""
    out: dict[str, tuple[int, int]] = {}
    for abs_p, rel in walk_files(root, excludes):
        try:
            st = os.stat(_lp(abs_p))
        except OSError:
            continue
        out[rel] = (st.st_size, int(st.st_mtime))
    return out


# ---------------------------------------------------------------- 备份文件夹

def _stamp() -> str:
    return datetime.now().strftime(STAMP_FORMAT)


def _write_marker(folder: str, marker: str, data: dict) -> None:
    """在备份文件夹里放一个标记文件。清理时**只删带标记的**，这是安全底线。"""
    try:
        p = os.path.join(folder, marker)
        tmp = p + ".tmp"
        with open(_lp(tmp), "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(_lp(tmp), _lp(p))
    except Exception as e:                            # noqa: BLE001
        log.warning("写标记文件失败 %s：%s", folder, e)


def _read_marker(folder: str, marker: str) -> dict | None:
    p = os.path.join(folder, marker)
    if not os.path.isfile(_lp(p)):
        return None
    try:
        with open(_lp(p), "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:                                 # noqa: BLE001
        return {}


def is_our_folder(folder: str, marker: str) -> bool:
    return _read_marker(folder, marker) is not None


def unique_folder(target: str, base_name: str) -> str:
    """在 target 下取一个不冲突的文件夹名（同一分钟重复触发时加 _2、_3）。"""
    cand = os.path.join(target, base_name)
    if not os.path.exists(_lp(cand)):
        return cand
    for i in range(2, 100):
        cand = os.path.join(target, f"{base_name}_{i}")
        if not os.path.exists(_lp(cand)):
            return cand
    return os.path.join(target, f"{base_name}_{os.urandom(2).hex()}")


def list_full_backups(target: str) -> list[dict]:
    """列出所有「完整的」备份文件夹（只认带标记的），新的在前。"""
    out: list[dict] = []
    if not os.path.isdir(_lp(target)):
        return out
    try:
        names = os.listdir(_lp(target))
    except OSError:
        return out
    for name in names:
        d = os.path.join(target, name)
        if not os.path.isdir(_lp(d)):
            continue
        data = _read_marker(d, MARKER_FULL)
        if data is None:
            continue                                  # 不是我们建的，不列、不碰
        files = 0
        total = 0
        for cur, _dirs, fnames in os.walk(_lp(d)):
            for fn in fnames:
                if fn == MARKER_FULL:
                    continue
                try:
                    st = os.stat(_lp(os.path.join(cur, fn)))
                except OSError:
                    continue
                files += 1
                total += st.st_size
        out.append({
            "name": name,
            "path": d,
            "created": float(data.get("created") or 0),
            "source": str(data.get("source") or ""),
            "ok": bool(data.get("ok", True)),
            "error_count": int(data.get("error_count") or 0),
            "files": files,
            "bytes": total,
        })
    # ⚠️ 按「实际创建时间」排序，**不要按名字排**：
    # 名字只精确到分钟，同一分钟内多次备份时，清理释放掉的名字会被复用，
    # 按名字排会把刚建好的那份当成"最旧的"而立刻删掉（自测里真出现过）。
    out.sort(key=lambda x: (x["created"], x["name"]), reverse=True)
    return out


def find_mirror_folder(target: str) -> str | None:
    """在备份文件夹里找已有的镜像文件夹（配置丢了好复用，不会又建一个）。"""
    if not os.path.isdir(_lp(target)):
        return None
    try:
        names = sorted(os.listdir(_lp(target)), reverse=True)
    except OSError:
        return None
    for name in names:
        d = os.path.join(target, name)
        if os.path.isdir(_lp(d)) and _read_marker(d, MARKER_MIRROR) is not None:
            return d
    return None


def prune_full_backups(target: str, keep_recent: int) -> int:
    """只保留最近 N 份完整备份。**只删带标记的文件夹**，其他一律不动。"""
    keep = max(0, int(keep_recent or 0))
    if keep <= 0:
        return 0
    items = list_full_backups(target)          # 已按名字倒序（最新在前）
    removed = 0
    for item in items[keep:]:
        d = item["path"]
        if not is_our_folder(d, MARKER_FULL):  # 再确认一次，双保险
            continue
        try:
            shutil.rmtree(_lp(d))
            removed += 1
            log.info("清理旧完整备份：%s", item["name"])
        except Exception as e:                 # noqa: BLE001
            log.warning("清理失败 %s：%s", d, e)
    return removed


# ---------------------------------------------------------------- 公共校验

def _prepare(job: dict) -> tuple[str, str] | str:
    """校验并返回 (源, 目标)。不合格时返回错误文案。"""
    src = str(job.get("source") or "").strip()
    dst = str(job.get("target") or "").strip()
    if not src or not dst:
        return "源文件夹或备份文件夹未设置"
    src, dst = norm_dir(src), norm_dir(dst)
    if not os.path.isdir(_lp(src)):
        return f"源文件夹不存在：{src}"
    if src == dst or is_subpath(dst, src) or is_subpath(src, dst):
        return "源文件夹与备份文件夹不能互相包含（必须分开）"
    if not os.path.exists(_lp(dst)):
        try:
            os.makedirs(_lp(dst), exist_ok=True)
        except Exception as e:                        # noqa: BLE001
            return f"无法创建备份文件夹：{e}"
    if not os.path.isdir(_lp(dst)):
        return "备份文件夹不是一个目录"
    return (src, dst)


# ---------------------------------------------------------------- 完整备份

def run_full(job: dict, trigger: str = "手动", dry_run: bool = False) -> JobResult:
    """完整备份：新建一个带时间戳的文件夹，把源里所有内容拷进去，然后清理超额旧份。"""
    res = JobResult(job_id=str(job.get("id") or "?"), job_name=str(job.get("name") or ""),
                    mode=MODE_FULL, trigger=trigger)
    if not job.get("enabled", True):
        res.ok, res.message = False, "任务已停用"
        res.finished_at = time.time()
        return res

    prep = _prepare(job)
    if isinstance(prep, str):
        res.ok, res.message = False, prep
        res.finished_at = time.time()
        return res
    src, dst = prep

    excludes = list(job.get("exclude") or [])
    target_name = os.path.basename(dst.rstrip("\\/")) or "备份"
    folder = unique_folder(dst, target_name + _stamp())
    res.folder = os.path.basename(folder)

    log.info("开始%s [%s] 触发=%s → %s", MODE_LABEL[MODE_FULL], res.job_name, trigger, res.folder)
    try:
        if not dry_run:
            os.makedirs(_lp(folder), exist_ok=True)
            _write_marker(folder, MARKER_FULL, {
                "kind": "full", "created": time.time(), "source": src, "ok": False, "errors": [],
            })

        for abs_src, rel in walk_files(src, excludes):
            abs_dst = os.path.join(folder, rel)
            try:
                if not dry_run:
                    res.bytes_copied += _copy_file(abs_src, abs_dst)
                res.copied += 1
            except Exception as e:                    # noqa: BLE001
                res.errors.append(f"{rel}: {e}")
                log.warning("复制失败 %s：%s", rel, e)

        all_excludes = excludes + [MARKER_FULL]
        files_after = sum(1 for _ in walk_files(folder, all_excludes)) if not dry_run else res.copied
        if not dry_run and files_after == 0 and not res.errors:
            # 源是空的：仍然保留这一份（表示"那一刻是空的"），但记一笔
            log.info("源文件夹是空的，%s 里只写了标记文件", res.folder)

        if not dry_run:
            _write_marker(folder, MARKER_FULL, {
                "kind": "full", "created": time.time(), "source": src,
                "ok": not res.errors, "errors": res.errors[:20],
                "files": files_after,
            })

        # 清理：只留最近 N 份（只删带标记的）
        if not dry_run:
            keep = int((job.get("full") or {}).get("keep_recent") or 0)
            res.pruned = prune_full_backups(dst, keep)
    except Exception as e:                            # noqa: BLE001
        res.ok = False
        res.message = str(e)
        log.exception("%s异常 [%s]", MODE_LABEL[MODE_FULL], res.job_name)

    res.ok = res.ok and not res.errors
    if res.errors:
        res.message = f"{len(res.errors)} 个文件没能复制"
    res.finished_at = time.time()
    log.info("%s完成 [%s] %s（耗时 %.1fs）", MODE_LABEL[MODE_FULL], res.job_name,
             res.summary(), res.duration)
    return res


# ---------------------------------------------------------------- 镜像备份

def run_mirror(job: dict, trigger: str = "手动", dry_run: bool = False) -> JobResult:
    """镜像备份：只保留一份镜像文件夹，只同步差异，并把源里已删的文件从镜像里删掉。"""
    res = JobResult(job_id=str(job.get("id") or "?"), job_name=str(job.get("name") or ""),
                    mode=MODE_MIRROR, trigger=trigger)
    if not job.get("enabled", True):
        res.ok, res.message = False, "任务已停用"
        res.finished_at = time.time()
        return res

    prep = _prepare(job)
    if isinstance(prep, str):
        res.ok, res.message = False, prep
        res.finished_at = time.time()
        return res
    src, dst = prep

    excludes = list(job.get("exclude") or [])
    cfg = job.get("mirror") or {}
    folder = str(cfg.get("folder") or "").strip()

    # 没配过、或配的那个不在了 → 复用已存在的镜像；真的没有才新建一个
    if not folder or not os.path.isdir(_lp(folder)):
        existing = find_mirror_folder(dst)
        if existing:
            folder = existing
        else:
            target_name = os.path.basename(dst.rstrip("\\/")) or "备份"
            # 名字不带时间：镜像只有一份、建好就一直复用，带时间反而会"过期"让人误会
            folder = unique_folder(dst, f"{target_name}-镜像")
            if not dry_run:
                os.makedirs(_lp(folder), exist_ok=True)
    res.folder = os.path.basename(folder)

    log.info("开始%s [%s] 触发=%s → %s", MODE_LABEL[MODE_MIRROR], res.job_name, trigger, res.folder)
    try:
        if not dry_run:
            os.makedirs(_lp(folder), exist_ok=True)
            _write_marker(folder, MARKER_MIRROR, {
                "kind": "mirror", "created": time.time(), "source": src, "synced": 0,
            })

        # 1. 源里有 → 镜像里没有的复制过去，有变化的更新
        for abs_src, rel in walk_files(src, excludes):
            abs_dst = os.path.join(folder, rel)
            try:
                if not os.path.exists(_lp(abs_dst)):
                    if not dry_run:
                        res.bytes_copied += _copy_file(abs_src, abs_dst)
                    res.copied += 1
                elif _needs_copy(abs_src, abs_dst):
                    if not dry_run:
                        res.bytes_copied += _copy_file(abs_src, abs_dst)
                    res.updated += 1
                else:
                    res.skipped += 1
            except Exception as e:                    # noqa: BLE001
                res.errors.append(f"{rel}: {e}")
                log.warning("同步失败 %s：%s", rel, e)

        # 2. 源里没有、镜像里还有的 → 删掉（真镜像）
        for abs_dst, rel in walk_files(folder, excludes, {MARKER_MIRROR}):
            abs_src = os.path.join(src, rel)
            if os.path.exists(_lp(abs_src)):
                continue
            try:
                if not dry_run:
                    os.remove(_lp(abs_dst))
                res.removed += 1
            except Exception as e:                    # noqa: BLE001
                res.errors.append(f"删除 {rel}: {e}")
                log.warning("删除失败 %s：%s", rel, e)

        if not dry_run:
            _prune_empty_dirs(folder, {MARKER_MIRROR})
            _write_marker(folder, MARKER_MIRROR, {
                "kind": "mirror", "created": _read_marker(folder, MARKER_MIRROR).get("created", time.time()),
                "source": src, "synced": time.time(),
            })
    except Exception as e:                            # noqa: BLE001
        res.ok = False
        res.message = str(e)
        log.exception("%s异常 [%s]", MODE_LABEL[MODE_MIRROR], res.job_name)

    res.ok = res.ok and not res.errors
    if res.errors:
        res.message = f"{len(res.errors)} 个文件没能同步"
    res.finished_at = time.time()
    log.info("%s完成 [%s] %s（耗时 %.1fs）", MODE_LABEL[MODE_MIRROR], res.job_name,
             res.summary(), res.duration)
    return res


def _prune_empty_dirs(root: str, skip_names: set[str]) -> None:
    """自底向上删除空目录（保留 root 本身）。"""
    for cur, _dirs, _files in os.walk(_lp(root), topdown=False):
        if os.path.normcase(cur) == os.path.normcase(_lp(root)):
            continue
        name = os.path.basename(cur)
        if name in skip_names:
            continue
        try:
            if not os.listdir(cur):
                os.rmdir(cur)
        except Exception:
            pass


# ---------------------------------------------------------------- 统一入口

def run_job(job: dict, mode: str = MODE_FULL, trigger: str = "手动",
            dry_run: bool = False) -> JobResult:
    """执行一次备份。同一任务串行执行；同模式并发触发会被合并，跑完补跑一次。"""
    job_id = str(job.get("id") or "?")
    runner = run_mirror if mode == MODE_MIRROR else run_full

    lock = _lock_for(job_id)
    if not lock.acquire(blocking=False):
        mark_pending(job_id, mode)
        res = JobResult(job_id=job_id, job_name=str(job.get("name") or ""), mode=mode,
                        trigger=trigger, ok=False)
        res.message = "已有备份正在进行，本次触发已合并"
        res.finished_at = time.time()
        return res

    try:
        res = runner(job, trigger=trigger, dry_run=dry_run)
        return res
    finally:
        lock.release()
        if _take_pending(job_id, mode):
            log.info("检测到合并的触发，立即补跑一次 [%s %s]", job.get("name"), MODE_LABEL.get(mode))
            threading.Thread(target=run_job, args=(job, mode),
                             kwargs={"trigger": "补跑", "dry_run": dry_run},
                             daemon=True).start()


# ================================================================ 恢复
#
# 恢复到「源文件夹旁边同一层」，用带时间戳的名字，**绝不覆盖任何现有文件**。
# 单个文件恢复时若源里已有同名文件，自动改名另存 <名字>_恢复<时间戳><扩展名>。

def list_backup_tree(target: str, name: str) -> dict:
    """把某个完整备份文件夹的内容列成一棵树，供界面展开浏览。"""
    ok, folder = _safe_backup_dir(target, name)
    if not ok:
        return {"ok": False, "error": folder}
    root: dict = {"name": name, "type": "folder", "children": []}

    def _walk(abs_dir: str, node: dict) -> None:
        try:
            entries = sorted(os.scandir(_lp(abs_dir)), key=lambda e: (not e.is_dir(), e.name.lower()))
        except Exception:                             # noqa: BLE001
            return
        for entry in entries:
            if entry.name == MARKER_FULL:
                continue
            rel = os.path.relpath(entry.path, folder).replace("\\", "/")
            try:
                if entry.is_dir(follow_symlinks=False):
                    child = {"name": entry.name, "type": "folder", "rel": rel, "children": []}
                    node["children"].append(child)
                    _walk(entry.path, child)
                elif entry.is_file(follow_symlinks=False):
                    st = entry.stat()
                    node["children"].append({
                        "name": entry.name, "type": "file", "rel": rel,
                        "size": st.st_size, "mtime": st.st_mtime,
                    })
            except Exception:                         # noqa: BLE001
                continue

    _walk(folder, root)
    return {"ok": True, "tree": root, "files": len(list(walk_files(folder, [], {MARKER_FULL})))}


def _safe_backup_dir(target: str, name: str) -> tuple[bool, str]:
    """把备份名解析成绝对目录。

    返回 (是否成功, 目录或错误文案)。**不要用"返回值是不是字符串"判断成败** ——
    成功时返回的目录也是字符串，之前正是这么写错的，导致恢复功能整个失效。
    """
    n = str(name or "").strip()
    if not n or n in (".", "..") or any(c in n for c in '\\/:*?"<>|'):
        return False, "备份名不合法"
    d = os.path.join(target, n)
    if not is_subpath(d, target) or norm_dir(d) == norm_dir(target):
        return False, "备份名不合法"
    if not os.path.isdir(_lp(d)):
        return False, "这份备份不存在"
    if _read_marker(d, MARKER_FULL) is None:
        return False, "这不是本软件创建的备份文件夹"
    return True, d


def _unique_path(path: str) -> tuple[str, bool]:
    """目标已存在时改名另存。返回 (最终路径, 是否改了名)。"""
    if not os.path.exists(_lp(path)):
        return path, False
    d, name = os.path.split(path)
    stem, ext = os.path.splitext(name)
    stamp = datetime.now().strftime(STAMP_FORMAT)
    cand = os.path.join(d, f"{stem}_恢复{stamp}{ext}")
    i = 2
    while os.path.exists(_lp(cand)):
        cand = os.path.join(d, f"{stem}_恢复{stamp}_{i}{ext}")
        i += 1
    return cand, True


def restore_backup(job: dict, name: str, rels: list[str] | None = None) -> RestoreResult:
    """恢复。

    rels 为空 → 整批恢复：把整个备份文件夹复制到**源文件夹旁边同一层**（名字原样，重名则加 _恢复<时间>）。
    rels 有值 → 逐项恢复：复制回源文件夹内原来的相对位置，**重名自动改名另存，绝不覆盖**。
    """
    res = RestoreResult(backup=str(name or ""))
    target = str(job.get("target") or "").strip()
    src = str(job.get("source") or "").strip()
    if not target or not os.path.isdir(_lp(target)):
        res.ok, res.message = False, "备份文件夹不存在"
        return res

    ok_dir, folder = _safe_backup_dir(target, name)
    if not ok_dir:
        res.ok, res.message = False, folder
        return res

    try:
        if not rels:
            # 整批：放到源文件夹旁边（同一层），不覆盖任何东西
            parent = os.path.dirname(norm_dir(src)) if src else target
            if not os.path.isdir(_lp(parent)):
                parent = target
            dest, renamed = _unique_path(os.path.join(parent, os.path.basename(folder)))
            shutil.copytree(_lp(folder), _lp(dest),
                            ignore=shutil.ignore_patterns(MARKER_FULL), dirs_exist_ok=False)
            res.restored = 1
            res.renamed = 1 if renamed else 0
            res.dest = dest
            log.info("整批恢复 → %s", dest)
            return res

        # 逐项：放回源文件夹里原来的位置；重名就改名另存
        if not src or not os.path.isdir(_lp(src)):
            res.ok, res.message = False, "源文件夹不存在，无法放回"
            return res
        for rel in rels:
            rel_clean = str(rel or "").replace("/", os.sep).lstrip("\\/")
            if not rel_clean or ".." in rel_clean.split(os.sep):
                res.errors.append(f"{rel}: 路径不合法")
                continue
            from_path = os.path.join(folder, rel_clean)
            if not os.path.isfile(_lp(from_path)):
                res.errors.append(f"{rel}: 这份备份里没有这个文件")
                continue
            to_path = os.path.join(src, rel_clean)
            final, renamed = _unique_path(to_path)
            try:
                _copy_file(from_path, final)
                res.restored += 1
                res.renamed += 1 if renamed else 0
            except Exception as e:                    # noqa: BLE001
                res.errors.append(f"{rel}: {e}")
        res.dest = norm_dir(src)
        res.ok = not res.errors
        if res.errors:
            res.message = f"{len(res.errors)} 项没能恢复"
        log.info("逐项恢复 %d 项 → %s", res.restored, res.dest)
        return res
    except Exception as e:                            # noqa: BLE001
        res.ok, res.message = False, str(e)
        log.exception("恢复失败")
        return res
