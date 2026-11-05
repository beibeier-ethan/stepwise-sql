@echo off
chcp 65001 >nul
title Stepwise SQL

rem ============ 工作目录 ============
rem 默认用「本 bat 所在目录」作为项目目录（把 bat 放在 app_cloud.py 旁边即可）
cd /d "%~dp0"
rem 如果 bat 不想放在项目里，注释掉上面那行，改用下面这行的绝对路径：
rem cd /d "D:\你的项目绝对路径\stepwise-sql"

echo ============================================
echo   Stepwise SQL - 本地 SQL 学习助手
echo ============================================
echo 当前目录：%CD%
echo.

if not exist "app_cloud.py" (
    echo [错误] 当前目录找不到 app_cloud.py。
    echo 请把本文件放到 app_cloud.py 同目录，或右键编辑本文件修改 cd /d 的路径。
    echo.
    pause
    exit /b 1
)

where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 没找到 python。请安装 Python 3.9 以上，并在安装时勾选 "Add python.exe to PATH"。
    echo.
    pause
    exit /b 1
)

python -c "import streamlit, pandas, sqlglot" >nul 2>nul
if errorlevel 1 (
    echo [提示] 缺少依赖，正在安装 streamlit / pandas / sqlglot ...
    python -m pip install -r requirements.txt
    if errorlevel 1 (
        echo [提示] 从 requirements.txt 安装失败，改用镜像源重试 ...
        python -m pip install streamlit pandas sqlglot -i https://pypi.tuna.tsinghua.edu.cn/simple
    )
    echo.
)

if not exist ".env" (
    echo [提示] 还没找到 .env 文件。可以现在创建，也可以在网页右上角临时填 Key。
    echo        做法：把 .env.example 复制一份，重命名为 .env，用记事本填入 OPENAI_API_KEY=sk-xxxx
    echo.
)

echo 正在启动，浏览器会自动打开 http://localhost:8501
echo 关闭本窗口即可停止服务。
echo.
python -m streamlit run app_cloud.py

echo.
echo 服务已退出。若是异常退出，请把上面的报错截图保存下来。
pause
