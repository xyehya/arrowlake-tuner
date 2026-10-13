@echo off
rem Restore-Tuning.cmd - one-click re-apply of YOUR accepted tuning (%ProgramData%\arltuner\accepted, an admin-only directory; copied from Linux /etc/arltuner/accepted) after ANY boot, cold or warm.
rem Elevates itself (UAC), then runs tools\win\restore_tuning.ps1 : you type CONFIRM once (never unattended).
rem Works cold (all offsets stock) and warm: the one accepted profile is applied by writing only the words that differ (rounds within the policy's step limits), then every word is compared.
rem Success line: "RESTORED: all <n> cores/clusters match the accepted state."  Close OCCT / XTU first. Revert = cold boot. No accepted state there = it stops.
net session >nul 2>&1
if errorlevel 1 (
    powershell -NoProfile -Command "Start-Process -Verb RunAs -FilePath '%~f0'"
    exit /b
)
cd /d "%~dp0..\.."
powershell -NoProfile -ExecutionPolicy Bypass -File tools\win\restore_tuning.ps1
echo.
pause
