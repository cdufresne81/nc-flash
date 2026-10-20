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

[InstallDelete]
; Runs before [Files] copies the new build. Clears the previous version's
; bundled runtime so a DLL or .pyd that the new build no longer ships can't
; be loaded by it. _internal holds only files this installer put there:
; settings, ROMs, reads and logs live in %APPDATA%\NCFlash, ~\.nc-flash and
; the registry, never under {app}. ShouldClearRuntime (below) skips it unless
; this upgrades an existing NC Flash install, and when a folder setting
; points inside _internal.
Type: filesandordirs; Name: "{app}\_internal"; Check: ShouldClearRuntime

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

[Code]
const
  // Where NC Flash keeps its folder settings (QSettings "NCFlash", "NCFlash").
  SettingsPathsKey = 'Software\NCFlash\NCFlash\paths';

function IsInsideDir(Path, Dir: String): Boolean;
begin
  StringChangeEx(Path, '/', '\', True);
  Path := Lowercase(AddBackslash(Path));
  Dir := Lowercase(AddBackslash(Dir));
  Result := Copy(Path, 1, Length(Dir)) = Dir;
end;

// Check for the [InstallDelete] entry: clear {app}\_internal only when it is
// an earlier NC Flash's runtime and no NC Flash folder setting (workspace,
// ROMs, definitions, ...) points inside it. Otherwise leave it as it is.
function ShouldClearRuntime: Boolean;
var
  Internal, Value: String;
  Names: TArrayOfString;
  I: Integer;
begin
  Result := False;
  if not FileExists(ExpandConstant('{app}\{#MyAppExeName}')) then
  begin
    Log('No earlier NC Flash in this folder: _internal left alone.');
    Exit;
  end;
  Internal := ExpandConstant('{app}\_internal');
  if RegGetValueNames(HKCU, SettingsPathsKey, Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if RegQueryStringValue(HKCU, SettingsPathsKey, Names[I], Value) and
         IsInsideDir(Value, Internal) then
      begin
        Log('Setting ' + Names[I] + ' points inside _internal (' + Value +
            '): _internal left alone.');
        Exit;
      end;
  Log('Clearing the previous runtime in ' + Internal);
  Result := True;
end;

