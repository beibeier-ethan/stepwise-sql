"""app_cloud.py —— Stepwise SQL 的入口与界面：数据窗口 / 解析窗口 / AI 窗口。

启动：python -m streamlit run app_cloud.py（Windows 可双击同目录「启动StepwiseSQL.bat」）
依赖：streamlit、pandas、sqlglot + 标准库（sqlite3/urllib/os/pathlib），无其它第三方包。
分工：data_layer.py 管数据库（含只读保护），explainers.py 管 SQL 解析与讲解，本文件放环境引导、API 调用和 UI。
密钥：优先读根目录 .env（零依赖解析），弹窗里可临时输入覆盖。
设计：受 Python Tutor 启发，用 AST 把 SQL 拆成可逐步回放的执行阶段，而不是只给一句答案。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from pathlib import Path

import pandas as pd
import streamlit as st

import data_layer as dl
from explainers import build_steps, extract_sql, validate_sql

# ============================ 项目文件与 .env 引导 ============================
APP_DIR = Path(__file__).resolve().parent
ENV_FILE = APP_DIR / ".env"
ENV_EXAMPLE = APP_DIR / ".env.example"
GITIGNORE = APP_DIR / ".gitignore"
GITIGNORE_REQUIRED = (".env", "*.db", "*.sqlite", "*.sqlite3", "__pycache__/")


def load_env(path: Path = ENV_FILE) -> dict[str, str]:
    """极简 .env 读取（零依赖）：跳过空行与 # 注释，按第一个 = 拆分，注入 os.environ。

    返回文件里声明的键值（即使因同名系统环境变量已存在而没注入），供界面报告来源。
    真·环境变量优先于文件，与 python-dotenv 默认行为一致。
    """
    values: dict[str, str] = {}
    try:
        text = path.read_text(encoding="utf-8-sig", errors="ignore")
    except OSError:                              # 文件不存在或没权限：静默跳过
        return values
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.lower().startswith("export "):   # 兼容 shell 风格的写法
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not key:
            continue
        if len(value) > 1 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]                  # OPENAI_API_KEY="sk-xxx" 去掉成对引号
        values[key] = value
        if not os.environ.get(key):
            os.environ[key] = value
    return values


def ensure_gitignore(path: Path = GITIGNORE) -> list[str]:
    """确保 .gitignore 存在且含必要条目，返回本次补齐的条目；已齐全则不动（幂等）。"""
    try:
        text = path.read_text(encoding="utf-8", errors="ignore") if path.exists() else ""
        lines = {line.strip() for line in text.splitlines()}
        missing = [item for item in GITIGNORE_REQUIRED if item not in lines]
        if missing:
            sep = "" if not text or text.endswith("\n") else "\n"
            path.write_text(text + sep + "\n".join(missing) + "\n", encoding="utf-8")
        return missing
    except OSError:                              # 只读目录等问题不该拦住应用启动
        return []


def ensure_env_example(path: Path = ENV_EXAMPLE) -> bool:
    """没有 .env.example 就生成模板，别人 clone 后照着建 .env 即可。"""
    if path.exists():
        return False
    try:
        path.write_text("OPENAI_API_KEY=your_api_key_here\n", encoding="utf-8")
        return True
    except OSError:
        return False


DOTENV = load_env()                    # 启动第一件事就读 .env；改文件后重启服务生效
GITIGNORE_ADDED = ensure_gitignore()   # 防泄露底线：.env / 数据库 / 缓存不进版本库
ENV_EXAMPLE_CREATED = ensure_env_example()

# ============================ 可配置常量区 ============================
APP_TITLE = "Stepwise SQL"
API_KEY = os.environ.get("OPENAI_API_KEY", "").strip()   # 来自 .env 或系统环境变量，绝不写死
BASE_URL = "https://api.deepseek.com/v1"         # OpenAI 兼容接口前缀（不同服务商不同）
MODEL_NAME = "deepseek-chat"                     # 模型名，随服务商变化
API_TIMEOUT = 120                                # 单次请求超时（秒）
PREVIEW_ROWS = dl.PREVIEW_ROWS                   # 数据预览行数
SQL_RETRY_TIMES = 2                              # sqlglot 校验失败后的最大重试次数
UPLOAD_SUFFIXES = ["sqlite", "sqlite3", "db", "csv", "sql"]

SYSTEM_PROMPT = (
    "你是一名 SQLite SQL 专家。根据给定的数据库结构，把用户的自然语言需求翻译成一条 SQLite 查询语句。\n"
    "用户可能用中文、英文或其他语言描述需求，请先理解语义再翻译成 SQL，不要因为语言不同而拒答。\n"
    "硬性要求：\n"
    "1. 只输出 SQL 本身，不要解释、不要 markdown 代码块、不要分号结尾。\n"
    "2. 只能使用结构中出现的表名和列名，不要编造字段。\n"
    "3. 使用 SQLite 支持的语法（字符串用单引号，日期用字符串比较）。\n"
    "4. 只写一条 SELECT 语句，不要写 INSERT/UPDATE/DELETE/DDL。"
)  # 想要更精简的输出，改这一段就够了
# =====================================================================


# ------------------------------ 会话状态 ------------------------------
def init_state() -> None:
    for key, default in (
        ("conn", None), ("db_path", None), ("source", ""), ("loaded_key", None), ("import_log", []),
        ("api_key", API_KEY), ("nl2sql", None), ("api_error", None),
        ("explain_steps", []), ("step_pos", 1), ("query_result", None),
    ):
        st.session_state.setdefault(key, default)


def reset_database() -> None:
    dl.close_connection(st.session_state.get("conn"), st.session_state.get("db_path"))
    st.session_state.update(conn=None, db_path=None, source="", import_log=[], nl2sql=None,
                            api_error=None, explain_steps=[], step_pos=1, query_result=None)


# ------------------------------ 顶部：标题 + API Key 弹窗 ------------------------------
def render_header() -> str:
    """标题与右上角的 ⚙️ 弹窗；Key 只在内存与 Session State 里，不回显、不落盘。"""
    title_col, key_col = st.columns([6, 1])
    with title_col:
        st.title(APP_TITLE)
        st.caption("导入数据 → 自然语言生成 SQL → sqlglot 校验（失败自动重试）→ 执行 → 按执行顺序分步讲解")
    with key_col:
        st.write("")
        return render_api_key_popover()


def render_api_key_popover() -> str:
    """弹窗只做「临时覆盖」；永久配置走根目录 .env，这里顺手报告加载状态。"""
    with st.popover("⚙️ 配置 API Key"):
        st.caption(f"接口 `{BASE_URL}` · 模型 `{MODEL_NAME}`")
        typed = st.text_input("API Key", type="password", key="api_key_field",
                              placeholder="留空则使用 .env 里的 Key",
                              help="只保存在当前浏览器会话，仅临时覆盖 .env")
        manual = typed.strip()
        key = manual or API_KEY
        if manual:
            st.success("✅ 本次会话使用手动输入的 Key（临时覆盖，关闭页面即失效）")
        elif DOTENV.get("OPENAI_API_KEY") and key == DOTENV["OPENAI_API_KEY"]:
            st.success("✅ 已从本地 .env 文件自动加载密钥")
        elif key:
            st.success("✅ 已从系统环境变量读取 OPENAI_API_KEY")
        else:
            st.error("还没有 Key：按下面的说明建一个 .env，或直接在上面输入。")
        if GITIGNORE_ADDED:
            st.caption("🧷 已自动补齐 `.gitignore`：" + "、".join(GITIGNORE_ADDED))
        if ENV_EXAMPLE_CREATED:
            st.caption("📄 已生成模板 `.env.example`，可安全提交到 GitHub")
        st.caption("💡 永久配置：在根目录创建 .env 文件，写入 OPENAI_API_KEY=sk-xxxx（此文件已被自动加入 .gitignore，不会上传至 GitHub）")
    st.session_state["api_key"] = key
    return key


# ------------------------------ 云端 API（OpenAI 兼容） ------------------------------
def friendly_http_error(exc: urllib.error.HTTPError, url: str) -> str:
    """把常见状态码翻译成能直接照做的中文提示，附服务端返回片段。"""
    try:
        body = exc.read().decode("utf-8", errors="ignore")[:300].replace("\n", " ")
    except Exception:  # noqa: BLE001 - 读流失败时不该盖住原始错误
        body = "（读不到响应体）"
    hints = {400: "请求格式被拒绝，检查 messages 结构或模型名拼写",
             401: "API Key 无效或已过期，核对 Key 与 BASE_URL 是否同一家服务商",
             402: "账户余额不足，请充值后再试", 403: "无权限访问该模型（未开通/欠费/地区受限）",
             404: f"接口路径不存在，检查 BASE_URL 是否含 /v1（当前请求 {url}）",
             422: "参数不被接受，确认 MODEL_NAME 被该服务商支持",
             429: "请求太频繁或超配额，等几秒再试，或换更便宜的模型",
             500: "服务端内部错误，稍后重试", 502: "网关错误，稍后重试"}
    return f"HTTP {exc.code} · {hints.get(exc.code, '未知错误')}\n\n服务端返回：{body}"


def call_chat_api(messages: list[dict], api_key: str, model: str = MODEL_NAME,
                  base_url: str = BASE_URL, timeout: int = API_TIMEOUT) -> str:
    """标准 OpenAI Chat Completions，用 urllib 发 POST，零额外依赖。"""
    if not api_key.strip():
        raise RuntimeError("缺少 API Key：请在根目录 .env 里配置 OPENAI_API_KEY，或在右上角弹窗临时输入。")
    url = base_url.rstrip("/") + "/chat/completions"
    payload = json.dumps({"model": model, "messages": messages, "temperature": 0,
                          "stream": False}, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json",
        "Authorization": f"Bearer {api_key.strip()}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
    except urllib.error.HTTPError as exc:      # 必须先于 URLError 捕获：HTTPError 是它的子类
        raise RuntimeError(friendly_http_error(exc, url)) from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        kind = "请求超时" if isinstance(reason, TimeoutError) or "timed out" in str(reason).lower() else "连不上"
        raise RuntimeError(f"{kind}（{url}）：{reason}\n\n检查网络/代理、BASE_URL，或调大常量 API_TIMEOUT。") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"返回的不是 JSON（可能被网关或代理拦截）：{raw[:200]}") from exc
    choices = data.get("choices") or []
    if not choices:
        error = data.get("error")
        detail = error.get("message") if isinstance(error, dict) else error
        raise RuntimeError(f"接口没有返回 choices：{detail or json.dumps(data, ensure_ascii=False)[:300]}")
    content = (choices[0].get("message") or {}).get("content") or ""
    if not content.strip():
        raise RuntimeError(f"模型返回空内容（finish_reason={choices[0].get('finish_reason')}），请重试或换模型。")
    return content.strip()


# ------------------------------ 自然语言 → SQL ------------------------------
def generate_sql(conn, question: str, api_key: str) -> dict:
    """自然语言 → SQL → 校验 → 失败回喂报错重试，最多重试 SQL_RETRY_TIMES 次。"""
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"{dl.build_schema_prompt(conn)}\n\n用户需求：{question}"},
    ]
    attempts: list[dict] = []
    sql = ""
    for index in range(SQL_RETRY_TIMES + 1):
        sql = extract_sql(call_chat_api(messages, api_key))
        ok, error, stmt = validate_sql(sql)
        attempts.append({"round": index + 1, "sql": sql, "ok": ok, "error": error})
        if ok:
            return {"ok": True, "sql": sql, "ast": stmt, "attempts": attempts}
        messages += [
            {"role": "assistant", "content": sql or "（空）"},
            {"role": "user", "content": (f"上面的 SQL 没通过 sqlglot 校验，报错是：{error}\n"
                                         "请修正后只输出一条可执行的 SQLite SELECT 语句。")},
        ]
    return {"ok": False, "sql": sql, "ast": None, "attempts": attempts}


# ------------------------------ 左侧：数据窗口 ------------------------------
def render_data_panel():
    files = st.file_uploader(
        "导入数据文件（可多选）", type=UPLOAD_SUFFIXES, accept_multiple_files=True,
        help=".sqlite/.db 直接打开（副本 + 只读查询）；.csv 写入当前库；.sql 用 executescript 执行",
    )
    upload_key = tuple((f.name, f.size) for f in files) if files else ()
    if files and st.session_state["loaded_key"] != upload_key:
        build_database(files)
        st.session_state["loaded_key"] = upload_key
    elif not files and st.session_state["loaded_key"]:
        reset_database()
        st.session_state["loaded_key"] = None

    conn = st.session_state.get("conn")
    if conn is None:
        st.info("请先上传一个 .sqlite / .db 文件，或上传 CSV 建表。数据库始终在本机，只有 schema 会随需求发给模型。")
        return None

    st.caption(f"当前数据源：**{st.session_state['source']}** · 🔒 导入完成后连接已锁定为只读")
    with st.expander("导入日志", expanded=False):
        for line in st.session_state.get("import_log") or ["（无）"]:
            st.write("- " + line)

    tables = dl.list_tables(conn)
    if not tables:
        st.warning("这个数据库里没有用户表，可能是一份空库或只含视图。")
        return conn

    st.markdown(f"**表清单**（共 {len(tables)} 张）")
    table = st.selectbox("选择表", tables, key="table_picker")
    cols = dl.table_columns(conn, table)
    st.markdown(f"**{table}** · 共 {len(cols)} 列 · {dl.count_rows(conn, table)} 行")
    st.dataframe(cols, hide_index=True, use_container_width=True, height=dl.grid_height(len(cols)))
    st.markdown(f"**前 {PREVIEW_ROWS} 行预览**")
    try:
        preview = dl.preview_table(conn, table)
        st.dataframe(preview, hide_index=True, use_container_width=True, height=dl.grid_height(len(preview)))
    except pd.errors.DatabaseError as exc:  # pragma: no cover - 防御性
        st.error(f"预览失败：{exc}")
    return conn


def build_database(files) -> None:
    """按「先 SQLite 文件、后 CSV/SQL」的顺序建库，最后把工作连接锁成只读。"""
    reset_database()
    log: list[str] = []
    conn, source = None, ""
    sqlite_files = [f for f in files if os.path.splitext(f.name)[1].lower() in dl.SQLITE_SUFFIXES]

    if sqlite_files:
        first = sqlite_files[0]
        conn, path, source = dl.open_sqlite_file(first.name, first.getvalue())
        st.session_state["db_path"] = path
        log.append(f"已打开 SQLite 数据库：{first.name}（复制为临时副本，原文件不会被写入）")
        for extra in sqlite_files[1:]:
            log.append(f"已跳过 {extra.name}：当前只支持一个 SQLite 数据库")

    for f in [f for f in files if f not in sqlite_files]:
        suffix = os.path.splitext(f.name)[1].lower()
        if suffix in (".csv", ".sql"):
            if conn is None:
                conn, _, source = dl.open_memory_db()
            if suffix == ".csv":
                table, rows = dl.import_csv(conn, f.name, f.getvalue())
                log.append(f"已导入 CSV {f.name} → 表 {table}（{rows} 行）")
            else:
                total = dl.run_sql_script(conn, f.getvalue())
                log.append(f"已执行脚本 {f.name}（执行后共 {total} 张表）")
        else:
            log.append(f"已跳过 {f.name}：不支持的文件类型")

    if conn is not None:
        dl.lock_readonly(conn)          # 导入结束即上锁，之后任何写操作都会被 SQLite 拒绝
        log.append("已把工作连接设为只读（PRAGMA query_only = ON）")
    st.session_state.update(conn=conn, source=source, import_log=log)


# ------------------------------ 右侧上：解析窗口 ------------------------------
def render_explain_panel() -> None:
    with st.container(border=True):
        st.subheader("🧩 解析窗口")
        steps = st.session_state.get("explain_steps") or []
        if not steps:
            st.caption("生成一条 SQL 后，这里会按执行顺序给出分步讲解，可拖动进度条逐步看。")
            st.text_area("SQL 分步讲解", value="（等待生成 SQL）", height=170, disabled=True)
            return
        if len(steps) > 1:
            if st.session_state.get("step_pos", 1) > len(steps):
                st.session_state["step_pos"] = 1
            pos = st.slider("执行进度", min_value=1, max_value=len(steps), key="step_pos")
        else:
            pos = 1
        step = steps[pos - 1]
        st.markdown(f"#### {step['title']}")
        st.markdown(step["body"])
        if step.get("clause"):
            st.code(step["clause"], language="sql")
        st.caption(f"第 {pos} / {len(steps)} 步 · 执行顺序：FROM/JOIN → WHERE → GROUP BY → HAVING → SELECT → ORDER BY → LIMIT")


# ------------------------------ 右侧下：AI 窗口 ------------------------------
def render_ai_panel(conn, api_key: str) -> None:
    with st.container(border=True):
        st.subheader("🤖 AI 窗口")
        st.caption(f"云端模型 `{MODEL_NAME}` · Key {'已就绪' if api_key else '未配置'}（右上角 ⚙️ 可改）")
        question = st.text_area(
            "描述你的查询需求 (Describe your query requirement)", key="question", height=90,
            placeholder="例如：找出金额大于 100 的订单 / e.g., Find orders with amount > 100",
        )
        if st.button("生成 SQL", type="primary", use_container_width=True):
            if not question.strip():
                st.warning("先写一句需求 / Please describe what you want to query，"
                           "比如「找出金额大于 100 的订单」/「Find orders with amount > 100」。")
            elif conn is None:
                st.warning("请先在左侧导入数据文件。")
            elif not api_key:
                st.error("请先在右上角配置 API Key")
            else:
                with st.spinner(f"{MODEL_NAME} 正在生成…"):
                    try:
                        result, error = generate_sql(conn, question.strip(), api_key), None
                    except RuntimeError as exc:
                        result, error = None, str(exc)
                st.session_state["nl2sql"] = result
                st.session_state["api_error"] = error
                st.session_state["explain_steps"] = build_steps(result["sql"]) if result and result["ok"] else []
                st.session_state["step_pos"] = 1
                st.session_state["query_result"] = None
                st.rerun()   # 让上方解析窗口立刻拿到新步骤

        with st.expander("🔍 查看即将发送给云端的内容", expanded=False):
            st.code(dl.build_schema_prompt(conn) if conn is not None else "（还没导入数据）", language="text")
            st.caption("仅发送表结构，不发送具体数据行；如需处理涉密数据，请改用本地模型。")

        if st.session_state.get("api_error"):
            st.error(st.session_state["api_error"])
        result = st.session_state.get("nl2sql")
        if result:
            render_sql_result(result, conn)


def render_sql_result(result: dict, conn) -> None:
    """展示 SQL、执行按钮与执行结果。"""
    rounds = len(result["attempts"])
    if result["ok"]:
        st.success(f"✅ 已通过 sqlglot 语法校验（共调用接口 {rounds} 次）")
    else:
        st.error(f"❌ 重试 {rounds} 次后仍未通过校验，请换一种说法或换更强的模型。")
    st.code(result["sql"] or "（空）", language="sql")

    if result["ok"] and conn is not None:
        if st.button("执行查询", use_container_width=True):
            try:
                df, elapsed, truncated, readonly = dl.run_query(
                    result["sql"], db_path=st.session_state.get("db_path"), conn=conn)
                st.session_state["query_result"] = {"sql": result["sql"], "df": df, "elapsed": elapsed,
                                                    "truncated": truncated, "readonly": readonly}
            except dl.QueryError as exc:
                st.session_state["query_result"] = {"sql": result["sql"], "error": str(exc)}
        shown = st.session_state.get("query_result")
        if shown and shown.get("sql") == result["sql"]:
            st.markdown("**执行结果**")
            if shown.get("error"):
                st.error(shown["error"])
            else:
                df = shown["df"]
                mode = "只读连接" if shown.get("readonly") else "内存库只读模式"
                note = f"（结果已截断，仅显示前 {len(df)} 行）" if shown.get("truncated") else ""
                st.caption(f"共 {len(df)} 行 · 耗时 {shown['elapsed']:.1f} ms · {mode}{note}")
                if df.empty:
                    st.info("查询执行成功，但没有命中任何行。")
                else:
                    st.dataframe(df, hide_index=True, use_container_width=True, height=dl.grid_height(len(df)))

    with st.expander(f"重试过程（{rounds} 轮）", expanded=not result["ok"]):
        for item in result["attempts"]:
            head = "第 1 轮" if item["round"] == 1 else f"第 {item['round']} 轮 · 已收到上一轮报错"
            st.markdown(f"**{head}** — " + ("校验通过" if item["ok"] else f"校验失败：{item['error']}"))
            st.code(item["sql"] or "（空）", language="sql")


# ------------------------------ 入口 ------------------------------
def main() -> None:
    st.set_page_config(page_title=APP_TITLE, layout="wide")
    init_state()
    api_key = render_header()
    left, right = st.columns([1.05, 1], gap="large")
    with left:
        conn = render_data_panel()
    with right:
        render_explain_panel()
        render_ai_panel(conn, api_key)


main()
