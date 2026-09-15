"""
备份引擎自测（0.2.0 模型）：完整备份 / 镜像备份 / 文件留存 / 恢复 / 变动指纹。
运行： python tools/selftest.py      结果写入 _selftest_report.txt
"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import config   # noqa: E402
import engine   # noqa: E402

TEST = ROOT / "_t" / (time.strftime("%Y%m%d_%H%M%S") + "_selftest")
SRC = TEST / "源"
DST = TEST / "备份"
REPORT = ROOT / "_selftest_report.txt"

lines: list[str] = []
FAILED: list[str] = []


def out(s: str = "") -> None:
    lines.append(s)


def check(cond: bool, label: str, extra: str = "") -> None:
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAILED.append(label + (" " + extra if extra else ""))
    out(f"  [{mark}] {label}" + (f"  ({extra})" if extra else ""))


def w(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def base_job(**over) -> dict:
    job = config.new_job("自测任务")
    job["id"] = "test1"
    job["source"] = str(SRC)
    job["target"] = str(DST)
    job["exclude"] = ["*.tmp"]
    for m in ("full", "mirror"):
        job[m]["schedule"] = {"enabled": False}
        job[m]["watch"] = {"enabled": False, "check_interval_minutes": 5}
    job["full"]["enabled"] = True
    job["full"]["keep_recent"] = 5
    job["mirror"]["enabled"] = False
    job.update(over)
    return job


def subdirs(p: Path) -> list[str]:
    if not p.is_dir():
        return []
    return sorted(d.name for d in p.iterdir() if d.is_dir())


def full_folders() -> list[str]:
    return sorted(b["name"] for b in engine.list_full_backups(str(DST)))


def main() -> int:
    TEST.mkdir(parents=True, exist_ok=True)
    SRC.mkdir(parents=True, exist_ok=True)
    DST.mkdir(parents=True, exist_ok=True)

    out("=" * 60)
    out("备份引擎自测（完整备份 / 镜像备份 / 文件留存 / 恢复）")
    out(f"源：{SRC}")
    out(f"备份：{DST}")
    out("=" * 60)

    # ---------------------------------------------------------- 1 完整备份
    out("\n[1] 完整备份：新建带时间戳的文件夹，源里的东西全拷进去")
    w(SRC / "a.txt", "aaa")
    w(SRC / "sub" / "b.txt", "bbbb")
    w(SRC / "skip.tmp", "临时文件应被排除")
    r = engine.run_full(base_job(), trigger="测试")
    check(r.ok, "执行成功", r.message)
    check(r.copied == 2, "拷了 2 个文件（.tmp 被排除）", f"copied={r.copied}")
    names = full_folders()
    check(len(names) == 1, "产生 1 份完整备份", str(names))
    folder = DST / names[0] if names else DST
    check(folder.name.startswith(DST.name), "文件夹名以「备份夹名」开头", folder.name)
    check(len(folder.name) >= len(DST.name) + 12, "名字里带 12 位时间戳", folder.name)
    check((folder / "a.txt").read_text(encoding="utf-8") == "aaa", "内容已拷进去")
    check((folder / "sub" / "b.txt").exists(), "子目录结构保留")
    check(not (folder / "skip.tmp").exists(), "*.tmp 被排除")
    check((folder / config.MARKER_FULL).exists(), "文件夹里放了标记文件（清理时靠它认亲）")

    # ---------------------------------------------------------- 2 再备份一份
    out("\n[2] 再备份一次：新增一份新的，旧的那份还在")
    time.sleep(0.2)
    w(SRC / "c.txt", "ccc")
    r = engine.run_full(base_job(), trigger="测试")
    names = full_folders()
    check(len(names) == 2, "变成 2 份完整备份", str(names))
    check((DST / names[-1] / "a.txt").exists() and (DST / names[-1] / "c.txt").exists(),
          "最新那份里是改过之后的样子")

    # ---------------------------------------------------------- 3 文件留存
    out("\n[3] 文件留存：只保留最近 2 份")
    job2 = base_job()
    job2["full"]["keep_recent"] = 2
    for i in range(3):
        w(SRC / "a.txt", f"第 {i} 版内容，特意写得长一点")
        engine.run_full(job2, trigger="测试")
    names = full_folders()
    check(len(names) == 2, "只剩 2 份", str(names))
    check((DST / names[-1] / "a.txt").read_text(encoding="utf-8").startswith("第 2 版"),
          "留下的是最新的那份")

    # ---------------------------------------------------------- 4 清理的安全性
    out("\n[4] 关键安全：清理只删「我们建的」，用户自己放进去的一律不碰")
    mine = DST / "我自己放的文件夹"
    mine.mkdir(exist_ok=True)
    (mine / "重要.txt").write_text("用户手动放进来的东西", encoding="utf-8")
    engine.prune_full_backups(str(DST), 1)
    check(mine.is_dir() and (mine / "重要.txt").exists(), "用户自己的文件夹没被删")
    check(len(full_folders()) == 1, "完整备份被裁到 1 份", str(full_folders()))

    # ---------------------------------------------------------- 5 镜像：第一次
    out("\n[5] 镜像备份：第一次建立镜像文件夹并同步")
    msrc, mdst = TEST / "镜源", TEST / "镜备份"
    w(msrc / "x.txt", "xxx")
    w(msrc / "d" / "y.txt", "yyyy")
    mjob = base_job(id="m1", source=str(msrc), target=str(mdst))
    mjob["full"]["enabled"] = False
    mjob["mirror"]["enabled"] = True
    r = engine.run_mirror(mjob, trigger="测试")
    check(r.ok, "执行成功", r.message)
    check(r.copied == 2, "同步了 2 个文件", f"copied={r.copied}")
    dirs = subdirs(mdst)
    check(len(dirs) == 1 and "-镜像" in dirs[0], "建立了 1 个镜像文件夹", str(dirs))
    check((mdst / dirs[0] / "x.txt").exists(), "内容已同步")
    first_name = dirs[0] if dirs else ""

    # ---------------------------------------------------------- 6 镜像：复用 + 增量 + 真镜像
    out("\n[6] 镜像：复用同一个文件夹，只同步变化，源里删的镜像里也删")
    time.sleep(0.1)
    w(msrc / "x.txt", "xxx 改过了，变长")
    w(msrc / "z.txt", "zzz 新增")
    os.remove(msrc / "d" / "y.txt")
    r = engine.run_mirror(mjob, trigger="测试")
    dirs = subdirs(mdst)
    check(len(dirs) == 1, "没有再多出一个镜像文件夹（复用）", str(dirs))
    check(bool(dirs) and dirs[0] == first_name, "用的还是原来那个文件夹", str(dirs))
    base = mdst / first_name
    check((base / "x.txt").read_text(encoding="utf-8").startswith("xxx 改过了"), "变化的内容已更新")
    check((base / "z.txt").exists(), "新增的文件已同步")
    check(not (base / "d" / "y.txt").exists(), "源里删掉的，镜像里也删掉了（真镜像）")
    check(r.updated >= 1 and r.copied >= 1 and r.removed >= 1,
          "计数正确", f"updated={r.updated} copied={r.copied} removed={r.removed}")

    # ---------------------------------------------------------- 7 镜像：无变化
    out("\n[7] 镜像：源没变时不做任何复制")
    r = engine.run_mirror(mjob, trigger="测试")
    check(r.copied == 0 and r.updated == 0 and r.removed == 0, "无变化", r.summary())

    # ---------------------------------------------------------- 8 安全护栏
    out("\n[8] 安全护栏：源与备份不能互相包含")
    bad = base_job(id="bad", source=str(SRC), target=str(SRC / "inner"))
    r = engine.run_full(bad, trigger="测试")
    check(not r.ok, "已拒绝嵌套路径", r.message)
    check(not (SRC / "inner").exists(), "没有真的创建出嵌套目录")

    # ---------------------------------------------------------- 9 源不存在
    out("\n[9] 源文件夹不存在时给出明确错误")
    r = engine.run_full(base_job(id="noSrc", source=str(TEST / "没有这个目录")), trigger="测试")
    check(not r.ok and "不存在" in r.message, "错误信息清晰", r.message)

    # ---------------------------------------------------------- 10 恢复：整批
    out("\n[10] 恢复：整批恢复到「源文件夹旁边」，不覆盖任何东西")
    names = full_folders()
    target_name = names[0]
    before = set(p.name for p in SRC.parent.iterdir())
    res = engine.restore_backup(base_job(), target_name)
    check(res.ok, "整批恢复成功", res.message or res.summary())
    check(Path(res.dest).is_dir(), "目标文件夹已生成", res.dest)
    check(Path(res.dest).parent == SRC.parent, "位置确实在源文件夹旁边（同一层）")
    check((Path(res.dest) / "a.txt").exists(), "里面的文件在")
    after = set(p.name for p in SRC.parent.iterdir())
    check(after - before == {Path(res.dest).name}, "只多出这一个文件夹，没动别的")
    res2 = engine.restore_backup(base_job(), target_name)
    check(res2.ok and res2.renamed == 1, "同名时自动改名另存", res2.dest)
    check(Path(res2.dest).name != Path(res.dest).name, "两份文件夹名字不同")

    # ---------------------------------------------------------- 11 恢复：单个文件
    out("\n[11] 恢复：单个文件放回原位；重名自动改名另存，绝不覆盖")
    w(SRC / "a.txt", "现在源里的新内容")
    res3 = engine.restore_backup(base_job(), target_name, rels=["a.txt"])
    check(res3.ok, "单项恢复成功", res3.message or res3.summary())
    check(res3.renamed == 1, "检测到重名并改名", f"renamed={res3.renamed}")
    check((SRC / "a.txt").read_text(encoding="utf-8") == "现在源里的新内容",
          "源里原来的文件没被动过")
    restored = [p for p in SRC.iterdir() if p.name.startswith("a_恢复")]
    check(len(restored) == 1, "另存出来一个带时间戳的文件", str([p.name for p in restored]))

    # ---------------------------------------------------------- 12 恢复护栏
    out("\n[12] 恢复护栏：不是本软件建的文件夹，不许恢复")
    bad_dir = DST / "冒充的备份"
    bad_dir.mkdir(exist_ok=True)
    res4 = engine.restore_backup(base_job(), "冒充的备份")
    check(not res4.ok and "不是本软件" in res4.message, "拒绝了没有标记的文件夹", res4.message)
    res5 = engine.restore_backup(base_job(), "../啥")
    check(not res5.ok, "非法名字被拒绝", res5.message)
    res6 = engine.restore_backup(base_job(), "20990101_0000")
    check(not res6.ok, "不存在的备份给出明确错误", res6.message)

    # ---------------------------------------------------------- 13 变动指纹
    out("\n[13] 变动检测用的指纹：变没变要判得出来")
    fs = TEST / "指纹源"
    w(fs / "one.txt", "1")
    fp1 = engine.fingerprint(str(fs), [])
    fp2 = engine.fingerprint(str(fs), [])
    check(fp1 == fp2, "没动过 → 指纹相同（不会误报变动）")
    time.sleep(0.05)
    w(fs / "two.txt", "2")
    fp3 = engine.fingerprint(str(fs), [])
    check(fp3 != fp1, "加了文件 → 指纹不同")
    w(fs / "two.txt", "2 变长了")
    fp4 = engine.fingerprint(str(fs), [])
    check(fp4 != fp3, "改了内容 → 指纹不同")

    # ---------------------------------------------------------- 汇总
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
        out("!!! 自测自身抛出异常，说明有未被测试覆盖的缺陷：")
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
