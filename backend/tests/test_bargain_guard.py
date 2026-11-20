"""
B3 议价护栏用例。

守住的五件事：
[1] 底价算得对 —— 百分比与金额两个上限**同时生效**，取更严的那个
[2] 出价解析对 —— 口语/中文数字/折扣/减免都能归一，且不把"3天""9成新"当价格
[3] 梯度合理   —— 不一次让到底、不重复报价、屠龙刀不接
[4] 保险丝有效 —— 模型写出低于底价的价格会被改写，改不掉就整条拦
[5] 默认关闭   —— 不打开就完全不影响现有流程（这条最重要，否则上线即风险）

跑法：python tests/test_bargain_guard.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="bargain_"))
os.environ["BARGAIN_DB"] = str(_TMP / "b.db")
os.environ["BARGAIN_CONFIG"] = str(_TMP / "b.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import bargain_guard as bg

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


# ---------------------------------------------------------------- [1] 底价
section("1 底价计算")

cfg = {"max_discount_percent": 10, "max_discount_amount": 100, "floor_absolute": 0}

# 160 元：10% → 144；减 100 → 60。取更严（更高）的 144
f = bg.compute_floor(160, cfg)
check(f["floor"] == 144.0, f"160/10%/100 → 底价 144（实际 {f['floor']}）")
check(f["binding"] == "百分比上限", "绑定的是百分比上限（更严）")
check(f["max_discount"] == 16.0, "最大让步 16 元")

# 便宜货：50 元，10% → 45；减 100 → -50。仍取 45
f = bg.compute_floor(50, cfg)
check(f["floor"] == 45.0, "50 元商品底价 45（金额上限不起作用）")

# 贵货：2000 元，10% → 1800；减 100 → 1900。这次金额上限更严
f = bg.compute_floor(2000, cfg)
check(f["floor"] == 1900.0, f"2000/10%/100 → 底价 1900（实际 {f['floor']}）")
check(f["binding"] == "金额上限", "绑定的是金额上限（贵货时金额更严）")

# 绝对底价会抬高 floor
f = bg.compute_floor(160, {**cfg, "floor_absolute": 150})
check(f["floor"] == 150.0, "绝对底价 150 覆盖 144")
check(f["binding"] == "全局绝对底价", "绑定说明改为绝对底价")

# 成本价会抬高 floor（不能亏本）
f = bg.compute_floor(160, cfg, item_floor=155)
check(f["floor"] == 155.0, "成本价 155 覆盖 144")
check(f["binding"] == "商品成本价", "绑定说明改为成本价")

# 成本价低于算出底价时不生效
f = bg.compute_floor(160, cfg, item_floor=100)
check(f["floor"] == 144.0, "成本价 100 不覆盖 144（取更高的）")

# 标价无效
f = bg.compute_floor(0, cfg)
check(f["ok"] is False, "标价 0 → 明确报错不瞎算")

# 百分比 0：完全不让价
f = bg.compute_floor(160, {**cfg, "max_discount_percent": 0, "max_discount_amount": 0})
check(f["floor"] == 160.0, "全部上限为 0 → 底价=标价（不让价）")


# ---------------------------------------------------------------- [2] 解析
section("2 出价解析")

cases = [
    ("130", 160, 130.0, "裸数字"),
    ("130块", 160, 130.0, "带单位"),
    ("¥130", 160, 130.0, "带符号"),
    ("130元包邮吗", 160, 130.0, "句中带单位"),
    ("少20", 160, 140.0, "减免（阿拉伯）"),
    ("便宜20", 160, 140.0, "便宜+数字"),
    ("打八折", 160, 128.0, "中文折扣"),
    ("9折", 160, 144.0, "阿拉伯折扣"),
    ("85折", 160, 136.0, "85折=8.5折（不是 85 折）"),
    ("一百三", 160, 130.0, "中文省略末位"),
    ("一百三十", 160, 130.0, "中文完整"),
    ("能不能100", 160, 100.0, "句尾裸数字"),
    ("便宜一百", 160, 60.0, "中文减免"),
    ("少十块", 160, 150.0, "中文小减免"),
]
for msg, lp, want, why in cases:
    got = bg.parse_offer(msg, lp)["offer"]
    check(got == want, f"{why}：{msg!r} → {want}（实际 {got}）")

not_price = [
    ("3天发货", "天数不是价格"),
    ("6张图", "张数不是价格"),
    ("9成新", "成色不是价格"),
    ("能便宜点吗", "没提数字"),
    ("160", "等于标价不算砍价"),
    ("你好", "闲聊"),
]
for msg, why in not_price:
    got = bg.parse_offer(msg, 160)["offer"]
    check(got is None, f"{why}：{msg!r} 不该解析出价格（实际 {got}）")

# 砍幅
check(bg.cut_ratio(130, 160) == 0.1875, "130/160 砍幅 18.75%")
check(bg.cut_ratio(None, 160) is None, "没出价 → 砍幅 None")
check(bg.cut_ratio(130, 0) is None, "标价 0 → 不除零")


# ---------------------------------------------------------------- [2b] 分档
section("2b 砍幅分档（对齐 148 份文档口径）")

check(bg.tier_of(0.02)["code"] == "tiny", "2% → 象征性")
check(bg.tier_of(0.10)["code"] == "normal", "10% → 正常")
check(bg.tier_of(0.19)["code"] == "common", "19% → 习惯性开口（文档原例：160→130）")
check(bg.tier_of(0.38)["code"] == "common", "38% → 习惯性开口（文档原例：160→100）")
check(bg.tier_of(0.41)["code"] == "hard", "41% → 屠龙刀")
check(bg.tier_of(None)["code"] == "none", "没出价 → none")

# 文档口径：19–38% 必须是 common，不能是 hard
check(all(bg.tier_of(r)["code"] == "common" for r in (0.19, 0.25, 0.30, 0.38)),
      "19%/25%/30%/38% 全部落在 common（这正是文档说的常态，不能当异常拒掉）")


# ---------------------------------------------------------------- [3] 决策
section("3 决策与让步梯度")

bg.save_config({"enabled": True, "max_rounds": 5, "max_discount_percent": 30,
                "max_discount_amount": 1000})
c = bg.get_config()

# 出价高于底价 → 直接接受
d = bg.decide("140", 160, chat_id="t1", cookie_id="ck", item_id="i1", cfg=c)
check(d["action"] == "accept", "140 ≥ 底价 112 → 接受")
check(d["target_price"] == 140.0, "接受价就是买家出价")

# 屠龙刀 → 拒
d = bg.decide("80", 160, chat_id="t2", cookie_id="ck", item_id="i1", cfg=c)
check(d["action"] == "reject", "80/160 砍 50% → 拒")

# 没提数字 → 不主动降
d = bg.decide("能便宜点吗", 160, chat_id="t3", cookie_id="ck", item_id="i1", cfg=c)
check(d["action"] == "no_offer", "没提数字 → no_offer")
check(d["target_price"] is None, "不主动给价")

# 梯度：逐步收窄，不一次到底
bg.save_config({"max_rounds": 10, "max_discount_percent": 30, "max_discount_amount": 1000})
c = bg.get_config()
seq = []
for msg in ["120", "118", "116", "114"]:
    d = bg.decide(msg, 160, chat_id="grad", cookie_id="ck", item_id="i1", cfg=c)
    seq.append(d["target_price"])
check(len(set(seq)) > 1, f"多轮还价价格会变化（{seq}），不是死报一个数")
check(seq == sorted(seq, reverse=True), f"价格单调下降（{seq}）")
check(all(p >= 112 for p in seq), f"任何一轮都不破底价 112（{seq}）")

# 一路砍到底之后不再重复报价（用独立会话，避免被上面的梯度会话干扰）
bg.save_config({"max_rounds": 20, "max_discount_percent": 30, "max_discount_amount": 1000})
c2 = bg.get_config()
for _ in range(12):
    d = bg.decide("100", 160, chat_id="floorhit", cookie_id="ck", item_id="i1", cfg=c2)
check(d["action"] == "escalate",
      f"连续砍到贴底后转人工，不无限重复报价（action={d['action']}）")

# 轮数用尽
bg.save_config({"max_rounds": 2, "max_discount_percent": 30, "max_discount_amount": 1000})
c = bg.get_config()
for msg in ["120", "118"]:
    bg.decide(msg, 160, chat_id="cap", cookie_id="ck", item_id="i1", cfg=c)
d = bg.decide("116", 160, chat_id="cap", cookie_id="ck", item_id="i1", cfg=c)
check(d["action"] == "escalate" and d["escalate"], "超过 2 轮 → 转人工")

# 会话状态独立：不同 chat 不互相污染
d = bg.decide("130", 160, chat_id="other", cookie_id="ck", item_id="i1", cfg=c)
check(d["rounds"] == 0, "新会话轮数从 0 开始（不受别的会话影响）")

# 不同 item 也独立
s1 = bg.get_session("grad", "ck", "i1")
s2 = bg.get_session("grad", "ck", "i2")
check(s1["rounds"] > 0 and s2["rounds"] == 0, "同一会话不同商品状态隔离")


# ---------------------------------------------------------------- [4] 保险丝
section("4 回复回读校验（真正的保险丝）")

D = {"ok": True, "floor": 144.0, "list_price": 160.0, "offer": 130.0,
     "tier": "common", "cut_ratio": 0.19, "target_price": 152.0,
     "action": "counter", "chat_id": "x", "cookie_id": "c", "item_id": "i"}

r = bg.enforce("好的，144元给你，包邮", D)
check(r["ok"] and r["reply"] == "好的，144元给你，包邮", "合规回复原样通过")
check(not r["violations"], "合规回复无违规记录")

r = bg.enforce("行吧，130元就130元", D)
check(r["ok"] and "130" not in r["reply"], "130 低于底价 → 数字被改写")
check("144" in r["reply"], "改写为底价 144")
check(r["rewritten"] is True, "标记已改写")

r = bg.enforce("¥100拿走", D)
check(r["ok"] and "100" not in r["reply"], "¥100 被改写")

r = bg.enforce("最低120，不能再少了", D)
check("120" not in r["reply"], "「最低120」被改写")

r = bg.enforce("这个价还能再便宜哦", D)
check("还能再便宜" not in r["reply"], "暗示可让价的话术被改写")

r = bg.enforce("可以商量，你出个价", D)
check("可以商量" not in r["reply"], "「可以商量」被改写")

# 关键：非价格的数字不能动
r = bg.enforce("3天发货，6张实拍图，9成新，160元", D)
check(r["reply"] == "3天发货，6张实拍图，9成新，160元",
      f"非价格数字不被误改（实际 {r['reply']!r}）")

# 空回复
r = bg.enforce("", D)
check(r["ok"] is False, "空回复 → 拦")

# 决策无效
r = bg.enforce("随便", {"ok": False})
check(r["ok"] is False, "决策无效 → 拦")

# prompt_block 必须含硬约束关键词
pb = bg.prompt_block(D)
check("绝对底价" in pb, "约束块含「绝对底价」")
check("144" in pb, "约束块含具体底价数字")
check("不得违反" in pb or "不许" in pb, "约束块语气是硬约束不是建议")


# ---------------------------------------------------------------- [4b] 诊断
section("4b 底价 vs 市场常态的诊断")

bg.save_config({"max_discount_percent": 10, "max_discount_amount": 100})
dg = bg.diagnose_floor()
check(dg["allowed_ratio"] == 0.1, "10%/100 → 160 元货只能让 10%")
check(dg["tight"] is True, "10% 被判为紧于市场常态（19–38%）")
check("19%" in dg["note"] or "19" in dg["note"], "诊断说明里引用了市场常态数字")
check(dg["verdict"], f"有明确结论：{dg['verdict']}")

bg.save_config({"max_discount_percent": 40, "max_discount_amount": 100})
dg = bg.diagnose_floor()
check(dg["tight"] is False, "40% 不再算紧")
check(dg["allowed_ratio"] >= 0.38, "40% 覆盖市场常态上限")
bg.save_config({"max_discount_percent": 10, "max_discount_amount": 100})


# ---------------------------------------------------------------- [4c] 上下文
# 拆解 super-butler/app/ai_reply_engine.py 打出来的四个真缺口，逐条钉死。
section("4c 上下文感知（无单位报价 / 费用词 / 让价额 / 思维链）")

D2 = {"ok": True, "floor": 144.0, "list_price": 160.0, "offer": 130.0,
      "tier": "common", "cut_ratio": 0.19, "target_price": 152.0,
      "action": "counter", "chat_id": "y", "cookie_id": "c", "item_id": "j"}

# 缺口①：中文模型最自然的成交写法都没有货币单位，旧版全部放行
for raw in ("行吧 130 拿走", "可以，130", "130给你", "130", "130成交", "好吧 130 包邮"):
    r = bg.enforce(raw, D2)
    check(r["ok"] and "130" not in r["reply"] and "144" in r["reply"],
          f"无单位报价被改写：{raw!r} → {r['reply']!r}")

# 缺口②：费用/让价额不是售价，改写成底价会把句子说反
for raw in ("运费10元，我们包邮", "定金20元，尾款发货", "补差价15元", "首重12元",
            "押金50元", "包邮的，运费我出12元", "给你便宜10元", "少10块", "走顺丰加8元"):
    r = bg.enforce(raw, D2)
    check(r["reply"] == raw and not r["violations"],
          f"费用/让价额原样保留：{raw!r} → {r['reply']!r}")

# 但「便宜点130吧」是目标价，不是让价额 —— 用标价 20%–200% 的量级区分
r = bg.enforce("便宜点130吧", D2)
check("130" not in r["reply"] and "144" in r["reply"],
      f"「便宜点130」按报价处理 → {r['reply']!r}")

# 缺口③：裸数字只在标价 20%–200% 区间内才算报价（与 parse_offer 同一把尺）
r = bg.enforce("1600", D2)
check(r["reply"] == "1600", "1600 不在区间 → 不当作报价，也不会被切成 160")
r = bg.enforce("120-150斤可以穿", D2)
check(r["reply"] == "120-150斤可以穿", "区间数字（体重/尺码）不动")
r = bg.enforce("3天发货，9成新，130元", D2)
check("3天" in r["reply"] and "9成新" in r["reply"] and "130" not in r["reply"],
      f"非价格数字不动、真价格被改：{r['reply']!r}")

# 按位置改写：一句话两个低价，两个都要改；同句里的非价格数字不受影响
r = bg.enforce("130和120都不行", D2)
check("130" not in r["reply"] and "120" not in r["reply"], "同一句两个低价全部改写")

# 裸数字议价要能进 price 意图（否则 B3 不启动、轮数也不计数）
for raw in ("167", "160吧", "165元", "¥150", "150 行不行", "160 拿走"):
    check(bg.looks_like_price_offer(raw), f"识别为报价：{raw!r}")
for raw in ("2XL 有吗", "120-150斤", "怎么用", "1+1=？", "什么时候发货", "在吗"):
    check(not bg.looks_like_price_offer(raw), f"不当作报价：{raw!r}")

# 缺口④：思维链 / 提示词泄漏
r = bg.scrub_model_output("<think>底价是 176.4，先报 180</think>可以，180 元")
check(r["ok"] and "think" not in r["text"] and "176.4" not in r["text"],
      f"<think> 块被剥掉 → {r['text']!r}")
r = bg.scrub_model_output("议价硬约束：本轮最多报到 176.4 元")
check(not r["ok"], "提示词原文泄漏 → 整条拦")
r = bg.scrub_model_output("这已经是绝对底价了，不能再少")
check(r["ok"], "「绝对底价」是正常话术，不算泄漏")
r = bg.scrub_model_output("<think>只有思考没有答案</think>")
check(not r["ok"], "只剩思考、没有可发内容 → 拦")

# 接线：detect_intent 认纯数字；scrub 必须在 enforce 之前
src2 = Path("ai_reply_engine.py").read_text(encoding="utf-8")
check("looks_like_price_offer" in src2, "detect_intent 已接入 looks_like_price_offer")
check(0 <= src2.find("scrub_model_output") < src2.find("_bg.enforce("),
      "先剥思维链，再走底价保险丝")


# ---------------------------------------------------------------- [5] 默认关闭
section("5 默认姿态")

# 清掉配置，回到默认
Path(os.environ["BARGAIN_CONFIG"]).unlink(missing_ok=True)
cfg = bg.get_config()
check(cfg["enabled"] is False, "默认关闭（第 31 天才建议开）")
check(cfg["max_rounds"] == 3, "默认最多 3 轮")
check(cfg["max_discount_percent"] == 10, "默认让价上限 10%")
check(cfg["escalate_on_exceed"] is True, "默认超界转人工")

# 关着的时候，ai_reply_engine 不该调用 decide
src = Path("ai_reply_engine.py").read_text(encoding="utf-8")
check("if _bg.get_config().get(\"enabled\"):" in src,
      "ai_reply_engine 里用 enabled 开关守着，关着不进入议价逻辑")
check("bargain_guard" in src, "ai_reply_engine 已接入 bargain_guard")

# 只读性：B3 不应有任何改价/发布调用
bsrc = Path(bg.__file__).read_text(encoding="utf-8")
banned = ["publish_item", "edit_price", "update_price", "downShelf", "mtop"]
hits = [b for b in banned if b in bsrc]
check(not hits, f"bargain_guard 不含任何改价/发布/接口调用（命中 {hits}）")


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
