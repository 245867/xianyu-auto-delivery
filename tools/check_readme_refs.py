# -*- coding: utf-8 -*-
"""
核对 README 中引用的本地图片/文件是否真实存在。

用法：python tools/check_readme_refs.py
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
README = os.path.join(ROOT, "README.md")


def main():
    src = open(README, encoding="utf-8").read()

    refs = []
    # Markdown 图片/链接
    for m in re.finditer(r"!?\[[^\]]*\]\(([^)]+)\)", src):
        refs.append((src[: m.start()].count("\n") + 1, m.group(1)))
    # <img src="...">
    for m in re.finditer(r"<img[^>]+src=[\"']([^\"']+)[\"']", src):
        refs.append((src[: m.start()].count("\n") + 1, m.group(1)))

    local, remote, missing = [], [], []
    for ln, url in refs:
        u = url.strip()
        if u.startswith(("http://", "https://", "mailto:", "#")):
            remote.append((ln, u))
            continue
        path = os.path.normpath(os.path.join(ROOT, u.split("#")[0]))
        (local if os.path.exists(path) else missing).append((ln, u))

    print(f"README 中共 {len(refs)} 个引用：本地 {len(local)}、外链 {len(remote)}\n")

    print("=== 本地引用检查 ===")
    for ln, u in local:
        print(f"  L{ln:4} OK   {u}")

    print(f"\n=== 缺失的本地文件（{len(missing)}） ===")
    if not missing:
        print("  （无）")
    for ln, u in missing:
        print(f"  L{ln:4} 缺失 {u}")

    print(f"\n=== 外链（{len(remote)}，未做网络校验） ===")
    for ln, u in remote[:12]:
        print(f"  L{ln:4} {u[:88]}")

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
