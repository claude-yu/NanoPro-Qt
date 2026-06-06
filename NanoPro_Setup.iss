; Inno Setup 脚本：把 dist\NanoPro 打成双击即装的 setup.exe（开始菜单快捷方式 + 卸载程序）。
; 编译： "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe" NanoPro_Setup.iss
; 产物： Output\SciEdit_NanoPro_Setup_v1.14.exe

#define MyAppName "SciEdit 科研图编辑器"
#define MyAppExeName "NanoPro.exe"
#define MyAppVersion "1.15"
#define MyAppPublisher "NanoPro"

[Setup]
AppId={{7E9A2B14-3C5D-4F68-9A0B-1C2D3E4F5A6B}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher={#MyAppPublisher}
; 默认装到当前用户目录，不需要管理员权限（师妹无需管理员也能装）
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
DefaultDirName={autopf}\NanoPro
DefaultGroupName=SciEdit NanoPro
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
OutputDir=Output
OutputBaseFilename=SciEdit_NanoPro_Setup_v1.15
SetupIconFile=NanoPro.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern

[Languages]
Name: "en"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式（Create desktop shortcut）"; GroupDescription: "附加任务："; Flags: unchecked

[Files]
Source: "dist\NanoPro\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{group}\SciEdit 科研图编辑器"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 SciEdit 科研图编辑器"; Filename: "{uninstallexe}"
Name: "{userdesktop}\SciEdit 科研图编辑器"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即运行 SciEdit"; Flags: nowait postinstall skipifsilent
