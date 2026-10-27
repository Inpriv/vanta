; Vanta per-user installer (no administrator rights needed).
; Build: makensis /DVERSION=0.1.0 /DSOURCE=path\to\vanta.exe /DOUTFILE=Vanta-Setup-0.1.0.exe installer.nsi

Unicode true
!include "MUI2.nsh"
!include "FileFunc.nsh"
!include "LogicLib.nsh"

!ifndef VERSION
  !define VERSION "0.0.0"
!endif
!ifndef SOURCE
  !define SOURCE "..\..\target\release\vanta.exe"
!endif
!ifndef OUTFILE
  !define OUTFILE "Vanta-Setup-${VERSION}.exe"
!endif

!define APP "Vanta"
!define UNINST_KEY "Software\Microsoft\Windows\CurrentVersion\Uninstall\Vanta"

Name "${APP}"
OutFile "${OUTFILE}"
InstallDir "$LOCALAPPDATA\Programs\Vanta"
InstallDirRegKey HKCU "Software\Vanta" "InstallDir"
RequestExecutionLevel user
SetCompressor /SOLID lzma
BrandingText "Vanta ${VERSION}"

VIProductVersion "${VERSION}.0"
VIAddVersionKey "ProductName" "Vanta"
VIAddVersionKey "FileDescription" "Vanta installer"
VIAddVersionKey "FileVersion" "${VERSION}"
VIAddVersionKey "ProductVersion" "${VERSION}"
VIAddVersionKey "LegalCopyright" "Inpriv. GPL-3.0-or-later"

!define MUI_ICON "..\icons\vanta.ico"
!define MUI_UNICON "..\icons\vanta.ico"
!define MUI_ABORTWARNING
!define MUI_FINISHPAGE_RUN "$INSTDIR\vanta.exe"
!define MUI_FINISHPAGE_RUN_TEXT "Start Vanta"
!define MUI_FINISHPAGE_SHOWREADME ""
!define MUI_FINISHPAGE_SHOWREADME_TEXT "Create a desktop shortcut"
!define MUI_FINISHPAGE_SHOWREADME_FUNCTION CreateDesktopShortcut
!define MUI_FINISHPAGE_SHOWREADME_NOTCHECKED

!insertmacro MUI_PAGE_LICENSE "..\..\LICENSE"
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Function CreateDesktopShortcut
  CreateShortcut "$DESKTOP\Vanta.lnk" "$INSTDIR\vanta.exe"
FunctionEnd

Section "Vanta" SecMain
  SectionIn RO
  ; Give a running Vanta (e.g. one that started this update) time to exit.
  Sleep 1500
  SetOutPath "$INSTDIR"
  File "/oname=vanta.exe" "${SOURCE}"
  WriteUninstaller "$INSTDIR\uninstall.exe"

  CreateDirectory "$SMPROGRAMS\Vanta"
  CreateShortcut "$SMPROGRAMS\Vanta\Vanta.lnk" "$INSTDIR\vanta.exe"

  WriteRegStr HKCU "Software\Vanta" "InstallDir" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayName" "Vanta"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayVersion" "${VERSION}"
  WriteRegStr HKCU "${UNINST_KEY}" "Publisher" "Inpriv"
  WriteRegStr HKCU "${UNINST_KEY}" "URLInfoAbout" "https://getvanta.xyz"
  WriteRegStr HKCU "${UNINST_KEY}" "DisplayIcon" "$INSTDIR\vanta.exe"
  WriteRegStr HKCU "${UNINST_KEY}" "InstallLocation" "$INSTDIR"
  WriteRegStr HKCU "${UNINST_KEY}" "UninstallString" '"$INSTDIR\uninstall.exe"'
  WriteRegStr HKCU "${UNINST_KEY}" "QuietUninstallString" '"$INSTDIR\uninstall.exe" /S'
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoModify" 1
  WriteRegDWORD HKCU "${UNINST_KEY}" "NoRepair" 1
  ${GetSize} "$INSTDIR" "/S=0K" $0 $1 $2
  IntFmt $0 "0x%08X" $0
  WriteRegDWORD HKCU "${UNINST_KEY}" "EstimatedSize" "$0"

  ; Silent updates started by Vanta pass /RESTART to relaunch it.
  ${GetParameters} $R0
  ClearErrors
  ${GetOptions} $R0 "/RESTART" $R1
  ${IfNot} ${Errors}
    Exec '"$INSTDIR\vanta.exe"'
  ${EndIf}
SectionEnd

Section "Uninstall"
  Delete "$INSTDIR\vanta.exe"
  Delete "$INSTDIR\vanta.old"
  Delete "$INSTDIR\uninstall.exe"
  RMDir "$INSTDIR"
  Delete "$SMPROGRAMS\Vanta\Vanta.lnk"
  RMDir "$SMPROGRAMS\Vanta"
  Delete "$DESKTOP\Vanta.lnk"
  DeleteRegKey HKCU "${UNINST_KEY}"
  DeleteRegKey HKCU "Software\Vanta"
  ; Game data in %APPDATA%\Vanta is kept on purpose (worlds, instances).
SectionEnd
