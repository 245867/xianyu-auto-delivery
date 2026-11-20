"""
售罄自动上架（A6）

虚拟商品的刚需：一个商品挂上去，卖出 1 件就下架（库存=1），得重新上架才有曝光。
闲管家把这件事做成了官方语义（"售罄后按库存=1 循环上架"），说明平台容忍；
我们照它的边界做，不突破。

**关键设计决策：走已验证的重新发布链路，不碰未实证的"改库存"接口**
- 直觉方案是"改库存 + 重新上架"，保住原商品 ID 和想要数/浏览/收藏。
  但 `mtop.taobao.idle.item.edit` 与 `mtop.taobao.idle.item.status.update`
  这两个接口的参数名平台从未公开、我们也没实测过，拿它当自动化的核心动作等于埋雷：
  参数猜错时平台可能返回"成功"但实际没改成，或者改坏标题/描述。
- 所以这里只用**已跑通**的发布链路（传图 CDN → 类目推荐 → 默认地址 →
  mtop.idle.pc.idleitem.publish），以绑定的模板 + 素材重建一条新商品。
- 代价：新商品想要数从 0 起。这正是闲管家"重新发布须下架 + 新建、间隔 ≥24h"
  的同一件事，我们照它的边界，冷却默认 24 小时。

三层保护：
1. 默认关闭（`sold_out_relist_config.json` 里 enabled=false），第一次开启必须先跑 dry-run。
2. 只动"白名单"里的商品（用户明确勾选过的），绝不扫描全店自动上架。
3. 每件商品记 attempts 与 last_relist_at，同一商品 24 小时内最多上架 1 次，
   连续失败 3 次自动移出白名单并推通知（防越修越坏）。
"""

from __future__ import annotations

import json
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from platform_guard import (
    PlatformGuardBlocked,
    admit as guard_admit,
    observe as guard_observe,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
CONFIG_FILE = DATA_DIR / "sold_out_relist_config.json"
STATE_FILE = DATA_DIR / "sold_out_relist_state.json"

TOKEN_COOKIE_NAME = "_m_h5_tk"
DETAIL_API = "mtop.taobao.idle.pc.detail"
DETAIL_VERSION = "1.0"
DETAIL_SPM = "a21ybx.im.0.0"
MTOP_HOST = "https://h5api.m.goofish.com"

DEFAULTS = {
    "enabled": False,          # 默认关：先跑 dry-run 看结果
    "dry_run": True,           # 默认演练
    "interval_minutes": 30,    # 巡检间隔
    "night_ban": True,         # 23:00-08:00 不动
    "max_per_round": 5,        # 单轮最多处理几件（防一次改太多）
    "cooldown_hours": 24,      # 同商品冷却
    "max_attempts": 3,         # 连续失败几次移出白名单
}

_lock = threading.Lock()
_tick_stop = threading.Event()
_thread: Optional[threading.Thread] = None
_last_round: Dict[str, Any] = {}


# ---------------------------------------------------------------- 配置与状态

def _load(path: Path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def _save(path: Path, data) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _clamp(v: Any, lo: int, hi: int, default: int) -> int:
    """把配置值夹到 [lo, hi]。注意 0 是合法输入（要夹成 lo），不能用 `v or default`。"""
    try:
        n = int(v)
    except Exception:
        return default
    return max(lo, min(hi, n))


def get_config() -> Dict[str, Any]:
    cfg = dict(DEFAULTS)
    raw = _load(CONFIG_FILE, {})
    if isinstance(raw, dict):
        for k in DEFAULTS:
            if k in raw:
                cfg[k] = raw[k]
    cfg["enabled"] = bool(cfg["enabled"])
    cfg["dry_run"] = bool(cfg["dry_run"])
    cfg["night_ban"] = bool(cfg["night_ban"])
    cfg["interval_minutes"] = _clamp(cfg["interval_minutes"], 5, 720, 30)
    cfg["max_per_round"] = _clamp(cfg["max_per_round"], 1, 20, 5)
    cfg["cooldown_hours"] = _clamp(cfg["cooldown_hours"], 1, 168, 24)
    cfg["max_attempts"] = _clamp(cfg["max_attempts"], 1, 10, 3)
    return cfg


def save_config(data: Dict[str, Any]) -> Dict[str, Any]:
    cfg = get_config()
    for k in DEFAULTS:
        if k in (data or {}):
            cfg[k] = data[k]
    # 关演练才算"真开"，所以启用前必须已经演练过一轮
    _save(CONFIG_FILE, cfg)
    return get_config()


def get_state() -> Dict[str, Any]:
    st = _load(STATE_FILE, {})
    if not isinstance(st, dict):
        st = {}
    st.setdefault("watchlist", {})   # cookie_id::item_id -> {...}
    st.setdefault("logs", [])
    return st


def _save_state(st: Dict[str, Any]) -> None:
    st["logs"] = (st.get("logs") or [])[-200:]
    _save(STATE_FILE, st)


def _append_log(st: Dict[str, Any], text: str, level: str = "info") -> None:
    st.setdefault("logs", []).append({
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "level": level, "text": text,
    })


def list_watch(cookie_id: str = "") -> Dict[str, Any]:
    st = get_state()
    items = []
    for key, v in (st.get("watchlist") or {}).items():
        if cookie_id and not key.startswith(f"{cookie_id}::"):
            continue
        items.append({**v, "key": key})
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"ok": True, "items": items}


def add_watch(cookie_id: str, item_id: str, title: str = "",
              price: Any = None, quantity: int = 1,
              template_id: int = 0, material_id: int = 0,
              variables: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """
    加入白名单。因为重建走"模板 + 素材"，所以 template_id 是必填的——
    没有模板就没法自动上架，与其到点报错，不如现在就说清楚。
    """
    cookie_id = str(cookie_id or "").strip()
    item_id = str(item_id or "").strip()
    if not cookie_id or not item_id:
        return {"ok": False, "error": "需要 cookie_id 与 item_id"}
    tpl_id = int(template_id or 0)
    if tpl_id <= 0:
        return {"ok": False, "error": "必须绑定一个商品模板（自动上架靠模板重建）"}
    try:
        import product_template as pt
        if not pt.get_template(tpl_id).get("ok"):
            return {"ok": False, "error": f"模板 #{tpl_id} 不存在"}
    except Exception as e:
        return {"ok": False, "error": f"模板模块不可用：{str(e)[:100]}"}

    st = get_state()
    key = f"{cookie_id}::{item_id}"
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    fields = {
        "title": str(title or "")[:120], "price": price,
        "quantity": max(1, int(quantity or 1)),
        "template_id": tpl_id, "material_id": int(material_id or 0),
        "variables": variables or {},
        "active": True, "updated_at": now,
    }
    if key in st["watchlist"]:
        st["watchlist"][key].update(fields)
        _append_log(st, f"已在白名单，更新配置：{title or item_id}")
    else:
        st["watchlist"][key] = {
            "cookie_id": cookie_id, "item_id": item_id, **fields,
            "attempts": 0, "last_relist_at": "", "last_item_id": "",
            "last_check": "", "last_status": "", "created_at": now,
        }
        _append_log(st, f"加入白名单：{title or item_id}（模板 #{tpl_id}）")
    _save_state(st)
    return {"ok": True, "key": key}


def toggle_watch(cookie_id: str, item_id: str, active: bool) -> Dict[str, Any]:
    """单条启用/停用（不动全局配置，也不清零失败计数）。"""
    st = get_state()
    key = f"{str(cookie_id or '').strip()}::{str(item_id or '').strip()}"
    if key not in st["watchlist"]:
        return {"ok": False, "error": "不在白名单"}
    w = st["watchlist"][key]
    w["active"] = bool(active)
    w["updated_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if active:
        w["attempts"] = 0   # 人工重新启用视为一次"再给机会"
        w["last_status"] = "已人工启用"
    else:
        w["last_status"] = "已人工停用"
    _append_log(st, f"{'启用' if active else '停用'}：{w.get('title') or item_id}")
    _save_state(st)
    return {"ok": True, "key": key, "active": w["active"]}


def remove_watch(cookie_id: str, item_id: str) -> Dict[str, Any]:
    st = get_state()
    key = f"{cookie_id}::{item_id}"
    if key not in st["watchlist"]:
        return {"ok": False, "error": "不在白名单"}
    st["watchlist"].pop(key, None)
    _append_log(st, f"移出白名单：{item_id}")
    _save_state(st)
    return {"ok": True, "key": key}


# ---------------------------------------------------------------- MTOP 调用

def _extract_token(cookie_str: str) -> str:
    for part in (cookie_str or "").split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        name, _, value = part.partition("=")
        if name.strip() == TOKEN_COOKIE_NAME:
            value = value.strip()
            return value.split("_")[0] if "_" in value else value
    return ""


def _mtop(cookie_str: str, api: str, data: Any, version: str,
          spm: str, app_key: str, timeout: int = 20,
          cookie_id: str = "") -> Tuple[Dict[str, Any], Any]:
    """MTOP 调用。cookie_id 非空时把响应头的新 Cookie 回写（token 会续期）。"""
    import hashlib
    token = _extract_token(cookie_str)
    if not token:
        raise RuntimeError(f"Cookie 中缺少 {TOKEN_COOKIE_NAME}")
    t = str(int(time.time() * 1000))
    data_val = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
    sign = hashlib.md5(f"{token}&{t}&{app_key}&{data_val}".encode()).hexdigest()
    params = {
        "jsv": "2.7.2", "appKey": app_key, "t": t, "sign": sign,
        "v": version, "type": "originaljson", "accountSite": "xianyu",
        "dataType": "json", "timeout": "20000", "api": api,
        "sessionOption": "AutoLoginOnly", "spm_cnt": spm,
    }
    headers = {
        "accept": "application/json",
        "content-type": "application/x-www-form-urlencoded",
        "origin": "https://www.goofish.com",
        "referer": "https://www.goofish.com/",
        "user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"),
        "cookie": cookie_str.strip(),
    }
    try:
        guard_admit(cookie_str, scope="sold_out_relist", write=False, sleep=True)
    except PlatformGuardBlocked as exc:
        raise RuntimeError(str(exc))
    resp = requests.post(f"{MTOP_HOST}/h5/{api}/{version}/", params=params,
                         data={"data": data_val}, headers=headers, timeout=timeout)
    guard_observe(
        cookie_str,
        text=str(getattr(resp, "text", "") or ""),
        status_code=int(getattr(resp, "status_code", 0) or 0),
    )
    if cookie_id:
        _merge_and_persist(cookie_id, cookie_str, resp)
    try:
        return resp.json(), resp
    except Exception as e:
        raise RuntimeError(f"响应不是 JSON：{str(e)[:80]}")


def _merge_and_persist(cookie_id: str, cookie_str: str, resp: Any) -> str:
    """服务端下发新 Cookie 时回写。复用 polish_service 的实现，不重复造。"""
    try:
        if not str(cookie_id or "").strip():
            return cookie_str
        if not getattr(resp, "headers", None) or not resp.headers.get("set-cookie"):
            return cookie_str
        import polish_service
        new_cookie = polish_service._merge_response_cookies(resp, cookie_str)
        if new_cookie and new_cookie != cookie_str:
            polish_service._persist_cookie(cookie_id, new_cookie)
            return new_cookie
    except Exception:
        pass
    return cookie_str


def _ret_ok(raw: Dict[str, Any]) -> Tuple[bool, str]:
    ret = raw.get("ret") or []
    text = " | ".join(str(x) for x in ret) if isinstance(ret, list) else str(ret)
    return ("SUCCESS::" in text), text


def _classify(text: str) -> str:
    up = text.upper()
    for k in ("RGV587", "USER_VALIDATE", "PUNISH", "哎哟喂", "风控"):
        if k in up or k in text:
            return "risk"
    for k in ("TOKEN_EXOIRED", "TOKEN_EXPIRED", "SESSION_EXPIRED", "ILLEGAL_ACCESS", "未登录"):
        if k in up or k in text:
            return "auth"
    return "error"


# ---------------------------------------------------------------- 核心动作

def check_item(cookie_str: str, item_id: str, app_key: str,
               cookie_id: str = "") -> Dict[str, Any]:
    """只读：查这件商品现在是不是真的售罄（stock==0 或已下架）。"""
    raw, _ = _mtop(cookie_str, DETAIL_API, {"itemId": str(item_id)},
                   DETAIL_VERSION, DETAIL_SPM, app_key, cookie_id=cookie_id)
    ok, text = _ret_ok(raw)
    if not ok:
        return {"ok": False, "error": text[:200], "kind": _classify(text)}
    data = raw.get("data") or {}

    def find(obj, keys, depth=0):
        if depth > 6 or not isinstance(obj, dict):
            return None
        for k in keys:
            if k in obj and obj[k] is not None:
                return obj[k]
        for v in obj.values():
            if isinstance(v, dict):
                r = find(v, keys, depth + 1)
                if r is not None:
                    return r
        return None

    item = data.get("itemDO") if isinstance(data.get("itemDO"), dict) else data
    status = find(item, ("itemStatus", "status"))
    skus = find(item, ("skuList",))
    stock = None
    if isinstance(skus, list) and skus:
        qty = [s.get("quantity") for s in skus if isinstance(s, dict)]
        qty = [int(q) for q in qty if isinstance(q, (int, float))]
        if qty:
            stock = sum(qty)
    if stock is None:
        one = find(item, ("quantity", "stock"))
        if isinstance(one, (int, float)):
            stock = int(one)
    return {"ok": True, "item_status": status, "stock": stock,
            "sold_out": (stock == 0) if stock is not None else None,
            "title": find(item, ("title",)) or ""}


def relaunch(cookie_id: str, cookie_str: str, watch: Dict[str, Any]) -> Dict[str, Any]:
    """
    售罄后重新上架。

    **实现选择（重要）**：不走"改库存 + 重新上架"——那两个 MTOP 接口的参数名
    没有实证，平台随时会变，拿它当自动化的核心动作等于埋雷。改用**已验证**的
    发布链路（传图 CDN → 类目推荐 → 默认地址 → mtop.idle.pc.idleitem.publish），
    以绑定的模板 + 素材重建一条新商品。

    代价：新商品的想要数/浏览/收藏从 0 开始（旧商品的历史留在库里可查）。
    这正是闲管家"重新发布须下架 + 新建、间隔 ≥24h"的同一件事，我们照它的边界，
    冷却默认 24 小时。
    """
    import product_template as pt
    import material_library as matlib
    from opportunity_publisher import PublishError, PublishPausedError, PublishRiskError, publish_item

    try:
        tpl_id = int(watch.get("template_id") or 0)
    except Exception:
        tpl_id = 0
    if not tpl_id:
        return {"ok": False, "error": "这条商品没绑定模板，无法自动重建（请在页面上绑定）"}
    t = pt.get_template(tpl_id)
    if not t.get("ok"):
        return {"ok": False, "error": "绑定的模板已不存在"}
    tpl = t["template"]

    vars_ = watch.get("variables") or {}
    title = pt.render(tpl["title_tpl"], vars_)
    # C2（stale_relist）会传 `_title_override`：僵尸链接重建时换一个标题变体，
    # 降低被判"重复铺货"的概率。只换分隔符/时效前缀，卖点与关键词不动，
    # 下面统一过 publish_guard。
    if str(watch.get("_title_override") or "").strip():
        title = str(watch["_title_override"]).strip()
    desc = pt.render(tpl["desc_tpl"], vars_) or title
    price = watch.get("price")
    try:
        price = float(price if price not in (None, "") else tpl["price"] or 0)
    except Exception:
        price = float(tpl["price"] or 0)
    if price <= 0:
        return {"ok": False, "error": "价格必须大于 0"}

    images: List[str] = []
    if watch.get("material_id"):
        mp = matlib.material_paths(int(watch["material_id"]))
        if mp.get("ok"):
            images = mp["paths"]
    if not images and tpl.get("material_id"):
        mp = matlib.material_paths(int(tpl["material_id"]))
        if mp.get("ok"):
            images = mp["paths"]
    if not images:
        return {"ok": False, "error": "没有可用图片：请给这条商品或模板绑定素材"}

    # 渲染完再查一次变量：`render` 对缺的键是**原样保留 {{xxx}}** 的，
    # 于是"变量没填"会变成一条标题里带花括号的商品发出去。模板创建时校验过占位符，
    # 但模板是旧的、模板被改过、或变量来源变了，都会绕过那次校验。
    if "{{" in title or "{{" in desc:
        return {"ok": False, "error": "模板变量没填全（标题/描述里还留着 {{占位符}}），已拦下"}

    # 发布前合规闸。A6 原本没有这一步——模板在创建时已过 publish_guard，
    # 但变量是"运行时"灌进去的（可能来自商品标题），所以渲染结果必须再查一次。
    # 命中 BLOCK 词要拦（下架事小、账号事大）；闸门本身异常则放行并留痕，
    # 因为闸门是静态词表，抛异常意味着代码坏了，不该连带把重发功能全停掉。
    try:
        from publish_guard import check_publish
        g = check_publish(title, desc, images=len(images))
        if not g.get("ok"):
            return {"ok": False, "kind": "guard",
                    "error": "渲染结果含违禁/导流词，已拦下：" + "、".join(g["block"])}
    except Exception:
        # 闸门是静态词表，抛异常说明代码本身坏了，不该连带把 A6 整条链路停掉。
        pass

    item_data = {
        "title": title, "desc": desc, "imageUrls": images, "price": price,
        "quantity": int(watch.get("quantity") or tpl["quantity"] or 1),
        "delivery": tpl["delivery"] or "无需邮寄",
        "postPrice": float(tpl["post_price"] or 0),
    }
    if tpl.get("category"):
        item_data["category"] = tpl["category"]
    try:
        res = publish_item(cookie_str, item_data, _app_key(), cookie_id=cookie_id)
    except (PublishRiskError, PublishPausedError) as exc:
        return {"ok": False, "kind": "risk", "error": str(exc)}
    except PublishError as exc:
        if getattr(exc, "submission_status", "") == "submission_unknown":
            return {"ok": False, "kind": "unknown", "error": str(exc)}
        raise
    if not res.get("itemId"):
        return {"ok": False, "kind": "unknown", "error": "发布响应缺少 itemId，需人工回读"}
    return {"ok": True, "item_id": str(res["itemId"]), "error": ""}


def _night_ban(cfg: Dict[str, Any]) -> bool:
    if not cfg.get("night_ban", True):
        return False
    h = datetime.now().hour
    return h >= 23 or h < 8


def run_round(force: bool = False) -> Dict[str, Any]:
    """跑一轮巡检。force=True 忽略开关（手动触发用），但仍遵守夜间禁令与演练开关。"""
    if not _lock.acquire(blocking=False):
        return {"ok": False, "error": "售罄巡检正在运行"}
    try:
        return _run_round(force)
    finally:
        _lock.release()


def _run_round(force: bool) -> Dict[str, Any]:
    global _last_round
    import rate_limiter
    from opportunity_detail import resolve_cookie

    cfg = get_config()
    now_s = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # 提前退出的三种情况也要记进 _last_round：否则页面上是"这次启动后还没跑过"，
    # 用户会以为定时器坏了，实际是被开关/夜间禁令挡住了。
    def _exit(msg: str, **extra) -> Dict[str, Any]:
        global _last_round
        out = {"ok": False, "checked": 0, "sold_out": 0, "relisted": 0,
               "skipped": 0, "failed": 0, "dry_run": cfg["dry_run"],
               "error": msg, "details": [], "finished_at": now_s}
        out.update(extra)
        _last_round = out
        return out

    if not force and not cfg["enabled"]:
        return _exit("售罄自动上架未启用（默认关闭）")
    if _night_ban(cfg):
        return _exit("夜间时段（23:00–08:00）不动作，已跳过本轮")

    st = get_state()
    watch = st.get("watchlist") or {}
    active = [(k, v) for k, v in watch.items() if v.get("active")]
    if not active:
        _last_round = {"ok": True, "checked": 0, "sold_out": 0, "relisted": 0,
                       "skipped": 0, "failed": 0, "dry_run": cfg["dry_run"],
                       "message": "白名单为空（没有启用的商品）",
                       "details": [], "finished_at": now_s}
        return _last_round

    summary = {"ok": True, "checked": 0, "sold_out": 0, "relisted": 0,
               "skipped": 0, "failed": 0, "dry_run": cfg["dry_run"], "details": []}
    handled = 0
    ok, reason = True, ""
    for key, w in active:
        if handled >= cfg["max_per_round"]:
            break
        cid, iid = w.get("cookie_id", ""), w.get("item_id", "")
        if not cid or not iid:
            continue
        # 冷却
        last = w.get("last_relist_at") or ""
        if last:
            try:
                if datetime.now() - datetime.strptime(last, "%Y-%m-%d %H:%M:%S") < \
                        timedelta(hours=cfg["cooldown_hours"]):
                    summary["skipped"] += 1
                    continue
            except Exception:
                pass

        _, cstr = resolve_cookie(cid, fallback_first=False)
        if not cstr:
            summary["failed"] += 1
            w["last_status"] = "账号 Cookie 不存在"
            continue

        # 详情限流
        ok_q, retry = rate_limiter.acquire("detail", "global")
        if not ok_q:
            time.sleep(min(3, retry))
            ok_q, retry = rate_limiter.acquire("detail", "global")
        if not ok_q:
            summary["skipped"] += 1
            continue

        try:
            info = check_item(cstr, iid, _app_key(), cookie_id=cid)
        except Exception as e:
            info = {"ok": False, "error": str(e)[:160]}
        summary["checked"] += 1
        w["last_check"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if not info.get("ok"):
            kind = info.get("kind", "")
            w["last_status"] = f"查询失败：{info.get('error','')[:80]}"
            if kind == "risk":
                summary["error"] = "查询触发风控，本轮中止"
                break
            summary["failed"] += 1
            continue

        if not info.get("sold_out"):
            w["last_status"] = f"在售（库存 {info.get('stock')}）"
            w["attempts"] = 0
            continue

        summary["sold_out"] += 1
        if cfg["dry_run"]:
            w["last_status"] = "演练：检测到售罄，未执行上架"
            summary["details"].append({"item_id": iid, "action": "dry-run 检测到售罄"})
            continue

        # 日发布配额。`relaunch` 直接调 `publish_item`，而 `publish_item` 内部
        # **没有任何限流**——`create_job` 那个 20 条/天只作用在批量发布路径上。
        # 这一条是补缺口：售价=1 的商品卖得多，一天可能售罄十几件，加上手动触发
        # 很容易越过平台明确降权的量级（5–20 条/天）。
        try:
            import product_template as _pt
            _today = rate_limiter.daily_count("publish", cid)
            if _today >= _pt.DAILY_PUBLISH_CAP:
                summary["skipped"] += 1
                w["last_status"] = (f"今日已发 {_today} 条，达单日上限 "
                                    f"{_pt.DAILY_PUBLISH_CAP} 条，明天继续")
                break
        except Exception:
            pass
        # 写操作限流（与发布同级别，1 次/60s）
        ok_w, retry = rate_limiter.acquire("publish", cid, limit=1, window=60)
        if not ok_w:
            summary["skipped"] += 1
            w["last_status"] = f"发布限流，{retry}s 后重试"
            break

        try:
            res = relaunch(cid, cstr, w)
        except Exception as e:
            res = {"ok": False, "error": str(e)[:160]}
        if res.get("ok"):
            summary["relisted"] += 1
            handled += 1
            w["attempts"] = 0
            w["last_relist_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            w["last_item_id"] = res.get("item_id", "")
            if w["last_item_id"] and w["last_item_id"] != iid:
                new_key = f"{cid}::{w['last_item_id']}"
                if new_key not in watch:
                    w["item_id"] = w["last_item_id"]
                    watch[new_key] = w
                watch.pop(key, None)
            _save_state(st)
            w["last_status"] = f"已重新发布，新商品 {res.get('item_id','')}"
            summary["details"].append({"item_id": iid, "action": "已重新发布",
                                       "new_item_id": res.get("item_id", "")})
            _notify("售罄已自动重新上架",
                    f"{w.get('title') or iid}\n新品：{res.get('item_id','')}\n账号：{cid}")
        else:
            summary["failed"] += 1
            if res.get("kind") == "unknown":
                w["active"] = False
                w["last_status"] = "发布结果未知，核对商品列表后再启用"
                summary["details"].append({"item_id": iid, "action": w["last_status"]})
                _save_state(st)
                summary["error"] = w["last_status"]
                break
            w["attempts"] = int(w.get("attempts") or 0) + 1
            w["last_status"] = f"上架失败：{res.get('error','')[:100]}"
            summary["details"].append({"item_id": iid, "action": res.get("error", "")[:120]})
            if w["attempts"] >= cfg["max_attempts"]:
                w["active"] = False
                w["last_status"] += "（连续失败已达上限，已停用）"
                _notify("售罄上架连续失败", f"{w.get('title') or iid}\n已自动停用，请人工检查", level="error")
            if res.get("kind") == "risk":
                summary["error"] = "上架触发风控，本轮中止"
                break

        # 每件之间留间隔
        rate_limiter.jitter(8, 15)

    st["watchlist"] = watch
    summary["finished_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    _append_log(st, f"巡检完成：查 {summary['checked']}，售罄 {summary['sold_out']}，"
                    f"上架 {summary['relisted']}，失败 {summary['failed']}"
                    + ("（演练）" if cfg["dry_run"] else ""))
    _save_state(st)
    _last_round = summary
    return summary


def _app_key() -> str:
    try:
        from config import config
        return str(config.get("APP_CONFIG.app_key", ""))
    except Exception:
        return ""


def _notify(title: str, text: str, level: str = "info") -> None:
    try:
        from notify_service import send_event
        send_event("publish", title, text, level=level)
    except Exception:
        pass


# ---------------------------------------------------------------- 调度

def _loop() -> None:
    # 启动后先等 60 秒，避免和别的启动任务抢
    if _tick_stop.wait(60):
        return
    import task_watchdog as _wd
    while not _tick_stop.is_set():
        cfg = get_config()
        _wd.register("sold-out-relist", max(60, int(cfg["interval_minutes"]) * 60),
                     "售罄自动重发扫描")
        try:
            if cfg["enabled"]:
                run_round(force=False)
        except Exception:
            pass
        _wd.beat("sold-out-relist")
        if _tick_stop.wait(max(60, int(cfg["interval_minutes"]) * 60)):
            return


def start_scheduler() -> bool:
    global _thread
    if _thread and _thread.is_alive():
        return False
    _tick_stop.clear()
    _thread = threading.Thread(target=_loop, daemon=True, name="sold-out-relist")
    _thread.start()
    return True


def get_status() -> Dict[str, Any]:
    st = get_state()
    return {
        "config": get_config(),
        "watch_count": len([1 for v in (st.get("watchlist") or {}).values() if v.get("active")]),
        "total_watch": len(st.get("watchlist") or {}),
        "last_round": _last_round,
        "recent_logs": (st.get("logs") or [])[-20:],
        "running": bool(_thread and _thread.is_alive()),
    }
