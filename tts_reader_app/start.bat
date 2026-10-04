@echo off
chcp 65001 >nul
title TTS Book Reader
cd /d "%~dp0"
python tts_reader.py
pause
