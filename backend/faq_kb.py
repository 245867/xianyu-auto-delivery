"""
买家提问观测与 FAQ 覆盖率（方向三）
====================================

**为什么需要它**

我们原来有一只眼睛是瞎的：`ai_conversations` 只在 `generate_reply()` 里写入，
而回复链路是「API → 关键词 → AI → 默认」。也就是说 —— **关键词越答得上的问题，
越不会留下任何记录**。卖家想知道"买家都在问什么"，能看到的只有 AI 没接住的那部分，
而且散在几千行日志里。于是补词条全靠猜，猜的方向永远是"我自己想到的问题"。

这个模块只做三件事，都不碰发货和回复的判定：

1. **每一条买家消息先落盘，再决定怎么回**（`log`），回复完补上结果（`note`）。
2. 算**覆盖率**：有多少问题是我们真答上了，多少是没人管的（`stats`）。
3. 把没人管的问题按**字面聚类**，给出"应该加哪条词条"的建议（`suggestions`）。

**三个刻意的克制**

- **不自动加词条。** 一条词条会自动发言，错一条比缺一条贵得多；建议只进页面，
  由人点"采用"。价格类问题尤其不能进词条 —— 关键词的优先级高于 AI，
  一旦命中就把 B3 议价整条绕过去了。
- **单独一个库**（`data/faq_kb.db`）。提问日志是旁路数据，丢了不影响发货；
  放进主库就得跟着主库做迁移，收益为零（拆解 12 的迁移原语也不该为它开口子）。
- **归一化只做兜底，且要求 ≥ 4 个字**。买家打「多久 发货？」「多久发货?」
  「多久发货呀」原来一条都命不中；归一化后能对上，但短词（如"1"）会误命中，
  所以设长度门槛，并且精确子串永远优先。

**归一化做了什么**：NFKC（全角转半角）→ 去空白 → 去标点/表情 → 小写。
只保留汉字、数字、字母。
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from loguru import logger
from sqlite_runtime import connect_sqlite

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = PROJECT_ROOT / "data"
DB_PATH = Path(os.environ.get("FAQ_DB") or (DATA_DIR / "faq_kb.db"))

# 归一化后至少 4 个字才允许模糊命中：「多久发货」可以，「1」「包邮」不行
MIN_FUZZY_LEN = 4

# 只保留：汉字、数字、小写字母
_NON_WORD = re.compile(r"[^0-9a-z一-鿿]+")

# 答上了的四种来源（API / 关键词 / 关键词归一化 / AI / 默认）
ANSWERED = ("api", "keyword", "keyword_normalized", "ai", "default")
OUTCOME_LABELS = {
    "api": "API 回复",
    "keyword": "关键词命中",
    "keyword_normalized": "关键词（归一化）",
    "ai": "AI 生成",
    "default": "默认回复",
    "none": "没有回复",
    "empty": "命中但内容为空",
}

# 聚类时永远不单独建议的碎片（它们会出现在更长的候选里，只是不该单独成条）
_STOP_GRAMS = {
    "你好", "您好", "在吗", "在么", "在不在", "老板", "亲亲", "请问", "一下", "麻烦",
    "可以", "能不", "不能", "什么", "怎么", "这个", "那个", "是不", "不是", "有没",
    "没有", "多少", "几个", "是不", "的话", "谢谢", "好的", "收到", "嗯嗯",
}


# --------------------------------------------------------------------- 归一化
def norm(text: str) -> str:
    """把一句话压成可比对的形状：全角→半角、去空白、去标点/表情、小写。"""
    s = unicodedata.normalize("NFKC", str(text or "")).lower()
    return _NON_WORD.sub("", s)


def match_normalized(keyword: str, message: str) -> bool:
    """关键词是否命中（归一化兜底）。精确子串由调用方先判，这里只管"换个写法"。"""
    kw, msg = norm(keyword), norm(message)
    if len(kw) < MIN_FUZZY_LEN or not msg:
        return False
    return kw in msg


def keyword_parts(keyword: str) -> List[str]:
    """
    一条词条可以写多行，每一行都是独立触发词（嗷嗷鱼 FAQ Q9 的做法）。

    好处是把同一件事的各种问法收在一条规则里：「有货吗 / 还有货吗 / 现货吗」
    写三行，比建三条规则更好维护，也不会互相抢命中。
    """
    return [p.strip() for p in str(keyword or "").replace("\r", "").split("\n") if p.strip()]


def match_keyword(keyword: str, message: str) -> bool:
    """精确子串优先，某一行命中即可；都没命中再对每一行做归一化兜底。"""
    msg = str(message or "")
    parts = keyword_parts(keyword)
    if not parts or not msg:
        return False
    low = msg.lower()
    if any(p.lower() in low for p in parts):
        return True
    return any(match_normalized(p, msg) for p in parts)


# --------------------------------------------------------------------- 存储
def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = connect_sqlite(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE IF NOT EXISTS questions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            cookie_id  TEXT,
            chat_id    TEXT,
            user_id    TEXT,
            item_id    TEXT,
            raw        TEXT NOT NULL,
            norm       TEXT NOT NULL,
            outcome    TEXT DEFAULT '',
            matched    TEXT DEFAULT '',
            created_at TEXT NOT NULL
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_created ON questions(created_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_q_outcome ON questions(outcome)")
    conn.commit()
    return conn


@contextmanager
def _tx():
    """短连接：自动提交，并且**一定关闭**（sqlite 的 `with conn` 只管事务，不管关闭）。"""
    conn = _connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def log(cookie_id: str, chat_id: str, user_id: str, item_id: str, raw: str,
        now: Optional[datetime] = None) -> Optional[int]:
    """落一条买家提问，返回 id（用于回填结果）。写不进去返回 None，绝不抛。"""
    raw = str(raw or "").strip()
    if not raw:
        return None
    now = now or datetime.now()
    try:
        with _tx() as conn:
            cur = conn.execute(
                "INSERT INTO questions (cookie_id,chat_id,user_id,item_id,raw,norm,created_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (str(cookie_id or ""), str(chat_id or ""), str(user_id or ""),
                 str(item_id or ""), raw[:2000], norm(raw), now.strftime("%Y-%m-%d %H:%M:%S")))
            return int(cur.lastrowid)
    except Exception as e:
        logger.warning(f"提问落盘失败（不影响回复）: {e}")
        return None


def note(qid: Optional[int], outcome: str, matched: str = "") -> bool:
    """回填这条问题最后是怎么处理的。qid 为空（落盘失败）时静默跳过。"""
    if not qid:
        return False
    try:
        with _tx() as conn:
            conn.execute("UPDATE questions SET outcome=?, matched=? WHERE id=?",
                         (str(outcome or ""), str(matched or "")[:200], int(qid)))
        return True
    except Exception as e:
        logger.warning(f"提问结果回填失败（不影响回复）: {e}")
        return False


def safe_log(*args, **kwargs) -> Optional[int]:
    try:
        return log(*args, **kwargs)
    except Exception:
        return None


def safe_note(*args, **kwargs) -> bool:
    try:
        return note(*args, **kwargs)
    except Exception:
        return False


# --------------------------------------------------------------------- 统计
def _since(days: int) -> str:
    return (datetime.now() - timedelta(days=max(1, int(days)))).strftime("%Y-%m-%d %H:%M:%S")


def stats(days: int = 30, cookie_id: Optional[str] = None) -> Dict[str, Any]:
    """覆盖率：答上 / 总数，以及各来源分布。"""
    where, args = "created_at>=?", [_since(days)]
    if cookie_id:
        where += " AND cookie_id=?"
        args.append(str(cookie_id))
    try:
        with _tx() as conn:
            rows = conn.execute(f"SELECT outcome, COUNT(*) n FROM questions WHERE {where} "
                                f"GROUP BY outcome", args).fetchall()
    except Exception as e:
        return {"ok": False, "error": str(e)[:160], "total": 0, "coverage": 0.0, "by_outcome": {}}

    by: Dict[str, int] = {}
    for r in rows:
        key = str(r["outcome"] or "none")
        by[key] = by.get(key, 0) + int(r["n"])

    total = sum(by.values())
    answered = sum(by.get(k, 0) for k in ANSWERED)
    return {
        "ok": True,
        "days": int(days),
        "total": total,
        "answered": answered,
        "coverage": round(answered / total, 4) if total else 0.0,
        "by_outcome": by,
        "labels": {k: OUTCOME_LABELS.get(k, k) for k in by},
        "error": "",
    }


def recent_unmatched(days: int = 30, limit: int = 50,
                     cookie_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """最近没答上的原话（新→旧）。"""
    where, args = "created_at>=? AND (outcome='none' OR outcome='' OR outcome='empty')", [_since(days)]
    if cookie_id:
        where += " AND cookie_id=?"
        args.append(str(cookie_id))
    args.append(int(limit))
    try:
        with _tx() as conn:
            rows = conn.execute(
                f"SELECT raw,norm,outcome,created_at FROM questions WHERE {where} "
                f"ORDER BY id DESC LIMIT ?", args).fetchall()
        return [{"raw": r["raw"], "norm": r["norm"], "outcome": r["outcome"],
                 "created_at": r["created_at"]} for r in rows]
    except Exception as e:
        logger.warning(f"读取未命中提问失败: {e}")
        return []


# --------------------------------------------------------------------- 聚类
# 句首/句尾的语气词：先剥掉再聚类，否则"请问有货"会因为多两个字排到"有货吗"前面
_EDGE_WORDS = ("请问", "想问", "问下", "你好", "您好", "老板", "亲", "麻烦", "我想",
               "谢谢", "在吗", "打扰")


def _strip_edges(text: str) -> str:
    t = str(text or "")
    changed = True
    while changed:
        changed = False
        for w in _EDGE_WORDS:
            if t.startswith(w) and len(t) > len(w):
                t, changed = t[len(w):], True
            if t.endswith(w) and len(t) > len(w):
                t, changed = t[:-len(w)], True
    return t


def _cluster(texts: Iterable[str], min_count: int = 3, max_len: int = 6,
             top: int = 200) -> List[Tuple[str, int]]:
    """
    轻量字面聚类：2–6 字 n-gram 的文档频率。
    排序不是"越长越好"：3–5 个字最像一个能当词条用的短语；更长的多是整句话。
    """
    df: Counter = Counter()
    for t in texts:
        t = _strip_edges(str(t or ""))
        if len(t) < 2:
            continue
        grams = set()
        for n in range(2, min(max_len, len(t)) + 1):
            for i in range(len(t) - n + 1):
                g = t[i:i + n]
                if g in _STOP_GRAMS:
                    continue
                if g.isdigit() or (g.isascii() and len(g) < 4):
                    continue
                grams.add(g)
        for g in grams:
            df[g] += 1

    cand = [(g, c) for g, c in df.items() if c >= min_count]
    # 长短语优先：先确定"到底在问什么"，再让它的碎片（"货吗"）被包含关系吃掉。
    cand.sort(key=lambda x: (len(x[0]), x[1]), reverse=True)

    accepted: List[Tuple[str, int]] = []
    for g, c in cand:
        # 已被某条更长的短语包含，且那条短语的解释力差不多（次数差不到一倍）→ 丢掉。
        # 差一倍以上的保留：那说明短的才是大聚类，长的只是它的一种问法
        #（例如"激活"30 次 vs "怎么激活"3 次，两个都该出现）。
        if any(g in a and c <= 2 * ca for a, ca in accepted):
            continue
        accepted.append((g, c))
        if len(accepted) >= top:
            break
    # 展示按人数排：问得最多的排前面
    accepted.sort(key=lambda x: (x[1], len(x[0])), reverse=True)
    return accepted


def _existing_keyword_norms(cookie_id: Optional[str]) -> List[str]:
    """现有词条的归一化形式（用来把"其实已经答上了"的短语剔掉）。"""
    try:
        from db_manager import db_manager
        kws = db_manager.get_keywords_with_type(cookie_id) if cookie_id else []
        return [norm(k.get("keyword") or "") for k in kws if k.get("keyword")]
    except Exception:
        return []


def suggestions(days: int = 30, min_count: int = 3, limit: int = 10,
                cookie_id: Optional[str] = None) -> List[Dict[str, Any]]:
    """
    该补哪些词条：把"没人答上"的问题聚类，剔除已经被现有词条覆盖的。
    只给建议，不写库 —— 由人在页面上点"采用"。
    """
    where, args = "created_at>=? AND (outcome='none' OR outcome='' OR outcome='empty')", [_since(days)]
    if cookie_id:
        where += " AND cookie_id=?"
        args.append(str(cookie_id))
    try:
        with _tx() as conn:
            rows = conn.execute(f"SELECT raw,norm FROM questions WHERE {where} LIMIT 5000",
                                args).fetchall()
    except Exception as e:
        logger.warning(f"读取提问失败: {e}")
        return []

    texts = [str(r["norm"] or "") for r in rows]
    raws = [str(r["raw"] or "") for r in rows]
    pairs = list(zip(texts, raws))
    clusters = _cluster(texts, min_count=min_count)

    covered = _existing_keyword_norms(cookie_id)
    out: List[Dict[str, Any]] = []
    for phrase, count in clusters:
        if len(phrase) < 2:
            continue
        if any(kw and (kw in phrase or phrase in kw) for kw in covered):
            continue
        examples = [raw for norm_t, raw in pairs if phrase in norm_t][:3]
        out.append({"phrase": phrase, "count": count, "examples": examples})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------- 起手词条
# 虚拟商品的通用问答。**价格类刻意不收**：关键词优先级高于 AI，收了就把 B3 议价绕过去。
# 导入时只补缺，不覆盖已有词条（改错一句自动回复的代价比缺一句大）。
STARTER_PACK: List[Tuple[str, str]] = [
    ("多久发货", "拍下后自动发货，一般 1 分钟内就能收到。超时没收到直接在这里说一声，我马上处理。"),
    ("什么时候发货", "自动发货，拍下后 1 分钟内到。没收到的话在这里回我一句。"),
    ("怎么发货", "发货内容是卡密/激活信息，会以聊天消息直接发给你，不需要物流，也没有快递单号。"),
    ("发到哪里", "直接发到这个聊天窗口，不用留地址。"),
    ("有货吗", "有货，拍下就能自动发。"),
    ("能开发票吗", "可以开电子发票，确认收货后把抬头和税号发我，当天开。"),
    ("支持验货吗", "支持。收到后先验证，有问题直接在这里找我，不用先确认收货。"),
    ("用不了", "别急，把报错提示或截图发我，我看一下马上处理。"),
    ("怎么用", "卡密的使用步骤我整理好了，回我一句「教程」我发给你。"),
    ("是全新的吗", "全新未使用，一次性卡密，只发给你一个人。"),
    ("能退吗", "虚拟商品发出后不支持无理由退，但如果是商品本身的问题，我负责到底。"),
    ("售后", "售后直接在这里找我，看到就回。"),
]


def add_keyword(cookie_id: str, keyword: str, reply: str) -> Dict[str, Any]:
    """
    把一条建议变成真词条（人点"采用"时调用）。只追加，不覆盖已有词条。
    """
    keyword, reply = str(keyword or "").strip(), str(reply or "").strip()
    if not keyword or not reply:
        return {"ok": False, "error": "关键词和回复都不能为空"}
    try:
        from db_manager import db_manager
        existing = db_manager.get_keywords_with_type(cookie_id) or []
        if any(str(k.get("keyword") or "") == keyword and not k.get("item_id") for k in existing):
            return {"ok": False, "error": "这条词条已经存在"}
        merged = [(str(k.get("keyword") or ""), str(k.get("reply") or ""),
                   str(k.get("item_id") or "")) for k in existing if k.get("type", "text") == "text"]
        merged.append((keyword, reply, ""))
        ok = db_manager.save_text_keywords_only(cookie_id, merged)
        return {"ok": bool(ok), "keyword": keyword, "total": len(merged),
                "error": "" if ok else "写入失败，看后端日志"}
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


def import_starter(cookie_id: str, entries: Optional[List[Tuple[str, str]]] = None) -> Dict[str, Any]:
    """
    把起手词条补进现有词条表（只补缺，不覆盖）。需要人从页面上显式触发。
    """
    try:
        from db_manager import db_manager
        entries = entries or STARTER_PACK
        existing = db_manager.get_keywords_with_type(cookie_id) or []
        have = {(str(k.get("keyword") or ""), str(k.get("item_id") or "")) for k in existing}
        merged = [(str(k.get("keyword") or ""), str(k.get("reply") or ""),
                   str(k.get("item_id") or "")) for k in existing if k.get("type", "text") == "text"]
        added = []
        for kw, reply in entries:
            if (kw, "") in have:
                continue
            merged.append((kw, reply, ""))
            added.append(kw)
        ok = db_manager.save_text_keywords_only(cookie_id, merged)
        return {"ok": bool(ok), "added": added, "total": len(merged), "error": ""}
    except Exception as e:
        return {"ok": False, "added": [], "total": 0, "error": str(e)[:200]}


def summary(days: int = 30, cookie_id: Optional[str] = None) -> Dict[str, Any]:
    """页面一次拿全。"""
    return {"ok": True, "stats": stats(days, cookie_id),
            "recent": recent_unmatched(days, 30, cookie_id),
            "suggestions": suggestions(days, 3, 10, cookie_id),
            "starter": [{"keyword": k, "reply": r} for k, r in STARTER_PACK]}


__all__ = ["MIN_FUZZY_LEN", "ANSWERED", "OUTCOME_LABELS", "STARTER_PACK",
           "norm", "match_normalized", "keyword_parts", "match_keyword",
           "log", "note", "safe_log", "safe_note",
           "stats", "recent_unmatched", "suggestions", "import_starter", "add_keyword",
           "summary"]
