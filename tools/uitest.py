"""
端到端自测（0.2.0 模型）：设置界面(HTTP) + 配置保存 + 两种备份模式 + 恢复，全链路走一遍。
运行： python tools/uitest.py     结果写入 _uitest_report.txt
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

TEST = ROOT / "_t" / (time.strftime("%Y%m%d_%H%M%S") + "_uitest")
os.environ["XQB_DATA_DIR"] = str(TEST / "data")
os.environ["XQB_NO_BROWSER"] = "1"
os.environ["XQB_NO_OPEN"] = "1"          # 恢复完不要真的弹资源管理器

import config  # noqa: E402
config.setup_logging()

import engine             # noqa: E402
from trayapp import Controller   # noqa: E402
from webui import serve          # noqa: E402

SRC = TEST / "源"
DST = TEST / "备份"
REPORT = ROOT / "_uitest_report.txt"

lines: list[str] = []
FAILED: list[str] = []


def out(s: str = "") -> None:
    lines.append(s)


def check(cond: bool, label: str, extra: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(label + ("  " + extra if extra else ""))
    out(f"  [{mark}] {label}" + (f"  ({extra})" if extra else ""))


def w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def http(url: str, method: str = "GET", body: dict | None = None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read()
        ctype = r.headers.get("Content-Type", "")
        if "json" in ctype:
            return r.status, json.loads(raw.decode("utf-8"))
        return r.status, raw.decode("utf-8")


def job(job_id: str, name: str, full_on: bool = True, mirror_on: bool = False) -> dict:
    j = config.new_job(name)
    j["id"] = job_id
    j["source"] = str(SRC)
    j["target"] = str(DST)
    j["exclude"] = ["*.tmp"]
    j["full"]["enabled"] = full_on
    j["full"]["keep_recent"] = 3
    j["mirror"]["enabled"] = mirror_on
    for m in ("full", "mirror"):
        # 触发都关掉：这一段测的是接口和引擎，触发另有专段
        j[m]["schedule"] = {"enabled": False, "type": "interval",
                            "interval_minutes": 60, "daily_time": "02:00"}
        j[m]["watch"] = {"enabled": False, "check_interval_minutes": 5}
    return j


def wait_result(url: str, key: str, timeout: float = 40):
    """等某个 任务:模式 出现执行结果。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(0.4)
        _, s = http(url + "api/state")
        res = (s["runtime"].get("results") or {}).get(key)
        if res:
            return res
    return None


def main() -> int:
    TEST.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    DST.mkdir(parents=True, exist_ok=True)
    (TEST / "data").mkdir(parents=True, exist_ok=True)

    out("=" * 60)
    out("端到端自测（界面 + 两种备份模式 + 恢复）")
    out(f"数据目录：{TEST / 'data'}")
    out("=" * 60)

    c = Controller()
    httpd, url = serve(c, 45788)
    out(f"\n[0] 服务已启动：{url}")

    try:
        # ---------------------------------------------------- 1 页面
        out("\n[1] 设置界面可访问，且是新的布局")
        st, html = http(url)
        check(st == 200, "首页返回 200", str(st))
        check("完整备份" in html and "镜像备份" in html, "页面上有两种备份模式")
        check("文件留存" in html, "页面上有「文件留存」")
        check("打开备份夹" not in html, "顶部不再有「打开备份夹」（已按用户要求删掉）")
        check("恢复" in html and "历史版本" not in html, "入口从「历史版本」改成了「恢复」")

        # ---------------------------------------------------- 2 初始状态
        out("\n[2] /api/state 结构")
        st, s = http(url + "api/state")
        check(st == 200, "返回 200")
        check(set(["app", "jobs", "runtime"]) <= set(s.keys()), "含 app/jobs/runtime")
        check("status" in s["runtime"] and "results" in s["runtime"],
              "runtime 里带各模式的状态与结果")

        # ---------------------------------------------------- 3 保存配置
        out("\n[3] 保存配置（完整备份 + 镜像备份 各一个任务）")
        st, r = http(url + "api/save", "POST",
                     {"app": {"port": 45788},
                      "jobs": [job("j1", "完整备份任务", True, False),
                               job("j2", "镜像任务", False, True)]})
        check(r.get("ok") is True, "保存返回 ok", str(r))
        _, s = http(url + "api/state")
        check(len(s["jobs"]) == 2, "配置里有 2 个任务", str(len(s["jobs"])))
        stt = s["runtime"]["status"]["j1"]
        check(stt["full"]["enabled"] is True and stt["mirror"]["enabled"] is False,
              "任务1 只开了完整备份")
        stt2 = s["runtime"]["status"]["j2"]
        check(stt2["mirror"]["enabled"] is True and stt2["full"]["enabled"] is False,
              "任务2 只开了镜像备份")
        check(Path(s["runtime"]["config_path"]).exists(), "配置文件已落盘")

        # ---------------------------------------------------- 4 完整备份
        out("\n[4] 手动跑一次完整备份：备份文件夹里出现带时间戳的文件夹")
        w(SRC / "文档.txt", "第一版内容")
        w(SRC / "子目录" / "图纸.txt", "图纸内容")
        http(url + "api/run", "POST", {"id": "j1", "mode": "full"})
        res = wait_result(url, "j1:full")
        check(res is not None, "产生了执行结果")
        if res:
            check(res.get("ok") is True, "执行成功", str(res.get("summary")))
            check(res.get("mode") == "full", "结果里标了模式 full")
            check(res.get("copied") == 2, "拷了 2 个文件", f"copied={res.get('copied')}")
        backups = engine.list_full_backups(str(DST))
        check(len(backups) == 1, "备份文件夹里有 1 份完整备份",
              str([b["name"] for b in backups]))
        b0 = backups[0] if backups else None
        check(bool(b0) and b0["name"].startswith(DST.name), "文件夹名以备份夹名开头",
              b0["name"] if b0 else "")
        check(bool(b0) and (Path(b0["path"]) / "文档.txt").exists(), "内容确实在里面")

        # ---------------------------------------------------- 5 镜像备份
        out("\n[5] 手动跑镜像：建一个镜像文件夹；再跑一次还是同一个（复用）")
        http(url + "api/run", "POST", {"id": "j2", "mode": "mirror"})
        res = wait_result(url, "j2:mirror")
        check(res is not None and res.get("ok") is True, "镜像执行成功",
              str(res.get("summary")) if res else "")
        mirrors = [d for d in DST.iterdir() if d.is_dir() and "-镜像" in d.name]
        check(len(mirrors) == 1, "建了 1 个镜像文件夹", str([d.name for d in mirrors]))
        check(bool(mirrors) and not any(c.isdigit() for c in mirrors[0].name),
              "镜像文件夹名不带时间（只有一份、不会过期）",
              mirrors[0].name if mirrors else "")
        first = mirrors[0].name if mirrors else ""
        w(SRC / "文档.txt", "第二版内容，改长了一点")
        w(SRC / "新增.txt", "新加的")
        http(url + "api/run", "POST", {"id": "j2", "mode": "mirror"})
        res2 = wait_result(url, "j2:mirror")
        time.sleep(0.6)
        mirrors2 = [d for d in DST.iterdir() if d.is_dir() and "-镜像" in d.name]
        check(len(mirrors2) == 1, "还是只有 1 个镜像文件夹（复用，没多建）",
              str([d.name for d in mirrors2]))
        check(bool(mirrors2) and mirrors2[0].name == first, "用的还是原来那个", first)
        base = DST / first
        check((base / "新增.txt").exists(), "新增的文件同步过去了")
        check((base / "文档.txt").read_text(encoding="utf-8").startswith("第二版"),
              "变化的内容更新了")

        # ---------------------------------------------------- 6 变动扫描（指纹）
        out("\n[6] 变动触发：扫描能不能发现变化")
        fp1 = engine.fingerprint(str(SRC), [])
        fp2 = engine.fingerprint(str(SRC), [])
        check(fp1 == fp2, "原地再扫一次：指纹相同（不会误报）")
        w(SRC / "又加了.txt", "x")
        fp3 = engine.fingerprint(str(SRC), [])
        check(fp3 != fp1, "加了文件：指纹变了")
        st, s = http(url + "api/state")
        check("watch_check_interval" in s["runtime"]["status"]["j2"]["mirror"],
              "状态里带「每几分钟检查一次」")

        # ---------------------------------------------------- 7 恢复：列出与树
        out("\n[7] 恢复界面用的接口")
        st, r = http(url + "api/backups?id=j1")
        check(r.get("ok") is True and len(r.get("backups") or []) >= 1,
              "能列出可选的备份", str(len(r.get("backups") or [])))
        name = (r.get("backups") or [{}])[0].get("name", "")
        st, r2 = http(url + "api/backups?id=j1&name=" + urllib.parse.quote(name, safe=""))
        check(r2.get("ok") is True, "能取到某一份的文件树", str(r2.get("error") or ""))
        tree = (r2.get("tree") or {}).get("children") or []
        kinds = {n["type"] for n in tree}
        check("file" in kinds and "folder" in kinds, "树里既有文件又有文件夹", str(kinds))
        folder_node = next((n for n in tree if n["type"] == "folder"), None)
        check(bool(folder_node) and len(folder_node.get("children") or []) >= 1,
              "文件夹里能展开看到子文件")

        # ---------------------------------------------------- 8 恢复：整批
        out("\n[8] 整批恢复：整份文件夹复制到源文件夹旁边，不覆盖任何东西")
        before = set(p.name for p in SRC.parent.iterdir())
        st, r3 = http(url + "api/restore", "POST", {"id": "j1", "name": name})
        check(r3.get("ok") is True, "整批恢复成功",
              str(r3.get("message") or r3.get("error") or "")[:60])
        dest = r3.get("dest") or ""
        check(bool(dest) and Path(dest).is_dir(), "目标文件夹已生成", dest)
        check(bool(dest) and Path(dest).parent == SRC.parent,
              "位置在源文件夹旁边（同一层）")
        after = set(p.name for p in SRC.parent.iterdir())
        check(after - before == {Path(dest).name} if dest else False,
              "只多出这一个文件夹，没动别的")

        # ---------------------------------------------------- 9 恢复：单个文件
        out("\n[9] 单个文件恢复：放回原位，重名自动改名另存")
        w(SRC / "文档.txt", "现在是源里的最新内容")
        st, r4 = http(url + "api/restore", "POST",
                      {"id": "j1", "name": name, "rels": ["文档.txt"]})
        check(r4.get("ok") is True, "单项恢复成功", str(r4.get("summary")))
        check(r4.get("renamed") == 1, "检测到重名并改名另存", str(r4.get("renamed")))
        check((SRC / "文档.txt").read_text(encoding="utf-8") == "现在是源里的最新内容",
              "源里原来的文件没被动过")
        renamed = [p for p in SRC.iterdir() if p.name.startswith("文档_恢复")]
        check(len(renamed) == 1, "另存出了一个带时间戳的文件",
              str([p.name for p in renamed]))

        # ---------------------------------------------------- 10 异常路径
        out("\n[10] 异常处理")
        st, r5 = http(url + "api/restore", "POST", {"id": "j1", "name": "不是备份的文件夹"})
        check(r5.get("ok") is False, "不是本软件建的文件夹不能恢复",
              str(r5.get("message") or "")[:40])
        st, r6 = http(url + "api/run", "POST", {"id": "不存在的任务", "mode": "full"})
        check(st == 200, "不存在的任务不会让服务崩溃")
        st, r7 = http(url + "api/backups?id=" + urllib.parse.quote("不存在的任务", safe=""))
        check(r7.get("ok") is False, "查不存在的任务返回失败而不是崩")
        st, r8 = http(url + "api/save", "POST",
                      {"app": {}, "jobs": [dict(job("bad", "坏任务"), source="", target="")]})
        check(r8.get("ok") is True, "缺路径的配置仍能保存")

        # ---------------------------------------------------- 11 暂停与日志
        out("\n[11] 暂停监听 / 日志接口")
        http(url + "api/pause", "POST", {"paused": True})
        _, s = http(url + "api/state")
        check(s["runtime"]["watch_paused"] is True, "状态显示已暂停")
        http(url + "api/pause", "POST", {"paused": False})
        _, s = http(url + "api/state")
        check(s["runtime"]["watch_paused"] is False, "状态显示已恢复")
        st, r9 = http(url + "api/log?limit=50")
        check(st == 200 and isinstance(r9.get("lines"), list), "返回日志数组")
        check(len(r9["lines"]) > 0, "日志非空", f"{len(r9['lines'])} 行")

    finally:
        try:
            c.triggers.stop()
        except Exception:
            pass
        try:
            httpd.shutdown()
        except Exception:
            pass

    out("\n" + "=" * 60)
    if FAILED:
        out(f"结果：{len(FAILED)} 项未通过")
        for f in FAILED:
            out("  - " + f)
    else:
        out("结果：全部通过")
    out("=" * 60)
    return 1 if FAILED else 0


if __name__ == "__main__":
    code = 1
    try:
        code = main()
    except Exception:                                    # noqa: BLE001
        import traceback
        out("")
        out("!!! 自测自身抛出异常：")
        out(traceback.format_exc())
        FAILED.append("自测异常中断")
        code = 1
    finally:
        text = "\n".join(lines)
        REPORT.write_text(text, encoding="utf-8")
        try:
            print(text)
        except Exception:
            pass
    raise SystemExit(code)
