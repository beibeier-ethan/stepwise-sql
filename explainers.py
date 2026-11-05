"""explainers.py —— SQL 解析、校验与中文分步讲解（纯函数，不依赖 Streamlit）。

对外只暴露三样东西：
    validate_sql(sql)  -> (是否通过, 错误信息, AST)
    build_steps(sql)   -> 步骤列表 [{"title", "body", "clause"}, ...]
    DIALECT            -> 解析方言

每个 explain_xxx 都是独立函数，只认一个 Select 节点，缺对应子句就返回 None，
想改某一步的文案只改那个函数即可，不会牵动其它步骤。
"""

from __future__ import annotations

import re

import sqlglot
from sqlglot import exp

DIALECT = "sqlite"   # 与执行引擎保持一致

# 二元运算符 → 中文
BINARY_CN = {
    exp.GT: "大于", exp.GTE: "大于等于", exp.LT: "小于", exp.LTE: "小于等于",
    exp.EQ: "等于", exp.NEQ: "不等于", exp.Like: "模糊匹配", exp.ILike: "忽略大小写匹配",
    exp.Add: "加上", exp.Sub: "减去", exp.Mul: "乘以", exp.Div: "除以", exp.Mod: "对…取余",
}
AGG_CN = {exp.Sum: "求和", exp.Avg: "求平均", exp.Min: "取最小值", exp.Max: "取最大值"}


# ------------------------------ 基础设施 ------------------------------
def clean_error(message: str) -> str:
    """去掉 sqlglot 报错里的 ANSI 颜色码，避免回喂模型或显示时出现乱码。"""
    return re.sub(r"\x1b\[[0-9;]*m", "", message).strip()


def extract_sql(text: str) -> str:
    """从模型输出里剥掉 markdown 围栏、前言后话，只留下 SQL。"""
    if not text:
        return ""
    fenced = re.search(r"```(?:sql)?\s*(.+?)```", text, flags=re.S | re.I)
    if fenced:
        text = fenced.group(1)
    match = re.search(r"\b(select|with)\b.*", text, flags=re.S | re.I)
    if match:
        text = match.group(0)
    return text.strip().strip("`").strip().rstrip(";").strip()


def validate_sql(sql: str) -> tuple[bool, str, "exp.Expression | None"]:
    """用 sqlglot 做语法校验，返回（是否通过，错误信息，AST）。"""
    if not sql or not sql.strip():
        return False, "模型没有返回任何 SQL。", None
    try:
        stmt = sqlglot.parse_one(sql, read=DIALECT)
    except sqlglot.errors.SqlglotError as exc:
        return False, f"sqlglot 语法错误：{clean_error(str(exc))}", None
    if stmt is None:
        return False, "sqlglot 无法解析这条语句。", None
    if isinstance(stmt, (exp.Insert, exp.Update, exp.Delete, exp.Create, exp.Drop)):
        return False, "只允许 SELECT 查询，拒绝写操作或 DDL。", stmt
    if stmt.find(exp.Select) is None:
        return False, "这不是一条查询语句（缺少 SELECT）。", stmt
    return True, "", stmt


def _clause(stmt: exp.Select, *names: str):
    """按候选 key 取子句节点，兼容不同 sqlglot 版本（from / from_）。"""
    for name in names:
        node = stmt.args.get(name)
        if node:
            return node
    return None


def _columns(node) -> list[str]:
    """节点里出现的所有列（带表前缀，便于教学时点名）。"""
    return sorted({c.sql(dialect=DIALECT) for c in node.find_all(exp.Column)})


def _literals(node) -> list[str]:
    """节点里出现的所有字面量，字符串保留引号。"""
    out = []
    for lit in node.find_all(exp.Literal):
        out.append(f"'{lit.this}'" if lit.is_string else str(lit.this))
    return out


def cn_expr(node) -> str:
    """把 AST 片段翻成中文读法，用于讲解文案里点名具体的列与值。"""
    if node is None:
        return "（空）"
    if isinstance(node, exp.Column):
        return node.sql(dialect=DIALECT)
    if isinstance(node, exp.Star):
        return "*"
    if isinstance(node, exp.Literal):
        return f"'{node.this}'" if node.is_string else str(node.this)
    if isinstance(node, exp.Null):
        return "NULL"
    if isinstance(node, exp.Boolean):
        return "真" if node.this else "假"
    if isinstance(node, exp.Alias):
        return f"{cn_expr(node.this)}（输出列名 {node.alias}）"
    if isinstance(node, exp.Paren):
        return f"（{cn_expr(node.this)}）"
    if isinstance(node, exp.Not):
        return f"不满足「{cn_expr(node.this)}」"
    if isinstance(node, exp.In):
        values = "、".join(cn_expr(e) for e in node.expressions)
        return f"{cn_expr(node.this)} 属于（{values}）"
    if isinstance(node, exp.Between):
        low = cn_expr(node.args.get("low"))
        high = cn_expr(node.args.get("high"))
        return f"{cn_expr(node.this)} 介于 {low} 和 {high} 之间"
    if isinstance(node, exp.Is):
        return f"{cn_expr(node.this)} 是 {cn_expr(node.expression)}"
    if isinstance(node, exp.Count):
        return f"COUNT({node.this.sql(dialect=DIALECT) if node.this else '*'})"
    for cls, label in AGG_CN.items():
        if isinstance(node, cls):
            col = node.this.sql(dialect=DIALECT) if node.this else "*"
            return f"对 {col} {label}"
    if isinstance(node, exp.And):
        return f"{cn_expr(node.left)} 并且 {cn_expr(node.right)}"
    if isinstance(node, exp.Or):
        return f"{cn_expr(node.left)} 或者 {cn_expr(node.right)}"
    if isinstance(node, exp.Binary):
        label = BINARY_CN.get(type(node))
        if label:
            return f"{cn_expr(node.left)} {label} {cn_expr(node.right)}"
    return node.sql(dialect=DIALECT)


def _step(title: str, body: str, node) -> dict:
    return {"title": title, "body": body, "clause": node.sql(dialect=DIALECT) if node is not None else ""}


# ------------------------------ 各子句讲解 ------------------------------
def explain_from(stmt: exp.Select) -> dict | None:
    """FROM：数据来源表。"""
    from_ = _clause(stmt, "from", "from_")
    if not from_:
        return None
    tables = [from_.this] + list(from_.expressions or [])
    names = []
    for table in tables:
        if isinstance(table, exp.Table):
            suffix = f"（别名 {table.alias}）" if table.alias else ""
            names.append(f"`{table.name}`{suffix}")
        elif isinstance(table, exp.Subquery):
            names.append("一个子查询结果（嵌套写法本阶段暂不细讲）")
    body = (
        f"SQL 的真实执行顺序和你书写的顺序不一样：第一步不是 SELECT，而是先锁定数据来自哪里。"
        f"这里从 {'、'.join(names)}读取全部记录——这一步既不筛选也不计算，可以理解成把整张表先搬进内存。"
        f"如果表带了别名，后面每个子句都要用别名指代它，不能再写原表名。"
    )
    if len(tables) > 1:
        body += "\n\n注意：FROM 后面用逗号并列多张表、却没有 ON 条件时，会产生笛卡尔积（行数相乘），一般都要改成显式 JOIN。"
    return _step("FROM · 先确定数据从哪张表来", body, from_)


def explain_join(stmt: exp.Select) -> dict | None:
    """JOIN：多表拼接。"""
    joins = stmt.args.get("joins") or []
    if not joins:
        return None
    lines = []
    for join in joins:
        table = join.this
        table_name = table.name if isinstance(table, exp.Table) else "（子查询）"
        alias = f"（别名 {table.alias}）" if isinstance(table, exp.Table) and table.alias else ""
        side = (join.args.get("side") or "").upper()
        kind = (join.args.get("kind") or "").upper()
        if side == "LEFT":
            meaning = "左连接：左表的行全部保留，右表对不上的部分填 NULL"
        elif side == "RIGHT":
            meaning = "右连接：右表的行全部保留，左表对不上的部分填 NULL"
        elif side == "FULL":
            meaning = "全连接：两边都保留，缺失侧填 NULL"
        elif kind == "CROSS":
            meaning = "交叉连接：两表行数相乘，不写 ON 条件"
        else:
            meaning = "内连接：只有两边都能匹配上的行才保留，匹配不上就整行丢掉"
        on = join.args.get("on")
        condition = cn_expr(on) if on else "（没有 ON 条件）"
        lines.append(f"- `{table_name}`{alias} —— {meaning}；拼装条件：「{condition}」")
    body = (
        "JOIN 把另一张表的列“接”到当前结果上，靠 ON 后面的条件配对："
        "数据库会为左表的每一行去右表里找满足条件的行，找到就拼成一行新记录。\n\n"
        + "\n".join(lines)
        + "\n\n这一步发生在 WHERE 之前，所以 WHERE 里可以直接用两张表的列。"
    )
    return _step("JOIN · 按条件把多张表拼成一张宽表", body, joins[0])


def explain_where(stmt: exp.Select) -> dict | None:
    """WHERE：逐行过滤。"""
    where = _clause(stmt, "where")
    if not where or where.this is None:
        return None
    node = where.this
    cols = _columns(node)
    vals = _literals(node)
    body = (
        f"WHERE 在 FROM/JOIN 之后、分组聚合之前执行：它一行一行地判断，"
        f"只留下满足「{cn_expr(node)}」的行。\n\n"
        f"用到的列：{'、'.join(f'`{c}`' for c in cols) if cols else '（无）'}；"
        f"参与比较的值：{'、'.join(vals) if vals else '（无字面量）'}。\n\n"
        "因为此时还没分组，条件只能针对“单行自己的列”，不能写 COUNT/SUM 这类聚合函数——"
        "那要等 GROUP BY 之后交给 HAVING。"
    )
    return _step("WHERE · 先把不符合条件的行丢掉", body, where)


def explain_group_by(stmt: exp.Select) -> dict | None:
    """GROUP BY：分组。"""
    group = _clause(stmt, "group")
    if not group or not group.expressions:
        return None
    keys = [cn_expr(e) for e in group.expressions]
    key_sql = "、".join(f"`{e.sql(dialect=DIALECT)}`" for e in group.expressions)
    body = (
        f"GROUP BY 按 {'、'.join(keys)} 把行分桶：这些列取值完全相同的行，被放进同一个组，"
        f"每组最终只输出一行。\n\n"
        f"分组键是 {key_sql}。分完组之后，SELECT 里的 COUNT(*)、SUM(...) 才是“在每个组内部”统计的，"
        f"而不是对整张表统计。"
    )
    return _step("GROUP BY · 按某些列把行分桶", body, group)


def explain_having(stmt: exp.Select) -> dict | None:
    """HAVING：过滤分组。"""
    having = _clause(stmt, "having")
    if not having or having.this is None:
        return None
    node = having.this
    body = (
        f"HAVING 过滤的对象是“组”，不是行：前面的分组已经算出了每组的统计值，"
        f"这里再把不满足「{cn_expr(node)}」的组整组丢掉。\n\n"
        "和 WHERE 的区别一句话记住：WHERE 在分组前筛行，HAVING 在分组后筛组；"
        "所以只有 HAVING 里能写 COUNT/SUM 这类聚合函数。"
    )
    return _step("HAVING · 分组算完再筛掉整组", body, having)


def explain_select(stmt: exp.Select) -> dict | None:
    """SELECT：决定输出哪些列。"""
    projections = list(stmt.expressions or [])
    if not projections:
        return None
    items = []
    for proj in projections:
        alias = proj.alias
        target = proj.this if isinstance(proj, exp.Alias) else proj
        label = f"`{alias}`" if alias else f"`{target.sql(dialect=DIALECT)}`"
        if isinstance(target, exp.Star):
            desc = "通配符，输出这张表的所有列"
        elif isinstance(target, exp.Column):
            desc = "原样输出该列的原始值"
        else:
            desc = cn_expr(target)
        items.append(f"- {label}：{desc}")
    body = (
        "SELECT 决定最终交出哪些列、叫什么名字：\n\n"
        + "\n".join(items)
        + "\n\n它在逻辑执行顺序里其实排在 WHERE、GROUP BY 之后——所以 SELECT 里起的别名，"
        "同一层的 WHERE 一般用不了（SQLite 会宽松放行，换 MySQL/PostgreSQL 就报错了）。"
    )
    step = _step("SELECT · 最后决定输出哪些列", body, None)
    step["clause"] = "SELECT " + ", ".join(p.sql(dialect=DIALECT) for p in projections)
    return step


def explain_order_by(stmt: exp.Select) -> dict | None:
    """ORDER BY：排序。"""
    order = _clause(stmt, "order")
    if not order or not order.expressions:
        return None
    items = []
    for i, ordered in enumerate(order.expressions, 1):
        direction = "从大到小（DESC）" if ordered.args.get("desc") else "从小到大（ASC，默认）"
        items.append(f"{i}. `{ordered.this.sql(dialect=DIALECT)}` {direction}")
    body = (
        "ORDER BY 对最终结果集排序，发生在过滤、分组、投影都算完之后：\n\n"
        + "\n".join(items)
        + "\n\n正因为排序在最后，这里可以直接用 SELECT 里起的别名（比如聚合结果的别名），"
        "SQLite 会先算出结果再按它排。"
    )
    return _step("ORDER BY · 给结果排序", body, order)


def explain_limit(stmt: exp.Select) -> dict | None:
    """LIMIT / OFFSET：截取行数。"""
    limit = _clause(stmt, "limit")
    offset = _clause(stmt, "offset")
    if not limit and not offset:
        return None
    node = limit or offset
    take = limit.expression.sql(dialect=DIALECT) if limit and limit.expression else "（未指定）"
    body = f"LIMIT 是整条 SQL 的最后一步：上面所有计算都完成后，只取前 {take} 行交出去。"
    if offset:
        body += f" 同时设置了 OFFSET {offset.expression.sql(dialect=DIALECT)}，会先跳过这么多行再取。"
    body += "\n\n它不影响计算过程，只影响“输出多少行”——所以想看总数，得单独跑一次 COUNT(*)。"
    return _step("LIMIT · 只取前几行", body, node)


# 固定执行顺序，别随意调整：这是 SQL 的真实求值顺序
EXPLAINER_ORDER = (
    explain_from, explain_join, explain_where, explain_group_by,
    explain_having, explain_select, explain_order_by, explain_limit,
)


def build_steps(sql: str) -> list[dict]:
    """把一条 SQL 变成按执行顺序排列的讲解步骤。扁平 SQL 直接可用，嵌套会退化为单步提示。"""
    try:
        stmt = sqlglot.parse_one(sql, read=DIALECT)
    except sqlglot.errors.SqlglotError as exc:
        return [_step("无法解析", f"sqlglot 解析失败：{clean_error(str(exc))}", None)]

    if stmt is None:
        return [_step("无法解析", "sqlglot 没有返回语法树。", None)]

    if not isinstance(stmt, exp.Select):
        body = (
            "当前只对单条 SELECT 做分步讲解。这条语句是 UNION / CTE 等复合结构，"
            "本阶段先整条看：\n\n"
            f"`{stmt.sql(dialect=DIALECT)}`"
        )
        return [_step("整条 SQL · 暂不支持分步", body, stmt)]

    steps: list[dict] = []
    for explainer in EXPLAINER_ORDER:
        try:
            step = explainer(stmt)
        except Exception:  # noqa: BLE001 - 单个 explainer 出问题不该拖垮整页
            step = None
        if step:
            steps.append(step)

    if not steps:
        steps.append(_step("整条 SQL", "没能从这个查询里提取出可讲解的子句。", stmt))

    for index, step in enumerate(steps, 1):
        step["index"] = index
        step["title"] = f"第 {index} 步 · {step['title']}"
    return steps
