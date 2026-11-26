@echo off
REM xhs-heat 每日采集 + 发布（Windows 任务计划调用）
REM 计划时间：每天 16:40（避开盘中与收盘瞬时流量）
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo [%date% %time%] 开始采集 >> logs\collect.log 2>&1
python scripts\collect_xhs.py --push >> logs\collect.log 2>&1
echo [%date% %time%] 退出码 %errorlevel% >> logs\collect.log 2>&1
