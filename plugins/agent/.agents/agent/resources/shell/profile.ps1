# The profile, split by concern. $PROFILE dot-sources this file and nothing else; every part below is
# dot-sourced into the same scope, so the $script:-scoped tables each part owns stay visible to it.
$parts = @('env.ps1', 'hidden-bash-profile.ps1', 'docker.ps1', 'worktrees.ps1', 'procs.ps1')
foreach ($part in $parts) {
    $file = Join-Path $PSScriptRoot $part
    if (Test-Path $file) { . $file }
    else { Write-Host "profile: missing $part" -ForegroundColor Red }
}
