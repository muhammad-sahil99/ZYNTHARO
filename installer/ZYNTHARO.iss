; ZYNTHARO Windows installer. Build on Windows with Inno Setup 6 after tools/build_windows_bundle.py.
; The build wrapper validates payload/bundle-manifest.json before invoking this script.
; Windows 10 1809 is the floor because that is where per-monitor DPI awareness and the UI
; Automation APIs this product depends on are reliably present; Windows 11 is the primary target.
; This repository ships the beta of the family. The shortcut, program-group and uninstall names
; carry the beta marker so it can sit beside the alpha build and the Rust/Tauri core on one machine
; without either overwriting the other's shortcuts.
#define ProductName "ZYNTHARO Beta"
#define ProductVersion GetEnv("ZYNTHARO_VERSION")
#if ProductVersion == ""
  #error ZYNTHARO_VERSION must be set by the release build wrapper.
#endif
#define PayloadDir GetEnv("ZYNTHARO_PAYLOAD_DIR")
#if PayloadDir == ""
  #error ZYNTHARO_PAYLOAD_DIR must point to the validated PyInstaller payload.
#endif

[Setup]
AppId={{F13D6DC0-A43A-4D57-B1EC-9E2C37A00B18}
AppName={#ProductName}
AppVersion={#ProductVersion}
AppPublisher=ZYNTHARO
DefaultDirName={autopf}\ZYNTHARO
DefaultGroupName={#ProductName}
DisableProgramGroupPage=yes
OutputDir=..\dist\installer
OutputBaseFilename=ZYNTHARO-Beta-Setup-{#ProductVersion}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
PrivilegesRequired=lowest
UninstallDisplayName={#ProductName}
; Signing is intentionally supplied by the private release environment, never committed here.
SignTool=signing
SignedUninstaller=yes

[Files]
Source: "{#PayloadDir}\*"; DestDir: "{app}"; Flags: recursesubdirs ignoreversion

[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\ZYNTHARO.exe"
Name: "{autodesktop}\{#ProductName}"; Filename: "{app}\ZYNTHARO.exe"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Additional shortcuts:"; Flags: unchecked

[Run]
Filename: "{app}\ZYNTHARO.exe"; Description: "Launch ZYNTHARO Beta"; Flags: nowait postinstall skipifsilent
