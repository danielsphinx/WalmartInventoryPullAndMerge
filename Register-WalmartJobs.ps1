<#
Registers (or re-registers) the four Walmart scheduled tasks on this PC. Run as Daniel, no admin needed.

  pwsh -File Register-WalmartJobs.ps1 -Mode Shadow   uploads, tracking and orders as dry runs; the export runs live
  pwsh -File Register-WalmartJobs.ps1 -Mode Live     everything live
  pwsh -File Register-WalmartJobs.ps1 -Mode Off      all four disabled (rollback)

Every task runs as Daniel, only while he is logged on (G: is his mapped share and WM_QOS_CORRELATION_ID is his
User variable), starts when available after a missed time, never overlaps itself, needs the network, and
ignores battery state. Python runs windowless (pythonw.exe). The export task runs `walmart_jobs.py export`,
which runs the BAT's worker pass in a hidden cmd.exe and waits for main.py (conhost --headless never ran it).
#>
param([Parameter(Mandatory)][ValidateSet('Shadow', 'Live', 'Off')][string]$Mode)
$ErrorActionPreference = 'Stop'

$Project = 'G:\Python Projects\WalmartInventoryPullAndMerge'
$Pythonw = 'C:\Program Files\Python310\pythonw.exe'
$Jobs = "`"$Project\walmart_jobs.py`""
$Bat = "`"$Project\.BAT File Refresh\Inventory Refresh.bat`""
$Dry = if ($Mode -eq 'Shadow') { ' --dry-run' } else { '' }
$User = "$env:COMPUTERNAME\$env:USERNAME"

function Exec($command, $arguments) {
    "<Exec><Command>$([Security.SecurityElement]::Escape($command))</Command><Arguments>$([Security.SecurityElement]::Escape($arguments))</Arguments><WorkingDirectory>$([Security.SecurityElement]::Escape($Project))</WorkingDirectory></Exec>"
}

$tasks = @(
    @{ Name = 'Walmart Upload Watcher'; Start = '00:02'; Every = 'PT5M'; Limit = 'PT2H'; Enabled = $Mode -ne 'Off'
       Actions = (Exec $Pythonw "$Jobs uploads$Dry") + (Exec $Pythonw "$Jobs tracking$Dry")
       Description = 'Uploads the converter batch (marker driven) and the hourly Walmart tracking. See walmart_jobs.py.' },
    @{ Name = 'Walmart Order Export'; Start = '00:07'; Every = 'PT15M'; Limit = 'PT10M'; Enabled = $Mode -ne 'Off'
       Actions = (Exec $Pythonw "$Jobs orders$Dry")
       Description = 'Walmart pending orders to the two CSVs the robot orders workflow reads. See walmart_jobs.py.' },
    @{ Name = 'Walmart Feed Status'; Start = '00:12'; Every = 'PT30M'; Limit = 'PT10M'; Enabled = $Mode -ne 'Off'
       Actions = (Exec $Pythonw "$Jobs feedstatus") + (Exec $Pythonw "$Jobs status")
       Description = 'Checks posted feeds with Walmart and writes G:\Documents And Logs\walmart_jobs_status.txt.' },
    @{ Name = 'Walmart Inventory Export'; Start = '00:50'; Every = 'PT1H'; Limit = 'PT3H'; Enabled = $Mode -ne 'Off'  # main.py only reads from Walmart: live in Shadow too
       Actions = (Exec $Pythonw "$Jobs export")
       Description = 'main.py through Inventory Refresh.bat (lock, log, exit code in Bat Walmart Logs.txt).' }
)

foreach ($t in $tasks) {
    $start = (Get-Date).ToString('yyyy-MM-dd') + 'T' + $t.Start + ':00'
    $xml = @"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>$User</Author><Description>$([Security.SecurityElement]::Escape($t.Description)) Mode: $Mode.</Description></RegistrationInfo>
  <Triggers>
    <TimeTrigger><StartBoundary>$start</StartBoundary><Repetition><Interval>$($t.Every)</Interval><StopAtDurationEnd>false</StopAtDurationEnd></Repetition><Enabled>true</Enabled></TimeTrigger>
    <LogonTrigger><UserId>$User</UserId><Delay>PT2M</Delay><Enabled>true</Enabled></LogonTrigger>
  </Triggers>
  <Principals><Principal id="Author"><UserId>$User</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>$(([string]$t.Enabled).ToLower())</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>$($t.Limit)</ExecutionTimeLimit>
    <Priority>7</Priority>
  </Settings>
  <Actions Context="Author">$($t.Actions)</Actions>
</Task>
"@
    Register-ScheduledTask -TaskName $t.Name -Xml $xml -Force | Out-Null
    $state = (Get-ScheduledTask -TaskName $t.Name).State
    Write-Output ("{0,-26} {1,-9} every {2,-6} {3}" -f $t.Name, $state, $t.Every, ($t.Actions -replace '<[^>]+>', ' ' -replace '\s+', ' ').Trim())
}
