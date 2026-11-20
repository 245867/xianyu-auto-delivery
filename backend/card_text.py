"""
批量卡密的取行规则（拆解 14：嗷嗷鱼的卡密去重口径）
====================================================

批量卡券就是一段文本，一行一条。原来 `consume_batch_data()` 取第一行、
把剩下的写回去 —— 如果库里本来就有两条一模一样的卡密（粘贴时手抖、
供应商导出的两批有重叠、中途补货复制了旧文件），**同一张卡会发给两个买家**。
这不是并发问题，取出时的原子删除挡不住它。

嗷嗷鱼 FAQ Q24 的口径是"导入时先去重，系统取出时也没法替你判断哪条是真的"。
他们说得对，但只靠导入环节不够 —— 只要有一次漏了，事故就发生了。所以这里做
两层：**取出一条时，把库里所有与它相同的行一起丢掉**。代价是库存数会少掉重复的
那部分（本来就该少），收益是"同一个卡密绝不发出第二次"变成一条结构性保证。

纯函数，不碰数据库，方便单测。
"""

from __future__ import annotations

from typing import List, Tuple


def split_codes(raw: str) -> List[str]:
    """一段文本 → 卡密行（去空白行、去首尾空格，保留顺序）。"""
    return [ln.strip() for ln in str(raw or "").split("\n") if ln.strip()]


def take_first(raw: str) -> Tuple[str, str, int]:
    """
    取出第一条卡密，并把**所有与它相同的行**一起去掉。

    返回 `(first, remaining_text, dropped)`：
      - `first` 为空表示没有库存；
      - `dropped` 是被顺带清掉的重复行数（>0 时调用方应该记一条日志）。
    """
    lines = split_codes(raw)
    if not lines:
        return "", "", 0
    first = lines[0]
    rest = [ln for ln in lines[1:] if ln != first]
    dropped = len(lines[1:]) - len(rest)
    return first, "\n".join(rest), dropped


__all__ = ["split_codes", "take_first"]