"""
拆解 1c：卖家端订单直连接口用例（不起浏览器拿订单）。

守住七件事：
[1] 字段映射   —— 金额取 totalPrice、数量取 buyNum；缺 totalPrice 时单价×件数兜底
[2] 请求形态   —— orderIds 逗号串、orderSearchParam 是 JSON 字符串、卖家来源头齐全
[3] 分页终止   —— 只看 nextPage；totalCount=0 不能当成"没有下一页"之外的判断依据
[4] 令牌续期   —— TOKEN_EXOIRED（平台自己的错拼）也要重签重试，新令牌来自失败响应
[5] 永不炸主流程 —— 接口失败返回空字典，由调用方回退浏览器
[6] 自我熔断   —— 连续失败后暂停，别每单都去撞墙
[7] 顺序契约   —— process_orders_batch 的返回值必须与入参逐位对齐

跑法：python tests/test_seller_api.py
"""

import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import seller_api as sa

FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


COOKIE = "unb=1; _m_h5_tk=abc123_1699999999999; cookie2=xyz"


class FakeHeaders(dict):
    def getlist(self, key):
        v = self.get(key)
        if v is None:
            return []
        return v if isinstance(v, list) else [v]


class FakeResp:
    def __init__(self, payload, headers=None, status=200):
        self._payload = payload
        self.headers = FakeHeaders(headers or {})
        self.status_code = status

    def json(self):
        return self._payload


class FakeRequests(types.ModuleType):
    def __init__(self, script=None):
        super().__init__("requests")
        self.script = list(script or [])
        self.calls = []

    def post(self, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "params": dict(params or {}),
                           "data": dict(data or {}), "headers": dict(headers or {})})
        item = self.script.pop(0) if self.script else FakeResp({"ret": ["SUCCESS::ok"]})
        if isinstance(item, Exception):
            raise item
        return item


def install(script=None, app_key="test-app-key"):
    fake = FakeRequests(script)
    sa.requests = fake
    sa._app_key = lambda: app_key
    return fake


ITEM = {
    "commonData": {"orderId": 3847123456789, "itemId": 99887766,
                   "orderStatus": "等待卖家发货", "createTime": "2026-11-01 10:00:00",
                   "inRefund": False, "sellerRateStatus": 5},
    "buyerInfoVO": {"buyerId": 55667788, "userNick": "买家甲", "name": "张三",
                    "phone": "138****0000", "address": "上海市浦东新区", "isPrivacy": False},
    "priceVO": {"totalPrice": "176.40", "auctionPrice": "180.00", "buyNum": 2,
                "postFee": "0.00", "refundFee": "0.00"},
    "itemVO": {"title": "二手书一本"},
    "rightVO": {"btnList": [{"tradeAction": "RATE"}, {"tradeAction": "SHIP"}]},
}


# ---------------------------------------------------------------- [1] 字段映射
section("1 字段映射")
row = sa.parse_sold_order(ITEM)
check(row["order_id"] == "3847123456789", "订单号转字符串（接口给的是整数）")
check(row["amount"] == "176.40", "金额取 totalPrice（真实成交额，不是挂牌价 180.00）")
check(row["auction_price"] == "180.00", "挂牌价单独留档，便于后面算折扣")
check(row["quantity"] == "2", "数量取 buyNum（多件单不会算成 1 件）")
check(row["order_status"] == "pending_ship", "中文状态文案已归一化")
check(row["status_text"] == "等待卖家发货", "原始文案保留")
check(row["buyer_id"] == "55667788", "买家 ID 拿到（DOM 抓不到这个）")
check(row["receiver_name"] == "张三" and row["receiver_address"].startswith("上海"),
      "收货信息一次拿全")
check(row["can_rate"] is True and row["trade_actions"] == ["RATE", "SHIP"],
      "可执行动作解析（RATE → 能评价）")
check(row["from_seller_api"] is True, "标记来源，便于排查是哪条路径给的数")

no_total = dict(ITEM)
no_total["priceVO"] = {"auctionPrice": "50.00", "buyNum": 3}
row2 = sa.parse_sold_order(no_total)
check(row2["amount"] == "150.00", "缺 totalPrice 时用单价×件数兜底（并标注差异口径）")


# ---------------------------------------------------------------- [2] 请求形态
section("2 请求形态")
fake = install([FakeResp({"ret": ["SUCCESS::ok"], "data": {"module": {
    "items": [ITEM], "totalCount": 1, "nextPage": False}}})])
batch = sa.get_sold_orders(COOKIE, order_ids=["111", "222"], rows_per_page=20)
call = fake.calls[0]
check(call["params"]["api"] == "mtop.taobao.idle.trade.merchant.sold.get", "接口名正确")
check(call["headers"]["origin"] == "https://www.goofish.com",
      "来源必须是 www（seller 来源会无权限）")
check("idle_site_biz_code" not in call["headers"], "不带业务线标识头（带了反而无权限）")
check(call["data"]["data"].find('"orderIds":"111,222"') > 0, "orderIds 是逗号串")
check('"orderSearchParam":"{}"' in call["data"]["data"], "orderSearchParam 是 JSON 字符串")
import json as _json
payload = _json.loads(call["data"]["data"])
check(payload["queryCode"] == "ALL" and payload["pageNumber"] == 1, "默认查全部、第一页")
check(call["params"]["sign"] and len(call["params"]["sign"]) == 32, "签名是 md5 32 位")
check(batch["items"] and batch["next_page"] is False, "响应结构解析正确")


# ---------------------------------------------------------------- [3] 分页终止
section("3 分页终止")
page1 = {"ret": ["SUCCESS::ok"], "data": {"module": {
    "items": [ITEM], "totalCount": 0, "nextPage": True}}}
page2 = {"ret": ["SUCCESS::ok"], "data": {"module": {
    "items": [dict(ITEM, commonData=dict(ITEM["commonData"], orderId=999))],
    "totalCount": 0, "nextPage": False}}}
fake = install([FakeResp(page1), FakeResp(page2)])
rows = sa.iter_sold_orders(COOKIE, page_interval=0)
check(len(rows) == 2, "totalCount=0 但 nextPage=true 时继续翻页（不会被 0 骗停）")
check(len(fake.calls) == 2, "第二页 nextPage=false 后停止")
check(rows[0]["commonData"]["orderId"] == 3847123456789, "顺序保持")

# 重复页去重
dup = {"ret": ["SUCCESS::ok"], "data": {"module": {
    "items": [ITEM], "totalCount": 0, "nextPage": True}}}
fake = install([FakeResp(dup), FakeResp(dict(dup)), FakeResp(
    {"ret": ["SUCCESS::ok"], "data": {"module": {"items": [], "nextPage": False}}})])
rows = sa.iter_sold_orders(COOKIE, page_interval=0)
check(len(rows) == 1, "翻页重叠的订单只收一次")


# ---------------------------------------------------------------- [4] 令牌续期
section("4 令牌续期")
expired = FakeResp({"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]},
                   headers={"set-cookie": "_m_h5_tk=NEWTOKEN_1700000000000; Path=/"})
ok = FakeResp({"ret": ["SUCCESS::ok"], "data": {"module": {"items": [ITEM]}}})
fake = install([expired, ok])
batch = sa.get_sold_orders(COOKIE)
check(len(fake.calls) == 2, "令牌过期后自动重试一次（拼写 EXOIRED 也认）")
check(fake.calls[1]["headers"]["cookie"].find("NEWTOKEN") > 0,
      "重试用响应下发的新令牌（新令牌在失败的响应里）")
check(fake.calls[0]["params"]["sign"] != fake.calls[1]["params"]["sign"], "重新签名了")

# 非令牌类错误不重试
fake = install([FakeResp({"ret": ["FAIL_BIZ_IDLE_USER_UNAUTHORIZED::无权限"]})])
try:
    sa.get_sold_orders(COOKIE)
    check(False, "业务失败应当抛错")
except sa.SellerApiError as e:
    check("UNAUTHORIZED" in str(e), "业务失败原样抛出接口错误")
check(len(fake.calls) == 1, "非令牌错误不重试")


# ---------------------------------------------------------------- [5][6] 不炸主流程 / 熔断
section("5 接口失败不炸主流程；6 连续失败自我熔断")
sa._reset_state()
fake = install([RuntimeError("connection reset")] * 10)
got = sa.fetch_orders_by_ids(COOKIE, "acc1", ["1", "2"])
check(got == {}, "异常时返回空字典，不抛给主循环")
check(len(fake.calls) == 1, "一次失败不在同一轮里死磕（chunk 一次）")

sa._reset_state()
fake = install([FakeResp({"ret": ["FAIL_BIZ_SYSTEM_ERROR::err"]})] * 10)
for _ in range(3):
    sa.fetch_orders_by_ids(COOKIE, "acc1", ["1"])
n_after_three = len(fake.calls)
sa.fetch_orders_by_ids(COOKIE, "acc1", ["1"])
check(n_after_three == 3, "三连失败前每轮都真的发了请求")
check(len(fake.calls) == n_after_three, "熔断后不再发请求（接口改了字段也不会每单撞墙）")

sa._reset_state()
os.environ["SELLER_API_ENABLED"] = "0"
fake = install([FakeResp({"ret": ["SUCCESS::ok"]})])
check(sa.fetch_orders_by_ids(COOKIE, "acc1", ["1"]) == {} and not fake.calls,
      "总开关关掉就完全走原路径")
os.environ.pop("SELLER_API_ENABLED", None)


# ---------------------------------------------------------------- [7] 顺序契约
section("7 顺序契约（process_orders_batch）")
sa._reset_state()
sa._disable_for("")  # 不影响
os.environ["SELLER_API_ENABLED"] = "1"

import utils.order_fetcher_optimized as ofo

BROWSER_ROW = {"order_id": "B", "order_status": "completed", "status_text": "交易成功",
               "source": "browser"}


async def _fake_complete(order_id, **kw):
    return dict(BROWSER_ROW, order_id=order_id)


async def run_merge(direct_rows):
    orig_fetch = sa.fetch_orders_by_ids
    orig_complete = ofo.fetch_order_complete
    sa.fetch_orders_by_ids = lambda *a, **k: {r["order_id"]: r for r in direct_rows}
    ofo.fetch_order_complete = _fake_complete
    try:
        return await ofo.process_orders_batch(["A", "B", "C"], "acc1", COOKIE,
                                             max_concurrent=2, use_pool=False)
    finally:
        sa.fetch_orders_by_ids = orig_fetch
        ofo.fetch_order_complete = orig_complete


direct = [{"order_id": "A", "order_status": "pending_ship", "source": "api"},
          {"order_id": "C", "order_status": "completed", "source": "api"}]
merged = asyncio.run(run_merge(direct))
check([r["order_id"] for r in merged] == ["A", "B", "C"],
      f"返回值与入参逐位对齐（实得 {[r['order_id'] for r in merged]}）")
check(merged[0]["source"] == "api" and merged[2]["source"] == "api",
      "接口覆盖到的订单不再走浏览器")
check(merged[1]["source"] == "browser", "没覆盖到的订单回退浏览器")

merged_all = asyncio.run(run_merge([{"order_id": x, "source": "api"} for x in "ABC"]))
check([r["source"] for r in merged_all] == ["api"] * 3, "全部覆盖时一个浏览器都不起")

no_api = asyncio.run(run_merge([]))
check([r["source"] for r in no_api] == ["browser"] * 3, "接口全空时行为与改造前一致")

src = (ROOT / "utils" / "order_fetcher_optimized.py").read_text(encoding="utf-8")
check("fetch_orders_by_ids" in src and "asyncio.to_thread" in src,
      "同步 HTTP 放在线程里跑，不阻塞事件循环")
check("direct_map" in src and "remaining" in src, "回退逻辑按缺哪些单来，而不是整批放弃")

os.environ.pop("SELLER_API_ENABLED", None)



# ---------------------------------------------------------------- [8] 复盘成交口径
section("8 复盘成交口径（sales_summary）")
sa._reset_state()


def _order(oid, status, total, buy_num=1, refund=False):
    return {
        "commonData": {"orderId": oid, "itemId": 99887766, "orderStatus": status,
                       "createTime": "2026-11-05 10:00:00", "inRefund": refund,
                       "sellerRateStatus": 5},
        "buyerInfoVO": {"buyerId": 1, "userNick": "b", "name": "n", "phone": "",
                        "address": "", "isPrivacy": False},
        "priceVO": {"totalPrice": total, "auctionPrice": total, "buyNum": buy_num},
        "itemVO": {"title": "x"}, "rightVO": {"btnList": []},
    }


p1 = {"ret": ["SUCCESS::ok"], "data": {"module": {"items": [
    _order(1, "等待卖家发货", "100.00"),
    _order(2, "交易成功", "50.50"),
    _order(3, "交易关闭", "999.00"),          # 取消单不计
], "nextPage": True}}}
p2 = {"ret": ["SUCCESS::ok"], "data": {"module": {"items": [
    _order(4, "退款中", "30.00", refund=True),
    _order(5, "交易成功", "20.00"),
], "nextPage": False}}}
fake = install([FakeResp(p1), FakeResp(p2)])
res = sa.sales_summary(COOKIE, "acc1", days=7)
check(res["ok"] is True, "正常拉到数据")
check(res["orders"] == 4, f"取消单不算成交（实得 {res['orders']}）")
check(abs(res["amount"] - 200.50) < 0.01, f"成交额按 totalPrice 累加（实得 {res['amount']}）")
check(res["refund_orders"] == 1, "退款单单独计")
check(res["pending_ship"] == 1, "待发货单独计")
check(res["cancelled"] == 1 and res["pages"] == 2, "取消数留档、翻了两页")
import json as _j2
_sp = _j2.loads(_j2.loads(fake.calls[0]["data"]["data"])["orderSearchParam"] or "{}")
check(bool(_sp.get("createStart")), f"带时间窗（{_sp.get('createStart')}），不是全量历史")

# 页数上限：下一百页也只读到 max_pages
sa._reset_state()
always_next = {"ret": ["SUCCESS::ok"], "data": {"module": {
    "items": [_order(9, "交易成功", "1.00")], "nextPage": True}}}
fake = install([FakeResp(always_next) for _ in range(10)])
res = sa.sales_summary(COOKIE, "acc1", days=7, max_pages=2)
check(len(fake.calls) == 2, f"最多翻 max_pages 页（实得 {len(fake.calls)} 次请求）")
check(res["ok"] is True and res["pages"] == 2, "到上限也算成功（拿到的就是最近的部分）")

# 失败不炸，且关掉开关直接不出门
sa._reset_state()
fake = install([RuntimeError("boom")])
res = sa.sales_summary(COOKIE, "acc1")
check(res["ok"] is False and "boom" in res["reason"], "接口炸了返回 ok=False，不抛")
check(len(fake.calls) == 1, "只试一次（复盘不值得重试三次）")

sa._reset_state()
os.environ["SELLER_API_ENABLED"] = "0"
fake = install()
res = sa.sales_summary(COOKIE, "acc1")
check(res["ok"] is False and "关闭" in res["reason"], "开关关着就不发请求")
check(len(fake.calls) == 0, "一个请求都没发")
os.environ.pop("SELLER_API_ENABLED", None)

sa._reset_state()
sa._disable_for("acc1", 60)
res = sa.sales_summary(COOKIE, "acc1")
check(res["ok"] is False and "冷却" in res["reason"], "熔断中的账号不再撞墙")
sa._reset_state()


# ---------------------------------------------------------------- 汇总
print("\n" + "=" * 60)
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")
