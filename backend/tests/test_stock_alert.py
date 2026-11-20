"""
拆解 13：卡券低库存预警
=======================

来源：嗷嗷鱼 FAQ Q25——"后台每 30 分钟扫描一次启用中的「批量数据」卡券，
剩余 ≤ 10 条时推一条提醒，同一渠道 24 小时内只提醒一次"。

我们照口径做，只改一处：**按卡券**去重，不是全局。主力卡券和冷门卡券
不该抢同一次提醒额度；同一张卡券仍然 24 小时只提醒一次。

守住的四件事：
[1] 只有 type='data' 有库存概念（text/api/image 每次都一样/现取，不返回库存）
[2] 阈值边界：≤10 提醒，11 不提醒；停用的卡券不看
[3] 同一张卡券 24 小时内只提醒一次，过点会再提醒
[4] 状态文件损坏 → 当作没提醒过（宁可多提醒，不能从此不提醒）

跑法：cd backend && python3 tests/test_stock_alert.py
"""

import json
import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="stock_alert_"))
os.environ["STOCK_ALERT_STATE"] = str(_TMP / "state.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 替身：卡券表 + 通知服务
CARDS = []


class _DB:
    def get_all_cards(self, user_id=None):
        return CARDS


_dbm = type(sys)("db_manager")
_dbm.db_manager = _DB()
sys.modules["db_manager"] = _dbm

SENT = []


def _send_event(event, title, content, level="info", force=False):
    SENT.append({"event": event, "title": title, "content": content})
    return {"ok": True}


_ns = type(sys)("notify_service")
_ns.send_event = _send_event
sys.modules["notify_service"] = _ns

import stock_alert as sa  # noqa: E402

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


def card(cid, name, n, typ="data", enabled=True, spec=None):
    c = {"id": cid, "name": name, "type": typ, "enabled": enabled,
         "data_content": "\n".join(f"card-{cid}-{i}" for i in range(n))}
    if spec:
        c["is_multi_spec"], c["spec_name"], c["spec_value"] = True, "规格", spec
    return c


# ---------------------------------------------------------------- [1]
section("1 只有批量数据卡有库存")
check(sa.remaining_of(card(1, "A", 5)) == 5, "5 行 → 5")
check(sa.remaining_of({"id": 2, "type": "text", "text_content": "固定文字"}) is None,
      "文本卡没有库存概念")
check(sa.remaining_of({"id": 3, "type": "api", "api_config": "{}"}) is None, "API 卡没有")
check(sa.remaining_of(card(4, "B", 0)) == 0, "空数据 → 0")
check(sa.remaining_of({"id": 5, "type": "data", "data_content": "a\n\n b \n"}) == 2,
      "空行不计")

# ---------------------------------------------------------------- [2]
section("2 阈值边界与停用")
CARDS.clear()
CARDS.extend([
    card(10, "正好10条", 10),
    card(11, "11条", 11),
    card(12, "停用但只剩1条", 1, enabled=False),
    card(13, "文本卡", 0, typ="text"),
    card(14, "多规格剩2条", 2, spec="月卡"),
])
res = sa.scan()
ids = {i["card_id"] for i in res["low"]}
check(res["ok"] is True, "scan 成功")
check(res["checked"] == 3, "只数了启用中的批量数据卡（10/11/14）")
check(10 in ids, "正好 10 条会提醒（≤）")
check(11 not in ids, "11 条不提醒")
check(12 not in ids, "停用的不提醒")
check(13 not in ids, "文本卡不提醒")
check(14 in ids, "多规格卡按剩余条数判断")
label = [i["name"] for i in res["low"] if i["card_id"] == 14][0]
check("月卡" in label, "多规格卡带规格名（知道是哪个规格断货）")

# ---------------------------------------------------------------- [3]
section("3 同一张卡券 24 小时只提醒一次")
SENT.clear()
now = datetime(2026, 1, 1, 9, 0, 0)
r1 = sa.maybe_notify(now=now)
check(len(SENT) == 1, "第一次推了一条")
check(SENT[0]["event"] == "risk", "用 risk 事件（不新增事件类型）")
check("正好10条" in SENT[0]["content"] and "多规格剩2条" in SENT[0]["content"],
      "一条通知里列出所有低库存卡券")
check({i["card_id"] for i in r1["notified"]} == {10, 14}, "notified 里是这两张")

SENT.clear()
r2 = sa.maybe_notify(now=now + timedelta(hours=1))
check(len(SENT) == 0, "1 小时后不重复推")
check({i["card_id"] for i in r2["skipped"]} == {10, 14}, "标成 skipped 而不是消失")

SENT.clear()
r3 = sa.maybe_notify(now=now + timedelta(hours=25))
check(len(SENT) == 1, "25 小时后重新提醒")
check({i["card_id"] for i in r3["notified"]} == {10, 14}, "两张都重新进提醒")

# 只新掉一张：只有它被提醒
SENT.clear()
CARDS.append(card(15, "新断货", 3))
r4 = sa.maybe_notify(now=now + timedelta(hours=26))
check({i["card_id"] for i in r4["notified"]} == {15}, "只有新掉的卡被提醒（按卡券去重）")
check({i["card_id"] for i in r4["skipped"]} == {10, 14}, "老的仍在冷却期")
check(len(SENT) == 1 and "新断货" in SENT[0]["content"], "通知内容只含新掉的那张")

# ---------------------------------------------------------------- [4]
section("4 状态文件坏了不能从此不提醒")
Path(os.environ["STOCK_ALERT_STATE"]).write_text("{not json", encoding="utf-8")
SENT.clear()
r5 = sa.maybe_notify(now=now + timedelta(hours=27))
check(len(SENT) == 1, "坏文件 → 当成没提醒过，照常提醒")
check(r5["ok"] is True, "返回值仍 ok")

# ---------------------------------------------------------------- [5]
section("5 数据库读不到时不抛异常（心跳里调用）")


class _BadDB:
    def get_all_cards(self, user_id=None):
        raise RuntimeError("库锁了")


sys.modules["db_manager"].db_manager = _BadDB()
SENT.clear()
r6 = sa.maybe_notify(now=now + timedelta(days=3))
check(r6["ok"] is False and "库锁了" in r6.get("error", ""), "返回 ok=False + 原因")
check(len(SENT) == 0, "没有乱发通知")

# ---------------------------------------------------------------- [6]
section("6 接线：心跳里真的会调")
src = (BACKEND / "XianyuAutoAsync.py").read_text(encoding="utf-8")
check("import stock_alert as _sa" in src, "心跳里 import stock_alert")
check("await asyncio.to_thread(_sa.maybe_notify)" in src, "用 to_thread 跑（同步扫库不堵事件循环）")
check("库存预警异常（不影响心跳）" in src, "异常只记日志，不影响心跳")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")