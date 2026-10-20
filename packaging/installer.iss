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
; this upgrades an existing NC Flash install, and when any NC Flash setting
; (a folder, the J2534 DLL, a recent or open ROM) points inside _internal.
; Inno does not roll deletions back: a cancelled or failed upgrade leaves
; NC Flash unable to start until the installer is run again.
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
  // NC Flash's settings: QSettings("NCFlash", "NCFlash") under HKCU.
  SettingsKey = 'Software\NCFlash\NCFlash';

var
  // {app}\_internal, lowercased with a trailing backslash.
  InternalPrefix: String;

function NormalizedPath(Path: String): String;
begin
  StringChangeEx(Path, '/', '\', True);
  Result := AnsiLowercase(AddBackslash(Path));
end;

// True when Path is _internal itself or anything under it. The bundled
// default color map is the one exception: the new build installs it again.
function PointsInside(Path: String): Boolean;
begin
  Result := False;
  if Path = '' then
    Exit;
  Path := NormalizedPath(Path);
  if Path = InternalPrefix + 'colormaps\default.map\' then
    Exit;
  Result := Copy(Path, 1, Length(InternalPrefix)) = InternalPrefix;
end;

// Every string and string-list value under Key and its subkeys (folders,
// the J2534 DLL, recent and open ROMs, ...). True if any points inside.
function SettingPointsInside(Key: String): Boolean;
var
  Names: TArrayOfString;
  I, Sep: Integer;
  Value, Item: String;
begin
  Result := False;
  if RegGetValueNames(HKCU, Key, Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
    begin
      if RegQueryStringValue(HKCU, Key, Names[I], Value) then
        Value := Value + #0
      else if not RegQueryMultiStringValue(HKCU, Key, Names[I], Value) then
        Continue
      else
        Value := Value + #0;
      while Value <> '' do
      begin
        Sep := Pos(#0, Value);
        Item := Copy(Value, 1, Sep - 1);
        Delete(Value, 1, Sep);
        if PointsInside(Item) then
        begin
          Log('Setting ' + Key + '\' + Names[I] + ' points inside _internal (' +
              Item + '): _internal left alone.');
          Result := True;
          Exit;
        end;
      end;
    end;
  if RegGetSubkeyNames(HKCU, Key, Names) then
    for I := 0 to GetArrayLength(Names) - 1 do
      if SettingPointsInside(Key + '\' + Names[I]) then
      begin
        Result := True;
        Exit;
      end;
end;

// Check for the [InstallDelete] entry: clear {app}\_internal only when it is
// an earlier NC Flash's runtime (NCFlash.exe next to it, NC Flash's own
// bundled folders inside it) and no NC Flash setting points inside it.
// Otherwise leave it as it is: stale files are the safe failure.
function ShouldClearRuntime: Boolean;
var
  Internal: String;
begin
  Result := False;
  Internal := ExpandConstant('{app}\_internal');
  if not FileExists(ExpandConstant('{app}\{#MyAppExeName}')) or
     not (DirExists(Internal + '\examples\metadata') or
          DirExists(Internal + '\j2534_bridge_32')) then
  begin
    Log('No earlier NC Flash in this folder: _internal left alone.');
    Exit;
  end;
  InternalPrefix := NormalizedPath(Internal);
  if SettingPointsInside(SettingsKey) then
    Exit;
  Log('Clearing the previous runtime in ' + Internal);
  Result := True;
end;
