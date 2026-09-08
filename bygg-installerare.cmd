@echo off
rem ══════════════════════════════════════════════════════════════════════════
rem  Bygger dist\AnnonsviktSetup.exe
rem
rem  Kraver Inno Setup 6:  winget install -e --id JRSoftware.InnoSetup
rem  Python behovs bara for att rita om ikonen.
rem ══════════════════════════════════════════════════════════════════════════
setlocal
cd /d "%~dp0"

echo.
echo   Ritar ikonen ...
python verktyg\skapa_ikon.py || (echo   Kunde inte rita ikonen. & exit /b 1)

set "ISCC="
for %%P in (
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do if exist %%P set "ISCC=%%~P"

if not defined ISCC (
    echo.
    echo   Inno Setup hittades inte. Installera det med:
    echo       winget install -e --id JRSoftware.InnoSetup
    echo.
    exit /b 1
)

set "VER="
for /f "delims=" %%V in ('python verktyg\las_version.py') do set "VER=%%V"
if not defined VER (echo   Kunde inte lasa versionsnumret ur annonsvikt.py. & exit /b 1)

echo   Bygger version %VER% med "%ISCC%" ...
echo.
"%ISCC%" /Qp /DVersion=%VER% "installer\annonsvikt.iss" || (echo   Bygget misslyckades. & exit /b 1)

echo.
echo   Klart: dist\AnnonsviktSetup.exe
for %%F in ("dist\AnnonsviktSetup.exe") do echo   Storlek: %%~zF byte
echo.
