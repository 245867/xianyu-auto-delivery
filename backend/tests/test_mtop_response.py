"""
MTOP 响应归因测试
=================
锁死"把临时抖动说成接口坏了"这类错误归因。

真实场景：网关 502 返回一整个 HTML 错误页 → 原实现 `resp.json()` 抛异常 →
上层报"接口返回异常" → 运维去查接口版本/参数，实际只是网络抖了一下。

三类必须分开：transport（可重试）/ auth（换 Cookie）/ risk（必须停）。

跑法（backend 目录）：python3 tests/test_mtop_response.py
"""

from __future__ import annotations

import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))

import mtop_response as M  # noqa: E402

PASSES = 0
FAILS: list[str] = []


def check(name: str, cond: bool, detail="") -> None:
    global PASSES
    if cond:
        PASSES += 1
        print(f"  ✅ {name}")
    else:
        FAILS.append(name)
        print(f"  ❌ {name}  {detail}")


class Resp:
    def __init__(self, status=200, json_data=None, text=""):
        self.status_code = status
        self._json = json_data
        self.text = text

    def json(self):
        if self._json is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._json


# ---------------------------------------------------------------- 1. 传输层
print("\n【1】网关/网络失败 → transport（可重试，不代表接口坏了）")

for code in (500, 502, 503, 504, 429, 408):
    k, raw, txt = M.parse_mtop_response(Resp(status=code, text="<html>502 Bad Gateway</html>"))
    check(f"HTTP {code} → transport", k == "transport", f"实际 {k}")

k, _, txt = M.parse_mtop_response(
    Resp(status=502, text="<html><body>502 Bad Gateway</body></html>"))
check(k == "transport", "502 HTML 错误页 → transport（不是 biz）", k)
check("502" in txt, "错误信息里带上状态码", txt)

k, _, txt = M.parse_mtop_response(Resp(status=200, text="not json at all"))
check(k == "transport", "200 但响应不是 JSON → transport", k)

k, _, txt = M.parse_mtop_response(Resp(status=200, json_data=[1, 2, 3]))
check(k == "transport", "JSON 但不是对象 → transport", k)


# ---------------------------------------------------------------- 2. 鉴权
print("\n【2】鉴权失效 → auth（换 Cookie 可重试）")

for code in (401, 403):
    k, _, _ = M.parse_mtop_response(Resp(status=code))
    check(f"HTTP {code} → auth", k == "auth", k)

AUTH_RETS = [
    "FAIL_SYS_TOKEN_EXOIRED::令牌过期",
    "FAIL_SYS_TOKEN_EXPIRED::令牌过期",
    "FAIL_SYS_SESSION_EXPIRED::session 过期",
    "FAIL_SYS_ILLEGAL_ACCESS::非法请求",
    "FAIL_SYS_TOKEN_EMPTY::令牌为空",
]
for t in AUTH_RETS:
    k, raw, txt = M.parse_mtop_response(Resp(json_data={"ret": [t]}))
    check(f"{t[:32]!r} → auth", k == "auth", k)


# ---------------------------------------------------------------- 3. 风控
print("\n【3】风控 → risk（必须停，不能重试）")

RISK_RETS = [
    "RGV587_ERROR::SM::哎哟喂,被挤爆啦,请稍后重试",
    "FAIL_SYS_USER_VALIDATE::需要验证",
    "FAIL_SYS_PUNISH::账号被处罚",
]
for t in RISK_RETS:
    k, raw, txt = M.parse_mtop_response(Resp(json_data={"ret": [t]}))
    check(f"{t[:32]!r} → risk", k == "risk", k)


# ---------------------------------------------------------------- 4. 正常
print("\n【4】能解析就交给业务判断（ok）")

k, raw, txt = M.parse_mtop_response(
    Resp(json_data={"ret": ["SUCCESS::调用成功"], "data": {"x": 1}}))
check(k == "ok", "SUCCESS → ok", k)
check(M.is_success(txt), "is_success 认 SUCCESS")
check(raw.get("data", {}).get("x") == 1, "原始 JSON 原样返回")

k, _, txt = M.parse_mtop_response(Resp(json_data={"ret": ["FAIL_BIZ_XXX::业务失败"]}))
check(k == "ok", "业务失败也是 ok（不是传输问题）", k)
check(not M.is_success(txt), "但 is_success 为 False")

# 关键：业务失败与传输失败的区分
check(M.parse_mtop_response(Resp(json_data={"ret": ["FAIL_BIZ"]}))[0] != "transport",
      "业务失败绝不能被归成 transport（否则会无限重试）")


# ---------------------------------------------------------------- 5. 边界
print("\n【5】边界：空返回、缺字段、风控优先于鉴权")

k, _, txt = M.parse_mtop_response(Resp(json_data={}))
check(k == "ok", "空 JSON 对象 → ok（ret 为空，业务层判失败）", k)
check(txt == "", "ret_text 为空串")

k, _, txt = M.parse_mtop_response(Resp(json_data={"ret": "SUCCESS::单个字符串"}))
check(k == "ok" and txt == "SUCCESS::单个字符串", "ret 是字符串而不是数组也能处理", txt)

# 同时含风控与鉴权字样：风控优先（更严重的先判）
both = "RGV587_ERROR::哎哟喂 FAIL_SYS_TOKEN_EXOIRED"
k, _, _ = M.parse_mtop_response(Resp(json_data={"ret": [both]}))
check(k == "risk", "风控 + 鉴权同时出现 → risk 优先", k)

# 大小写不敏感
k, _, _ = M.parse_mtop_response(Resp(json_data={"ret": ["fail_sys_token_exoired"]}))
check(k == "auth", "小写 ret 也能识别", k)


# ---------------------------------------------------------------- 6. explain
print("\n【6】explain 给人看清原因")

check("网络" in M.explain("transport", "HTTP 502"),
      "transport 说明可重试")
check("Cookie" in M.explain("auth", "token expired"),
      "auth 说明要刷 Cookie")
check("风控" in M.explain("risk", "RGV587"),
      "risk 说明必须停")
check(M.explain("ok", "SUCCESS::调用成功") == "成功", "ok + SUCCESS → 成功")
check("业务" in M.explain("ok", "FAIL_BIZ"), "ok + 失败 → 业务失败")


# ---------------------------------------------------------------- 7. 真·集成
print("\n【7】接进 care_message 后：502 不再变成「接口异常」")

import care_message as cm  # noqa: E402

cm._mtop.__globals__  # 存在即可


class BadResp:
    status_code = 502
    text = "<html>502 Bad Gateway</html>"

    def json(self):
        raise ValueError("no json")


# 直接测 care 的 _mtop 内部逻辑无法脱离 requests，这里测它用的解析器
k, _, txt = M.parse_mtop_response(BadResp())
check(k == "transport", "care 链路会拿到 transport（进而抛可重试异常）", k)
check(M.explain(k, txt).startswith("网络"), "归因文案指向网络层")


print("\n【8】处置分级：错误码决定下一次做什么（拆解 9 / goofish-cli）")

# 分类只是手段，**分类决定重试**才是目的。四档：
# ok 已成功 / stop 停（重试只会重复犯错）/ refresh 先换登录态 / retry 可退避重试
POLICIES = [
    ("SUCCESS::调用成功", "ok", "成功就是成功，别往下走"),
    ("RGV587_ERROR::哎哟喂,被挤爆啦", "stop", "风控 → 停整轮"),
    ("FAIL_SYS_USER_VALIDATE::需要验证", "stop", "要验证 → 停，别硬闯"),
    ("FAIL_SYS_PUNISH::账号被处罚", "stop", "被处罚 → 停"),
    ("FAIL_SYS_ILLEGAL_ACCESS::非法访问", "stop",
     "非法访问是风控层拒绝，换 Cookie 也救不了（不是 refresh）"),
    ("ILLEGAL_REQUEST::非法请求", "stop", "签名/参数错 → 停"),
    ("FAIL_SYS_SIGN_ERROR::签名错误", "stop", "签名错 → 停（我们配错了，重试 5 次还是错）"),
    ("ORDER_STATUS_ERROR::订单状态不正确", "stop",
     "订单已发货还反复点确认 = 持续骚扰平台状态机"),
    ("FAIL_BIZ::订单已经发货", "stop", "平台说已发货 → 没有可做的事"),
    ("FAIL_BIZ_ITEM_NOT_FOUND::商品不存在", "stop", "目标没了，重试不会让它回来"),
    ("FAIL_SYS_TOKEN_EXOIRED::令牌过期", "refresh", "令牌层 → 刷新后可重试"),
    ("FAIL_SYS_SESSION_EXPIRED::session 过期", "refresh", "session 层 → 刷新后可重试"),
    ("FAIL_BIZ_XXX::未知业务错误", "retry", "认不出来的业务错 → 可退避重试"),
    ("", "retry", "空文本 → 当可重试（宁可多试一次，别把抖动判死）"),
]
for text, want, why in POLICIES:
    got = M.retry_policy(text)
    check(f"{text[:34] or '(空)'!r} → {want}", got == want, f"实际 {got}；{why}")

# 风控优先于一切：同一段文本里既有风控又有状态错，必须先停
check(M.retry_policy("RGV587_ERROR 订单状态不正确") == "stop", "风控与状态错同时出现 → stop")
# 但令牌失效要先判 refresh，别把"我没登录"当成"平台不让做"
check(M.retry_policy("FAIL_SYS_TOKEN_EXOIRED 订单状态不正确") == "refresh",
      "令牌失效优先于状态错（没登录时看到的'状态'本来就不可信）")

check("停止" in M.policy_text("stop", "x"), "stop 的说明能直接写日志")
check("保活" in M.policy_text("refresh"), "refresh 的说明指向保活")
check("重试" in M.policy_text("retry"), "retry 的说明写明可重试")


print("\n【9】t 精度：'秒×1000' 和真毫秒长得一样，但末三位恒为 0")

import re  # noqa: E402
import time as _time  # noqa: E402
_t = str(int(_time.time() * 1000))
check(len(_t) == 13 and not _t.endswith("000"), f"真毫秒是 13 位、末三位不恒 0（{_t}）")
check(str(int(_time.time()) * 1000).endswith("000"), "老写法末三位恒为 000（goofish-cli 的实现注释）")

bad = re.compile(r"int\(time\.time\(\)\)\s*\*\s*1000")
offenders = sorted(p.name for p in BACKEND.glob("*.py")
                   if bad.search(p.read_text(encoding="utf-8")))
check(offenders == [], f"后端源码里已经没有'秒×1000'的 t（{offenders}）")


print("\n" + "=" * 62)
if FAILS:
    print(f"通过 {PASSES} 项，失败 {len(FAILS)} 项")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print(f"通过 {PASSES} 项，失败 0 项")
sys.exit(0)
