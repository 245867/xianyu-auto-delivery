"""发布前合规闸回归测试

来源：用户整理的 148 份闲鱼实操文档（生财有术，去重后约 185 万字）里反复出现的
违规口径。本测试锁死三件事：

1. **BLOCK 级必须拦住**——微信/高仿/A货/代充这些词，命中就不能提交到平台；
2. **WARN 级必须只提示不拦**——「正品」「100%」在二手文案里很常见，全拦会让工具没法用；
3. **不能误杀**——「裸机」「假一赔十」「LED」这些正常二手词一个都不能判红。
   第 3 条是这套设计的关键：文档作者说的是"平台会抓"，不是"用到就死"，
   所以只收词组不收单字。

跑法（backend 目录）：python3 tests/test_publish_guard.py
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

# 隔离自定义词表：绝不能读到用户真实 data/publish_guard_extra.json
_TMP = tempfile.mkdtemp(prefix="publish_guard_")
os.environ["PUBLISH_GUARD_EXTRA"] = str(Path(_TMP) / "extra.json")

import publish_guard as pg  # noqa: E402

pg.EXTRA_FILE = Path(os.environ["PUBLISH_GUARD_EXTRA"])

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name} {detail}")


def test_block():
    print("\n[1] 一级：命中即拒（导流 / 售假）")
    for word in ["微信", "加V", "站外", "私下交易", "高仿", "A货", "复刻", "莆田", "代充", "刷单"]:
        r = pg.check_publish("iPhone 13 128G", f"有意向{word}聊，价格好商量")
        check(f"拦住了「{word}」", r["ok"] is False, str(r))
        check(f"「{word}」出现在 block 列表", word in r["block"], str(r["block"]))

    # 标题里也要抓
    r = pg.check_publish("加V看实拍 iPhone13", "成色好")
    check("标题里的导流词也拦", r["ok"] is False and "加V" in r["block"], str(r))

    # tags 也要查
    r = pg.check_publish("iPhone 13", "成色好", tags=["微信交易"])
    check("标签里的导流词也拦", r["ok"] is False, str(r))


def test_warn_not_block():
    print("\n[2] 二级：只提示不拦截（夸大 / 极限词）")
    r = pg.check_publish("iPhone 13 128G 正品", "保证正品，支持验货，100%原装")
    check("「正品/保证/100%」不拦发布", r["ok"] is True, str(r))
    check("但一定给出提示", len(r["warnings"]) >= 1, str(r["warnings"]))
    joined = " ".join(r["warnings"])
    check("提示里点名了「正品」", "正品" in joined, joined)
    check("提示里点名了「100%」", "100%" in joined, joined)

    r2 = pg.check_publish("二手手机 全网最低价", "最好用的备用机")
    check("极限词不拦但提示", r2["ok"] is True and len(r2["warnings"]) >= 1, str(r2))


def test_no_false_positive():
    print("\n[3] 不误杀：真实二手文案必须放行且不报噪音")
    normal = [
        ("iPhone 13 Pro 128G 国行", "自用一年，屏幕无划痕，裸机加充电线，可小刀"),
        ("ThinkPad X1 Carbon 笔记本", "LED 屏无坏点，USB 接口正常，送原装包"),
        ("雅马哈民谣吉他 F310", "九成新，弹了半年，音色很正，支持同城自提"),
        ("戴森吹风机 HD08", "专柜买的，有小票，闲置转让，只走平台交易"),
        ("机械键盘 87 键 茶轴", "键帽齐全，无打油，包邮出"),
    ]
    for title, desc in normal:
        r = pg.check_text(title, desc)
        check(f"放行：{title[:16]}", r["ok"] is True, str(r["block"]))
        check(f"无噪音：{title[:16]}", not r["all_hits"], str(r["all_hits"]))

    # 「裸机」不能因为收了「裸」就误杀 —— 这条专门锁「只收词组不收单字」的设计
    check("词表里没有单字「裸」", "裸" not in pg.BLOCK_WORDS and "裸" not in pg.WARN_WORDS)
    check("词表里没有单字「假」", "假" not in pg.BLOCK_WORDS and "假" not in pg.WARN_WORDS)
    check("词表里没有单字「ED」（会误杀 LED）", "ED" not in pg.WARN_WORDS)


def test_title_rules():
    print("\n[4] 标题规则（30 字上限 / 前 25 字放核心信息）")
    r = pg.check_text("iPhone 13 Pro Max 256G 国行全网通双卡双待 自用一年成色极好无磕碰带原包装", "好")
    notes = " ".join(r["notes"])
    check("超 30 字给提示", "30" in notes, notes)

    r2 = pg.check_text("手机", "好")
    check("标题过短给提示", any("偏短" in n for n in r2["notes"]), str(r2["notes"]))

    r3 = pg.check_text("iPhone 13 128G", "")
    check("正文为空给提示", any("正文为空" in n for n in r3["notes"]), str(r3["notes"]))


def test_category():
    print("\n[5] 三级：品类红线只提示")
    r = pg.check_publish("考研数学全套教材", "九成新，含真题册")
    check("教材不拦发布", r["ok"] is True, str(r))
    check("但提示需要资质", any("品类红线" in w for w in r["warnings"]), str(r["warnings"]))


def test_extra_and_allow():
    print("\n[6] 自定义词表 + 白名单")
    pg.EXTRA_FILE.parent.mkdir(parents=True, exist_ok=True)
    pg.EXTRA_FILE.write_text(
        '{"block": ["测试违禁词"], "warn": ["测试提醒词"], "allow": ["正品"]}',
        encoding="utf-8")

    r = pg.check_publish("正常标题", "含测试违禁词")
    check("自定义 block 生效", r["ok"] is False, str(r))
    r = pg.check_publish("正常标题", "含测试提醒词")
    check("自定义 warn 生效但不拦", r["ok"] is True and any("测试提醒词" in w for w in r["warnings"]), str(r))
    r = pg.check_publish("正品 iPhone", "保证正品")
    check("allow 能豁免「正品」", not any("正品" in w for w in r["warnings"]), str(r["warnings"]))

    # 坏文件不能把发布流程带崩
    pg.EXTRA_FILE.write_text("{ this is not json", encoding="utf-8")
    r = pg.check_publish("iPhone 13", "成色好")
    check("词表文件坏掉时仍能正常判断", r["ok"] is True, str(r))
    pg.EXTRA_FILE.unlink(missing_ok=True)


def test_assert_clean():
    print("\n[7] 硬闸 assert_clean")
    try:
        pg.assert_clean("正常标题", "加微信详聊")
        check("违禁内容抛异常", False, "没有抛")
    except ValueError as e:
        check("违禁内容抛 ValueError", "微信" in str(e), str(e))
    try:
        pg.assert_clean("iPhone 13", "成色好，支持验货")
        check("正常内容不抛", True)
    except Exception as e:
        check("正常内容不抛", False, str(e))


def test_images():
    print("\n[8] 图片数量提示（6 张以上是文档共识）")
    r = pg.check_text("iPhone 13 128G", "成色好", images=1)
    check("1 张图提示加到 6 张", any("6 张" in n for n in r["notes"]), str(r["notes"]))
    r2 = pg.check_text("iPhone 13 128G", "成色好", images=8)
    check("8 张图不提示", not any("张图" in n for n in r2["notes"]), str(r2["notes"]))
    r3 = pg.check_text("iPhone 13 128G", "成色好", images=0)
    check("0 张图提示必须有主图", any("主图" in n for n in r3["notes"]), str(r3["notes"]))


def main():
    test_block()
    test_warn_not_block()
    test_no_false_positive()
    test_title_rules()
    test_category()
    test_extra_and_allow()
    test_assert_clean()
    test_images()

    print(f"\n{'=' * 56}")
    if FAILS:
        print(f"失败 {len(FAILS)} 项 / 通过 {PASSES} 项")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print(f"全部通过（{PASSES} 项）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
