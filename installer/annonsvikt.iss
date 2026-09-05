; ═══════════════════════════════════════════════════════════════════════════
;  Annonsvikt — installationsguide för Windows
;
;  Byggs med:  bygg-installerare.cmd
;  Resultat:   dist\AnnonsviktSetup.exe
;
;  Installerar per användare i %LOCALAPPDATA%\Programs\Annonsvikt, så ingen
;  administratörsbehörighet och ingen UAC-fråga behövs. Efter att filerna
;  kopierats körs installera_beroenden.ps1, som letar upp eller installerar
;  Python, skapar en egen miljö och hämtar Playwright med Chromium.
; ═══════════════════════════════════════════════════════════════════════════

#define Namn          "Annonsvikt"
#define Version       "1.0.0"
#define Utgivare      "Upphandling24"
#define Webb          "https://github.com/bonordlin/annonsvikt"
#define Beskrivning   "Mäter vikten på display-annonser"

[Setup]
AppId={{7C4F1A62-9B3E-4D58-A1C7-2E6B0F9D4A31}
AppName={#Namn}
AppVersion={#Version}
AppVerName={#Namn} {#Version}
AppPublisher={#Utgivare}
AppPublisherURL={#Webb}
AppSupportURL={#Webb}
VersionInfoDescription={#Beskrivning}
VersionInfoProductName={#Namn}
VersionInfoVersion={#Version}

; Per användare: inget UAC, inga administratörsrättigheter.
PrivilegesRequired=lowest
DefaultDirName={localappdata}\Programs\{#Namn}
DefaultGroupName={#Namn}
DisableProgramGroupPage=yes
DisableDirPage=auto
UninstallDisplayName={#Namn}
UninstallDisplayIcon={app}\app\annonsvikt.ico

OutputDir=..\dist
OutputBaseFilename=AnnonsviktSetup
SetupIconFile=annonsvikt.ico
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "svenska"; MessagesFile: "compiler:Languages\Swedish.isl"

[CustomMessages]
svenska.SkapaSkrivbordsikon=Skapa en genväg på skrivbordet
svenska.ForberederMiljo=Förbereder Python-miljön och hämtar webbläsaren. Det tar några minuter.
svenska.StartaEfterat=Starta {#Namn} nu
svenska.OppnaLogg=Visa installationsloggen
svenska.BeroendenMisslyckades=Programfilerna är installerade, men körmiljön kunde inte förberedas.%n%nAnnonsvikt startar inte förrän det är åtgärdat. Vanligaste orsaken är att internetanslutningen bröts under installationen.%n%nDu kan försöka igen med "Reparera Annonsvikt" på Start-menyn. Loggen finns i:%n%1
svenska.PowerShellSaknas=PowerShell hittades inte på datorn, så beroendena kunde inte installeras automatiskt.

[Tasks]
Name: "skrivbordsikon"; Description: "{cm:SkapaSkrivbordsikon}"; GroupDescription: "{cm:AdditionalIcons}"

[Files]
Source: "..\annonsvikt.py";          DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\annonsvikt_gui.py";      DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\README.md";              DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\requirements.txt";       DestDir: "{app}\app"; Flags: ignoreversion
Source: "..\test-tvaannonser.html";  DestDir: "{app}\app"; Flags: ignoreversion
Source: "annonsvikt.ico";            DestDir: "{app}\app"; Flags: ignoreversion
Source: "installera_beroenden.ps1";  DestDir: "{app}";     Flags: ignoreversion
Source: "Annonsvikt.cmd";            DestDir: "{app}";     Flags: ignoreversion

[Icons]
; Huvudgenvägen pekar rakt på miljöns pythonw.exe — inget konsolfönster blinkar förbi.
Name: "{group}\{#Namn}"; Filename: "{app}\venv\Scripts\pythonw.exe"; \
      Parameters: """{app}\app\annonsvikt_gui.py"""; WorkingDir: "{app}\app"; \
      IconFilename: "{app}\app\annonsvikt.ico"; Comment: "{#Beskrivning}"
Name: "{autodesktop}\{#Namn}"; Filename: "{app}\venv\Scripts\pythonw.exe"; \
      Parameters: """{app}\app\annonsvikt_gui.py"""; WorkingDir: "{app}\app"; \
      IconFilename: "{app}\app\annonsvikt.ico"; Comment: "{#Beskrivning}"; \
      Tasks: skrivbordsikon
Name: "{group}\{#Namn} på kommandoraden"; Filename: "{app}\Annonsvikt.cmd"; \
      WorkingDir: "{app}\app"; IconFilename: "{app}\app\annonsvikt.ico"; \
      Comment: "Öppnar ett fönster där kommandot annonsvikt kan köras"
Name: "{group}\Reparera {#Namn}"; Filename: "powershell.exe"; \
      Parameters: "-NoProfile -ExecutionPolicy Bypass -File ""{app}\installera_beroenden.ps1"" -InstallDir ""{app}"""; \
      WorkingDir: "{app}"; IconFilename: "{app}\app\annonsvikt.ico"; \
      Comment: "Installerar om Python-miljön och webbläsaren"

[Run]
Filename: "{app}\venv\Scripts\pythonw.exe"; Parameters: """{app}\app\annonsvikt_gui.py"""; \
      WorkingDir: "{app}\app"; Description: "{cm:StartaEfterat}"; \
      Flags: nowait postinstall skipifsilent

[UninstallDelete]
Type: filesandordirs; Name: "{app}\venv"
Type: filesandordirs; Name: "{app}\app\__pycache__"
Type: files;          Name: "{app}\installationslogg.txt"

[Code]
var
  BeroendenOk: Boolean;

function PowerShellSokvag(): String;
begin
  Result := ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe');
  if not FileExists(Result) then
    Result := 'powershell.exe';
end;

{ Kör beroendeinstallationen med synligt fönster, så att användaren ser att något
  händer under de minuter det tar att hämta Chromium. }
procedure ForberedMiljon();
var
  Kod: Integer;
  Logg: String;
begin
  { Vid tyst installation finns ingen synlig guide att skriva i. }
  if WizardForm <> nil then
  begin
    WizardForm.StatusLabel.Caption := ExpandConstant('{cm:ForberederMiljo}');
    WizardForm.ProgressGauge.Style := npbstMarquee;
  end;

  if not Exec(PowerShellSokvag(),
      '-NoProfile -ExecutionPolicy Bypass -File "' + ExpandConstant('{app}\installera_beroenden.ps1') +
      '" -InstallDir "' + ExpandConstant('{app}') + '"',
      ExpandConstant('{app}'), SW_SHOW, ewWaitUntilTerminated, Kod) then
  begin
    MsgBox(ExpandConstant('{cm:PowerShellSaknas}'), mbError, MB_OK);
    BeroendenOk := False;
    Exit;
  end;

  BeroendenOk := (Kod = 0);
  if not BeroendenOk then
  begin
    Logg := ExpandConstant('{app}\installationslogg.txt');
    MsgBox(FmtMessage(ExpandConstant('{cm:BeroendenMisslyckades}'), [Logg]), mbError, MB_OK);
  end;
  if WizardForm <> nil then
    WizardForm.ProgressGauge.Style := npbstNormal;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if CurStep = ssPostInstall then
    ForberedMiljon();
end;

{ Lyckades inte miljöbygget vore det oärligt att erbjuda "starta programmet nu". }
function ShouldSkipPage(PageID: Integer): Boolean;
begin
  Result := False;
end;

procedure CurPageChanged(CurPageID: Integer);
begin
  if (CurPageID = wpFinished) and (not BeroendenOk) then
    WizardForm.RunList.Visible := False;
end;
