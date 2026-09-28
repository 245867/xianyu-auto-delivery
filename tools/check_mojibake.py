# -*- coding: utf-8 -*-
"""
核验项目内文本文件是否存在乱码（mojibake）。

判定思路：
  1. 遍历源码/文档/配置类文本文件（跳过二进制、依赖目录、构建产物）；
  2. UTF-8 严格解码失败即视为编码损坏；
  3. 解码成功后再检测典型的乱码特征：
     - U+FFFD 替换字符
     - UTF-8 被按 GBK 读、或 GBK 被按 UTF-8 读产生的典型字符块
       （如 "锟斤拷"、"锘"、"脗"、"Ã¤"、"â€" 等）
  4. 打印命中文件、行号与上下文，便于人工复核。

用法：python tools/check_mojibake.py
"""
import io
import os
import re
import sys

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", ".idea", ".vscode",
    "dist", "build", ".pytest_cache", ".mypy_cache", "logs", "data",
    "static", "uploads", ".ruff_cache",
}
TEXT_EXT = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".json", ".md", ".txt", ".yml", ".yaml",
    ".css", ".html", ".bat", ".sh", ".toml", ".ini", ".cfg", ".sql", ".env",
    ".example", ".dockerfile", ".conf",
}
TEXT_NAMES = {"Dockerfile", ".gitignore", ".dockerignore", "requirements.txt", "LICENSE"}

# 典型乱码特征
PATTERNS = [
    (r"\ufffd", "U+FFFD 替换字符"),
    (r"锟斤拷", "UTF-8/GBK 互转乱码（锟斤拷）"),
    (r"锘", "UTF-8 BOM 被误读（锘）"),
    (r"[脗脙聬聹]{2,}", "GBK 误读为 UTF-8 的连续字符"),
    (r"(?:Ã[\x80-\xbf]|â€|Ð[°-¿]){2,}", "Latin-1 误读 UTF-8（Ã / â€）"),
    (r"[鏂鐨涓浣犳]{4,}", "GBK 误读为 UTF-8 的汉字串"),
]

MAX_BYTES = 3 * 1024 * 1024


# 本脚本自身包含乱码特征字符串（作为检测规则），必须排除
SELF = os.path.abspath(__file__)


def iter_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".git")]
        for fn in filenames:
            ext = os.path.splitext(fn)[1].lower()
            if ext in TEXT_EXT or fn in TEXT_NAMES or ext == "":
                p = os.path.join(dirpath, fn)
                if os.path.abspath(p) == SELF:
                    continue
                yield p


def main():
    total = 0
    bad_decode = []
    suspicious = []

    for path in iter_files():
        try:
            if os.path.getsize(path) > MAX_BYTES:
                continue
            raw = open(path, "rb").read()
        except OSError:
            continue
        if b"\x00" in raw[:2048]:        # 二进制
            continue
        total += 1
        rel = os.path.relpath(path, ROOT)

        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as e:
            bad_decode.append((rel, str(e)))
            continue

        for ln, line in enumerate(text.splitlines(), 1):
            for pat, desc in PATTERNS:
                if re.search(pat, line):
                    suspicious.append((rel, ln, desc, line.strip()[:100]))
                    break

    print(f"已检查文本文件 {total} 个\n")

    print("=== 1) UTF-8 解码失败（编码已损坏） ===")
    if not bad_decode:
        print("  （无）")
    for rel, err in bad_decode:
        print(f"  {rel}\n      {err}")

    print(f"\n=== 2) 疑似乱码内容（{len(suspicious)} 处） ===")
    if not suspicious:
        print("  （无）")
    for rel, ln, desc, ctx in suspicious[:40]:
        print(f"  {rel}:{ln}  [{desc}]\n      {ctx}")
    if len(suspicious) > 40:
        print(f"  ... 其余 {len(suspicious) - 40} 处省略")

    ok = not bad_decode and not suspicious
    print("\n结论:", "未发现乱码文件" if ok else "存在需要人工复核的文件")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
