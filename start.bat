@echo off
chcp 65001 >nul
title Reader3 - EPUB 阅读器

REM 检查是否有文件拖拽到此脚本上
where uv >nul 2>nul
if %errorlevel%==0 (
    if "%~1"=="" (
        uv run start.py
    ) else (
        uv run start.py %*
    )
) else (
    if "%~1"=="" (
        py -3 start.py
    ) else (
        py -3 start.py %*
    )
)

pause

