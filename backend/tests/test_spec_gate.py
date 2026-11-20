"""规格匹配闸回归测试（拆解 26）
================================
守住的四件事，任何一条回退都会让"多规格订单发错货"重新出现：

[1] 解析器按**内容**分类，不按元素个数：双规格订单在页面上是 3 个
    `.sku--u_ddZval`（规格 + 规格 + 数量），老代码 `len==2 → 规格+数量`
    必然零解析。
[2] 分号拼两组（`颜色:黑;尺码:L`）能拆成两维；全角冒号/分号同样处理；
    列表下标（`1:xx`）不能被当成规格。
[3] 组数判定"对不上就停"：声明有规格却解析不出 → 停；声明 2 组解析出 1 组 → 停。
    `spec_group_count = None` 是"未声明"，不能当成 1。
[4] 卡券匹配严格按维数：一维订单只落一维卡，两维订单只落两维卡；
    给了规格却匹配不到时**返回空**，不再回落到普通关键词卡（发错货）。

跑法（在 backend 目录）：`python3 tests/test_spec_gate.py`
没有 pytest 时也能直接跑；本文件不依赖 pytest。
"""

from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))
sys.path.insert(0, str(ROOT))

# 必须在 import db_manager 之前指定测试库，绝不能碰真实 data/xianyu_data.db
_TMP = tempfile.mkdtemp(prefix="spec_gate_")
os.environ["DB_PATH"] = str(Path(_TMP) / "test.db")

import db_manager as dbm  # noqa: E402
import spec_gate  # noqa: E402
from utils.order_detail_fetcher import OrderDetailFetcher  # noqa: E402

FAILS: list[str] = []
PASSES = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ok   {name}")
    else:
        FAILS.append(name)
        print(f"  FAIL {name}" + (f"  <- {detail}" if detail else ""))


def section(title: str) -> None:
    print(f"\n== {title}")


# ——————————————————————————— 1. 解析器 ———————————————————————————

section("解析器：按内容分类 + 分号拆两组")

fetcher = OrderDetailFetcher()
pairs = OrderDetailFetcher._parse_spec_pairs

check("单组 颜色:黑", pairs("颜色:黑") == [("颜色", "黑")])
check("两组 分号+半角", pairs("颜色:黑;尺码:L") == [("颜色", "黑"), ("尺码", "L")])
check("两组 全角冒号+全角分号", pairs("颜色：黑；尺码：L") == [("颜色", "黑"), ("尺码", "L")])
check("两组 带空格", pairs(" 颜色 : 黑 ; 尺码 : L ") == [("颜色", "黑"), ("尺码", "L")])
check("下标 1:2 不是规格", pairs("1:2") == [])
check("无冒号 数量:1 之外", pairs("x1") == [])
check("空串", pairs("") == [])
check("缺值 颜色:", pairs("颜色:") == [])

parsed = fetcher._parse_sku_content("颜色:黑;尺码:L")
check("单串两组 → 四字段",
      parsed.get("spec_name") == "颜色" and parsed.get("spec_value") == "黑"
      and parsed.get("spec_name_2") == "尺码" and parsed.get("spec_value_2") == "L",
      str(parsed))

parsed1 = fetcher._parse_sku_content("颜色:黑")
check("单串一组 → 两字段且无第二维",
      parsed1.get("spec_name") == "颜色" and not parsed1.get("spec_name_2"),
      str(parsed1))


# ——————————————————————————— 2. 组数判定 ———————————————————————————

section("组数判定：对不上就停，未声明不等于 1")

ok, reason = spec_gate.check_spec_shape(is_multi_spec=True, declared_groups=2, parsed_groups=2)
check("声明 2 组 + 解析 2 组 → 放行", ok and reason == spec_gate.REASON_OK, reason)

ok, reason = spec_gate.check_spec_shape(is_multi_spec=True, declared_groups=2, parsed_groups=1)
check("声明 2 组 + 解析 1 组 → 停", not ok and reason == spec_gate.REASON_GROUP_MISMATCH, reason)

ok, reason = spec_gate.check_spec_shape(is_multi_spec=True, declared_groups=1, parsed_groups=2)
check("声明 1 组 + 解析 2 组 → 停", not ok and reason == spec_gate.REASON_GROUP_MISMATCH, reason)

ok, reason = spec_gate.check_spec_shape(is_multi_spec=True, declared_groups=2, parsed_groups=0)
check("声明 2 组 + 解析失败 → 停(parse_failed)",
      not ok and reason == spec_gate.REASON_PARSE_FAILED, reason)

ok, reason = spec_gate.check_spec_shape(is_multi_spec=True, declared_groups=None, parsed_groups=0)
check("有标记但未声明 + 解析失败 → 停", not ok and reason == spec_gate.REASON_PARSE_FAILED, reason)

ok, reason = spec_gate.check_spec_shape(is_multi_spec=True, declared_groups=None, parsed_groups=2)
check("有标记未声明 + 解析 2 组 → 放行（靠严格卡券兜住）", ok, reason)

ok, reason = spec_gate.check_spec_shape(is_multi_spec=False, declared_groups=0, parsed_groups=0)
check("普通商品 → 放行", ok and reason == spec_gate.REASON_OK, reason)

check("needs_detail_fetch(None) 为 False", spec_gate.needs_detail_fetch(False, None) is False)
check("needs_detail_fetch(0) 为 False", spec_gate.needs_detail_fetch(False, 0) is False)
check("needs_detail_fetch(1) 为 True", spec_gate.needs_detail_fetch(False, 1) is True)
check("needs_detail_fetch(标记) 为 True", spec_gate.needs_detail_fetch(True, None) is True)
check("needs_detail_fetch('2') 认字符串", spec_gate.needs_detail_fetch(False, "2") is True)

check("spec_desc 两组", spec_gate.spec_desc("颜色", "黑", "尺码", "L") == "颜色:黑;尺码:L")
check("spec_desc 一组", spec_gate.spec_desc("颜色", "黑") == "颜色:黑")
check("card_spec_key 把 None 归一成空串",
      spec_gate.card_spec_key({"spec_name": "颜色", "spec_value": "黑"}) == ("颜色", "黑", "", ""))


# ——————————————————————————— 3. 数据库 ———————————————————————————

section("数据库：两维规格的建表、唯一性与匹配")

DB = dbm.DBManager(os.environ["DB_PATH"])

# cards.user_id 是 NOT NULL，先建一个账号再用
DB.create_user("spec_tester", "spec_tester@example.com", "test-only-pass")
USER_ID = DB.get_user_by_username("spec_tester")["id"]

# 三张卡：普通卡、一维多规格、两维多规格
plain_id = DB.create_card(name="网盘资料", card_type="text", text_content="普通卡内容", user_id=USER_ID)
one_id = DB.create_card(name="网盘资料", card_type="text", text_content="一维-黑",
                        is_multi_spec=True, spec_name="颜色", spec_value="黑", user_id=USER_ID)
two_id = DB.create_card(name="网盘资料", card_type="text", text_content="两维-黑-L",
                        is_multi_spec=True, spec_name="颜色", spec_value="黑",
                        spec_name_2="尺码", spec_value_2="L", user_id=USER_ID)

check("三张卡都创建成功", all([plain_id, one_id, two_id]))

DB.create_delivery_rule(keyword="网盘资料", card_id=plain_id)
DB.create_delivery_rule(keyword="网盘资料", card_id=one_id)
DB.create_delivery_rule(keyword="网盘资料", card_id=two_id)

hit2 = DB.get_delivery_rules_by_keyword_and_spec("网盘资料", "颜色", "黑", "尺码", "L")
check("两维规格 → 只命中两维卡",
      len(hit2) == 1 and hit2[0]["card_id"] == two_id,
      str([(r["card_id"], r["spec_name_2"]) for r in hit2]))
check("两维命中结果带第二维",
      hit2 and hit2[0].get("spec_name_2") == "尺码" and hit2[0].get("spec_value_2") == "L")

hit1 = DB.get_delivery_rules_by_keyword_and_spec("网盘资料", "颜色", "黑")
check("一维规格 → 只命中一维卡（不落两维卡）",
      len(hit1) == 1 and hit1[0]["card_id"] == one_id,
      str([r["card_id"] for r in hit1]))

miss = DB.get_delivery_rules_by_keyword_and_spec("网盘资料", "颜色", "绿")
check("规格对不上 → 返回空", miss == [])
check("规格对不上也不落普通卡（老 bug）", all(r["card_id"] != plain_id for r in miss))

miss2 = DB.get_delivery_rules_by_keyword_and_spec("网盘资料", "颜色", "黑", "尺码", "XL")
check("两维里第二维对不上 → 返回空", miss2 == [], str([r["card_id"] for r in miss2]))

hit_plain = DB.get_delivery_rules_by_keyword_and_spec("网盘资料")
check("不给规格 → 只命中普通卡",
      len(hit_plain) == 1 and hit_plain[0]["card_id"] == plain_id,
      str([(r["card_id"], r["is_multi_spec"]) for r in hit_plain]))

# 唯一性：两维卡的"同名同规格"才算重复
dup_blocked = False
try:
    DB.create_card(name="网盘资料", card_type="text", text_content="重复",
                   is_multi_spec=True, spec_name="颜色", spec_value="黑",
                   spec_name_2="尺码", spec_value_2="L", user_id=USER_ID)
except ValueError:
    dup_blocked = True
check("完全相同的两维规格 → 拒绝重复", dup_blocked)

same_first = DB.create_card(name="网盘资料", card_type="text", text_content="两维-黑-M",
                            is_multi_spec=True, spec_name="颜色", spec_value="黑",
                            spec_name_2="尺码", spec_value_2="M", user_id=USER_ID)
check("第一维相同、第二维不同 → 允许", bool(same_first))

half_blocked = False
try:
    DB.create_card(name="网盘资料", card_type="text", text_content="半截",
                   is_multi_spec=True, spec_name="颜色", spec_value="白",
                   spec_name_2="尺码", user_id=USER_ID)
except ValueError:
    half_blocked = True
check("第二维只给一半 → 拒绝", half_blocked)

ignored = DB.create_card(name="普通卡带规格", card_type="text", text_content="x",
                         is_multi_spec=False, spec_name="颜色", spec_value="黑", user_id=USER_ID)
card = DB.get_card_by_id(ignored)
check("非多规格卡携带规格字段 → 落库时清空",
      card and card.get("spec_name") is None and card.get("spec_name_2") is None,
      str(card))

check("create_card 返回值里的第二维可读",
      DB.get_card_by_id(two_id).get("spec_name_2") == "尺码")

DB.update_card(two_id, spec_value_2="XL")
check("update_card 能改第二维",
      DB.get_card_by_id(two_id).get("spec_value_2") == "XL")

# —— 商品级规格声明 ——
DB.save_item_info(cookie_id="c1", item_id="i1", item_data={"title": "测试商品", "detail": "详情"})
meta = DB.get_item_spec_meta("c1", "i1")
check("未声明时返回 None（不是 1）", meta["found"] and meta["spec_group_count"] is None, str(meta))
check("未声明时 is_multi_spec 为 False", meta["is_multi_spec"] is False)

check("写 2 组声明成功", DB.update_item_spec_meta("c1", "i1", 2, ["颜色", "尺码"]))
meta = DB.get_item_spec_meta("c1", "i1")
check("读到 2 组 + 组名顺序",
      meta["spec_group_count"] == 2 and meta["spec_names"] == ["颜色", "尺码"], str(meta))
check("3 组被拒绝（平台最多 2 组）", DB.update_item_spec_meta("c1", "i1", 3) is False)
check("拒绝后库里仍是 2", DB.get_item_spec_meta("c1", "i1")["spec_group_count"] == 2)
check("不存在的商品写声明返回 False", DB.update_item_spec_meta("c1", "nope", 2) is False)
check("不存在的商品读声明安全",
      DB.get_item_spec_meta("c1", "nope") == {"found": False, "is_multi_spec": False,
                                             "spec_group_count": None, "spec_names": None})

# —— 订单两维落库 ——
DB.insert_or_update_order(order_id="o1", item_id="i1", buyer_id="b1",
                          spec_name="颜色", spec_value="黑",
                          spec_name_2="尺码", spec_value_2="L",
                          quantity="1", amount="19.90")
order = DB.get_order_by_id("o1")
check("订单保存两维规格",
      order.get("spec_name") == "颜色" and order.get("spec_name_2") == "尺码"
      and order.get("spec_value_2") == "L", str(order))

DB.insert_or_update_order(order_id="o1", spec_value_2="M")
check("订单更新第二维生效", DB.get_order_by_id("o1").get("spec_value_2") == "M")

# —— 迁移后的表结构 ——
conn = sqlite3.connect(os.environ["DB_PATH"])
cols_item = {r[1] for r in conn.execute("PRAGMA table_info(item_info)")}
cols_card = {r[1] for r in conn.execute("PRAGMA table_info(cards)")}
cols_order = {r[1] for r in conn.execute("PRAGMA table_info(orders)")}
conn.close()

check("item_info 有 spec_group_count / spec_names",
      {"spec_group_count", "spec_names"} <= cols_item, str(cols_item))
check("cards 有 spec_name_2 / spec_value_2",
      {"spec_name_2", "spec_value_2"} <= cols_card, str(cols_card))
check("orders 有 spec_name_2 / spec_value_2",
      {"spec_name_2", "spec_value_2"} <= cols_order, str(cols_order))

# 重开一次库：迁移必须幂等，且不能把已有数据搞丢
DB2 = dbm.DBManager(os.environ["DB_PATH"])
check("重开库后两维卡还在",
      DB2.get_card_by_id(two_id).get("spec_value_2") == "XL")
check("重开库后规格声明还在",
      DB2.get_item_spec_meta("c1", "i1")["spec_group_count"] == 2)


print("\n" + "=" * 60)
print(f"通过 {PASSES} 项，失败 {len(FAILS)} 项")
if FAILS:
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)