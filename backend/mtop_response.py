"""
MTOP 响应解析（唯一入口）
=========================
解决一个会**误导归因**的问题：网关 502 返回 HTML 错误页时，原实现直接
`resp.json()`，抛出的异常被上层归类成"接口返回异常"，于是：
  - 临时网络抖动 → 被当成接口不兼容 / 参数错 / 数据问题
  - 因为归类错，重试策略也错（该重试的不重试，不该重试的狂重试）

对照来源：`23Star/xianyu-super-butler` 的 `tests/test_failure_attribution.py`
（它踩过同一个坑：网关 502 返 HTML 时没检查状态码，最后被报成
「闲鱼接口未返回"在售"分组」，把临时抖动说成数据问题）。

**三类必须分开**：
  `transport` — 网络/网关层（5xx、429、空体、非 JSON）。可重试，且要退避。
  `auth`      — 401/403 或 ret 含 TOKEN/SESSION/ILLEGAL_ACCESS。刷新 Cookie 后可重试。
  `biz`       — HTTP 200 且 JSON 正常，但 ret 不是 SUCCESS。按业务语义处理。

用法：
    kind, raw, text = parse_mtop_response(resp)
    if kind == "transport": ...退避重试...
    elif kind == "auth":    ...换 Cookie 重试一次...
    else:                   ...按 ret 分类...
"""

from __future__ import annotations

from typing import Any, Dict, Tuple

# 鉴权类 ret 片段（大小写不敏感）
AUTH_MARKERS = (
    "TOKEN_EXOIRED", "TOKEN_EXPIRED", "TOKEN_EMPTY",
    "SESSION_EXPIRED", "SESSION_EXOIRED", "ILLEGAL_ACCESS",
    "FAIL_SYS_SESSION", "未登录", "登录已失效",
)

# 风控类 ret 片段（出现即停整轮）
RISK_MARKERS = (
    "RGV587", "USER_VALIDATE", "PUNISH", "哎哟喂", "被挤爆", "风控",
)

# 签名/参数类（拆解 9，来自 goofish-cli 的 SignError 分类）：
# 这类失败是**我们自己的** appKey / t / sign 拼错了，重试 N 次也只会错 N 次。
# 以前它落在"业务失败"里，被当成可重试，白白烧次数还掩盖了真原因。
SIGN_MARKERS = (
    "ILLEGAL_REQUEST", "SIGN_ERROR", "INVALID_SIGN", "签名错误", "签名校验失败",
)

# 目标不存在（商品被删、订单号写错）。重试不会让它重新存在。
NOTFOUND_MARKERS = (
    "ITEM_NOT_FOUND", "NOT_FOUND", "商品不存在", "商品已被删除", "已下架",
)

# 平台状态机已经往前走了一步，或明确不接受这次操作。
# 典型：订单已经"已发货/已完成/已关闭"，我们再点确认发货 → ORDER_STATUS_ERROR。
# 这不叫失败，叫**没有可做的事**：继续重试既不会成功，还会把"卡住了"的假信号
# 混进待人工列表（拆解 9，dengyie 的 not_required_terminal）。
STATUS_MARKERS = (
    "ORDER_STATUS_ERROR", "订单状态不正确", "订单状态错误", "订单状态已变更",
    "已经发货", "已发货", "无需发货", "订单已关闭", "交易已关闭",
)

# 网关/限流状态码
_RETRYABLE_HTTP = (408, 425, 429, 500, 502, 503, 504)


def ret_text(raw: Dict[str, Any]) -> str:
    """把 ret 数组拼成可读文本。"""
    if not isinstance(raw, dict):
        return ""
    r = raw.get("ret") or []
    return " | ".join(str(x) for x in r) if isinstance(r, list) else str(r)


def has_marker(text: str, markers) -> bool:
    up = str(text or "").upper()
    raw = str(text or "")
    return any(m.upper() in up or m in raw for m in markers)


def parse_mtop_response(resp: Any) -> Tuple[str, Dict[str, Any], str]:
    """
    返回 `(kind, raw_json, ret_text)`。

    kind 取值：
      - `"transport"` 网络/网关层失败（**不代表接口有问题**）
      - `"auth"`      鉴权失败（换 Cookie 可重试）
      - `"risk"`      风控（必须停，不能重试）
      - `"ok"`        能解析出 JSON（业务成败交给 ret 判断）
    """
    status = 0
    try:
        status = int(getattr(resp, "status_code", 0) or 0)
    except Exception:
        status = 0

    if status in _RETRYABLE_HTTP:
        return "transport", {}, f"HTTP {status}（网关/限流，可退避重试）"
    if status in (401, 403):
        return "auth", {}, f"HTTP {status}（鉴权失效）"

    raw: Any = None
    try:
        raw = resp.json()
    except Exception:
        # 典型场景：502 返回一整个 HTML 错误页。**这是传输层问题**，
        # 不是"接口返回格式变了"，不能据此判定接口不可用。
        body_head = ""
        try:
            body_head = str(getattr(resp, "text", "") or "")[:120]
        except Exception:
            pass
        if status >= 400:
            return "transport", {}, f"HTTP {status}，响应非 JSON：{body_head}"
        return "transport", {}, f"HTTP {status} 响应非 JSON（可能是网关错误页）：{body_head}"

    if not isinstance(raw, dict):
        return "transport", {}, f"响应 JSON 不是对象：{type(raw).__name__}"

    text = ret_text(raw)
    if has_marker(text, RISK_MARKERS):
        return "risk", raw, text
    if has_marker(text, AUTH_MARKERS):
        return "auth", raw, text
    return "ok", raw, text


def is_success(text: str) -> bool:
    return "SUCCESS" in str(text or "").upper()


# 这几个 auth 码是"换 Cookie 就能救"的；ILLEGAL_ACCESS 是风控层拒绝，刷新救不回来。
# 与 goofish-cli 的 `_is_recoverable_auth_error` 同一套判断。
_RECOVERABLE_AUTH = ("TOKEN_EXOIRED", "TOKEN_EXPIRED", "TOKEN_EMPTY",
                     "SESSION_EXPIRED", "SESSION_EXOIRED", "令牌过期", "未登录")


def retry_policy(text: str) -> str:
    """
    拿到一段 ret/错误文本后，唯一要回答的问题：**下一步做什么**。

    返回：
      `ok`      已成功
      `stop`    立刻停 —— 重试只会重复犯同一个错，或者重复骚扰平台
      `refresh` 先刷新登录态再重试 —— 别盲目重试（必然失败），也别判死（换 Cookie 能救）
      `retry`   普通业务/网络失败 —— 可退避重试

    和 `parse_mtop_response` 的分工：那个回答"失败属于哪一层"（transport/auth/risk），
    这个回答"下一轮怎么处理"。同一段文本两层结论可以不同 ——
    `FAIL_SYS_ILLEGAL_ACCESS` 在归因层是 auth，在处置层是 stop。

    这张表的来历（拆解 9，goofish-cli）：它把错误分成 exit code 75/76/77/78/79 五类，
    核心不是分类本身，而是**分类决定重试**。缺了这一层，最贵的两种错就会混进"可重试"：
    签名错（我们配错了，重试 5 次还是错）和订单状态错（平台已经发货了，我们还在点）。
    """
    text = str(text or "")
    if not text:
        return "retry"
    if is_success(text):
        return "ok"
    if has_marker(text, RISK_MARKERS):
        return "stop"
    if has_marker(text, SIGN_MARKERS):
        return "stop"
    if has_marker(text, ("ILLEGAL_ACCESS", "非法访问")):
        return "stop"
    if has_marker(text, _RECOVERABLE_AUTH):
        return "refresh"
    if has_marker(text, STATUS_MARKERS):
        return "stop"
    if has_marker(text, NOTFOUND_MARKERS):
        return "stop"
    return "retry"


_POLICY_LABEL = {
    "ok": "成功",
    "stop": "停止重试（重试不会改变结果）",
    "refresh": "登录态失效，等保活刷新后再试",
    "retry": "可退避重试",
}


def policy_text(policy: str, text: str = "") -> str:
    """写日志/落库用的一句话。"""
    label = _POLICY_LABEL.get(str(policy or ""), str(policy or ""))
    return f"{label}：{text}" if text else label


def explain(kind: str, text: str) -> str:
    """给人看的一句话归因（写日志/通知时用，别把三种混成"失败"）。"""
    if kind == "transport":
        return f"网络/网关层失败，可重试：{text}"
    if kind == "auth":
        return f"登录态失效，需刷新 Cookie：{text}"
    if kind == "risk":
        return f"触发平台风控，必须停止：{text}"
    if is_success(text):
        return "成功"
    return f"业务失败：{text}"
