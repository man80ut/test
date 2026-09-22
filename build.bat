@echo off
chcp 65001 >nul
cd /d %~dp0

echo [1/3] 准备虚拟环境...
if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
)
".venv\Scripts\python.exe" -m pip install -q --upgrade pip
".venv\Scripts\python.exe" -m pip install -q -r requirements.txt pyinstaller
if errorlevel 1 goto fail

echo [2/3] 清理旧产物...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

echo [3/3] 打包中（约 1-3 分钟）...
".venv\Scripts\pyinstaller.exe" video-studio.spec --noconfirm
if errorlevel 1 goto fail

copy /y ".env.example" "dist\.env.example" >nul
echo.
echo 完成：dist\VideoStudio.exe
echo 双击运行后浏览器会自动打开 http://127.0.0.1:8000
goto :eof

:fail
echo 打包失败，请查看上方错误信息。
pause
