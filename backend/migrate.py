"""
加列迁移的唯一入口（拆解 12：`sqlite-utils` 的 "加列不锁库、不丢数据"）

**原来的状态：同一段五行的代码，在五个模块里各写了一遍。**

```python
cols = {r[1] for r in conn.execute("PRAGMA table_info(care_actions)")}
if "rechecks" not in cols:
    conn.execute("ALTER TABLE care_actions ADD COLUMN rechecks INTEGER DEFAULT 0")
```

它不难，但每次都要记得三件事：**先查再改**（`ALTER TABLE ... ADD COLUMN` 对已存在的列会报错）、
**不要重建表**（用户数据在里面）、**改完要 commit**。忘掉任何一条，升级路径就断一次，
而这类错误只在"老用户升级"时出现——你自己的开发机是新建的库，永远不会复现。

`sqlite-utils` 的做法是把这件事收成一个原语（`add_column` / `transform` 的前置检查），
调用点只声明"我要这一列"。我们照这个思路收成两个函数：

    columns(conn_or_cursor, "表名")                       -> set[str]
    ensure_columns(conn_or_cursor, "表名", {"列": "DDL"})  -> 实际新增的列

约定：
- **只加列，不改类型、不删列、不重建表**。SQLite 的 `ALTER TABLE` 也只支持到这一步；
  需要改类型的场景应该新建表 + 拷数据，那是另一件事，不该藏在这个函数里。
- 表名/列名做白名单校验（`^[A-Za-z_][A-Za-z0-9_]*$`）。DDL 片段必须来自代码常量，
  但这些名字有时来自配置，拼进 f-string 之前挡一道，成本几乎为零。
- 幂等：跑两次不报错，第二次返回空列表。
- 用 `conn_or_cursor` 兼收：`sqlite3.Connection` 和 `Cursor` 都行，调用点不用改风格。

`db_manager.py` 里的迁移**没有**挪过来：那里的加列和"数据回填"（把旧 `item_detail`
里的封面 URL 拆到独立字段）交错在同一个事务里，而且它管着用户主库。把一段活的、
有数据的迁移改写成"更整齐的样子"，收益是代码好看，风险是用户的订单表。
分开走：新模块用这里，主库保持原样，等哪天真要动主库时再单独做。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any, Dict, Iterable, List, Set, Union

_IDENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

ConnOrCursor = Union[sqlite3.Connection, sqlite3.Cursor]


def _check_ident(name: str, what: str) -> str:
    name = str(name or "").strip()
    if not _IDENT.match(name):
        raise ValueError(f"非法的{what}: {name!r}")
    return name


def _conn(conn_or_cursor: ConnOrCursor) -> Any:
    """Cursor 上拿 connection；Connection 上就是它自己。"""
    return getattr(conn_or_cursor, "connection", None) or conn_or_cursor


def columns(conn_or_cursor: ConnOrCursor, table: str) -> Set[str]:
    """这张表现在有哪些列。表不存在时返回空集合（不抛）。"""
    table = _check_ident(table, "表名")
    try:
        rows = conn_or_cursor.execute(f'PRAGMA table_info("{table}")').fetchall()
    except sqlite3.Error:
        return set()
    out: Set[str] = set()
    for r in rows:
        # sqlite3.Row 支持下标，普通 tuple 也支持；列名统一在第 2 位
        try:
            out.add(str(r["name"]))
        except (TypeError, IndexError, KeyError):
            out.add(str(r[1]))
    return out


def ensure_columns(conn_or_cursor: ConnOrCursor, table: str,
                   spec: Dict[str, str], *, commit: bool = True) -> List[str]:
    """
    把 `spec` 里缺的列补上，返回实际新增的列名（已存在的跳过）。

    `spec` 形如 `{"rechecks": "INTEGER DEFAULT 0"}`。**只加列**，不改已有列。
    """
    table = _check_ident(table, "表名")
    have = columns(conn_or_cursor, table)
    if not have:
        # 表还不存在：这不是迁移的职责（建表该在建表的地方写），直接跳过，
        # 免得把一个拼错的表名变成"悄悄建了一张空表"。
        return []
    added: List[str] = []
    for name, ddl in (spec or {}).items():
        col = _check_ident(name, "列名")
        if col in have:
            continue
        try:
            conn_or_cursor.execute(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {ddl}')
        except sqlite3.OperationalError as e:
            # 两个连接同时升级同一个老库：先查后改之间有缝隙，对方刚加完，
            # 我方这条 ALTER 就撞 "duplicate column name"。这不是迁移失败，
            # 是同一件正确的事被做了两遍——按"已有该列"继续，不往上报 500。
            # （离线复现过：两个线程同时首次打开 op_idempotency 时必现。）
            if "duplicate column name" not in str(e).lower():
                raise
            continue
        added.append(col)
    if added and commit:
        try:
            _conn(conn_or_cursor).commit()
        except Exception:
            pass
    return added


def ensure_each(conn_or_cursor: ConnOrCursor, spec: Dict[str, Dict[str, str]],
                *, commit: bool = True) -> Dict[str, List[str]]:
    """一次补多张表：`{"cards": {...}, "item_info": {...}}` → `{表: [新增列]}`。"""
    return {t: ensure_columns(conn_or_cursor, t, cols, commit=commit)
            for t, cols in (spec or {}).items()}


__all__ = ["columns", "ensure_columns", "ensure_each"]