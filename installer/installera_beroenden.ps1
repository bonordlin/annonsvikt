<#
    installera_beroenden.ps1 — sätter upp körmiljön för Annonsvikt.

    Körs av installeraren efter att filerna kopierats. Kan även köras om från
    Start-menyn ("Reparera Annonsvikt") om något gått fel eller om beroendena
    behöver hämtas på nytt.

    Steg:
      1. Leta efter en användbar Python på datorn
      2. Installera Python om ingen finns
      3. Skapa en egen virtuell miljö i installationsmappen
      4. Installera Playwright och webbläsaren Chromium
      5. Kontrollera att allt faktiskt går att köra

    Avslutar med 0 om allt lyckades, annars ett felnummer.

    OBS: filen måste sparas som UTF-8 MED BOM. Utan BOM läser Windows PowerShell
    5.1 den som ANSI och alla å, ä och ö blir obegripliga i fönstret.
#>

param(
    [Parameter(Mandatory = $true)][string]$InstallDir,
    # Reparera Annonsvikt skickar den här: bygg om miljön även om den ser hel ut.
    [switch]$Tvinga
)

$ErrorActionPreference = 'Stop'
$MinstaPython = [Version]'3.10'
$PythonAttInstallera = '3.12.10'

$Venv = Join-Path $InstallDir 'venv'
$VenvPython = Join-Path $Venv 'Scripts\python.exe'
$AppMapp = Join-Path $InstallDir 'app'
$Logg = Join-Path $InstallDir 'installationslogg.txt'

# Python-kod som skickas med -c får inte innehålla citattecken: PowerShell
# manglar dem på väg till ett externt program. Därför den här stilen.
$KodVersion = 'import sys;print(sys.executable);print(sys.version_info[0]);print(sys.version_info[1])'

try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch {}

function Skriv([string]$Text, [string]$Farg = 'Gray') {
    Write-Host $Text -ForegroundColor $Farg
}

function Rubrik([string]$Text) {
    Write-Host ''
    Write-Host "  $Text" -ForegroundColor Cyan
    Write-Host ('  ' + ('-' * $Text.Length)) -ForegroundColor DarkGray
}

function Avbryt([string]$Text, [int]$Kod) {
    Write-Host ''
    Skriv "  FEL: $Text" Red
    Skriv "  Loggen finns i $Logg" DarkGray
    try { Stop-Transcript | Out-Null } catch {}
    exit $Kod
}

# Kör ett kommando och låt utdata synas, men fånga misslyckanden.
function Kor([string]$Fil, [string[]]$Argument, [string]$Beskrivning) {
    Skriv "  $Beskrivning …" DarkGray
    & $Fil @Argument
    if ($LASTEXITCODE -ne 0) {
        Avbryt "$Beskrivning misslyckades (felkod $LASTEXITCODE)." 3
    }
}

# ── Hittar en Python som duger ───────────────────────────────────────────────
function Provkor([string]$Fil, [string[]]$Forargument) {
    # Genvägen i WindowsApps är ingen riktig Python — den öppnar Microsoft Store.
    if ($Fil -like '*WindowsApps*') { return $null }
    $gammal = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $argument = @()
        if ($Forargument) { $argument += $Forargument }
        $argument += @('-c', $KodVersion)
        $svar = & $Fil @argument 2>$null
        if ($LASTEXITCODE -ne 0 -or -not $svar) { return $null }
        $rader = @($svar)
        if ($rader.Count -lt 3) { return $null }
        $version = [Version]("$($rader[1]).$($rader[2])")
        if ($version -lt $MinstaPython) { return $null }
        return [pscustomobject]@{
            Fil = $Fil; Forargument = $Forargument; Version = $version; Sokvag = $rader[0]
        }
    } catch {
        return $null
    } finally {
        $ErrorActionPreference = $gammal
    }
}

function Hitta-Python {
    $kandidater = @(
        @{ Fil = 'py';     Arg = @('-3') },
        @{ Fil = 'python'; Arg = @() }
    )
    foreach ($rot in @("$env:LOCALAPPDATA\Programs\Python", 'C:\Program Files', 'C:\Program Files (x86)')) {
        foreach ($m in Get-ChildItem $rot -Directory -Filter 'Python*' -ErrorAction SilentlyContinue |
                        Sort-Object Name -Descending) {
            $kandidater += @{ Fil = (Join-Path $m.FullName 'python.exe'); Arg = @() }
        }
    }
    foreach ($k in $kandidater) {
        $traff = Provkor $k.Fil $k.Arg
        if ($traff) { return $traff }
    }
    return $null
}

function Miljon-Duger {
    <#  Duger den befintliga miljön åt den nyss installerade koden? Då behöver
        ingenting byggas om, och en uppdatering tar sekunder i stället för en
        minut. Chromium provas inte här utan i slutkontrollen, som ändå körs.  #>
    if (-not (Test-Path $VenvPython)) { return $false }
    if (-not (Test-Path (Join-Path $Venv 'Scripts\pythonw.exe'))) { return $false }

    $fil = Join-Path $env:TEMP 'annonsvikt_duger.py'
    @(
        'import sys'
        "sys.path.insert(0, r'$AppMapp')"
        'import annonsvikt, annonsvikt_gui, uppdatering, tkinter'
        'import playwright'
    ) | Set-Content -Path $fil -Encoding UTF8

    $gammal = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & $VenvPython $fil 2>$null | Out-Null
    $kod = $LASTEXITCODE
    $ErrorActionPreference = $gammal
    Remove-Item $fil -ErrorAction SilentlyContinue
    return ($kod -eq 0)
}


function Stang-Appen([string]$Mapp) {
    <#  En körande Annonsvikt håller venv\Scripts\pythonw.exe öppen. Då går miljön
        varken att radera eller bygga om, och resultatet blir en trasig mapp.  #>
    $stangda = 0
    foreach ($namn in @('pythonw', 'python')) {
        foreach ($proc in Get-Process -Name $namn -ErrorAction SilentlyContinue) {
            try {
                $sokvag = $proc.Path
            } catch {
                $sokvag = $null
            }
            if ($sokvag -and $sokvag.StartsWith($Mapp, [StringComparison]::OrdinalIgnoreCase)) {
                Skriv "  Stänger Annonsvikt som körs (pid $($proc.Id)) …" DarkGray
                try { $proc.Kill(); $stangda++ } catch {}
            }
        }
    }
    if ($stangda) { Start-Sleep -Seconds 2 }
}


function Installera-Python {
    Rubrik 'Python saknas — installerar'
    Skriv '  Annonsvikt behöver Python för att köra. Det installeras nu.' Gray

    # winget först: snabbt, och håller Python uppdaterat med resten av systemet.
    $winget = Get-Command winget -ErrorAction SilentlyContinue
    if ($winget) {
        Skriv '  Försöker med winget …' DarkGray
        $gammal = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        & winget install -e --id Python.Python.3.12 --scope user `
            --accept-source-agreements --accept-package-agreements --disable-interactivity
        $kod = $LASTEXITCODE
        $ErrorActionPreference = $gammal
        if ($kod -eq 0) {
            $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                        [Environment]::GetEnvironmentVariable('Path', 'Machine')
            $traff = Hitta-Python
            if ($traff) { return $traff }
        }
        Skriv '  winget gick inte hela vägen — hämtar från python.org i stället.' DarkYellow
    }

    $arkitektur = if ([Environment]::Is64BitOperatingSystem) { 'amd64' } else { 'win32' }
    $fil = "python-$PythonAttInstallera-$arkitektur.exe"
    $url = "https://www.python.org/ftp/python/$PythonAttInstallera/$fil"
    $mal = Join-Path $env:TEMP $fil

    Skriv "  Hämtar $url" DarkGray
    try {
        $ProgressPreference = 'SilentlyContinue'
        Invoke-WebRequest -Uri $url -OutFile $mal -UseBasicParsing
    } catch {
        Avbryt ('Kunde inte hämta Python. Kontrollera internetanslutningen. ' +
                'Du kan också installera Python själv från python.org och sedan köra ' +
                'Reparera Annonsvikt på Start-menyn.') 4
    }

    Skriv '  Installerar Python (tyst, bara för din användare) …' DarkGray
    $p = Start-Process -FilePath $mal -Wait -PassThru -ArgumentList @(
        '/quiet', 'InstallAllUsers=0', 'PrependPath=1',
        'Include_launcher=1', 'Include_tcltk=1', 'Include_test=0'
    )
    Remove-Item $mal -ErrorAction SilentlyContinue
    if ($p.ExitCode -ne 0) {
        Avbryt "Python-installationen avslutades med felkod $($p.ExitCode)." 5
    }

    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'Machine')
    $traff = Hitta-Python
    if (-not $traff) {
        Avbryt 'Python installerades men kunde inte hittas efteråt. Starta om datorn och kör Reparera Annonsvikt.' 6
    }
    return $traff
}

# ══════════════════════════════════════════════════════════════════════════════

try { Start-Transcript -Path $Logg -Force | Out-Null } catch {}

Write-Host ''
Write-Host '  Annonsvikt — förbereder körmiljön' -ForegroundColor White
Write-Host '  Det här tar några minuter första gången. Fönstret stängs av sig självt.' -ForegroundColor DarkGray

Rubrik 'Söker efter Python'
$python = Hitta-Python
if ($python) {
    Skriv "  Hittade Python $($python.Version): $($python.Sokvag)" Green
} else {
    $python = Installera-Python
    Skriv "  Python $($python.Version) installerad." Green
}

Rubrik 'Skapar en egen Python-miljö för Annonsvikt'
$HoppaOver = $false
if (-not $Tvinga -and (Miljon-Duger)) {
    Skriv '  Miljön finns redan och fungerar — bygger inte om den.' Green
    $HoppaOver = $true
}

if (-not $HoppaOver -and (Test-Path $Venv)) {
    Skriv '  En tidigare miljö fanns — den ersätts.' DarkGray
    Stang-Appen $InstallDir
    Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue
    if (Test-Path $Venv) {
        # Andra försöket, ifall filerna nyss släppts.
        Start-Sleep -Seconds 3
        Remove-Item $Venv -Recurse -Force -ErrorAction SilentlyContinue
    }
    if (Test-Path $Venv) {
        Avbryt ('Den gamla Python-miljön kunde inte tas bort — någon fil är låst. ' +
                'Stäng Annonsvikt om det är öppet, och kör Reparera Annonsvikt igen. ' +
                'Hjälper inte det, starta om datorn och försök på nytt.') 9
    }
}
if (-not $HoppaOver) {
    $argument = @()
    if ($python.Forargument) { $argument += $python.Forargument }
    $argument += @('-m', 'venv', $Venv)
    Kor $python.Fil $argument 'Skapar miljön'
    if (-not (Test-Path $VenvPython)) {
        Avbryt 'Den virtuella miljön skapades inte som väntat.' 7
    }

    Rubrik 'Installerar Playwright'
    Kor $VenvPython @('-m', 'pip', 'install', '--upgrade', 'pip', '--disable-pip-version-check', '--quiet') 'Uppdaterar pip'
    Kor $VenvPython @('-m', 'pip', 'install', '--disable-pip-version-check', 'playwright') 'Hämtar Playwright'

    Rubrik 'Installerar webbläsaren Chromium'
    Skriv '  Ungefär 150 MB. Finns den redan på datorn går det fort.' DarkGray
    Kor $VenvPython @('-m', 'playwright', 'install', 'chromium') 'Hämtar Chromium'
}

Rubrik 'Kontrollerar att allt fungerar'
# Kontrollen skrivs till en fil i stället för att skickas med -c: PowerShell
# manglar citattecken och radbrytningar i argument till externa program.
$kontrollfil = Join-Path $env:TEMP 'annonsvikt_kontroll.py'
@(
    'import sys, tkinter'
    "sys.path.insert(0, r'$AppMapp')"
    'import annonsvikt, annonsvikt_gui, uppdatering'
    'from playwright.sync_api import sync_playwright'
    'with sync_playwright() as p:'
    '    webblasare = p.chromium.launch()'
    '    webblasare.close()'
    "print('OK')"
) | Set-Content -Path $kontrollfil -Encoding UTF8

Kor $VenvPython @($kontrollfil) 'Provstartar programmet'

# Genvägen startar pythonw.exe, inte python.exe. Går inte den att köra hjälper
# det inte att python.exe fungerar — då blir fönstret öppet men obrukbart.
$VenvPythonw = Join-Path $Venv 'Scripts\pythonw.exe'
if (-not (Test-Path $VenvPythonw)) {
    Avbryt 'pythonw.exe saknas i miljön — genvägen skulle inte fungera.' 10
}
$utfall = Join-Path $env:TEMP 'annonsvikt_pythonw.txt'
Remove-Item $utfall -ErrorAction SilentlyContinue
$kontrollw = Join-Path $env:TEMP 'annonsvikt_kontrollw.py'
@(
    'import sys'
    "sys.path.insert(0, r'$AppMapp')"
    'import annonsvikt, annonsvikt_gui, uppdatering, tkinter'
    'from playwright.sync_api import sync_playwright'
    "open(r'$utfall', 'w').write('OK')"
) | Set-Content -Path $kontrollw -Encoding UTF8
Skriv '  Provstartar så som genvägen gör …' DarkGray
$p = Start-Process -FilePath $VenvPythonw -ArgumentList $kontrollw -Wait -PassThru
Remove-Item $kontrollw -ErrorAction SilentlyContinue
if (-not (Test-Path $utfall)) {
    Avbryt ('Programmet gick inte att starta med pythonw.exe, som genvägen använder. ' +
            'Se loggen för detaljer.') 11
}
Remove-Item $utfall -ErrorAction SilentlyContinue

Remove-Item $kontrollfil -ErrorAction SilentlyContinue

Write-Host ''
Skriv '  Klart. Annonsvikt finns nu på Start-menyn.' Green
Write-Host ''
try { Stop-Transcript | Out-Null } catch {}
exit 0
