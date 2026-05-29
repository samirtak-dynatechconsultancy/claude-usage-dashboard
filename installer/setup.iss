; setup.iss — Inno Setup script for the Claude Code Usage Collector.
;
; Build steps:
;   1. powershell -File build_exe.ps1     ; produces dist\ClaudeUsageCollector.exe
;   2. ISCC.exe setup.iss                  ; produces Output\ClaudeUsageCollector-Setup.exe
;
; Distribution: end-users (or IT via GPO/PDQ/SCCM) run the produced setup.exe.
;
; Silent install for fleet rollouts:
;   ClaudeUsageCollector-Setup.exe /VERYSILENT /SUPPRESSMSGBOXES ^
;     /SERVERURL=https://your-app.vercel.app ^
;     /TOKEN=your-shared-ingest-token
;
; Interactive install: the wizard asks for both values on a custom page,
; pre-filled with command-line values if provided.

#define MyAppName       "Claude Code Usage Collector"
#define MyAppVersion    "1.0.0"
#define MyAppPublisher  "Internal"
#define MyAppExeName    "ClaudeUsageCollector.exe"
#define TaskName        "ClaudeCodeUsageCollector"
#define TaskIntervalMin 15

[Setup]
AppId={{8A4E4A2C-3B57-4F2A-9C1A-3B7E1A2D0001}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\ClaudeUsageCollector
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
PrivilegesRequired=admin
OutputDir=Output
OutputBaseFilename=ClaudeUsageCollector-Setup-{#MyAppVersion}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "dist\ClaudeUsageCollector.exe"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\collector\config.example.json"; DestDir: "{app}"; DestName: "config.example.json"; Flags: ignoreversion
Source: "CONSENT.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\Push usage data now"; Filename: "{app}\{#MyAppExeName}"; Parameters: "push"; WorkingDir: "{app}"
Name: "{group}\Show collector status"; Filename: "{app}\{#MyAppExeName}"; Parameters: "status"; WorkingDir: "{app}"
Name: "{group}\Uninstall"; Filename: "{uninstallexe}"

[Run]
; Run an initial push so the dashboard immediately sees this machine.
; Hidden — we don't want a console flashing in front of the user.
Filename: "{app}\{#MyAppExeName}"; Parameters: "push"; WorkingDir: "{app}"; \
    Flags: runhidden nowait; StatusMsg: "Running first push..."

; Register the recurring Scheduled Task (every {#TaskIntervalMin} minutes).
; /RL HIGHEST so the task can read the user's Claude Code dir under their profile.
; /F overwrites any prior task with the same name.
Filename: "schtasks.exe"; Parameters: \
    "/Create /F /SC MINUTE /MO {#TaskIntervalMin} /TN ""{#TaskName}"" /TR ""\""{app}\{#MyAppExeName}\"" push"" /RL HIGHEST /RU ""{username}"""; \
    Flags: runhidden; StatusMsg: "Registering Scheduled Task..."

[UninstallRun]
; Remove the Scheduled Task. /F = no confirmation prompt.
Filename: "schtasks.exe"; Parameters: "/Delete /F /TN ""{#TaskName}"""; Flags: runhidden

[UninstallDelete]
; The exe stops writing here at uninstall, but state.json / collector.log
; live under %LOCALAPPDATA% and may contain useful diagnostic info — leave
; them in place. The user can delete the folder manually.
Type: files; Name: "{app}\config.json"

[Code]
{ ──────────────────────────────────────────────────────────────────────────── }
{ Custom wizard page: SERVER_URL + INGEST_TOKEN entry                          }
{ ──────────────────────────────────────────────────────────────────────────── }
var
  ConfigPage:  TInputQueryWizardPage;
  ConsentPage: TOutputMsgMemoWizardPage;

function GetCmdLineParam(const Name: String): String;
var
  i: Integer;
  prefix: String;
begin
  Result := '';
  prefix := '/' + Name + '=';
  for i := 1 to ParamCount do
    if Pos(LowerCase(prefix), LowerCase(ParamStr(i))) = 1 then
    begin
      Result := Copy(ParamStr(i), Length(prefix) + 1, MaxInt);
      Exit;
    end;
end;

procedure InitializeWizard;
var
  ConsentText: TArrayOfString;
begin
  { Load the consent file shipped in [Files]; if missing, fall back to inline.}
  if not LoadStringsFromFile(ExpandConstant('{tmp}\CONSENT.txt'), ConsentText) then
  begin
    SetArrayLength(ConsentText, 5);
    ConsentText[0] := 'This software uploads metadata AND the full text of your';
    ConsentText[1] := 'Claude Code conversations to your team''s dashboard server.';
    ConsentText[2] := '';
    ConsentText[3] := 'Do not paste secrets, customer PII, or other sensitive data';
    ConsentText[4] := 'into Claude Code prompts on this machine.';
  end;

  ConsentPage := CreateOutputMsgMemoPage(
    wpWelcome,
    'Data Collection Notice',
    'Please read carefully before continuing',
    'By installing this collector you agree to upload your Claude Code usage data to your team''s dashboard server. ' +
    'This includes conversation content — not just token counts.',
    ''
  );
  { Memo text is set after page creation. }

  ConfigPage := CreateInputQueryPage(
    ConsentPage.ID,
    'Server Connection',
    'Where should this machine send its data?',
    'Get these values from whoever set up the dashboard. The server URL is the Vercel deployment; the ingest token is a shared team secret.'
  );
  ConfigPage.Add('Server URL (e.g. https://your-app.vercel.app):', False);
  ConfigPage.Add('Ingest token:', True);

  { Pre-fill from /SERVERURL= and /TOKEN= command-line parameters if present. }
  ConfigPage.Values[0] := GetCmdLineParam('SERVERURL');
  ConfigPage.Values[1] := GetCmdLineParam('TOKEN');
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  configPath: String;
  contents:   String;
  serverUrl:  String;
  ingestTok:  String;
begin
  if CurStep <> ssPostInstall then Exit;

  serverUrl := Trim(ConfigPage.Values[0]);
  ingestTok := Trim(ConfigPage.Values[1]);

  { Write config.json into the install dir. Use ASCII JSON so even non-ASCII
    hostnames don't trip the collector's json.loads on Python < 3.6. }
  configPath := ExpandConstant('{app}\config.json');
  contents :=
    '{' + #13#10 +
    '  "server_url":    "'  + serverUrl + '",' + #13#10 +
    '  "ingest_token":  "'  + ingestTok + '",' + #13#10 +
    '  "projects_dirs": null' + #13#10 +
    '}' + #13#10;
  SaveStringToFile(configPath, contents, False);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
var
  url: String;
begin
  Result := True;
  if CurPageID <> ConfigPage.ID then Exit;

  url := Trim(ConfigPage.Values[0]);
  if (Pos('http://', LowerCase(url)) <> 1) and (Pos('https://', LowerCase(url)) <> 1) then
  begin
    MsgBox('Server URL must start with http:// or https://', mbError, MB_OK);
    Result := False; Exit;
  end;
  if Trim(ConfigPage.Values[1]) = '' then
  begin
    MsgBox('Ingest token is required.', mbError, MB_OK);
    Result := False; Exit;
  end;
end;
