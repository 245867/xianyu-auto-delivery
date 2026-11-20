"""
回复侧合规闸（拆解 2：XianyuAutoAgent 的 `_safe_filter` 照出我们的一个真空）
=========================================================================

**我们原来的状态：发布有闸，回复没有闸。**

`publish_guard` 把「微信 / 加V / 私下转账 / 高仿」这类词挡在发布之前，
但买家在聊天里问「能加微信吗」「便宜点我私下转你」，AI 完全可能顺着回一句
「可以，加我微信 xxxx」。这句话一旦发出去：

- 平台判定站外导流，轻则限制回复，重则封号（用户自己整理的 148 份文档里，
  「导流」是封号梯度里靠前的一档）；
- 而且是**我们主动发出去的**，比买家试探更严重。

XianyuAutoAgent 做得很粗：命中 `["微信","QQ","支付宝","银行卡","线下"]` 就把整条回复
换成「[安全提醒]请通过平台沟通」。粗，但方向是对的——**回复是已经说出去的话，
不能像发布那样"给个提示让人改"**。

我们的做法比它细三处：

1. **复用发布侧词表**，不另建一份。两份词表迟早会漂移，而漂移的方向永远是
   「新词只加在一边」。
2. **加精确的号码/账号形态**：手机号 `1[3-9]xxxxxxxxx`、`微信:abc123` 这种写法，
   即使不含「微信」两个字也是在导流。词表挡不住，正则才能。
3. **命中就整条不发、转人工**，不做替换。理由：一条含导流词的回复，改成什么都
   已经不是卖家想说的话了；让人来接是最省事也最安全的出口。

**不用 CATEGORY_WORDS（图书/课程/药品）**：那是发布侧"这个品能不能卖"的概念，
买家聊天里提到「我买的是教材」不该拦。词表复用要挑，不是全抄。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List

ENV_SWITCH = "REPLY_GUARD_ENABLED"
# draft 档（售后/退款/承诺类）默认**扣下不发**，进发件箱 held 等人放行。
# 想退回"只标注、照发不误"的老行为，把 REPLY_TIER_ENFORCE 设成 0。
# 为什么默认反过来了（拆解 39 §八-3 验收口径）：这三档的意义是
# 「答错要赔的那类话，先让人看一眼」；如果默认照发，draft 就只是页面上的
# 一个装饰字段，和"命中即整条不发转人工"的合规闸自相矛盾。
ENV_ENFORCE_DRAFT = "REPLY_TIER_ENFORCE"

TIERS = ("auto", "draft", "block")

# draft 档（拆解 39 §八-3，嗷嗷鱼「答错了会不会赔」的分类法）：
# 这些话答错的代价不是"不好听"，而是赔钱/平台介入/售后纠纷，所以要先给人看。
# 注意与 WARN_WORDS（极限词）的区别：极限词是话术问题，这里是**承诺与售后**问题。
DRAFT_HINTS: List[str] = [
    "退款", "退货", "换货", "售后", "投诉", "差评", "举报", "平台介入", "客服介入",
    "赔偿", "补偿", "包退", "包换", "包修", "保修", "质保",
    "保证", "承诺", "百分百", "100%", "一定可以", "绝对", "永久",
    "开发票", "发票", "补发", "重发", "少发", "漏发",
]

# 手机号 / 微信账号 / QQ 号：不带「微信」两个字也在导流
_PHONE = re.compile(r"(?<![0-9])1[3-9][0-9]{9}(?![0-9])")
_CONTACT_ID = re.compile(
    r"(?:微信|薇信|威信|vx|wx|v信|企鹅|qq)\s*(?:号|id)?\s*[:：]?\s*[a-zA-Z0-9_-]{5,}",
    re.I)
_QQ_NUM = re.compile(r"(?:qq|企鹅)\s*(?:号)?\s*[:：]?\s*[0-9]{5,12}", re.I)

# 回复专属补充词：发布文案里可以出现，但卖家主动对买家说就是导流
REPLY_ONLY_BLOCK: List[str] = [
    "加我微信", "加我v", "加个微信", "留个电话", "打我电话", "私下来买",
    "私下转", "支付宝转", "扫码转账", "线下付款", "当面交易",
]


def enabled() -> bool:
    return (os.getenv(ENV_SWITCH, "1") or "1").strip() not in ("0", "false", "False", "off")


def _block_words() -> List[str]:
    """发布侧一级词表 + 用户自定义 block + 回复专属词。三级（品类红线）不取。"""
    words: List[str] = []
    try:
        import publish_guard
        words.extend(publish_guard.BLOCK_WORDS)
        words.extend((publish_guard._load_extra() or {}).get("block") or [])
        allow = set((publish_guard._load_extra() or {}).get("allow") or [])
    except Exception:
        allow = set()
    words.extend(REPLY_ONLY_BLOCK)
    return [w for w in dict.fromkeys(words) if w and w not in allow]


def _warn_words() -> List[str]:
    try:
        import publish_guard
        return list(dict.fromkeys(publish_guard.WARN_WORDS))
    except Exception:
        return []


def check(text: str) -> Dict[str, Any]:
    """返回 `{ok, blocked, warned, reason}`。`ok=False` 表示这条回复不能发。"""
    text = str(text or "")
    if not text.strip():
        return {"ok": True, "blocked": [], "warned": [], "reason": ""}
    if not enabled():
        return {"ok": True, "blocked": [], "warned": [], "reason": "闸门已关闭"}

    blocked: List[str] = [w for w in _block_words() if w and w in text]
    if _PHONE.search(text):
        blocked.append("手机号")
    m = _CONTACT_ID.search(text)
    if m:
        blocked.append(m.group(0)[:20])
    m = _QQ_NUM.search(text)
    if m:
        blocked.append(m.group(0)[:20])

    blocked = list(dict.fromkeys(blocked))
    warned = [w for w in _warn_words() if w and w in text]
    return {
        "ok": not blocked,
        "blocked": blocked,
        "warned": warned,
        "reason": ("含站外导流/违规词：" + "、".join(blocked)) if blocked else "",
    }


def enforce_draft() -> bool:
    """draft 档是否扣下不发。默认**是**；`REPLY_TIER_ENFORCE=0/false/off` 才关。"""
    return (os.getenv(ENV_ENFORCE_DRAFT, "1") or "1").strip().lower() not in ("0", "false", "off", "no")


def classify(text: str) -> Dict[str, Any]:
    """
    三档分流：auto / draft / block（拆解 39 §八-3）。

    - block：导流/违规词/号码形态 —— 与 `check()` 完全一致，整条不发；
    - draft：售后、退款、赔偿、承诺类 —— **能自动答，但答错要赔**，
      默认**扣下不发**：回复照常生成，但发送口把它登记成发件箱的 `held`
      等人放行；`REPLY_TIER_ENFORCE=0` 才退回"照发、只标注"；
    - auto：其余。

    单独一个函数而不是改 `check()` 的返回：`check()` 的 `ok` 已经被发送路径
    当成硬闸，往里塞"大概要人工"的软判断，会让旧的调用方行为悄悄变化。
    """
    base = check(text)
    text = str(text or "")
    if not base["ok"]:
        return {**base, "tier": "block", "draft_hits": [], "enforced": False}
    if not enabled() or not text.strip():
        return {**base, "tier": "auto", "draft_hits": [], "enforced": False}
    hits = [w for w in DRAFT_HINTS if w in text]
    # 极限词/绝对化承诺也在 draft：不是不能发，是发之前该有人看一眼
    hits = list(dict.fromkeys(hits + list(base.get("warned") or [])))
    tier = "draft" if hits else "auto"
    reason = ("需人工收尾：" + "、".join(hits[:6])) if hits else ""
    return {**base, "tier": tier, "draft_hits": hits,
            "reason": reason or base.get("reason", ""),
            "enforced": tier == "draft" and enforce_draft()}


__all__ = ["check", "classify", "enabled", "enforce_draft", "ENV_SWITCH",
           "ENV_ENFORCE_DRAFT", "REPLY_ONLY_BLOCK", "DRAFT_HINTS", "TIERS"]
