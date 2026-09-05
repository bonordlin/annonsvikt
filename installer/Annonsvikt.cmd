@echo off
rem  Öppnar ett kommandofönster med Annonsvikts egen Python-miljö på PATH,
rem  så att kommandot "annonsvikt" går att köra direkt.
setlocal
set "ROT=%~dp0"
set "VENV=%ROT%venv\Scripts"

if not exist "%VENV%\python.exe" (
    echo.
    echo   Python-miljon saknas. Kor "Reparera Annonsvikt" pa Start-menyn forst.
    echo.
    pause
    exit /b 1
)

set "PATH=%VENV%;%PATH%"
doskey annonsvikt="%VENV%\python.exe" "%ROT%app\annonsvikt.py" $*

echo.
echo   Annonsvikt - kommandoraden
echo.
echo   Exempel:
echo     annonsvikt b990849fd8c4b
echo     annonsvikt upphandling24.se --varv 3 --html sida.html
echo     annonsvikt --help
echo.

cd /d "%ROT%app"
cmd /k
