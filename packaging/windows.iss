#ifndef AppVersion
  #error Pass the release version with /DAppVersion=x.y.z
#endif

[Setup]
AppId=io.github.zhengye123188.personal-mail-assistant
AppName=个人邮件助手
AppVersion={#AppVersion}
VersionInfoVersion={#AppVersion}.0
AppPublisher=zhengye123188
AppPublisherURL=https://github.com/zhengye123188/personal-mail-assistant
DefaultDirName={localappdata}\Programs\PersonalMailAssistant
DisableProgramGroupPage=yes
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
WizardStyle=modern
OutputDir=..\dist
OutputBaseFilename=PersonalMailAssistant-Windows-x64-Setup
SetupIconFile=maildesk.ico
UninstallDisplayIcon={app}\PersonalMailAssistant.exe
Compression=lzma2/fast
SolidCompression=yes

[Files]
Source: "..\dist\PersonalMailAssistant\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs

[Icons]
Name: "{userprograms}\个人邮件助手"; Filename: "{app}\PersonalMailAssistant.exe"

[Run]
Filename: "{app}\PersonalMailAssistant.exe"; Description: "打开个人邮件助手"; Flags: nowait postinstall skipifsilent
