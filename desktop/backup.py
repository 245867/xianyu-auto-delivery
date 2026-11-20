# -*- coding: utf-8 -*-
"""
每日数据备份（Gate 0 第 9 条）。

只做一件事：把 data\\（Cookie、卡密、价格历史、通知配置）和 global_config.yml
打成一个 zip，放进工作区的 05-备份\\ 目录，保留最近 7 份。

注意：备份包内含账号凭据与卡密，属本机敏感文件 —— 只留在本机，绝不外传、
不上传网盘、不发群。备份目录在应用文件夹之外（工作区根目录），重装应用不会丢。
"""

from __future__ import annotations

import os
import sys
import zipfile
from datetime import datetime
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent.parent
WORKSPACE = APP_DIR.parent.parent          # G:\have time fish
BACKUP_DIR = WORKSPACE / "05-备份"
KEEP = 7
DATA_DIR = APP_DIR / "data"
CONFIG_FILE = APP_DIR / "global_config.yml"

# 素材库（data/materials/）是用户自己的图片资产，不是凭据；体积可能上 GB。
# 小库（≤200MB）顺手一起备份；大库跳过并提示，避免把每日备份撑爆。
MATERIALS_DIR = DATA_DIR / "materials"
MATERIALS_INCLUDE_MAX_BYTES = 200 * 1024 * 1024
MATERIAL_DB = DATA_DIR / "material_library.db"


def _dir_size(p: Path) -> int:
    total = 0
    for f in p.rglob("*"):
        try:
            if f.is_file():
                total += f.stat().st_size
        except Exception:
            pass
    return total


def _collect() -> list[Path]:
    files: list[Path] = []
    include_materials = False
    if MATERIALS_DIR.is_dir():
        include_materials = _dir_size(MATERIALS_DIR) <= MATERIALS_INCLUDE_MAX_BYTES
    if DATA_DIR.is_dir():
        for p in DATA_DIR.rglob("*"):
            if not p.is_file() or p.name.endswith((".log", ".tmp", ".pyc")):
                continue
            if MATERIALS_DIR in p.parents and not include_materials:
                continue
            files.append(p)
    if CONFIG_FILE.is_file():
        files.append(CONFIG_FILE)
    if not include_materials and MATERIAL_DB.is_file():
        # 图片没进包，库文件也别进，否则恢复后是一堆断链
        files = [f for f in files if f != MATERIAL_DB]
        print("素材库超过 200MB，本次备份跳过 data\\materials（图片资产请自行归档）")
    return files


def _prune() -> int:
    olds = sorted(BACKUP_DIR.glob("xianyu-backup-*.zip"))
    removed = 0
    for p in olds[:-KEEP]:
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    return removed


def run(force: bool = False) -> int:
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d")
    target = BACKUP_DIR / f"xianyu-backup-{stamp}.zip"
    if target.exists() and not force:
        print(f"今日已备份：{target.name}（跳过）")
        return 0

    files = _collect()
    if not files:
        print("没有可备份的数据（data\\ 与 global_config.yml 均不存在）")
        return 0

    tmp = target.with_suffix(".zip.tmp")
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in files:
                zf.write(f, f.relative_to(APP_DIR))
        os.replace(tmp, target)
    except Exception as exc:
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        print(f"备份失败：{exc!r}")
        return 1

    removed = _prune()
    size = target.stat().st_size / 1024
    print(f"已备份 {len(files)} 个文件 → {target}（{size:.0f} KB），清理旧备份 {removed} 份")
    return 0


if __name__ == "__main__":
    sys.exit(run(force="--force" in sys.argv))
