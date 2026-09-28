"""README 发布规范自检（对应《Git 发布通用指南》7.5 节）

覆盖强制项：5a/5b 全中文、6a AI 声明、6b 越界内容、7a~7d 顶部徽章、
8a~8d 运行截图、以及铁律 4 的发帖内容入库检查。

用法：python -X utf8 tools/check_readme_guide.py
退出码：0 = 全部通过；1 = 存在不通过项。
"""

import io
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")

failures = []
warnings = []


def section(title):
    print("\n=== %s ===" % title)


def fail(msg):
    failures.append(msg)
    print("  [不通过] %s" % msg)


def ok(msg):
    print("  [通过] %s" % msg)


def warn(msg):
    warnings.append(msg)
    print("  [待人工确认] %s" % msg)


def main():
    if not os.path.isfile(README):
        print("找不到 README.md")
        return 1

    text = io.open(README, encoding="utf-8").read()
    lines = text.splitlines()

    # ---------- 5a 英文小标题（H2~H6；H1 是项目名，指南明确豁免）----------
    section("5a. 英文小标题检查（期望无输出）")
    bad_h = [l for l in lines if re.match(r"^#{2,6}\s*[A-Za-z]", l)]
    if bad_h:
        for l in bad_h:
            fail("英文小标题：%s" % l.strip())
    else:
        ok("标题全中文")

    # ---------- 5b 常见英文小节词 ----------
    section("5b. 常见英文小节词（期望无输出）")
    words = ("run|build|install|usage|features|overview|getting started|"
             "license|plugins|appearance|files and secrets|configuration|requirements")
    bad_w = [l for l in lines
             if re.match(r"^#{1,6}\s*(%s)\b" % words, l, re.I)]
    if bad_w:
        for l in bad_w:
            fail("英文小节：%s" % l.strip())
    else:
        ok("无英文小节")

    # ---------- 6a AI 生成声明 / AIGC 元数据 ----------
    section("6a. AI 生成声明与 AIGC 元数据（期望无输出）")
    ai_hits = [l for l in lines
               if re.search(r"内容由AI生成|仅供参考|^\s*AIGC:|ContentProducer|ProduceID", l)]
    if ai_hits:
        for l in ai_hits:
            fail("AI 声明：%s" % l.strip())
    else:
        ok("无 AI 声明")

    # ---------- 6b 跨项目 / 跨阶段越界叙述 ----------
    section("6b. 越界内容（需人工确认）")
    cross = [l for l in lines
             if re.search(r"第一步|第二步|本仓库之外|另一个项目|后续项目|后续会", l)]
    if cross:
        for l in cross:
            warn("疑似越界：%s" % l.strip()[:80])
    else:
        ok("无越界内容")

    # ---------- 7a / 7b / 7c / 7d 顶部徽章 ----------
    section("7a. 顶部居中区（期望 >=1）")
    head = lines[:20]
    n_center = sum(1 for l in head if '<div align="center">' in l)
    (ok if n_center >= 1 else fail)("居中的 <div align=\"center\"> 共 %d 处" % n_center)

    section("7b. 四枚徽章槽位（每枚期望 1）")
    for slot in ("Language", "Platform", "UI", "License"):
        n = len(re.findall(r"!\[%s\]\(" % slot, text))
        (ok if n == 1 else fail)("%-9s %d 枚" % (slot, n))

    section("7c. 顶部徽章须为 Markdown 语法（期望无 HTML <img>）")
    head_text = "\n".join(head)
    if re.search(r"<img[^>]*img\.shields\.io", head_text):
        fail("顶部徽章用了 HTML <img>")
    else:
        ok("顶部徽章为 Markdown 图片语法")

    section("7d. 许可证徽章指向相对路径 LICENSE（期望 1）")
    n_lic = len(re.findall(r"\[!\[License\]\([^)]*\)\]\(LICENSE\)", text))
    (ok if n_lic == 1 else fail)("指向 LICENSE 的许可证徽章 %d 枚" % n_lic)

    # ---------- 8a / 8b / 8c 运行截图 ----------
    section("8a. 本地运行截图（期望 >=1）")
    md_imgs = re.findall(r"!\[[^\]]*\]\(([^)]+)\)", text)
    html_imgs = re.findall(r'<img[^>]+src="([^"]+)"', text)
    local = sorted({p for p in md_imgs + html_imgs
                    if not p.startswith(("http://", "https://", "data:"))})
    (ok if local else fail)("本地图片 %d 张" % len(local))

    section("8b. 相对路径是否真实存在（期望无 missing）")
    for p in local:
        full = os.path.join(ROOT, p.replace("/", os.sep))
        if os.path.isfile(full):
            print("  ok      %s" % p)
        else:
            fail("missing %s" % p)

    section("8c. 指向 releases / 图床的假图片（期望 0）")
    fake = re.findall(
        r'(!\[[^\]]*\]\(|<img[^>]+src=")https?://[^)"]*(releases|imgur|sm\.ms)', text)
    (ok if not fake else fail)("假图片 %d 处" % len(fake))

    # ---------- 8d 图片是否已被 git 跟踪 ----------
    section("8d. 图片是否已被 git 跟踪（期望 >0）")
    try:
        out = subprocess.run(
            ["git", "ls-files", "--", "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        tracked = [l for l in out.stdout.splitlines() if l.strip()]
    except Exception as e:  # noqa: BLE001
        tracked = []
        warn("git ls-files 执行失败：%s" % e)
    (ok if tracked else fail)("已跟踪图片 %d 个" % len(tracked))

    # ---------- 铁律 4 发帖内容入库 ----------
    section("铁律4. 发帖内容是否入库（期望空）")
    try:
        out = subprocess.run(
            ["git", "-c", "core.quotepath=false", "ls-files"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        posts = [l for l in out.stdout.splitlines()
                 if re.search(r"DZ论坛|发帖", l, re.I)]
    except Exception as e:  # noqa: BLE001
        posts = []
        warn("git ls-files 执行失败：%s" % e)
    if posts:
        for l in posts:
            fail("发帖内容入库：%s" % l)
    else:
        ok("发帖内容未入库")

    # ---------- 7.2 说明性图：Mermaid 架构图（与运行截图职责不同，都要有）----------
    section("7.2. Mermaid 说明性图（期望 >=1）")
    n_mermaid = len(re.findall(r"^```mermaid", text, re.M))
    (ok if n_mermaid >= 1 else fail)("Mermaid 代码块 %d 个" % n_mermaid)

    # ---------- 社群入口位置（7.4）----------    section("7.4. 社群章节须为最后一章")
    chapters = [(i, l.strip()) for i, l in enumerate(lines)
                if re.match(r"^##\s", l)]
    if not chapters:
        fail("未找到任何二级章节")
    else:
        last = chapters[-1][1]
        if "社群" in last:
            ok("最后一章为：%s" % last)
        else:
            fail("最后一章不是社群章节，而是：%s" % last)

    # ---------- 汇总 ----------
    print("\n" + "=" * 46)
    if failures:
        print("结论：不通过 %d 项，不允许推送" % len(failures))
        for f in failures:
            print("  - %s" % f)
        return 1
    print("结论：全部强制项通过")
    if warnings:
        print("另有 %d 项需人工确认" % len(warnings))
    return 0


if __name__ == "__main__":
    sys.exit(main())
