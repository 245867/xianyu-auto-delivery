"""
拆解 2：回复侧合规闸用例（XianyuAutoAgent 的 `_safe_filter` 照出我们的空档）。

守住五件事：
[1] 导流词命中就整条不发（微信/加V/私下转账……复用发布侧一级词表）
[2] 不带"微信"两个字也拦得住（手机号、微信号形态、QQ 号）
[3] 极限词只记录不拦（"保证正品"在二手语境里太常见，拦了就没法用）
[4] 不误伤：品类词（教材/课程）在聊天里不该拦，问价、发货说明必须放行
[5] 接线正确：拦掉的回复不进对话记录为正常回复，而是留"[转人工]"并且不返回内容

跑法：python tests/test_reply_guard.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import reply_guard as rg

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- [1] 导流词
section("1 导流词：命中即整条不发")
for text, word in [
    ("可以呀，加我微信聊", "微信"),
    ("这个价格你加V找我", "加V"),
    ("咱们私下转账便宜 50", "私下转账"),
    ("我给你加我好友，详聊", "加我好友"),
    ("走银行卡更便宜", "银行卡"),
    ("这个是高仿的", "高仿"),
]:
    r = rg.check(text)
    check(r["ok"] is False, f"拦住：{text}")
    check(any(h in word or word in h for h in r["blocked"]), f"命中词对得上：{r['blocked']}")

r = rg.check("加我微信")
check(r["reason"].startswith("含站外导流/违规词"), "给了人看得懂的原因")


# ---------------------------------------------------------------- [2] 号码形态
section("2 号码形态：不写微信两个字也拦")
for text in [
    "我电话 13812345678 你打给我",
    "vx:abc12345 详聊",
    "我的企鹅号 123456789",
    "微信：shop_2026",
]:
    r = rg.check(text)
    check(r["ok"] is False, f"拦住：{text}")

# 词表只能认出"微信"两个字，认不出后面那串账号 —— 正则补的就是这一段
r = rg.check("微信：shop_2026")
check(any(len(b) > 4 for b in r["blocked"]),
      f"账号一起截出来（不只是两个字）：{r['blocked']}")
r = rg.check("我的企鹅号 123456789")
check(any("123456789" in b for b in r["blocked"]), "QQ 号数字被截出来")
r = rg.check("我电话 13812345678 你打给我")
check("手机号" in r["blocked"], "手机号被单独标出来")

check(rg.check("我的订单号 3820261109123456789")["ok"] is True,
      "19 位订单号不会被当成手机号")
check(rg.check("这个型号 1381234567")["ok"] is True, "10 位数字不算手机号")


# ---------------------------------------------------------------- [3] 极限词
section("3 极限词：只记录不拦")
r = rg.check("这款是专柜正品，保证质量")
check(r["ok"] is True, "「正品/保证」放行")
check("正品" in r["warned"] and "保证" in r["warned"], f"但记录在案：{r['warned']}")

r = rg.check("全网最低价了，绝对不亏")
check(r["ok"] is True and "绝对" in r["warned"], "「绝对/最低」记录不拦")


# ---------------------------------------------------------------- [4] 不误伤
section("4 不误伤：正常回复必须放行")
for text in [
    "运费我出，明天发顺丰",
    "这本教材是第 3 版，带光盘",
    "可以小刀，160 拿走",
    "已经发货了，预计后天到",
    "稍等，我拍张实图给你看",
]:
    r = rg.check(text)
    check(r["ok"] is True and not r["blocked"], f"放行：{text}")

check(rg.check("")["ok"] is True, "空回复不报错（交给上游判空）")


# ---------------------------------------------------------------- [5] 接线
section("5 接线（回复路径）")
src = Path("ai_reply_engine.py").read_text(encoding="utf-8")
i_bargain = src.find("bargain_guard as _bg\n                        chk = _bg.enforce")
i_guard = src.find("reply_guard as _rg")
i_save = src.find("# 11. 保存AI回复到对话记录")
check(i_guard > 0, "回复路径已接入 reply_guard")
check(i_guard > i_bargain, "接在议价保险丝之后（先改价、再查违规）")
check(i_guard < i_save, "在保存/返回之前拦（否则已经记成正常回复了）")
block_seg = src[i_guard:i_guard + 1600]
check("return None" in block_seg, "命中后不返回任何内容（不发送）")
check("[转人工]" in block_seg, "留痕给人工接手")

os.environ["REPLY_GUARD_ENABLED"] = "0"
check(rg.check("加我微信")["ok"] is True, "总开关可关（排障兜底）")
os.environ.pop("REPLY_GUARD_ENABLED", None)


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
