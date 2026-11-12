; Установщик DryerCalc (NSIS 3, Unicode). Сборка:  makensis installer.nsi
; Нужна папка dist\DryerCalc\ (pyinstaller --noconfirm DryerCalc_dir.spec).
; Ставит в папку пользователя без прав администратора; данные (%APPDATA%\DryerCalc) не трогает.

;  программа 64-битная; установщик тоже 64-битный, если в NSIS есть amd64-заготовки (сборка NSIS для Linux),
;  иначе (официальный NSIS для Windows, GitHub Actions) — 32-битный Unicode: на Windows 10/11 x64 работает так же
!if /FileExists "${NSISDIR}\Stubs\lzma_solid-amd64-unicode"
  Target amd64-unicode
!else
  Unicode true
!endif
SetCompressor /SOLID lzma
SetCompressorDictSize 64
RequestExecutionLevel user

!define APPNAME "DryerCalc"
!define APPTITLE "DryerCalc — расчёт сушки зерна"
!ifndef VERSION
  !define VERSION "1.10.0"
!endif
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\${APPNAME}"

Name "${APPTITLE}"
OutFile "dist\DryerCalc_Setup.exe"
InstallDir "$LOCALAPPDATA\Programs\${APPNAME}"
InstallDirRegKey HKCU "Software\${APPNAME}" "InstallDir"
BrandingText "DryerCalc ${VERSION}"
VIProductVersion "${VERSION}.0"
VIAddVersionKey /LANG=1049 "ProductName" "DryerCalc"
VIAddVersionKey /LANG=1049 "FileDescription" "Установка DryerCalc — расчёт времени сушки зерна"
VIAddVersionKey /LANG=1049 "FileVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "ProductVersion" "${VERSION}"
VIAddVersionKey /LANG=1049 "LegalCopyright" "ExQII"

!include "MUI2.nsh"
!define MUI_ICON "assets\dryer.ico"
!define MUI_UNICON "assets\dryer.ico"
!define MUI_ABORTWARNING
!define MUI_WELCOMEPAGE_TEXT "Программа будет установлена в папку пользователя, права администратора не нужны.$\r$\n$\r$\nЖурнал, погода, партии и настройки хранятся отдельно (%APPDATA%\DryerCalc) и при обновлении или удалении программы сохраняются.$\r$\n$\r$\nЕсли DryerCalc запущен — закройте его перед установкой."
!define MUI_FINISHPAGE_RUN "$INSTDIR\DryerCalc.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Запустить DryerCalc"

!insertmacro MUI_PAGE_WELCOME
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "Russian"

Section "DryerCalc" SecMain
  SectionIn RO
  ; старая версия программы удаляется целиком (данные пользователя лежат в другом месте)
  RMDir /r "$INSTDIR\_internal"
  SetOutPath "$INSTDIR"
  File /r "dist\DryerCalc\*"
  File "/oname=Инструкция DryerCalc.txt" "dist\Инструкция DryerCalc.txt"
  File "/oname=Правила работы DryerCalc.txt" "dist\Правила работы DryerCalc.txt"
  WriteUninstaller "$INSTDIR\uninstall.exe"
  CreateDirectory "$SMPROGRAMS\DryerCalc"
  CreateShortcut "$SMPROGRAMS\DryerCalc\DryerCalc.lnk" "$INSTDIR\DryerCalc.exe" "" "$INSTDIR\DryerCalc.exe" 0
  CreateShortcut "$SMPROGRAMS\DryerCalc\Инструкция DryerCalc.lnk" "$INSTDIR\Инструкция DryerCalc.txt"
  CreateShortcut "$SMPROGRAMS\DryerCalc\Правила работы DryerCalc.lnk" "$INSTDIR\Правила работы DryerCalc.txt"
  CreateShortcut "$SMPROGRAMS\DryerCalc\Удалить DryerCalc.lnk" "$INSTDIR\uninstall.exe"
  CreateShortcut "$DESKTOP\DryerCalc.lnk" "$INSTDIR\DryerCalc.exe" "" "$INSTDIR\DryerCalc.exe" 0
  WriteRegStr HKCU "Software\${APPNAME}" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "${APPTITLE}"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "ExQII"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\DryerCalc.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
SectionEnd

Section "Uninstall"
  Delete "$DESKTOP\DryerCalc.lnk"
  RMDir /r "$SMPROGRAMS\DryerCalc"
  RMDir /r "$INSTDIR"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\${APPNAME}"
  MessageBox MB_OK "DryerCalc удалён. Журнал и настройки остались в папке %APPDATA%\DryerCalc — удалите её вручную, если они больше не нужны." /SD IDOK
SectionEnd
