"""MTOP submission for an order with verified carrier pickup."""

from mtop_response import RISK_MARKERS, has_marker
import rate_limiter
import seller_api

PHYSICAL_SHIP_VERSION = "1.0"
EXPRESS_CODES = {
    "圆通": "YTO",
    "中通": "ZTO",
    "申通": "STO",
    "韵达": "YUNDA",
    "顺丰": "SF",
    "极兔": "JT",
    "邮政 EMS": "EMS",
}


def ship_physical_order(order_id: str, express_company: str, express_no: str,
                        cookie_str: str, cookie_id: str, *, version: str = PHYSICAL_SHIP_VERSION,
                        sub_orders: tuple[tuple[str, str, str], ...] = ()) -> None:
    api = "mtop.taobao.idle.order.ship"
    if version != PHYSICAL_SHIP_VERSION:
        raise ValueError("实物发货只支持 MTOP 1.0")
    express_code = EXPRESS_CODES.get(express_company, express_company)
    if not all((order_id, express_no, cookie_str, cookie_id)) or express_code not in EXPRESS_CODES.values():
        raise ValueError("缺少实物发货参数或快递公司编码不受支持")
    sub_order_list = []
    seen_ids = set()
    for sub_order_id, company, tracking in sub_orders:
        if (
            not all(isinstance(value, str) and value and value == value.strip()
                    for value in (sub_order_id, company, tracking))
            or sub_order_id in seen_ids
        ):
            raise ValueError("实物发货子单需要唯一的子单号及有效面单")
        code = EXPRESS_CODES.get(company, company)
        if code not in EXPRESS_CODES.values():
            raise ValueError("实物发货子单的快递公司编码不受支持")
        seen_ids.add(sub_order_id)
        sub_order_list.append({"subOrderId": sub_order_id, "expressCode": code, "expressNo": tracking})
    payload = {
        "orderId": order_id,
        "expressCode": express_code,
        "expressNo": express_no,
        "subOrderList": sub_order_list,
    }
    try:
        result = seller_api._call(
            api, payload, cookie_str, version=version, timeout=20, retry_expired_token=False,
        )
        ret = result.get("ret")
        if not isinstance(ret, list) or not ret or not all(
            isinstance(value, str) and value.partition("::")[0] == "SUCCESS" for value in ret
        ):
            raise seller_api.SellerApiError(api, ret if isinstance(ret, list) else [])
        data = result.get("data")
        if (
            result.get("api") != api or result.get("v") != version or not isinstance(data, dict)
            or data.get("orderId") != order_id or data.get("success", bool(sub_order_list)) is not True
            or data.get("isSuccess") != "true"
        ):
            raise seller_api.SellerApiError(api, ["实物发货回执未确认该订单发货成功"])
    except seller_api.SellerApiError as exc:
        if has_marker(" ".join(exc.ret), RISK_MARKERS):
            rate_limiter.trip_circuit(cookie_id, reason="实物发货触发平台风控")
        raise
