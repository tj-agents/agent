# Killing any of these takes the machine down with it, so they never reach the picker.
$script:KillProtected = @(
    'System', 'Idle', 'Registry', 'Secure System', 'Memory Compression', 'MemCompression',
    'smss', 'csrss', 'wininit', 'winlogon', 'services', 'lsass', 'LsaIso', 'fontdrvhost', 'svchost'
)

function Get-ProcLabel {
    param($Proc)
    try { if ($Proc.Description) { return $Proc.Description } } catch { }
    return ''
}

function fkill {
    param(
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Query,
        [switch]$Each
    )

    if (-not (Get-Command fzf -ErrorAction SilentlyContinue)) {
        Write-Host "Missing dependency: fzf" -ForegroundColor Red; return
    }

    $procs = @(Get-Process -ErrorAction SilentlyContinue |
        Where-Object { $script:KillProtected -notcontains $_.ProcessName -and $_.Id -ne $PID })

    $rows = @{}
    $items = @()

    if ($Each) {
        foreach ($p in ($procs | Sort-Object WorkingSet64 -Descending)) {
            $line = "{0,9:N1} MB  {1,-32} {2,-7} {3}" -f ($p.WorkingSet64 / 1MB), $p.ProcessName, $p.Id, (Get-ProcLabel $p)
            $rows[$line] = @{ TargetPid = $p.Id; Label = "$($p.ProcessName) ($($p.Id))" }
            $items += $line
        }
    }
    else {
        $groups = $procs | Group-Object ProcessName |
            Sort-Object { ($_.Group | Measure-Object WorkingSet64 -Sum).Sum } -Descending
        foreach ($g in $groups) {
            $mb = ($g.Group | Measure-Object WorkingSet64 -Sum).Sum / 1MB
            $desc = ''
            foreach ($p in $g.Group) { $desc = Get-ProcLabel $p; if ($desc) { break } }
            $count = if ($g.Count -gt 1) { "x$($g.Count)" } else { '' }
            $line = "{0,9:N1} MB  {1,-32} {2,-7} {3}" -f $mb, $g.Name, $count, $desc
            $rows[$line] = @{ Name = $g.Name; Label = "$($g.Name) ($($g.Count) proc$(if ($g.Count -gt 1) { 'esses' }))" }
            $items += $line
        }
    }

    if ($items.Count -eq 0) { Write-Host "Nothing killable found." -ForegroundColor Red; return }

    $q = ($Query -join ' ').Trim()
    $selected = @()

    if ($q -and -not $Each) {
        $hit = @($items | Where-Object { $rows[$_].Name -like "*$q*" })
        if ($hit.Count -eq 1) { $selected = $hit }
    }
    if ($selected.Count -eq 0) {
        $fzfArgs = @('--prompt', 'kill> ', '--height', '60%', '--border', '--multi',
                     '--header', 'TAB marks several / ENTER kills / ESC cancels')
        if ($q) { $fzfArgs += @('--query', $q) }
        $selected = @($items | & fzf @fzfArgs)
    }
    if ($selected.Count -eq 0 -or [string]::IsNullOrWhiteSpace($selected[0])) { return }

    $stubborn = @()
    foreach ($s in $selected) {
        $t = $rows[$s]
        if (-not $t) { continue }

        if ($t.Name) {
            Get-Process -Name $t.Name -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
            cmd /c "taskkill /F /T /IM ""$($t.Name).exe"" >nul 2>&1 & exit /b 0"
            Start-Sleep -Milliseconds 250
            $left = @(Get-Process -Name $t.Name -ErrorAction SilentlyContinue)
        }
        else {
            Stop-Process -Id $t.TargetPid -Force -ErrorAction SilentlyContinue
            cmd /c "taskkill /F /T /PID $($t.TargetPid) >nul 2>&1 & exit /b 0"
            Start-Sleep -Milliseconds 250
            $left = @(Get-Process -Id $t.TargetPid -ErrorAction SilentlyContinue)
        }

        if ($left.Count) {
            Write-Host "needs admin: $($t.Label)" -ForegroundColor Yellow
            $stubborn += $t
        }
        else {
            Write-Host "killed: $($t.Label)" -ForegroundColor Green
        }
    }

    if ($stubborn.Count) {
        $lines = foreach ($t in $stubborn) {
            if ($t.Name) { "taskkill /F /T /IM ""$($t.Name).exe""" } else { "taskkill /F /T /PID $($t.TargetPid)" }
        }
        $body = ($lines -join "`n") + "`nStart-Sleep -Seconds 2"
        $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($body))
        Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile', '-EncodedCommand', $enc
    }
}
