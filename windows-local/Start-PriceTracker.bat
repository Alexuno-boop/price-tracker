@echo off
rem Double-click to open the Price History Tracker in your browser.
rem Extra arguments are passed through, e.g.  Start-PriceTracker.bat -Check
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0PriceTracker.ps1" %*
