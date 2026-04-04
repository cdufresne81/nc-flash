; Inno Setup Script for NCFlash
; Requires: Inno Setup 6 (https://jrsoftware.org/isinfo.php)
; Build:    iscc packaging\installer.iss

#define MyAppName "NC Flash"
#define MyAppVersion GetEnv("APP_VERSION")
#if MyAppVersion == ""
  #define MyAppVersion "0.0.0-dev"
#endif
#define MyAppPublisher "cdufresne81"
#define MyAppURL "https://github.com/cdufresne81/nc-flash"
#define MyAppExeName "NCFlash.exe"

[Setup]
AppId={{E8F3A2B1-7C4D-4E5F-9A6B-1D2E3F4A5B6C}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
AppPublisherURL={#MyAppURL}
AppSupportURL={#MyAppURL}/issues
DefaultDirName={autopf}\NCFlash
DefaultGroupName={#MyAppName}
AllowNoIcons=yes
OutputDir=..\Output
OutputBaseFilename=NCFlash-{#MyAppVersion}-Setup
SetupIconFile=..\assets\NCFlash.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ChangesAssociations=yes
ArchitecturesInstallIn64BitMode=x64compatible
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "binassoc"; Description: "Associate .bin files with NC Flash"; GroupDescription: "File associations:"; Flags: unchecked

[Files]
Source: "..\dist\NCFlash\{#MyAppExeName}"; DestDir: "{app}"; Flags: ignoreversion
Source: "..\dist\NCFlash\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "run-mcp.bat"; DestDir: "{app}"; Flags: ignoreversion

[Registry]
Root: HKA; Subkey: "Software\Classes\.bin"; ValueType: string; ValueName: ""; ValueData: "NCFlash.BinFile"; Flags: uninsdeletevalue; Tasks: binassoc
Root: HKA; Subkey: "Software\Classes\NCFlash.BinFile"; ValueType: string; ValueName: ""; ValueData: "NC Flash ROM File"; Flags: uninsdeletekey; Tasks: binassoc
Root: HKA; Subkey: "Software\Classes\NCFlash.BinFile\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: "{app}\{#MyAppExeName},0"; Tasks: binassoc
Root: HKA; Subkey: "Software\Classes\NCFlash.BinFile\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#MyAppExeName}"" ""%1"""; Tasks: binassoc

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"
Name: "{group}\{cm:UninstallProgram,{#MyAppName}}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; IconFilename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent

