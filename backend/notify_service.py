"""
多渠道通知推送 —— 设计借鉴三家成熟产品（均为功能思路，代码自写）：
- 嗷嗷鱼（商业 SaaS）：企微/钉钉/飞书/Bark 多渠道 + 事件级通知开关；
- Ydisks Xianyu Helper：Bark/DingTalk/Feishu/WeCom/邮件/自定义 Webhook；
- ai-goofish-monitor：ntfy 推送（"发现好价/风控告警"即时推）。

本地自用版只做 webhook 系（免 SMTP 配置，填 URL 即用）：
- 企业微信群机器人：POST {"msgtype":"markdown","markdown":{"content":...}}
- 钉钉群机器人：   POST {"msgtype":"markdown","markdown":{"title","text"}}
- 飞书群机器人：   POST {"msg_type":"text","content":{"text":...}}
- Bark（iOS）：    GET  {bark_url}/{title}/{content}
- 自定义 Webhook： POST {"event","title","content","level","time"}

配置存 data/notify_config.json（含 webhook 地址，属本机敏感配置，不外传）。
事件开关：polish（擦亮批次）/ risk（风控熔断）/ publish（发布失败或结果未知）
/ account（账号掉线、恢复、暂停、启用）/ diagnosis（店铺诊断高危信号）。

**为什么要分这五类，而不是全塞 risk**：卖家关掉"风控告警"通常只是想少听噪音，
但如果库存、掉线、诊断都挂在 risk 上，关一次就把真正要看的东西一起关掉了——
风险事件恰恰是最需要被看见的那条。所以诊断类单独成桶。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import math
import os
import re
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import quote

import requests
from loguru import logger

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "data" / "notify_config.json"
FAILURES_FILE = PROJECT_ROOT / "data" / "notify_failures.json"

EVENTS = ("polish", "risk", "publish", "account", "diagnosis", "monitor")
_failure_lock = threading.Lock()

# 发送失败重试：网络抖动、网关 5xx、限流 429 都值得再试一次；
# 401/403/404（key 错、地址错）重试只会重复撞墙，直接记失败。
# 提醒：webhook 不是幂等的——超时后重试可能让群里出现两条同样的消息。
# 这里选择"宁可重复，不可静默"，因为一条重复通知的代价远小于掉线没人知道。
try:
    RETRY_ATTEMPTS = max(1, int(os.getenv("NOTIFY_RETRY_ATTEMPTS", "3")))
except Exception:
    RETRY_ATTEMPTS = 3
try:
    RETRY_BACKOFF_SECONDS = max(0.0, float(os.getenv("NOTIFY_RETRY_BACKOFF_SECONDS", "0.8")))
except Exception:
    RETRY_BACKOFF_SECONDS = 0.8
FAILURE_LOG_LIMIT = 50
_sleep = time.sleep  # 测试可替换，避免真睡

DEFAULTS: Dict[str, Any] = {
    "enabled": True,
    "wecom": "",       # https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...
    "dingtalk": "",    # https://oapi.dingtalk.com/robot/send?access_token=...
    "feishu": "",      # https://open.feishu.cn/open-apis/bot/v2/hook/...
    "feishu_secret": "", # 飞书机器人安全设置加签 Secret（可选）
    "bark": "",        # https://api.day.app/你的KEY
    "custom": "",      # 任意接收 POST JSON 的地址
    "events": {"polish": True, "risk": True, "publish": True,
               "account": True, "diagnosis": True, "monitor": True},
    "timeout": 8,
}

_INCIDENT_LEDGER: Dict[str, float] = {}
_incident_lock = threading.Lock()


def sign_feishu_payload(payload: Dict[str, Any], secret: str, timestamp: Optional[int] = None) -> Dict[str, Any]:
    """
    飞书 Webhook 机器人 HMAC-SHA256 加签认证（吸收自 GitHub 统一项目 PR #11）。
    签名算法：timestamp + "\n" + secret 经 HMAC-SHA256 计算后进行 Base64 编码。
    """
    if not secret:
        return payload
    ts = str(int(timestamp if timestamp is not None else time.time()))
    key = (ts + "\n" + secret).encode("utf-8")
    sig = base64.b64encode(hmac.digest(key, b"", "sha256")).decode("utf-8")
    signed = dict(payload)
    signed["timestamp"] = ts
    signed["sign"] = sig
    return signed


def is_incident_duplicate(incident_id: str, ttl: int = 300) -> bool:
    """
    事故/预警事件持久化去重检查（吸收自 GitHub 统一项目 PR #11）。
    若同一 incident_id 在 ttl 冷却窗口内重复到达，返回 True 抑制重发，防止告警风暴。
    """
    if not incident_id:
        return False
    now = time.time()
    with _incident_lock:
        # 清理超过 1 小时的过期项，防止内存泄露
        expired = [k for k, ts in _INCIDENT_LEDGER.items() if now - ts > 3600]
        for k in expired:
            _INCIDENT_LEDGER.pop(k, None)
        last_ts = _INCIDENT_LEDGER.get(incident_id)
        if last_ts is not None and (now - last_ts < ttl):
            return True
        _INCIDENT_LEDGER[incident_id] = now
        return False


def _merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    out = dict(base)
    for k, v in (override or {}).items():
        if k == "events" and isinstance(v, dict):
            out["events"] = {**base.get("events", {}),
                             **{ek: bool(ev) for ek, ev in v.items() if ek in EVENTS}}
        elif k in base:
            out[k] = v
    return out


def get_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            return _merge(DEFAULTS, json.load(f))
    except Exception:
        return dict(DEFAULTS)


def save_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    merged = _merge(DEFAULTS, cfg or {})
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False, indent=2)
    return merged


def _channel_targets(cfg: Dict[str, Any]) -> Dict[str, str]:
    return {k: str(cfg.get(k) or "").strip()
            for k in ("wecom", "dingtalk", "feishu", "bark", "custom")}


def send_event(event: str, title: str, content: str, level: str = "info",
               force: bool = False, *, channel_payloads=None,
               incident_id: Optional[str] = None, incident_ttl: int = 300) -> Dict[str, Any]:
    """
    发送一条事件通知。event 必须在 EVENTS 内；未开启/无渠道时静默跳过。
    force=True（测试用）时忽略总开关与事件开关，只发已配置的渠道。
    incident_id: 可选事故唯一标识，同一标识在 incident_ttl 内重复触发会被去重抑制。
    返回 {"ok", "skipped", "results": {渠道: "ok" | "error: ..."}}
    """
    if incident_id and is_incident_duplicate(incident_id, incident_ttl):
        return {"ok": True, "skipped": True, "deduped": True,
                "results": {"_": f"事故 {incident_id} 在去重冷却窗口内，已抑制"}}

    try:
        cfg = get_config()
    except Exception:
        return {"ok": False, "skipped": True, "results": {}}
    if event not in EVENTS:
        return {"ok": False, "skipped": True, "results": {"_": f"未知事件 {event}"}}
    if not force:
        if not cfg.get("enabled", True):
            return {"ok": True, "skipped": True, "results": {"_": "通知总开关关闭"}}
        if not (cfg.get("events") or {}).get(event, True):
            return {"ok": True, "skipped": True, "results": {"_": f"事件 {event} 已关闭"}}

    targets = {k: v for k, v in _channel_targets(cfg).items() if v}
    if not targets:
        return {"ok": True, "skipped": True, "results": {"_": "未配置任何通知渠道"}}

    timeout = int(cfg.get("timeout") or 8)
    results: Dict[str, str] = {}
    attempts: Dict[str, int] = {}
    for name, url in targets.items():
        ok, err, tries = _send_with_retry(
            name, url, event, title, content, level, timeout,
            payload=(channel_payloads or {}).get(name),
        )
        attempts[name] = tries
        if ok:
            results[name] = "ok"
        else:
            results[name] = f"error: {err}"
            _record_failure(event, name, tries, err)
    return {"ok": any(v == "ok" for v in results.values()), "skipped": False,
            "results": results, "attempts": attempts}


def _mask_urls(text: str) -> str:
    """失败留痕里不能出现 webhook 地址（地址里带 key），一律替换成 <url>。"""
    return re.sub(r"https?://\S+", "<url>", str(text or ""))[:160]


def _retryable_status(code: int) -> bool:
    return code in (408, 425, 429) or code >= 500


def _channel_request(name: str, url: str, event: str, title: str, content: str,
                     level: str, timeout: int):
    if name == "wecom":
        return requests.post(url, json={
            "msgtype": "markdown",
            "markdown": {"content": f"**{title}**\n{content}"},
        }, timeout=timeout)
    if name == "dingtalk":
        return requests.post(url, json={
            "msgtype": "markdown",
            "markdown": {"title": title, "text": f"### {title}\n\n{content}"},
        }, timeout=timeout)
    if name == "feishu":
        feishu_payload = {
            "msg_type": "text",
            "content": {"text": f"{title}\n{content}"},
        }
        cfg = get_config()
        secret = cfg.get("feishu_secret") or os.getenv("FEISHU_SECRET") or ""
        if secret:
            feishu_payload = sign_feishu_payload(feishu_payload, secret)
        return requests.post(url, json=feishu_payload, timeout=timeout)
    if name == "bark":
        return requests.get(
            f"{url.rstrip('/')}/{quote(title[:40])}/{quote(content[:300])}",
            timeout=timeout)
    # custom
    return requests.post(url, json={
        "event": event, "title": title, "content": content,
        "level": level, "time": datetime.now().isoformat(),
    }, timeout=timeout)


def _send_with_retry(name: str, url: str, event: str, title: str, content: str,
                     level: str, timeout: int, *, payload=None):
    """返回 (ok, 错误摘要, 尝试次数)。只在可重试的失败上退避重试。"""
    last_err = ""
    tries = 0
    actual_payload = payload
    if payload is not None and name == "feishu":
        cfg = get_config()
        secret = cfg.get("feishu_secret") or os.getenv("FEISHU_SECRET") or ""
        if secret:
            actual_payload = sign_feishu_payload(payload, secret)

    for i in range(RETRY_ATTEMPTS):
        tries = i + 1
        try:
            resp = (requests.post(url, json=actual_payload, timeout=timeout) if actual_payload is not None
                    else _channel_request(name, url, event, title, content, level, timeout))
            code = int(resp.status_code)
            if 200 <= code < 300:
                if payload is not None:
                    try:
                        body = resp.json()
                    except ValueError:
                        return False, "机器人响应不是 JSON", tries
                    field = "code" if name == "feishu" else "errcode"
                    result = body.get(field) if isinstance(body, dict) else None
                    if result != 0:
                        return False, _mask_urls(f"机器人响应 {field}={result}"), tries
                return True, "", tries
            last_err = _mask_urls(f"HTTP {code} {str(getattr(resp, 'text', ''))[:80]}")
            if not _retryable_status(code):
                return False, last_err, tries
        except Exception as e:  # 连接失败、超时、DNS 等
            last_err = _mask_urls(str(e))
        if i + 1 < RETRY_ATTEMPTS and RETRY_BACKOFF_SECONDS > 0:
            _sleep(RETRY_BACKOFF_SECONDS * (2 ** i))
    return False, last_err, tries


def _record_failure(event: str, channel: str, attempts: int, error: str) -> None:
    """失败留痕：只记事件、渠道名、次数、脱敏后的错误，不记地址与消息正文。"""
    try:
        with _failure_lock:
            FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
            try:
                with open(FAILURES_FILE, "r", encoding="utf-8") as f:
                    items = json.load(f)
                if not isinstance(items, list):
                    items = []
            except Exception:
                items = []
            items.append({
                "time": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                "channel": channel,
                "attempts": int(attempts),
                "error": _mask_urls(error),
            })
            items = items[-FAILURE_LOG_LIMIT:]
            with open(FAILURES_FILE, "w", encoding="utf-8") as f:
                json.dump(items, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.warning(f"通知留痕写入失败，已跳过: {e}")


def get_recent_failures(limit: int = 20) -> list:
    """最近的通知发送失败（页面/接口只读展示用）。"""
    try:
        with _failure_lock, open(FAILURES_FILE, "r", encoding="utf-8") as f:
            items = json.load(f)
        if not isinstance(items, list):
            return []
        return list(reversed(items))[:max(0, int(limit or 0))]
    except Exception:
        return []


def notify_monitor_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Push a low-price discovery through the configured notification channels."""
    evaluation = item["ai_evaluation"]
    profit = evaluation.get("estimated_profit")
    profit_rate = evaluation.get("profit_rate")
    profit_text = f"¥{profit:.2f}" if isinstance(profit, (int, float)) and math.isfinite(profit) else "待评估"
    if isinstance(profit_rate, (int, float)) and math.isfinite(profit_rate):
        profit_text += f"（{profit_rate:.1%}）"
    title = "捡漏雷达测试" if item.get("test_mode") else "捡漏雷达发现"
    name = " ".join(item["title"].split())[:100]
    price = f"价格 ¥{item['price']:.2f}"
    estimate = f"预估利润 {profit_text}"
    item_url = item["item_url"]
    cover = next(iter(item["image_urls"]), "")
    markdown_name = re.sub(r"([\\`*_{}\[\]()#+!<>])", r"\\\1", name)
    markdown_cover = quote(cover, safe=":/?&=%#@+$,;~-._")
    dingtalk = {
        "title": title, "text": f"{markdown_name}\n\n{price}\n\n{estimate}",
        "singleTitle": "查看商品", "singleURL": item_url,
    }
    image_paragraph = []
    if cover:
        dingtalk["text"] += f"\n\n![商品主图]({markdown_cover})"
        image_paragraph = [[{"tag": "a", "text": "查看商品主图", "href": cover}]]
    article = {"title": name, "description": f"{price}\n{estimate}", "url": item_url}
    if cover:
        article["picurl"] = cover
    payloads = {
        "wecom": {"msgtype": "news", "news": {"articles": [article]}},
        "dingtalk": {"msgtype": "actionCard", "actionCard": dingtalk},
        "feishu": {"msg_type": "post", "content": {"post": {"zh_cn": {
            "title": title,
            "content": [
                [{"tag": "text", "text": f"{name}\n{price}\n{estimate}"}],
                [{"tag": "a", "text": "查看商品", "href": item_url}],
                *image_paragraph,
            ],
        }}}},
    }
    return send_event("monitor", title, "\n".join(filter(None, (name, price, estimate, item_url, cover))),
                      channel_payloads=payloads)


def notify_polish_summary(summary: Dict[str, Any]) -> None:
    """擦亮批次结束通知（有失败账号或全部成功都推一条，便于用户确认每日任务跑了）。"""
    try:
        total_s = int(summary.get("total_success") or 0)
        total_f = int(summary.get("total_fail") or 0)
        if not (total_s or total_f or summary.get("error")):
            return
        lines = [f"成功 {total_s} 个 / 失败 {total_f} 个"]
        for cid, acc in (summary.get("accounts") or {}).items():
            line = (f"- 账号 {cid}: 商品 {acc.get('items', 0)}，"
                    f"成功 {acc.get('success', 0)}，失败 {acc.get('fail', 0)}")
            if acc.get("warning"):
                line += f"\n  ⚠ {acc['warning']}"
            lines.append(line)
        if summary.get("error"):
            lines.append(f"⚠ {summary['error']}")
        send_event("polish", f"闲鱼擦亮完成（批次 {summary.get('batch_id', '')}）",
                   "\n".join(lines), level="warn" if total_f else "info")
    except Exception:
        pass


# ---------------------------------------------------------------- 账号状态通知
# 为什么要有：账号掉线时页面上的灯是灰的，但没人会一直盯着页面。卖家以为在自动
# 发货/自动回复，实际一条都没发出去——这是最典型的一种"沉默的故障"。
#
# 去重放在进程内：掉线后每几秒重试一次，不去重就会刷屏。进程重启后缓存清零，
# 但那时会重新连一次，状态也确实是真的，重报一次可以接受（比漏报强）。
_STATE_CACHE: Dict[str, Dict[str, Any]] = {}
try:
    STATE_DEDUPE_SECONDS = max(0, int(os.getenv("NOTIFY_STATE_DEDUPE_SECONDS", "600")))
except Exception:
    STATE_DEDUPE_SECONDS = 600

_STATE_TITLES = {
    "failed": ("⚠️ 账号掉线", "warn",
               "回复和自动发货都停了。检查网络/登录状态；需要时在账号页重新登录。"),
    "recovered": ("✅ 账号已恢复", "info",
                  "连接已恢复，待发的回复和发货会继续。"),
    "paused": ("⏸ 账号已暂停", "info",
               "后台已停止该账号；未发的回复和未发的卡都在队列里等恢复，不会自动补发未知结果。"),
    "resumed": ("▶️ 账号已恢复运行", "info",
                "后台任务已重新启动；暂停期间到期的发货租约会回收成 unknown，需人工核对。"),
}


def notify_account_state(cookie_id: str, state: str, reason: str = "",
                         force: bool = False) -> Dict[str, Any]:
    """
    账号状态通知。state 只接受 failed / recovered / paused / resumed。

    - 同一账号同一状态在窗口期内只推一次（重连风暴不刷屏）；
    - `recovered` 只有先前真的报过 failed 才推——正常重连（每几分钟一次）不算"恢复"，
      否则就成了每天几十条的噪音。
    """
    state = str(state or "").strip()
    if state not in _STATE_TITLES:
        return {"ok": False, "skipped": True, "results": {"_": f"未知账号状态 {state}"}}
    title, level, advice = _STATE_TITLES[state]
    cookie_id = str(cookie_id or "unknown")
    now = time.time()
    prev = _STATE_CACHE.get(cookie_id) or {}

    if not force:
        if state == "recovered" and prev.get("state") not in ("failed", "reconnecting"):
            _STATE_CACHE[cookie_id] = {"state": "connected", "ts": now}
            return {"ok": True, "skipped": True,
                    "results": {"_": "先前没有报过掉线，不发恢复通知"}}
        if prev.get("state") == state and now - float(prev.get("ts") or 0) < STATE_DEDUPE_SECONDS:
            return {"ok": True, "skipped": True, "results": {"_": "同一状态窗口内不重复推送"}}

    _STATE_CACHE[cookie_id] = {
        "state": "connected" if state == "recovered" else state, "ts": now}

    content = "\n".join([
        f"账号：{cookie_id}",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"原因：{reason or '未提供'}",
        f"影响：{advice}",
    ])
    return send_event("account", f"{title}（{cookie_id}）", content, level=level)


# ---------------------------------------------------------------- 诊断摘要
def notify_diagnosis_summary(report: Dict[str, Any],
                             cookie_id: str = "") -> Dict[str, Any]:
    """
    店铺诊断摘要：**只在出现 high 级店铺信号时推**。

    为什么只推 high：诊断每天跑一次，中低信号（发布量偏大、擦亮偏多）属于
    "知道就行"，每天推一遍会让人把通知静音，连账号级掉浏览这种真正要看的一起丢掉。
    """
    sigs = [s for s in ((report or {}).get("shop_signals") or [])
            if str(s.get("level") or "") == "high"]
    if not sigs:
        return {"ok": True, "skipped": True, "results": {"_": "没有高危信号"}}

    cid = str(cookie_id or (report or {}).get("cookie_id") or "")
    lines = []
    for s in sigs[:3]:
        lines.append(f"- {s.get('title', '')}：{s.get('evidence', '')}")
    actions = [str(a.get("action") or "") for a in ((report or {}).get("actions") or [])[:3]]
    actions = [a for a in actions if a]
    if actions:
        lines.append("该做的：")
        lines.extend(f"· {a}" for a in actions)
    lines.append(f"时间：{(report or {}).get('at') or datetime.now().isoformat(timespec='seconds')}")
    return send_event("diagnosis", f"店铺诊断：{len(sigs)} 条高危信号（{cid}）",
                      "\n".join(lines), level="warn")


# ---------------------------------------------------------------- 风控熔断
# 为什么这类事件要单独一条：熔断会让这个账号**所有**写操作（发布 / 发货 / 擦亮）
# 同时停下，而批次摘要只说明"擦亮了几个"。用户真正要知道的是：哪个号、因为
# 什么、停多久、现在别再做什么。
_RISK_SILENT_UNTIL: Dict[str, float] = {}


def notify_risk_trip(cookie_id: str, action: str, reason: str = "",
                     seconds: int = 0) -> Dict[str, Any]:
    """
    写操作撞到平台风控、账号被熔断时推一条（`risk` 桶）。

    同一账号在熔断窗口内只推第一条：熔断期间后续动作再撞墙是同一次事件的回声；
    连推十几条"触发风控"只会让人把 `risk` 桶关掉，而这一刀正好切掉最该看的东西。
    """
    cookie_id = str(cookie_id or "").strip()
    if not cookie_id:
        return {"ok": False, "skipped": True, "results": {"_": "缺少 cookie_id"}}
    now = time.time()
    if now < float(_RISK_SILENT_UNTIL.get(cookie_id) or 0):
        return {"ok": True, "skipped": True,
                "results": {"_": "该账号熔断期间已推过一次风控通知"}}
    stay = max(60, int(seconds or 0))
    _RISK_SILENT_UNTIL[cookie_id] = now + stay

    mins = max(1, int(round(stay / 60.0)))
    content = "\n".join([
        f"账号：{cookie_id}",
        f"时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
        f"触发动作：{action or '写操作'}",
        f"平台返回：{reason or '未提供'}",
        f"影响：该账号的发布 / 发货 / 擦亮等写操作已暂停，约 {mins} 分钟后自动解除"
        f"（连接恢复成功也会解除）。",
        "建议：先停手，看看账号有没有违规提醒；别连续重试，连续重试只会让平台更快升级处置。",
    ])
    return send_event("risk", f"⚠️ 账号触发平台风控（{cookie_id}）",
                      content, level="warn")


def send_test() -> Dict[str, Any]:
    """测试推送：忽略事件开关，直发已配置渠道。"""
    return send_event("polish", "闲鱼智控 Pro 通知测试",
                      f"这是一条测试消息，发送时间 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}。"
                      f"收到说明通知渠道配置成功。", force=True)


__all__ = ["get_config", "save_config", "send_event", "send_test",
           "notify_polish_summary", "notify_account_state",
           "notify_diagnosis_summary", "notify_risk_trip", "get_recent_failures",
           "EVENTS"]
