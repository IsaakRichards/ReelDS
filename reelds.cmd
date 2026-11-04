@echo off
rem ReelDS encoder launcher for Windows (x64; ARM64 Windows runs it through emulation).
rem Unpacks the bundled Python + numpy + Pillow + ffmpeg from vendor\ into .runtime\ on
rem first run, then runs pc\reelds.py with your arguments.
rem   reelds build "D:\Videos" --out E:\
rem   reelds selftest
rem   reelds                (or double-click) opens the GUI
setlocal
set "ROOT=%~dp0"
set "PLAT=windows-x64"
set "VENDOR=%ROOT%vendor\%PLAT%"
set "RT=%ROOT%.runtime\%PLAT%"
set "PY=%RT%\python\python.exe"

if not exist "%VENDOR%\VERSION" (
    echo reelds: vendor\%PLAT% is missing - re-download the repo or install Python and ffmpeg.
    exit /b 1
)
set /p VER=<"%VENDOR%\VERSION"

if exist "%RT%\.ok-%VER%" goto run
echo reelds: first run on %PLAT% - unpacking bundled Python, numpy, Pillow and ffmpeg...
if exist "%RT%" rmdir /s /q "%RT%"
mkdir "%RT%"
where tar >nul 2>nul || goto notar
tar -xzf "%VENDOR%\python.tar.gz" -C "%RT%" || goto fail
"%PY%" -m pip install --no-index --find-links "%VENDOR%\wheels" --disable-pip-version-check --no-warn-script-location -q numpy pillow imageio-ffmpeg || goto fail
type nul > "%RT%\.ok-%VER%"
echo reelds: ready.

:run
if "%~1"=="" (
    rem double-clicked: open the GUI without keeping a console window around
    start "" "%RT%\python\pythonw.exe" "%ROOT%pc\reelds.py" gui
    exit /b 0
)
"%PY%" "%ROOT%pc\reelds.py" %*
exit /b %ERRORLEVEL%

:notar
echo reelds: this Windows has no tar.exe - Windows 10 1803 or newer is required.
exit /b 1

:fail
echo reelds: setup failed. Delete the .runtime folder and try again.
exit /b 1
