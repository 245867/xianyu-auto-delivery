"""发布前合规闸（P0 安全项）

为什么要有这个文件
==================
2026-11-09 把用户整理的生财有术 148 份闲鱼实操文档（去重后 117 万字 / 79.6 万汉字）全部抽文读完，
其中「违规词」这一项在至少 9 份独立文档里重复出现，且口径高度一致：

    「假」「仿」「裸」「洗面奶」「计生用品」「正品」
    「SB」（USB 流下了泪水）、「ED」（LED 同样哭泣）、「ml」（用毫升代替）
    极限词：极致、第一、最好、历史最低、极其、100%、一定、绝对
    导流词：微信（平台违禁词，自己主动提会被限制发布和回复）

在此之前，我方只有 `opportunity_rewrite` 的**改写时脱敏**，`/api/opportunity/publish`
这条真正把内容送上平台的路径上**一道校验都没有**。也就是说：AI 改写过了、用户手工改回来了、
或者从模板库里翻出来直接发 —— 都能带着「微信」「100%」「正品」直接提交。

设计取舍（重要，别改成"一刀切全拦"）
==================================
1. **分三级，不是一张黑名单**：
   - BLOCK  导流/站外交易：命中即拒绝发布。这类词没有正当用法，误杀概率≈0。
   - WARN   夸大宣传/极限词/易判售假：只提示不拦截。二手文案里「保证正品」很常见，
     全拦会让工具没法用；提示到位，人来决定。
   - CATEGORY 品类红线（图书/课程/药品等）：只提示，因为是否违规取决于你有没有资质。
2. **不收单字**。「假」「仿」「裸」这三个字单独搜会把「裸机」（手机常用词）、
   「假一赔十」全判红 —— 文档作者说的是「平台会抓」，不是「用到就死」。
   所以这里只收**词组**，宁可漏也不要让工具变成噪音源。
3. **可扩展**：`data/publish_guard_extra.json` 里可以按
   `{"block": [...], "warn": [...], "allow": [...]}` 追加；`allow` 里的词会从命中结果里剔除
   （用于「我这个类目就是要写某个词」的例外）。

用法
====
    from publish_guard import check_publish
    ok, err, warnings = check_publish(title, desc)
    if not ok:
        return {"ok": False, "error": err}
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
# 可用 PUBLISH_GUARD_EXTRA 覆盖（测试用，避免读到用户真实词表）
EXTRA_FILE = Path(os.environ.get("PUBLISH_GUARD_EXTRA")
                  or (PROJECT_ROOT / "data" / "publish_guard_extra.json"))

# ---- 一级：命中即拒（导流 / 站外交易 / 明确违法） ----
BLOCK_WORDS: List[str] = [
    # 站外导流：闲鱼最讨厌的行为，自己主动提就算违规
    "微信", "VX", "vx", "Vx", "加V", "加v", "薇信", "威信", "QQ", "qq",
    "站外", "私下交易", "私下转账", "扫码付款", "加我好友",
    # 明确违法（不是「可能违规」，是「一定出事」）
    "高仿", "精仿", "复刻", "A货", "a货", "原单", "尾单", "莆田",
    "代充", "代开", "代实名", "解封", "刷单", "刷好评",
    "发票代开", "银行卡", "身份证",
]

# ---- 二级：提示（夸大 / 极限词 / 易判售假） ----
WARN_WORDS: List[str] = [
    # 极限词（广告法口径，闲鱼也抓）
    "100%", "百分百", "第一", "最好", "最低", "最便宜", "极致", "极其",
    "绝对", "史上", "全网", "唯一", "顶级", "国家级",
    # 绝对化承诺
    "保证", "包过", "包成功", "稳赚", "必赚", "无风险", "假一赔十",
    # 易判售假/侵权（不是违禁，但容易触发人工审核）
    # 「专柜」「免税」故意不收：二手文案里「专柜买的，有小票」是最常见的诚实描述，
    # 收了会把大半正常商品判成待审，噪音一多用户就不看提示了。
    "正品", "代购", "原厂", "官方授权", "品牌授权",
    # 平台明令的品类暗示
    "仿真", "管制", "处方",
    # 「标价非实价」这类自证引流的话：文档 030 明确写过，买家一举报就判引流违规、
    # 链接直接删。它本身不是违禁词，但写出来等于自己承认在导流，所以只提示 + 给替换话术。
    # 只收「非实价」（它已经覆盖「标价非实价」），避免同一条命中在提示里出现两次
    "非实价", "不是实价", "实价私聊",
]

# 命中后给的具体替换建议（只说"别写"没用，要告诉用户改成什么）
ADVICE: Dict[str, str] = {
    "非实价": "别写「标价非实价」——买家举报即判引流违规、链接会被删。"
              "改成「标价是闲鱼推荐价，勿拍，喜欢请咨询」这类说法。",
}

# ---- 三级：品类红线（只提示，是否合法取决于资质） ----
CATEGORY_WORDS: List[str] = [
    "图书", "教材", "教辅", "出版物", "书号", "ISBN",
    "课程", "网课", "教程", "资料", "源码", "破解", "激活码",
    "电子烟", "香烟", "酒", "药", "保健品", "医疗器械",
]

TITLE_LIMIT = 30  # 闲鱼标题 30 字；前 25 字放核心信息（文档共识）


def _load_extra() -> Dict[str, List[str]]:
    """读用户自定义词表。文件不存在 / 格式坏掉都不能影响发布主流程。"""
    empty = {"block": [], "warn": [], "allow": []}
    try:
        if not EXTRA_FILE.exists():
            return empty
        raw = json.loads(EXTRA_FILE.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return empty
        out = {}
        for k in ("block", "warn", "allow"):
            v = raw.get(k) or []
            out[k] = [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []
        return out
    except Exception:
        return empty


def _hits(text: str, words: List[str]) -> List[str]:
    return [w for w in words if w and w in text]


def check_text(title: str, desc: str = "", tags: Optional[List[str]] = None,
               images: Optional[int] = None) -> Dict[str, Any]:
    """返回结构化结果，不做任何 IO 之外的动作（纯函数，便于测试）。"""
    extra = _load_extra()
    blob = " ".join([title or "", desc or "", " ".join(tags or [])])
    allow = set(extra.get("allow") or [])

    block_words = BLOCK_WORDS + list(extra.get("block") or [])
    warn_words = WARN_WORDS + list(extra.get("warn") or [])

    block = [w for w in _hits(blob, block_words) if w not in allow]
    warn = [w for w in _hits(blob, warn_words) if w not in allow]
    category = [w for w in _hits(blob, CATEGORY_WORDS) if w not in allow]

    notes: List[str] = []
    t = (title or "").strip()
    if len(t) > TITLE_LIMIT:
        notes.append(f"标题 {len(t)} 字，超过闲鱼 {TITLE_LIMIT} 字上限，会被截断")
    if t and len(t) < 12:
        notes.append("标题偏短：闲鱼标题 30 字尽量写满，关键词覆盖越多曝光越高")
    if not (desc or "").strip():
        notes.append("正文为空：二手商品没有描述，咨询转化会明显偏低")
    # 图片数量：文档共识是「六张图以上，主图最重要」。只提示，因为一两张图也能发出去。
    if images is not None:
        if images <= 0:
            notes.append("没有图片：闲鱼必须至少一张主图")
        elif images < 3:
            notes.append(f"只有 {images} 张图：建议 6 张以上，第 1 张主图决定点击率")
        elif images < 6:
            notes.append(f"{images} 张图：可再加到 6 张以上，多图能显著提高咨询率")

    return {
        "ok": not block,
        "block": block,
        "warn": warn,
        "category": category,
        "notes": notes,
        "all_hits": sorted(set(block + warn + category)),
    }


def check_publish(title: str, desc: str = "",
                  tags: Optional[List[str]] = None,
                  images: Optional[int] = None) -> Dict[str, Any]:
    """发布前调用：返回 (是否放行, 错误文案, 提示列表)。"""
    r = check_text(title, desc, tags, images=images)
    warnings: List[str] = []
    if r["warn"]:
        warnings.append("以下词容易触发人工审核或判夸大宣传，建议改掉：" + "、".join(r["warn"]))
    for w in r["warn"]:
        if w in ADVICE:
            warnings.append(ADVICE[w])
    if r["category"]:
        warnings.append("命中品类红线词（图书/课程/药品等需资质）：" + "、".join(r["category"]))
    warnings.extend(r["notes"])
    return {"ok": r["ok"], "block": r["block"], "warnings": warnings}


def assert_clean(title: str, desc: str = "", tags: Optional[List[str]] = None) -> None:
    """给未来写路径用的硬闸：抛 ValueError 而不是返回 False，避免调用方忘记判。"""
    r = check_text(title, desc, tags)
    if r["block"]:
        raise ValueError("发布内容含平台违禁/导流词：" + "、".join(r["block"]))


__all__ = [
    "BLOCK_WORDS", "WARN_WORDS", "CATEGORY_WORDS", "TITLE_LIMIT", "ADVICE",
    "check_text", "check_publish", "assert_clean",
]
