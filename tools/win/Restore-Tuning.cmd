@echo off
rem Restore-Tuning.cmd - one-click re-apply of YOUR accepted tuning (%ProgramData%\arltuner\accepted, an admin-only directory; copied from Linux /etc/arltuner/accepted) after ANY boot, cold or warm.
rem Elevates itself (UAC), then runs tools\win\restore_tuning.ps1 : you type CONFIRM once (never unattended).
rem Works cold (all offsets stock) and warm (CPUs 1-7 + E kept, CPU 0 reset): the planner steps from what it reads, then every word is compared.
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
