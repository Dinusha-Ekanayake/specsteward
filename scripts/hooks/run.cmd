: << 'CMDBLOCK'
@echo off
rem specsteward hook runner. One file for Windows (cmd.exe runs this block) and for
rem macOS/Linux (sh skips it: the line above starts a here-document that swallows it).
rem
rem Why: on Windows a bare `bash` can be C:\Windows\System32\bash.exe, the WSL launcher,
rem which fails with "execvpe(/bin/bash) failed" when no Linux distribution is installed.
rem So Git Bash is located explicitly and that WSL stub is never used.
setlocal EnableExtensions
if "%~1"=="" (
  echo specsteward: run.cmd needs a hook name 1>&2
  exit /b 0
)
set "HOOK_SH=%~dp0%~1.sh"
set "GITBASH="

rem 1. Where Claude Code was told Git Bash is.
if defined CLAUDE_CODE_GIT_BASH_PATH if exist "%CLAUDE_CODE_GIT_BASH_PATH%" set "GITBASH=%CLAUDE_CODE_GIT_BASH_PATH%"

rem 2. Next to git.exe (...\Git\cmd\git.exe or ...\Git\bin\git.exe), wherever Git is installed.
if not defined GITBASH for /f "delims=" %%G in ('where git 2^>nul') do if not defined GITBASH (
  if exist "%%~dpG..\bin\bash.exe" set "GITBASH=%%~dpG..\bin\bash.exe"
)

rem 3. The usual install folders.
if not defined GITBASH if exist "%ProgramFiles%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles%\Git\bin\bash.exe"
if not defined GITBASH if exist "%ProgramFiles(x86)%\Git\bin\bash.exe" set "GITBASH=%ProgramFiles(x86)%\Git\bin\bash.exe"
if not defined GITBASH if exist "%LocalAppData%\Programs\Git\bin\bash.exe" set "GITBASH=%LocalAppData%\Programs\Git\bin\bash.exe"

rem 4. Any other bash on PATH, but not the WSL launcher or the Store stub.
if not defined GITBASH for /f "delims=" %%B in ('where bash 2^>nul') do if not defined GITBASH (
  echo %%B | findstr /i /c:"\Windows\System32" /c:"\WindowsApps" >nul || set "GITBASH=%%B"
)

if not defined GITBASH (
  echo specsteward: Git Bash was not found, so this hook did not run. Install Git for Windows. 1>&2
  exit /b 0
)
"%GITBASH%" "%HOOK_SH%"
exit /b %ERRORLEVEL%
CMDBLOCK
# macOS and Linux: the hook's own script, run with bash.
exec bash "$(dirname "$0")/$1.sh"
