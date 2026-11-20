"""
拆解 14：批量卡密去重 + 多行关键词

[1] 取行：空行/首尾空格被忽略，顺序不变
[2] 去重：取出一条时，库里所有与它相同的行一起清掉
[3] 连续性：连取三次，同一个码不会出现两次
[4] 多行关键词：一条词条写多行，每行都是独立触发词
[5] 接线：db_manager 用的是 card_text.take_first

跑法：cd backend && python3 tests/test_card_text.py
"""

import os
import sys
import tempfile
from pathlib import Path

os.environ["FAQ_DB"] = str(Path(tempfile.mkdtemp(prefix="cardtxt_")) / "faq.db")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import card_text as ct  # noqa: E402
import faq_kb as fk  # noqa: E402

BACKEND = Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- [1]
section("1 取行")
check(ct.split_codes("A\nB\n\n C \n") == ["A", "B", "C"], "空行和首尾空格被忽略")
check(ct.split_codes("") == [], "空文本 → 空列表")

# ---------------------------------------------------------------- [2]
section("2 取出一条时清掉所有相同的行")
first, rest, dropped = ct.take_first("卡A\n卡B\n卡A\n卡C\n卡A")
check(first == "卡A", "取到第一条")
check(dropped == 2, "顺带清掉 2 行重复（不是 1 行：剩下的两条卡A都要清）")
check(rest.split("\n") == ["卡B", "卡C"], "剩余只有真正不同的卡")

first2, rest2, dropped2 = ct.take_first("卡X\n卡Y")
check(first2 == "卡X" and dropped2 == 0 and rest2 == "卡Y", "没有重复时行为不变")

f0, r0, d0 = ct.take_first("")
check((f0, r0, d0) == ("", "", 0), "空库存返回空，不炸")

# ---------------------------------------------------------------- [3]
section("3 连取三次，不会重复")
raw = "K1\nK1\nK2\nK1\nK3"
seen = []
for _ in range(4):
    v, raw, _d = ct.take_first(raw)
    if v:
        seen.append(v)
check(seen == ["K1", "K2", "K3"], f"依次拿到 K1/K2/K3（实际 {seen}）")
check(ct.take_first(raw)[0] == "", "取完后是空的")

# ---------------------------------------------------------------- [4]
section("4 多行关键词")
kw = "有货吗\n现货吗"
check(fk.keyword_parts(kw) == ["有货吗", "现货吗"], "按行拆成两个触发词")
check(fk.match_keyword(kw, "还有现货吗") is True, "命中第二行")
check(fk.match_keyword(kw, "在吗") is False, "都不命中")
check(fk.match_keyword("多久发货\n什么时候发", "大概 什么时候 发？") is True,
      "多行 + 归一化兜底（空格/问号）")
check(fk.match_keyword("单行关键词", "这是单行关键词吧") is True, "单行词条行为不变")
check(fk.match_keyword("", "随便") is False, "空词条不命中")
check(fk.match_keyword("包邮\n1", "1") is True, "短行照样做精确匹配（只是不参与归一化）")

# ---------------------------------------------------------------- [5]
section("5 接线")
dm = (BACKEND / "db_manager.py").read_text(encoding="utf-8")
check("from card_text import split_codes" in dm,
      "批量库存通过 card_text 的同一套清洗规则解析")
check("codes = list(dict.fromkeys(raw_codes))" in dm,
      "消费前对完整库存去重，重复卡密不会连续被卖出")
check("重复卡密" in dm and "duplicates_removed" in dm,
      "清理重复卡密时记录实际数量")
check("消费批量数据成功" in dm, "消费后记录剩余库存")

src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("_fk.match_keyword(kw, message)" in src, "关键词判定走 match_keyword（多行 + 归一化）")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
