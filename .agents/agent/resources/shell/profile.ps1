# The profile, split by concern. $PROFILE dot-sources this file and nothing else; every part below is
# dot-sourced into the same scope, so the $script:-scoped tables each part owns stay visible to it.
$parts = @('env', 'hidden-bash-profile', 'fuzzy', 'docker', 'worktrees', 'agents', 'cr', 'procs')
foreach ($part in $parts) {
    $file = Join-Path $PSScriptRoot "$part.ps1"
    if (Test-Path $file) { . $file }
    else { Write-Host "profile: missing $part.ps1" -ForegroundColor Red }
}
