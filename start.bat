@echo off
chcp 65001 >nul
title Reader3 - EPUB 阅读器

REM 检查是否有文件拖拽到此脚本上
if "%~1"=="" (
    REM 没有参数，启动交互模式
    uv run start.py
) else (
    REM 有文件参数，处理所有拖拽的文件
    uv run start.py %*
)

pause

