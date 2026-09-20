@echo off
cd /d %~dp0client
pip install -r ..\requirements-client.txt
python main.py
