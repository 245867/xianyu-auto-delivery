"""
拆解 12：加列迁移原语用例（sqlite-utils 的 "加列不锁库、不丢数据"）。

守住的五件事：
[1] 只补缺的   —— 已存在的列跳过，不会被 ALTER 报错
[2] 幂等       —— 跑两次第二次返回空，表结构不变
[3] 不丢数据   —— 老数据原样在，新列给默认值
[4] 不建错表   —— 表名不存在时什么都不做（拼错表名不该变成悄悄建空表）
[5] 收敛       —— 五个模块不再各写一遍 PRAGMA + ALTER

跑法：python tests/test_migrate.py
"""

import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import migrate

BACKEND = Path(__file__).resolve().parent.parent
FAILS = []


def check(cond, msg):
    if cond:
        print(f"  ok   {msg}")
    else:
        FAILS.append(msg)
        print(f"  FAIL {msg}")


def section(t):
    print(f"\n[{t}]")


def new_conn(table_sql: str = ""):
    p = Path(tempfile.mkdtemp()) / "m.db"
    conn = sqlite3.connect(str(p))
    if table_sql:
        conn.execute(table_sql)
        conn.commit()
    return conn


# ---------------------------------------------------------------- [1] 补缺
section("1 只补缺的列")
conn = new_conn("CREATE TABLE cards (id INTEGER PRIMARY KEY, title TEXT)")
check(migrate.columns(conn, "cards") == {"id", "title"}, "columns 读得到现有列")
added = migrate.ensure_columns(conn, "cards", {
    "image_url": "TEXT",
    "after_delivery_enabled": "BOOLEAN DEFAULT TRUE",
})
check(added == ["image_url", "after_delivery_enabled"], "两个新列都加上了（顺序按声明）")
check("after_delivery_enabled" in migrate.columns(conn, "cards"), "确实在表里")
added2 = migrate.ensure_columns(conn, "cards", {
    "image_url": "TEXT",
    "new_col": "INTEGER DEFAULT 0",
})
check(added2 == ["new_col"], "已存在的 image_url 跳过，只加 new_col")

# ---------------------------------------------------------------- [2] 幂等
section("2 幂等：跑两次不出错")
check(migrate.ensure_columns(conn, "cards", {"image_url": "TEXT"}) == [], "再跑一次返回空")
check(len(migrate.columns(conn, "cards")) == 5, "列数没有翻倍（id/title/image_url/after_delivery_enabled/new_col）")

# ---------------------------------------------------------------- [3] 数据
section("3 不丢数据：老行还在，新列拿默认值")
conn.execute("INSERT INTO cards (title) VALUES ('老卡片')")
conn.commit()
migrate.ensure_columns(conn, "cards", {"note": "TEXT DEFAULT '无'"})
row = conn.execute("SELECT title, note FROM cards").fetchone()
check(row[0] == "老卡片", "老数据原样在")
check(row[1] == "无", "新列对老行取默认值")
# commit=False 时不替调用方提交
conn2 = sqlite3.connect(str(Path(tempfile.mkdtemp()) / "x.db"))
conn2.execute("CREATE TABLE t (id INTEGER)")
conn2.commit()
migrate.ensure_columns(conn2, "t", {"a": "TEXT"}, commit=False)
check("a" in migrate.columns(conn2, "t"), "commit=False 也能加列（只是不提交）")
conn2.commit()

# ---------------------------------------------------------------- [4] 安全
section("4 边界：拼错的表名、可疑的标识符")
check(migrate.ensure_columns(conn, "no_such_table", {"x": "TEXT"}) == [],
      "表不存在时返回空，不建空表")
check("no_such_table" not in {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}, "确实没有凭空建表")
try:
    migrate.ensure_columns(conn, "cards; DROP TABLE cards", {"x": "TEXT"})
    check(False, "非法表名应该抛异常")
except ValueError:
    check(True, "非法表名抛 ValueError")
try:
    migrate.ensure_columns(conn, "cards", {"x TEXT); DROP TABLE cards; --": "TEXT"})
    check(False, "非法列名应该抛异常")
except ValueError:
    check(True, "非法列名抛 ValueError")
check("cards" in {r[0] for r in conn.execute(
    "SELECT name FROM sqlite_master WHERE type='table'")}, "cards 表还在")

# ---------------------------------------------------------------- [4b] 多表
section("4b ensure_each：一次补多张表")
conn3 = new_conn()
conn3.executescript("CREATE TABLE a (id INTEGER); CREATE TABLE b (id INTEGER);")
conn3.commit()
out = migrate.ensure_each(conn3, {"a": {"x": "TEXT"}, "b": {"y": "INTEGER DEFAULT 0"}})
check(out == {"a": ["x"], "b": ["y"]}, "两张表各自的清单分开返回")

# ---------------------------------------------------------------- [5] 收敛
section("5 收敛：五个模块不再各写一遍 PRAGMA + ALTER")
targets = {
    "idempotency.py": "op_idempotency",
    "delivery_reliability.py": "delivery_cards",
    "reply_outbox.py": "reply_outbox",
    "care_message.py": "care_actions",
}
for fname, table in targets.items():
    src = (BACKEND / fname).read_text(encoding="utf-8")
    check("import migrate" in src, f"{fname} 用了迁移原语")
    check('PRAGMA table_info' not in src, f"{fname} 里没有裸 PRAGMA table_info 了")
    # 只看真正的 DDL：注释里提到 "ADD COLUMN" 是在解释为什么用它，不算违规
    check('ALTER TABLE' not in src, f"{fname} 里没有裸 ALTER TABLE 了（DDL 都进 migrate 调用）")

dm = (BACKEND / "db_manager.py").read_text(encoding="utf-8")
check("PRAGMA table_info" in dm, "db_manager 的迁移故意没动（主库 + 回填交错），明确留在原处")

# ---------------------------------------------------------------- [6] 并发撞列
section("6 并发升级：对方刚加完这一列，ALTER 撞列不该报错")

conn = new_conn("CREATE TABLE race (id INTEGER)")
_real_columns = migrate.columns
# 装作"PRAGMA 之后、ALTER 之前，另一个连接刚把 extra 加完"：永远只看到 id
migrate.columns = lambda c, t: {"id"}
try:
    a1 = migrate.ensure_columns(conn, "race", {"extra": "TEXT"})
    a2 = migrate.ensure_columns(conn, "race", {"extra": "TEXT"})
finally:
    migrate.columns = _real_columns

check(a1 == ["extra"], "第一次正常加上")
check(a2 == [], f"撞到 duplicate column 被当成「已有」而不是抛异常（{a2}）")
check(migrate.columns(conn, "race") == {"id", "extra"},
      f"列只加了一次，表结构正确（{migrate.columns(conn, 'race')}）")

print()
if FAILS:
    print(f"❌ {len(FAILS)} 项失败：")
    for f in FAILS:
        print("   -", f)
    sys.exit(1)
print("✅ 全部通过")