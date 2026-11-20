"""
C1 测试：自动评价 / 求小红花 / 收货致谢

全部用 stub 替换 `_mtop`，一个真实网络请求都不发。重点覆盖：
1. 分类器对平台真实报错的归类（分类错了会把"已评价"当失败无限重试）
2. **先查列表再提交**：拿不到平台 tradeId 就绝不提交（防评错单）
3. 幂等：同单同模板只写一次
4. 风控中断整轮
5. 限流撤回占位 / 超时不重试 的相反语义
6. 演练模式零写操作

运行：
    cd backend && python3 -I tests/test_care_message.py
"""

import os
import sys
import tempfile
from pathlib import Path

_TMP = Path(tempfile.mkdtemp(prefix="care_test_"))
os.environ["CARE_DB"] = str(_TMP / "care.db")
os.environ["CARE_CONFIG"] = str(_TMP / "care.json")
# 必须一起隔离：漏了这条，按账号的覆盖会写进真实的 data/care_accounts.json，
# 把用户的实际配置污染成测试账号（accA/accB/...）。这个坑本轮真踩过一次。
os.environ["CARE_ACCOUNT_CONFIG"] = str(_TMP / "care_accounts.json")
os.environ["RATE_LIMIT_STATE"] = str(_TMP / "rate.json")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 隔离外部依赖：db_manager 与 rate_limiter 都用替身
sys.modules["db_manager"] = type(sys)("db_manager")
sys.modules["db_manager"].db_manager = type("X", (), {})()

_RL = type(sys)("rate_limiter")
_RL.state = {"allow": True, "retry": 60}


def _acquire(scope="", key="", limit=None, window=None):
    return (_RL.state["allow"], _RL.state["retry"])


_RL.acquire = _acquire
sys.modules["rate_limiter"] = _RL

import care_message as cm  # noqa: E402

PASS, FAIL = 0, 0


def check(cond, label, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {label}")
    else:
        FAIL += 1
        print(f"  ❌ {label}  {extra}")


def reset():
    for f in (cm.DB_FILE, cm.CONFIG_FILE, cm.ACCOUNT_CONFIG_FILE):
        try:
            Path(f).unlink()
        except FileNotFoundError:
            pass


def cfg(**kw):
    cm.save_config(kw)


# ---------------------------------------------------------------- 1. 分类器
print("\n【1】平台返回归类（错了会导致无限重试或把成功当失败）")

CASES = [
    ("SUCCESS::调用成功", "ok"),
    ("FAIL_SYS_TOKEN_EXOIRED::令牌过期", "auth"),
    ("FAIL_SYS_SESSION_EXPIRED::session 过期", "auth"),
    ("FAIL_SYS_ILLEGAL_ACCESS::非法请求", "auth"),
    ("RGV587_ERROR::SM::哎哟喂,被挤爆啦", "risk"),
    ("FAIL_SYS_USER_VALIDATE::需要验证", "risk"),
    ("该订单已评价，请勿重复提交", "already"),
    ("已经评价过了", "already"),
    ("未完成的交易不允许评价", "waiting"),
    ("交易未完成，不能评价", "waiting"),
    ("超过30天的订单不允许评价", "skipped"),
    ("该商品不支持评价", "skipped"),
    ("请求 timeout", "unknown"),
    ("调用超时", "unknown"),
    ("FAIL_BIZ_SOMETHING_ELSE", "error"),
]
for text, expect in CASES:
    got = cm.classify_ret(text)
    check(got == expect, f"{text[:34]!r} → {expect}", f"实际 {got}")

check(cm.classify_ret("") == "error", "空返回 → error")


# ---------------------------------------------------------------- 2. tradeId 提取
print("\n【2】从列表行里挖 tradeId（挖不到就不能提交）")

check(cm.extract_trade_id({"bizOrderId": "111"}) == "111", "bizOrderId 优先")
check(cm.extract_trade_id({"tradeId": "222"}) == "222", "tradeId")
check(cm.extract_trade_id({"orderId": "333"}) == "333", "orderId")
check(cm.extract_trade_id({"id": "444"}) == "444", "id 兜底")
check(cm.extract_trade_id({"bizOrderId": "111", "orderId": "333"}) == "111",
      "多字段时按优先级取，不随机")
check(cm.extract_trade_id({"order": {"tradeId": "555"}}) == "555", "嵌套一层")
check(cm.extract_trade_id({"orderInfo": {"bizOrder": {"id": "666"}}}) == "666", "嵌套两层")
check(cm.extract_trade_id({"title": "没有 id"}) == "", "真没有就返回空（不瞎猜）")
check(cm.extract_trade_id({"bizOrderId": "null"}) == "", "'null' 字符串不算 id")
check(cm.extract_trade_id({}) == "", "空 dict → 空")


# ---------------------------------------------------------------- 3. 列表解析
print("\n【3】待评价列表解析（结构随页面版本变化，套三层也要挖到）")

RAW_FLAT = {"items": [{"bizOrderId": "A1", "title": "t1"}, {"bizOrderId": "A2"}],
            "totalCount": 2}
container = cm._unwrap_page(RAW_FLAT)
check(len(cm._find_first(container, ("items", "itemList", "orderList",
                                     "records", "list")) or []) == 2, "扁平结构")

RAW_NESTED = {"module": {"data": {"records": [{"tradeId": "B1"}], "total": 1}}}
c2 = cm._unwrap_page(RAW_NESTED)
rows = cm._find_first(c2, ("items", "itemList", "orderList", "records", "list")) or []
check(len(rows) == 1 and cm.extract_trade_id(rows[0]) == "B1", "三层嵌套也能挖到")

check(cm._unwrap_page({}) == {}, "空 data 不崩")


# ---------------------------------------------------------------- 4. 先查列表再提交
print("\n【4】必须先读到平台列表才提交（防评错单）")

CALLS = []


def fake_mtop_ok_list(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    CALLS.append((api, version, data))
    if api == cm.RATE_LIST_API:
        return ({"ret": ["SUCCESS::调用成功"],
                 "data": {"items": [{"bizOrderId": "T-REAL", "orderId": "O-1",
                                     "buyerRateStatus": "4", "rateResult": "1"}],
                          "totalCount": 1}}, None)
    if api == cm.RATE_CREATE_API:
        return {"ret": ["SUCCESS::调用成功"], "data": {"module": {"success": True}}}, None
    if api == cm.RED_FLOWER_API:
        return {"ret": ["SUCCESS::调用成功"], "data": {}}, None
    return {"ret": ["FAIL_UNKNOWN"]}, None


cm._mtop = fake_mtop_ok_list

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, ask_flower=False,
    thanks_message=False, max_per_run=5, delay_seconds_min=0, delay_seconds_max=0)

CALLS.clear()
r = cm.run_once("C1", "cookie=tok_abc; unb=1", order_ids=["O-1"])
check(r["ok"] is True, "run_once 成功", r)
check(len(r["actions"]) == 1, "处理了 1 单", r["actions"])
a0 = r["actions"][0]
check(a0["status"] == "sent", "评价提交成功 → sent", a0)

# 关键：提交用的是列表里的 T-REAL，不是我们自己库里的 O-1
create_calls = [c for c in CALLS if c[0] == cm.RATE_CREATE_API]
check(len(create_calls) == 1, "只调了一次 rate.create")
check(create_calls[0][1] == "4.0", "rate.create 用的是 v4.0（v1.0 不存在）", create_calls[0][1])
check(create_calls[0][2]["tradeId"] == "T-REAL",
      "提交的是列表里的 tradeId，不是本地 order_id", create_calls[0][2])
check(create_calls[0][2]["rate"] == 1, "rate=1 是好评")
check(create_calls[0][2]["createOrAppend"] == 0, "createOrAppend=0")


print("\n【5】订单不在待评价列表里 → 不提交，标 waiting")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0)

CALLS.clear()


def fake_empty_list(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    CALLS.append((api, version, data))
    if api == cm.RATE_LIST_API:
        return {"ret": ["SUCCESS::调用成功"], "data": {"items": [], "totalCount": 0}}, None
    if api == cm.RATE_CREATE_API:
        return {"ret": ["SUCCESS::调用成功"]}, None
    return {"ret": ["FAIL"]}, None


cm._mtop = fake_empty_list
r = cm.run_once("C1", "cookie=tok_abc", order_ids=["O-NOT-THERE"])
a = r["actions"][0]
check(a["status"] == "waiting", "不在列表 → waiting", a)
check(not [c for c in CALLS if c[0] == cm.RATE_CREATE_API],
      "**一次 rate.create 都没发**（这是防评错单的关键）")

# 再跑一次：waiting 已占键，不会重复去查
CALLS.clear()
r2 = cm.run_once("C1", "cookie=tok_abc", order_ids=["O-NOT-THERE"])
check(r2["actions"][0]["status"] == "duplicate", "waiting 也占幂等键，不重复查", r2["actions"][0])


# ---------------------------------------------------------------- 6. 风控中断
print("\n【6】风控：中断整轮，且不占死幂等键")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0, stop_on_risk=True)

CALLS.clear()


def fake_risk(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    CALLS.append((api, version, data))
    if api == cm.RATE_LIST_API:
        return {"ret": ["SUCCESS::调用成功"],
                "data": {"items": [{"bizOrderId": "T1", "buyerRateStatus": "4", "rateResult": "1"},
                                   {"bizOrderId": "T2", "buyerRateStatus": "4", "rateResult": "1"}],
                         "totalCount": 2}}, None
    if api == cm.RATE_CREATE_API:
        return {"ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦"]}, None
    return {"ret": ["FAIL"]}, None


cm._mtop = fake_risk
r = cm.run_once("C1", "cookie=tok_abc", order_ids=["T1", "T2"])
check(r["stopped"] == "risk", "stopped=risk", r)
check(len(r["actions"]) == 1, "只试了第 1 单就停了（没继续写第 2 单）", r["actions"])
check(r["actions"][0]["status"] == "risk_stop", "第 1 单标 risk_stop")

# 风控没写成功，占位要撤掉，以后还能重来
conn = cm._conn()
left = conn.execute("SELECT COUNT(*) n FROM care_actions WHERE idem_key=?",
                    (cm.make_key("rate", "T1", "thanks"),)).fetchone()
conn.close()
check(left["n"] == 0, "风控后占位被撤销（以后能重来）")


# ---------------------------------------------------------------- 7. 网络异常
print("\n【7】网络异常 / 鉴权过期")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0)


def fake_net_err(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    raise RuntimeError("连接被重置")


cm._mtop = fake_net_err
# 不指定 order_ids，才会真的去预读"全部待评价"
r = cm.run_once("C1", "cookie=tok", order_ids=None)
check(r["ok"] is False, "列表都读不到 → 整体 ok=False", r)
check(r["actions"] == [], "没有产生写动作", r["actions"])

# 指定单号时，单笔读列表失败也要"可重试"，不能落死 failed
r2 = cm.run_once("C1", "cookie=tok", order_ids=["O1"])
check(r2["actions"][0]["status"] == "failed" and r2["actions"][0].get("retryable"),
      "单笔读列表网络失败 → failed 但标记可重试", r2["actions"][0])
conn = cm._conn()
n = conn.execute("SELECT COUNT(*) n FROM care_actions WHERE idem_key=?",
                 (cm.make_key("rate", "O1", "thanks"),)).fetchone()["n"]
conn.close()
check(n == 0, "可重试的失败不占死幂等键")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0)


def fake_auth_list(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    if api == cm.RATE_LIST_API:
        return {"ret": ["FAIL_SYS_TOKEN_EXOIRED::令牌过期"]}, None
    return {"ret": ["SUCCESS"]}, None


cm._mtop = fake_auth_list
r = cm.run_once("C1", "cookie=tok", order_ids=["O1"])
check(r["stopped"] == "auth", "列表鉴权失败 → stopped=auth（调用方该去刷新 cookie）", r)


# ---------------------------------------------------------------- 8. 演练
print("\n【8】演练模式：只读列表，一个写请求都不发")

reset()
cfg(enabled=True, dry_run=True, auto_rate=True, ask_flower=True,
    max_per_run=5, delay_seconds_min=0, delay_seconds_max=0)

CALLS.clear()
cm._mtop = fake_mtop_ok_list
r = cm.run_once("C1", "cookie=tok_abc", order_ids=["O-1"])
statuses = [a["status"] for a in r["actions"]]
check(all(s == "dry_run" for s in statuses), "全部 dry_run", statuses)
writes = [c for c in CALLS if c[0] in (cm.RATE_CREATE_API, cm.RED_FLOWER_API)]
check(writes == [], "演练没有调用任何写接口", writes)
check(any(c[0] == cm.RATE_LIST_API for c in CALLS), "但读了列表（演练也要验证链路）")


# ---------------------------------------------------------------- 9. 幂等
print("\n【9】幂等：同单同模板只写一次")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0)
cm._mtop = fake_mtop_ok_list

CALLS.clear()
cm.run_once("C1", "cookie=tok", order_ids=["O-1"])
n_first = len([c for c in CALLS if c[0] == cm.RATE_CREATE_API])
CALLS.clear()
r2 = cm.run_once("C1", "cookie=tok", order_ids=["O-1"])
n_second = len([c for c in CALLS if c[0] == cm.RATE_CREATE_API])
check(n_first == 1 and n_second == 0, "第二次没再写", (n_first, n_second))
check(r2["actions"][0]["status"] == "duplicate", "标 duplicate")

# 换模板就是新动作（允许补一次不同的评价？不，rate.create 只能评一次，
# 但幂等键不同会再发一次——所以这条要确认"平台会拒绝"，我们不该主动换模板重发）
check(cm.make_key("rate", "O1", "thanks") != cm.make_key("rate", "O1", "thanks_digital"),
      "不同模板是不同幂等键（注意：平台实际只允许评一次）")


# ---------------------------------------------------------------- 10. 限流 vs 超时
print("\n【10】限流撤回占位 / 超时不重试 —— 语义相反，不能写反")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0)
cm._mtop = fake_mtop_ok_list

_RL.state["allow"] = False
CALLS.clear()
r = cm.run_once("C1", "cookie=tok", order_ids=["O-1"])
check(r["actions"][0]["status"] == "rate_limited", "限流 → rate_limited", r["actions"])
check(not [c for c in CALLS if c[0] == cm.RATE_CREATE_API], "限流时没发写请求")
conn = cm._conn()
n = conn.execute("SELECT COUNT(*) n FROM care_actions WHERE idem_key=?",
                 (cm.make_key("rate", "O-1", "thanks"),)).fetchone()["n"]
conn.close()
check(n == 0, "限流后占位被撤销（能重来）")

_RL.state["allow"] = True
reset()
cfg(enabled=True, dry_run=False, auto_rate=True, max_per_run=5,
    delay_seconds_min=0, delay_seconds_max=0)


def fake_timeout(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    if api == cm.RATE_LIST_API:
        return {"ret": ["SUCCESS"], "data": {"items": [
            {"bizOrderId": "T1", "buyerRateStatus": "4", "rateResult": "1"}]}}, None
    if api == cm.RATE_CREATE_API:
        return {"ret": ["FAIL_SYS_TIMEOUT::请求 timeout"]}, None
    return {"ret": ["FAIL"]}, None


cm._mtop = fake_timeout
r = cm.run_once("C1", "cookie=tok", order_ids=["T1"])
check(r["actions"][0]["status"] == "unknown", "超时 → unknown", r["actions"])
# 超时不重试：再跑一次应该被幂等拦住
r2 = cm.run_once("C1", "cookie=tok", order_ids=["T1"])
check(r2["actions"][0]["status"] == "duplicate",
      "超时保留占位，不自动重试（可能已受理，重发=重复评价）", r2["actions"])


# ---------------------------------------------------------------- 11. 总开关
print("\n【11】总开关关着时什么都不做")

reset()
cfg(enabled=False)
r = cm.run_once("C1", "cookie=tok", order_ids=["O1"])
check(r["ok"] is False and "未启用" in (r.get("error") or ""), "未启用直接返回", r)

reset()
cfg(enabled=True, auto_rate=False, ask_flower=False, thanks_message=False)
r = cm.run_once("C1", "cookie=tok", order_ids=["O1"])
check(r["ok"] is True and r["actions"] == [], "三个动作都没开 → 什么都不做", r)


# ---------------------------------------------------------------- 12. 文案
print("\n【12】文案：自定义优先，模板不含违规词")

c = {"rate_template": "thanks", "custom_feedback": "  "}
check(cm.render_feedback(c) == cm.DEFAULT_TEMPLATES["thanks"], "自定义是空白 → 用模板")
c2 = {"rate_template": "thanks", "custom_feedback": "  好评！  "}
check(cm.render_feedback(c2) == "好评！", "自定义去掉首尾空白后优先")
for k, v in cm.DEFAULT_TEMPLATES.items():
    check("返现" not in v and "红包" not in v, f"模板 {k} 不含返现/红包")
check("好评" not in cm.render_flower({}), "小红花文案不提好评（那是两回事）")


# ---------------------------------------------------------------- 13. 只读性
print("\n【13】没开对应开关时，不碰那个接口")

reset()
cfg(enabled=True, dry_run=False, auto_rate=True, ask_flower=False,
    thanks_message=False, max_per_run=5, delay_seconds_min=0, delay_seconds_max=0)
CALLS.clear()
cm._mtop = fake_mtop_ok_list
cm.run_once("C1", "cookie=tok", order_ids=["O-1"])
check(not [c for c in CALLS if c[0] == cm.RED_FLOWER_API], "ask_flower 关着 → 不调小红花")


# ---------------------------------------------------------------- 14. 超时回读核验
print("\n【14】超时不判死：回读列表核验（不重发）")

# 先造三条 unknown：两条是简单单，一条是"第 2 页才会出现"的单
reset()
cfg(enabled=True, dry_run=False, auto_rate=True, ask_flower=False,
    thanks_message=False, max_per_run=5, delay_seconds_min=0, delay_seconds_max=0)


def seed_unknown(order_id, trade_id=""):
    cm.claim("rate", order_id, "C1")
    cm.finish(cm.make_key("rate", order_id), "unknown", "请求超时")


seed_unknown("O-GONE", "T-GONE")     # 已从列表消失 → 应改判 sent
seed_unknown("O-STAY", "T-STAY")     # 还在列表 → 应改判 failed
seed_unknown("O-P2", "T-P2")         # 只在第 2 页 → 必须读到第 2 页才判

list_rows = [{"bizOrderId": "T-STAY", "orderId": "O-STAY"}] + \
            [{"bizOrderId": f"X{i}"} for i in range(49)]      # 第1页刚好 50 条，不会提前停
PAGE2 = [{"bizOrderId": "T-P2", "orderId": "O-P2"}]


def fake_list_paged(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    CALLS.append((api, version, data))
    if api == cm.RATE_LIST_API:
        page = int((data or {}).get("pageNumber") or 1)
        if page == 1:
            return ({"ret": ["SUCCESS::调用成功"],
                     "data": {"items": list_rows, "totalCount": 51}}, None)
        return ({"ret": ["SUCCESS::调用成功"],
                 "data": {"items": PAGE2, "totalCount": 51}}, None)
    return {"ret": ["FAIL_UNKNOWN"]}, None


cm._mtop = fake_list_paged
CALLS.clear()
rc = cm.recheck_unknown("cookie=tok_abc")

check(rc["ok"] is True, "核验成功", rc)
check(rc["checked"] == 3, "核验了 3 单", rc)
check(rc["promoted"] == 1, "消失的 1 单改判 sent", rc)
check(rc["reverted"] == 2, "仍在列表的 2 单改判 failed（含第 2 页那单）", rc)

rows = {r["order_id"]: r for r in cm.recent(50)}
check(rows["O-GONE"]["status"] == "sent", "O-GONE → sent（上次其实成功了）",
      rows.get("O-GONE", {}).get("status"))
check(rows["O-STAY"]["status"] == "failed", "O-STAY → failed（上次没受理，可重来）",
      rows.get("O-STAY", {}).get("status"))
check(rows["O-P2"]["status"] == "failed",
      "O-P2 → failed：该单在第 2 页仍待评价，只有读全分页才不会误判成已提交",
      rows.get("O-P2", {}).get("status"))

check(not [c for c in CALLS if c[0] == cm.RATE_CREATE_API],
      "核验过程一次 rate.create 都没调（只读不重发）")

# 列表读失败 → 整轮放弃，次数不涨，状态保持 unknown
reset()
seed_unknown("O-KEEP", "T-KEEP")


def fake_list_fail(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    CALLS.append((api, version, data))
    return {"ret": ["RGV587_ERROR::SM::哎哟喂,被挤爆啦"]}, None


cm._mtop = fake_list_fail
rc2 = cm.recheck_unknown("cookie=tok_abc")
check(rc2["ok"] is False and rc2["checked"] == 0, "列表被风控 → 不下任何结论", rc2)
rows = {r["order_id"]: r for r in cm.recent(50)}
check(rows["O-KEEP"]["status"] == "unknown", "读不到列表时保持 unknown，等下次再核")

# 核验次数上限：拿不到 id 的单最多问 2 次
reset()
seed_unknown("O-NOID", "")
PAGE2.clear()


def fake_list_empty(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    return ({"ret": ["SUCCESS::调用成功"],
             "data": {"items": [], "totalCount": 0}}, None)


cm._mtop = fake_list_empty
a = cm.recheck_unknown("cookie=tok_abc")
check(a["promoted"] == 1, "列表为空且读全 → 判为已提交（消失即成功）", a)

reset()
cm.claim("rate", "O-NOID2", "C1")
cm.finish(cm.make_key("rate", "O-NOID2"), "unknown", "超时")
# 列表没读全（满 50 条但 total 说更多）→ 不能下结论，只能涨次数


def fake_list_incomplete(cookie_str, api, data, version, spm="", app_key="", timeout=20,
              cookie_id=""):
    page = int((data or {}).get("pageNumber") or 1)
    rows = [{"bizOrderId": f"Y{page}-{i}"} for i in range(50)]
    return ({"ret": ["SUCCESS::调用成功"],
             "data": {"items": rows, "totalCount": 9999}}, None)


cm._mtop = fake_list_incomplete
for _ in range(3):
    cm.recheck_unknown("cookie=tok_abc")
rows = {r["order_id"]: r for r in cm.recent(50)}
check(rows["O-NOID2"]["status"] == "unknown", "列表读不全 → 保持 unknown")
check(int(rows["O-NOID2"].get("rechecks") or 0) == cm.MAX_TIMEOUT_RECHECKS,
      f"核验次数封顶在 {cm.MAX_TIMEOUT_RECHECKS}，不会无限问",
      rows["O-NOID2"].get("rechecks"))


# ---------------------------------------------------------------- 15. Cookie 续期回写
print("\n【15】响应头下发的新 Cookie 必须回写（不回写 = 签名慢慢全失效）")


class _FakeResp:
    def __init__(self, set_cookie):
        self.headers = {"set-cookie": set_cookie} if set_cookie else {}


MERGED = {"called": 0, "cookie": "", "persisted": ""}

_ps = type(sys)("polish_service")


def _fake_merge(resp, cookie_str):
    MERGED["called"] += 1
    sc = resp.headers.get("set-cookie", "")
    name, _, val = sc.split(";")[0].partition("=")
    parts = [p for p in cookie_str.split(";") if p.strip()]
    parts = [p for p in parts if p.split("=")[0].strip() != name.strip()]
    parts.append(f"{name.strip()}={val.strip()}")
    MERGED["cookie"] = "; ".join(parts)
    return MERGED["cookie"]


def _fake_persist(cookie_id, new_cookie):
    MERGED["persisted"] = f"{cookie_id}|{new_cookie}"


_ps._merge_response_cookies = _fake_merge
_ps._persist_cookie = _fake_persist
sys.modules["polish_service"] = _ps

MERGED.update({"called": 0, "cookie": "", "persisted": ""})
got = cm._merge_and_persist("C9", "_m_h5_tk=old_1; unb=1",
                            _FakeResp("_m_h5_tk=new_2; Path=/; HttpOnly"))
check(MERGED["called"] == 1, "有 set-cookie 时调用了合并")
check("new_2" in MERGED["cookie"], "新 token 合并进了 Cookie 串")
check("old_1" not in MERGED["cookie"], "旧 token 被替换掉（不是并存）")
check(MERGED["persisted"].startswith("C9|"), "回写时带上账号 ID", MERGED["persisted"])

MERGED.update({"called": 0, "cookie": "", "persisted": ""})
same = cm._merge_and_persist("C9", "_m_h5_tk=old_1", _FakeResp(""))
check(MERGED["called"] == 0, "没有 set-cookie → 不做任何事")
check(same == "_m_h5_tk=old_1", "Cookie 原样返回")

# 没传 cookie_id 时不应回写（核对接口、纯查询场景）
MERGED.update({"called": 0, "cookie": "", "persisted": ""})
check(cm._merge_and_persist("", "c=1", _FakeResp("a=b")) == "c=1",
      "无 cookie_id → 不合并（避免把公共查询写坏某个账号）")
check(MERGED["persisted"] == "", "无 cookie_id → 不回写")


# ---------------------------------------------------------------- 16. 按账号隔离
print("\n【16】评价/求花/致谢必须按账号隔离（一开全开的代价由买家承担）")

reset()

# 迁移语义：账号没有覆盖记录时**继承全局**。
# 这一条是升级安全的底线：已经在跑的账号不能因为升级突然停下。
cfg(enabled=True, dry_run=True, auto_rate=True, ask_flower=True, thanks_message=True)
a = cm.get_account_config("accA")
check(a["auto_rate"] is True and a["ask_flower"] is True,
      "没有覆盖记录 → 继承全局 True", f"{a['auto_rate']},{a['ask_flower']}")

# 只关 A 的求花，A 的评价必须还在（部分更新不能连带关掉别的）
cm.save_account_config("accA", {"ask_flower": False})
a = cm.get_account_config("accA")
b = cm.get_account_config("accB")
check(a["ask_flower"] is False, "A 的求花已关")
check(a["auto_rate"] is True, "只关求花不能把评价也关了", a["auto_rate"])
check(b["ask_flower"] is True, "B 没被动过，仍是全局的 True", b["ask_flower"])

# 反过来：只给 B 开，A 不受影响
cm.save_account_config("accB", {"auto_rate": True})
check(cm.get_account_config("accB")["auto_rate"] is True, "B 的评价开着")
check(cm.get_account_config("accA")["auto_rate"] is True, "A 仍是 True（本来就继承全局）")

# 关键：显式 False 与"没有记录"必须能区分
cfg(auto_rate=True)
cm.save_account_config("accC", {"auto_rate": False})
check(cm.get_account_config("accC")["auto_rate"] is False,
      "C 显式关掉 → 全局再怎么改都关着",
      "把 False 和'没记录'混同，会让'关掉一个账号'在全局变动时被重新打开")

# 全局改动不应该穿透已经显式设置的账号
cfg(auto_rate=True, ask_flower=True)
cfg(auto_rate=True)
check(cm.get_account_config("accA")["ask_flower"] is False,
      "全局改回 True 后，A 的显式 False 仍然生效")

# 三个动作互相独立
cm.save_account_config("accD", {"auto_rate": True, "ask_flower": False, "thanks_message": False})
d = cm.get_account_config("accD")
check((d["auto_rate"], d["ask_flower"], d["thanks_message"]) == (True, False, False),
      "三个开关互相独立", f"{d['auto_rate']},{d['ask_flower']},{d['thanks_message']}")

# 未知账号 = 继承全局（不是"全部关闭"）
unknown = cm.get_account_config("never-seen")
check(unknown["auto_rate"] is True, "未知账号继承全局，不是默认关", unknown["auto_rate"])

# 空 cookie_id 走全局
check(cm.get_config()["auto_rate"] is True, "get_config() 不带 id 走全局")

# run_once 必须按账号取配置——用结构断言而不是真跑：
# 真跑会走网络（fetch_pending），测试不能依赖外网，也不能靠超时来证明"没被拦住"。
import inspect  # noqa: E402
_run_src = inspect.getsource(cm.run_once)
check("get_account_config(cookie_id)" in _run_src,
      "run_once 用的是按账号配置，不是全局配置",
      "用全局配置就是'一开全开'，多账号会串")
check("get_config()" not in _run_src,
      "run_once 里不再直接调 get_config()（那会跳过账号覆盖）")

# 结构性验证：同一次调用里两个账号拿到不同结果
reset()
cfg(enabled=True, dry_run=True, auto_rate=True, ask_flower=False, thanks_message=False)
cm.save_account_config("accOFF", {"auto_rate": False})
c_off = cm.get_account_config("accOFF")
c_on = cm.get_account_config("accON")
check((c_off["auto_rate"], c_off["ask_flower"], c_off["thanks_message"]) == (False, False, False),
      "关闭的账号：三个动作全为 False（run_once 会判'都没开'并退出）", str(c_off))
check(c_on["auto_rate"] is True,
      "未关闭的账号：auto_rate 仍为 True（run_once 会继续跑）", str(c_on))

# 覆盖文件损坏不能崩（半截写入 / 手动改坏）
cm.ACCOUNT_CONFIG_FILE.write_text("{ 这不是 JSON", encoding="utf-8")
cm._ACCOUNT_CACHE["mtime"] = None
try:
    bad = cm.get_account_config("accA")
    ok_bad = bad.get("auto_rate") is not None
except Exception as e:
    ok_bad = False
    bad = e
check(ok_bad, "覆盖文件损坏 → 退回全局，不崩", str(bad)[:120])



print("\n" + "=" * 62)
print(f"通过 {PASS} 项，失败 {FAIL} 项")
sys.exit(0 if FAIL == 0 else 1)
