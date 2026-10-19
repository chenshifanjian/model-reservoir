; 模型蓄水池 —— Windows 安装包（Inno Setup 6）
;
; 设计要点（与 app.py 的数据目录逻辑强绑定）：
;   * per-user 安装到 %LOCALAPPDATA%\Programs\ModelReservoir，不弹 UAC。
;   * 绝不能装到 Program Files：打包版的 DATA_DIR = 可执行文件所在目录，
;     首启要写 config.json，装在 Program Files 会直接 PermissionError 起不来。
;   * 程序本体是 PyInstaller onefile，安装包只放一个 exe，卸载时保留用户数据。
;
; 构建（CI 里调用）：
;   ISCC.exe /DMyAppVersion=1.9.2 /DMyAppExe=D:\a\...\dist\ModelReservoir.exe model-reservoir.iss

#ifndef MyAppVersion
  #define MyAppVersion "0.0.0"
#endif
#ifndef MyAppExe
  #define MyAppExe "..\..\dist\ModelReservoir.exe"
#endif

#define MyAppName "模型蓄水池"
#define MyAppExeName "ModelReservoir.exe"

[Setup]
; AppId 必须永久不变，否则升级会变成「两个程序」而不是覆盖安装
AppId={{8B2F6C1E-3E4A-4C7B-9F2D-5A1C7E6B9D31}
AppName={#MyAppName}
AppVersion={#MyAppVersion}
AppVerName={#MyAppName} {#MyAppVersion}
AppPublisher=Model Reservoir
AppPublisherURL=https://github.com/chenshifanjian/model-reservoir
AppSupportURL=https://github.com/chenshifanjian/model-reservoir/issues
AppUpdatesURL=https://github.com/chenshifanjian/model-reservoir/releases
DefaultDirName={localappdata}\Programs\ModelReservoir
DefaultGroupName={#MyAppName}
DisableProgramGroupPage=yes
; 关键：用户级安装，目标目录可写
PrivilegesRequired=lowest
PrivilegesRequiredOverridesAllowed=dialog
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0
OutputDir=..\..\dist
OutputBaseFilename=ModelReservoir-v{#MyAppVersion}-windows-setup
SetupIconFile=..\..\assets\icon.ico
UninstallDisplayIcon={app}\{#MyAppExeName}
UninstallDisplayName={#MyAppName}
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
CloseApplications=yes
RestartApplications=no
VersionInfoVersion={#MyAppVersion}.0
VersionInfoProductName={#MyAppName}
VersionInfoDescription={#MyAppName} Installer

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "创建桌面快捷方式"; GroupDescription: "附加任务:"; Flags: unchecked

[Files]
Source: "{#MyAppExe}"; DestDir: "{app}"; DestName: "{#MyAppExeName}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"
Name: "{group}\卸载 {#MyAppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#MyAppName}"; Filename: "{app}\{#MyAppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#MyAppExeName}"; Description: "立即启动 {#MyAppName}"; Flags: nowait postinstall skipifsilent

; 卸载：只删程序本体，config.json / providers.json / usage 数据全部保留，
; 用户重装或退回绿色版时配置还在。
[UninstallDelete]
Type: files; Name: "{app}\{#MyAppExeName}"
