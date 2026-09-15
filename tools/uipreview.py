"""
界面预览：造一份演示数据 + 起设置界面，专门用来看「完整备份 / 镜像备份 / 恢复」长什么样。

- 演示数据全部在 `_t\\uipreview\\` 下面，通过环境变量 `XQB_DATA_DIR` 隔离，
  **绝不碰你的正式配置和正式备份文件夹**。
- 备份文件夹是**真的用备份引擎跑出来的**（不是手捏几个假目录），所以看到的就是真实行为。

用法
----
看效果（会打开浏览器）：
    python tools/uipreview.py

只自检（起服务、打接口、跑一次恢复，然后退出）：
    python tools/uipreview.py --check

按 Ctrl+C 结束预览。
"""
from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

DEMO = ROOT / "_t" / "uipreview"
CHECK = "--check" in sys.argv
os.environ["XQB_DATA_DIR"] = str(DEMO / "data")
os.environ["XQB_NO_OPEN"] = "1"          # 预览/自检都别真去弹资源管理器
if CHECK:
    os.environ["XQB_NO_BROWSER"] = "1"

import config                                    # noqa: E402
config.setup_logging()

import engine                                    # noqa: E402
from trayapp import Controller                    # noqa: E402
from webui import serve, open_in_browser           # noqa: E402

SRC = DEMO / "演示·源文件夹"
DST = DEMO / "演示·工作文件备份"
PORT = 45899
JOB_ID = "preview"


def _w(p: Path, text: str) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def build_demo(job: dict) -> None:
    """真的跑几遍引擎，造出几份完整备份和一个镜像文件夹。"""
    SRC.mkdir(parents=True, exist_ok=True)
    DST.mkdir(parents=True, exist_ok=True)

    # ① 第一次完整备份
    _w(SRC / "图纸" / "客厅立面.dwg.txt", "客厅立面 · 第一版")
    _w(SRC / "报价单.txt", "报价单 v1")
    engine.run_full(job, trigger="预览")

    # ② 改一个、加一个 → 再完整备份一份（这样恢复界面里就有两个日期可选）
    time.sleep(1.1)
    _w(SRC / "图纸" / "客厅立面.dwg.txt", "客厅立面 · 第二版（现在源里是这个）")
    _w(SRC / "施工说明.txt", "施工说明 · 第一版")
    engine.run_full(job, trigger="预览")

    # ③ 镜像备份也跑一次，让镜像文件夹出现
    engine.run_mirror(job, trigger="预览")


def make_job() -> dict:
    job = config.new_job("演示任务（预览用，可随便点）")
    job["id"] = JOB_ID
    job["source"] = str(SRC)
    job["target"] = str(DST)
    for m in ("full", "mirror"):
        # 预览脚本不想要后台一直动：触发都关掉，只看备份产物和恢复
        job[m]["schedule"] = {"enabled": False}
        job[m]["watch"] = {"enabled": False, "check_interval_minutes": 5}
    job["full"]["enabled"] = True
    job["full"]["keep_recent"] = 5
    job["mirror"]["enabled"] = True
    return job


def self_check(url: str) -> int:
    import urllib.parse
    import urllib.request

    def get(path: str) -> dict:
        with urllib.request.urlopen(url + path, timeout=30) as r:
            return json.loads(r.read().decode("utf-8"))

    def post(path: str, body: dict) -> dict:
        req = urllib.request.Request(
            url + path, data=json.dumps(body).encode("utf-8"), method="POST")
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    html = urllib.request.urlopen(url, timeout=20).read().decode("utf-8")
    print("页面 200，两种模式都在:", ("完整备份" in html and "镜像备份" in html))

    s = get("api/state")
    print("任务数:", len(s.get("jobs") or []))

    b = get(f"api/backups?id={JOB_ID}")
    names = [x["name"] for x in (b.get("backups") or [])]
    print("完整备份:", names)
    if not names:
        print("!! 没有造出完整备份")
        return 1

    t = get(f"api/backups?id={JOB_ID}&name=" + urllib.parse.quote(names[0], safe=""))
    tree = (t.get("tree") or {}).get("children") or []
    print("最新那份里的顶层内容:", [n["name"] for n in tree])
    if not tree:
        print("!! 备份里没有内容")
        return 1

    res = post("api/restore", {"id": JOB_ID, "name": names[0]})
    print("整批恢复:", res.get("summary"), "| ok =", res.get("ok"), "| dest =", res.get("dest"))
    mirrors = [d.name for d in DST.iterdir() if d.is_dir() and "-镜像" in d.name]
    print("镜像文件夹:", mirrors)
    return 0 if (res.get("ok") and mirrors) else 1


def main() -> int:
    c = Controller()
    job = make_job()
    build_demo(job)
    c.save_config({"app": {"port": PORT}, "jobs": [job]})
    httpd, url = serve(c, PORT)

    if CHECK:
        try:
            code = self_check(url)
        finally:
            httpd.shutdown()
        print("自检退出码:", code)
        return code

    print("=" * 62)
    print("预览已就绪：", url)
    print("点任务上的「恢复」就能挑日期、展开文件夹、试恢复。")
    print("演示数据在：", DEMO)
    print("按 Ctrl+C 结束预览。")
    print("=" * 62)
    if not os.environ.get("XQB_NO_BROWSER"):
        open_in_browser(url)
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        httpd.shutdown()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
