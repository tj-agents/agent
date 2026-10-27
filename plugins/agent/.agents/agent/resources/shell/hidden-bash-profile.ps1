$hiddenBash = Join-Path $PSScriptRoot 'hidden-bash.exe'
if (Test-Path -LiteralPath $hiddenBash -PathType Leaf) {
    $realBash = [Environment]::GetEnvironmentVariable('TJ_AGENTS_REAL_GIT_BASH', 'User')
    if (-not $realBash) { $realBash = 'C:\Program Files\Git\bin\bash.exe' }
    if ($realBash -ne $hiddenBash -and (Test-Path -LiteralPath $realBash -PathType Leaf)) {
        $env:TJ_AGENTS_REAL_GIT_BASH = $realBash
        $env:CLAUDE_CODE_GIT_BASH_PATH = $hiddenBash
    }
}
