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
echo   Skriver manifestet som appen laser for att hitta nya versioner ...
python verktyg\skapa_manifest.py || (echo   Manifestet kunde inte skrivas. & exit /b 1)

echo.
echo   Klart: dist\AnnonsviktSetup.exe
for %%F in ("dist\AnnonsviktSetup.exe") do echo   Storlek: %%~zF byte

rem  Publicera bara pa uttrycklig begaran: ett bygge under utveckling ska
rem  inte raka lagga upp nagot publikt.
if /I not "%~1"=="publicera" (
    echo.
    echo   Ge ut versionen med:  bygg-installerare.cmd publicera
    echo   Utan gh gar det ocksa for hand pa github.com/bonordlin/annonsvikt/releases/new
    echo.
    exit /b 0
)

echo.
echo   Ger ut version %VER% pa GitHub ...

where gh >nul 2>&1 || (
    echo   gh saknas. Installera med:  winget install -e --id GitHub.cli
    exit /b 1
)

gh auth status >nul 2>&1 || (
    echo   gh ar inte inloggat. Kor:  gh auth login
    exit /b 1
)

gh release view v%VER% >nul 2>&1 && (
    echo   Releasen v%VER% finns redan. Hoj VERSION i annonsvikt.py forst.
    exit /b 1
)

gh release create v%VER% "dist\AnnonsviktSetup.exe" "dist\version.json" ^
    --title "Annonsvikt %VER%" --notes-file "dist\noter.md" || (
    echo   Releasen kunde inte skapas.
    exit /b 1
)

echo.
echo   Utgiven. Alla installationer upptackar den inom ett dygn.
echo.
