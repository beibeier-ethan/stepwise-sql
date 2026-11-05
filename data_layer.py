"""data_layer.py —— 数据库相关的一切：导入、表结构、预览、schema 文本、执行查询。

这一层不碰 Streamlit，也不碰页面布局；所有函数只依赖传入的 sqlite3 连接，
方便单独测试或换个前端（比如 CLI）复用。
"""

from __future__ import annotations

import io
import os
import re
import sqlite3
import tempfile
import time
from pathlib import Path

import pandas as pd

SQLITE_SUFFIXES = (".sqlite", ".sqlite3", ".db")
PREVIEW_ROWS = 20          # 预览默认行数
RESULT_ROW_LIMIT = 1000    # 查询结果最多取这么多行，防止一次拖回百万行卡死页面


class QueryError(RuntimeError):
    """执行 SQL 时的可读错误（已翻译成中文提示）。"""


# ------------------------------ 标识符 ------------------------------
def safe_identifier(filename: str) -> str:
    """把文件名转换成合法的表名（保留中文与字母数字）。"""
    stem = Path(filename).stem
    cleaned = re.sub(r"\W+", "_", stem).strip("_").lower()
    if not cleaned:
        cleaned = "table_1"
    if cleaned[0].isdigit():
        cleaned = f"t_{cleaned}"
    return cleaned


def quote_ident(name: str) -> str:
    """SQLite 标识符转义。"""
    return '"' + name.replace('"', '""') + '"'


# ------------------------------ 连接与导入 ------------------------------
def open_sqlite_file(filename: str, data: bytes) -> tuple[sqlite3.Connection, str, str]:
    """把上传的 .sqlite/.db 复制成临时文件后打开，返回（可写连接，临时文件路径，来源名）。

    注意这里有两层保护：
    1. 原始上传文件只被读成 bytes，落盘的是**副本**，任何写操作都碰不到你的原文件；
    2. 这个可写连接只用于「导入 CSV / 执行 .sql 脚本」，导入结束会立刻用
       lock_readonly() 把它锁成只读，之后所有查询另走只读连接（见 open_readonly）。
    """
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=Path(filename).suffix or ".sqlite")
    tmp.write(data)
    tmp.close()
    conn = sqlite3.connect(tmp.name, check_same_thread=False)
    return conn, tmp.name, filename


def readonly_uri(db_path: str) -> str:
    """把本地路径转成 SQLite 的 file: URI，并带上 mode=ro。

    用 Path.as_uri() 而不是手工拼 "file:{path}"，这样 Windows 盘符、空格、
    中文路径都会被正确转义，不会因为路径里有 ? 或 # 而解析失败。
    """
    return Path(db_path).expanduser().resolve().as_uri() + "?mode=ro"


def open_readonly(db_path: str) -> sqlite3.Connection:
    """以只读模式打开数据库：mode=ro 下任何 INSERT/UPDATE/DELETE/DDL 都会被 SQLite 直接拒绝。"""
    return sqlite3.connect(readonly_uri(db_path), uri=True, check_same_thread=False)


def lock_readonly(conn: sqlite3.Connection) -> None:
    """把工作连接本身也锁成只读（PRAGMA query_only），双保险。

    必须在所有导入（CSV / .sql）完成、且已 commit 之后调用；调用后该连接只能读，
    再想写会报 "attempt to write a readonly database"。
    """
    try:
        conn.commit()
        conn.execute("PRAGMA query_only = ON")
    except sqlite3.Error:
        pass


def open_memory_db() -> tuple[sqlite3.Connection, str, str]:
    """没有数据库文件时（纯 CSV 场景）用的内存库。"""
    return sqlite3.connect(":memory:", check_same_thread=False), "", "内存数据库"


def import_csv(conn: sqlite3.Connection, filename: str, data: bytes) -> tuple[str, int]:
    """把 CSV 读入 pandas，再整表写入当前 SQLite 连接（写入后立刻 commit，供只读连接读取）。"""
    df = pd.read_csv(io.BytesIO(data))
    table = safe_identifier(filename)
    df.to_sql(table, conn, index=False, if_exists="replace")
    conn.commit()
    return table, len(df)


def run_sql_script(conn: sqlite3.Connection, data: bytes) -> int:
    """执行 .sql 建表/插入脚本，返回执行后的表数量。"""
    conn.executescript(data.decode("utf-8", errors="ignore"))
    conn.commit()
    return len(list_tables(conn))


def close_connection(conn: sqlite3.Connection | None, db_path: str | None) -> None:
    """关连接并删掉临时库文件。"""
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    if db_path and os.path.exists(db_path):
        try:
            os.remove(db_path)
        except OSError:
            pass


# ------------------------------ 结构读取 ------------------------------
def list_tables(conn: sqlite3.Connection) -> list[str]:
    """列出用户表（排除 SQLite 内部表）。"""
    sql = (
        "SELECT name FROM sqlite_master "
        "WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return [row[0] for row in conn.execute(sql).fetchall()]


def table_columns(conn: sqlite3.Connection, table: str) -> pd.DataFrame:
    """PRAGMA table_info 转成中文表头的 DataFrame。"""
    rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
    records = [
        {
            "序号": cid,
            "列名": name,
            "类型": (col_type or "（无声明）"),
            "非空": "是" if notnull else "",
            "默认值": "" if dflt is None else dflt,
            "主键": "PK" if pk else "",
        }
        for cid, name, col_type, notnull, dflt, pk in rows
    ]
    return pd.DataFrame(records)


def preview_table(conn: sqlite3.Connection, table: str, limit: int = PREVIEW_ROWS) -> pd.DataFrame:
    """取前 N 行数据。"""
    return pd.read_sql_query(f"SELECT * FROM {quote_ident(table)} LIMIT {int(limit)}", conn)


def count_rows(conn: sqlite3.Connection, table: str) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {quote_ident(table)}").fetchone()[0]


def grid_height(rows: int, max_height: int = 560) -> int:
    """让表格完整显示前 N 行，而不是被压缩成 10 行高的小滚动框。"""
    return min(max_height, 40 + 35 * max(rows, 1))


# ------------------------------ schema 上下文 ------------------------------
def build_schema_prompt(conn: sqlite3.Connection) -> str:
    """把「表名 + 列名 + 类型」拼成文本，不带任何具体数据，避免 token 溢出。"""
    lines = ["数据库结构（SQLite）："]
    for table in list_tables(conn):
        rows = conn.execute(f"PRAGMA table_info({quote_ident(table)})").fetchall()
        cols = []
        for _, name, col_type, notnull, _, pk in rows:
            flag = " 主键" if pk else ""
            flag += " 非空" if notnull and not pk else ""
            cols.append(f"{name} {col_type or 'TEXT'}{flag}")
        lines.append(f"表 {table}({', '.join(cols)})")
    return "\n".join(lines)


# ------------------------------ 执行查询 ------------------------------
ERROR_HINTS = (
    ("no such column", "列名不存在——检查字段名拼写，或该列是否属于这张表"),
    ("no such table", "表名不存在——检查表名，注意不要漏掉别名"),
    ("no such function", "SQLite 没有这个函数，换成 SQLite 支持的写法"),
    ("ambiguous column name", "多个表里有同名列，需要加表名前缀，例如 orders.amount"),
    ("syntax error", "SQLite 语法检查比 sqlglot 更严格，检查括号、逗号与关键字顺序"),
    ("misuse of aggregate", "聚合函数用错了位置，COUNT/SUM 不能直接写在 WHERE 里"),
    ("datatype mismatch", "类型不匹配，比如拿数字列去和文本比较"),
    ("readonly database", "当前是只读连接，写操作被拒绝——这层保护专门用来挡住误改数据，查询请只用 SELECT"),
    ("database is locked", "数据库被另一个连接占用，稍等一秒再点一次「执行查询」"),
)


def translate_sqlite_error(exc: sqlite3.Error) -> str:
    """把 SQLite 的英文报错翻成能照着改的中文提示。"""
    raw = str(exc)
    for needle, hint in ERROR_HINTS:
        if needle.lower() in raw.lower():
            return f"执行失败：{raw}\n\n可能原因：{hint}"
    return f"执行失败：{raw}"


def execute_query(conn: sqlite3.Connection, sql: str,
                  limit: int = RESULT_ROW_LIMIT) -> tuple[pd.DataFrame, float, bool]:
    """在给定连接上执行查询，返回（结果表，耗时毫秒，是否被截断）。

    行数超过 limit 时只取 limit 行并标记截断，避免把巨表整张拖进内存。
    """
    started = time.perf_counter()
    try:
        cursor = conn.execute(sql)
        columns = [desc[0] for desc in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(limit + 1)
    except sqlite3.Error as exc:
        raise QueryError(translate_sqlite_error(exc)) from exc
    elapsed_ms = (time.perf_counter() - started) * 1000
    truncated = len(rows) > limit
    df = pd.DataFrame(rows[:limit], columns=columns)
    return df, elapsed_ms, truncated


def run_query(sql: str, db_path: str | None = None,
              conn: sqlite3.Connection | None = None,
              limit: int = RESULT_ROW_LIMIT) -> tuple[pd.DataFrame, float, bool, bool]:
    """执行用户 SQL 的唯一入口：优先用「临时只读连接」，读完立刻关闭。

    返回值比 execute_query 多一个 readonly 标记，供界面显示“本次查询跑在只读连接上”。

    为什么每次查询新开连接、而不是复用一个只读连接？
    - 用户随时可能追加 CSV，复用的只读连接会读到旧数据（SQLite 快照语义）；
    - 每次新建 + finally 关闭，连接不会跨 Streamlit 线程漂移，也不会有残留锁。

    内存库（纯 CSV 场景）没有文件路径，无法用 mode=ro，退回会话连接——此时该连接
    已被 lock_readonly() 设成 PRAGMA query_only，同样是只读的。
    """
    if db_path and os.path.exists(db_path):
        ro: sqlite3.Connection | None = None
        try:
            ro = open_readonly(db_path)
            df, elapsed, truncated = execute_query(ro, sql, limit)
            return df, elapsed, truncated, True
        finally:
            if ro is not None:
                ro.close()
    if conn is None:
        raise QueryError("没有可用的数据库连接，请先导入数据文件。")
    df, elapsed, truncated = execute_query(conn, sql, limit)
    return df, elapsed, truncated, False
