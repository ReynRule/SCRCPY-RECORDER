@echo off
echo Building Android Recorder.exe with embedded scrcpy...
echo.

REM Requires: Python + PyQt6 + PyInstaller installed
REM pip install PyQt6 pyinstaller

if not exist scrcpy\scrcpy.exe (
    echo ERROR: scrcpy folder not found next to this script!
    pause
    exit /b 1
)

if not exist "ffmpeg\bin\ffmpeg.exe" (
    echo ERROR: ffmpeg\bin\ffmpeg.exe not found!
    pause
    exit /b 1 
)

pyinstaller --noconfirm --onefile --windowed ^
    --name "Android Recorder" ^
    --add-data "scrcpy;scrcpy" ^
    --add-data "ffmpeg;ffmpeg" ^
    --hidden-import PyQt6.QtCore ^
    --hidden-import PyQt6.QtWidgets ^
    --hidden-import PyQt6.QtGui ^
    ADBRecorder.py

if exist dist\ADBRecorder.exe (
    echo.
    echo SUCCESS!  dist\Android Recorder.exe  created.
    echo scrcpy is embedded inside the exe.
) else (
    echo Build failed.
)
pause
