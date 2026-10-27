# "I closed the terminal, put me back in what I was doing" - from ANY directory, any project on this
# machine. Takes the most recently touched conversation across all of ~/.claude/projects, moves to the
# directory that session was rooted in, and resumes it. `cl -Pick` opens the cross-project picker instead.
function cl {
    param([switch]$Pick, [int]$Count = 30)

    $root = Join-Path $env:USERPROFILE '.claude\projects'
    if (-not (Test-Path $root)) { Write-Host "No Claude history at $root" -ForegroundColor Red; return }

    if ($Pick) { cr -All -Count $Count; return }

    $f = @(Get-ChildItem $root -Directory -ErrorAction SilentlyContinue |
        ForEach-Object { Get-ChildItem $_.FullName -Filter *.jsonl -File -ErrorAction SilentlyContinue } |
        Where-Object { $_.Length -gt 2KB } |
        Sort-Object LastWriteTime -Descending | Select-Object -First 1)
    if ($f.Count -eq 0) { Write-Host "No conversations found." -ForegroundColor Red; return }
    $f = $f[0]
    $sid = [IO.Path]::GetFileNameWithoutExtension($f.Name)

    # The session's own cwd is recorded on its entries; resume from there or the project context is wrong.
    $cwd = ''
    try {
        $fs = New-Object IO.FileStream($f.FullName, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $len = [Math]::Min(65536, $fs.Length)
        $fs.Seek(-$len, [IO.SeekOrigin]::End) | Out-Null
        $buf = New-Object byte[] $len
        $fs.Read($buf, 0, $len) | Out-Null
        $fs.Dispose()
        $tailText = [Text.Encoding]::UTF8.GetString($buf)
        $m = [regex]::Matches($tailText, '"cwd":"((?:[^"\\]|\\.)*)"')
        if ($m.Count -gt 0) { $cwd = ($m[$m.Count - 1].Groups[1].Value -replace '\\\\', '\') }
    }
    catch { }

    if ($cwd -and (Test-Path -LiteralPath $cwd)) {
        Write-Host "-> $cwd" -ForegroundColor DarkGray
        Set-Location -LiteralPath $cwd
    }
    Write-Host "-> claude --resume $sid  ($($f.LastWriteTime.ToString('MM-dd HH:mm')))" -ForegroundColor DarkGray
    claude --resume $sid
}
