SET logfile=G:\Documents And Logs\Bat Walmart Logs.txt
@echo off
@echo Starting Script at %date% %time% >> %logfile%
start /min "C:\Program Files\JetBrains\WalmartInventoryPullAndMerge\Scripts\python.exe" "G:\Python Projects\WalmartInventoryPullAndMerge\main.py" >> %logfile% 2>&1
@echo Finished at %date% %time% >> %logfile%
