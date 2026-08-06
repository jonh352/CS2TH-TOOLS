; CS2TH 汰换小助手 — Inno Setup（LZMA2，目标分发体积 <100MB，与 CS2CT 同思路）
; 前置：.\build_setup.ps1 会先打 onedir：dist\CS2TH-Tools\
; 需安装 Inno Setup 6：https://jrsoftware.org/isinfo.php

#define MyAppName "CS2TH汰换小助手"
#define MyAppVersion "0.3.3"
#define MyAppPublisher "CS2TH"
#define MyAppExeName "CS2TH-Tools.exe"
#define MyAppId "{{A7C2E91B-4D5F-4A8E-9B1C-2E3F4A5B6C7D}"

[Setup]
AppId={#MyAppId}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} v{#MyAppVersion}
AppPublisher={#MyAppPublisher}
DefaultDirName={autopf}\{#MyAppName}
DefaultGroupName={#MyAppName}
OutputDir=dist
OutputBaseFilename=CS2TH-Tools_Setup_v{#MyAppVersion}
Compression=lzma2/ultra64
SolidCompression=yes
LZMAUseSeparateProcess=yes
WizardStyle=modern
PrivilegesRequired=admin
ArchitecturesInstallIn64BitMode=x64
DisableProgramGroupPage=no
SetupIconFile=assets\logo.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
MinVersion=10.0

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked

[Files]
; 打包整个 onedir（未预压缩的 DLL/node/枪图，LZMA 压缩率远好于再压 onefile）
Source: "dist\CS2TH-Tools\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "{cm:LaunchProgram,{#StringChange(MyAppName, '&', '&&')}}"; Flags: nowait postinstall skipifsilent
