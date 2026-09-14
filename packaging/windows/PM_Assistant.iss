#ifndef BuildDir
  #error BuildDir must be passed by build_installer.cmd
#endif
#ifndef OutputDir
  #error OutputDir must be passed by build_installer.cmd
#endif

#define MyAppName "PM Assistant"
#define MyAppVersion "1.0.0"
#define MyAppPublisher "PM Assistant"
#define MyAppExeName "PM Assistant.exe"

[Setup]
AppId={{7C20C076-7D11-4BC4-8468-D4E9CA5E0372}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={localappdata}\Programs\PM Assistant
DefaultGroupName=PM Assistant
DisableProgramGroupPage=yes
OutputDir={#OutputDir}
OutputBaseFilename=PM_Assistant_Setup_1.0.0
Compression=lzma2
SolidCompression=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
WizardStyle=modern
UninstallDisplayIcon={app}\{#MyAppExeName}

[Files]
Source: "{#BuildDir}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{autoprograms}\PM Assistant"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\PM Assistant"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Tasks]
Name: "desktopicon"; Description: "Создать ярлык на рабочем столе"; GroupDescription: "Ярлыки:"; Flags: unchecked

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "Запустить PM Assistant"; Flags: nowait postinstall skipifsilent

[UninstallDelete]
; User project data is intentionally NOT removed. It remains in
; %LOCALAPPDATA%\PM Assistant\data so uninstall/reinstall does not destroy projects.
