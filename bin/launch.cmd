:; . "${0%/*}/launch.sh"
@echo off
setlocal
set "arch=%PROCESSOR_ARCHITECTURE%"
if defined PROCESSOR_ARCHITEW6432 set "arch=%PROCESSOR_ARCHITEW6432%"
if /i "%arch%"=="ARM64" goto arm64
if /i "%arch%"=="AMD64" goto x64
echo whip-it: unsupported windows architecture >&2
exit /b 1
:arm64
"%~dp0native\aarch64-pc-windows-msvc\whip-it.exe" %*
exit /b %errorlevel%
:x64
"%~dp0native\x86_64-pc-windows-msvc\whip-it.exe" %*
exit /b %errorlevel%
