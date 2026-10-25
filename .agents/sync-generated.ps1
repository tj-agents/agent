#!/usr/bin/env pwsh
[CmdletBinding()]
param([switch]$Check)
$ErrorActionPreference = 'Stop'
$arguments = @('-B', (Join-Path $PSScriptRoot 'sync_generated.py'), '--root', (Split-Path -Parent $PSScriptRoot))
if ($Check) { $arguments += '--check' }
try {
    & python @arguments
    $exitCode = $LASTEXITCODE
    if ($null -eq $exitCode) { throw 'Python returned no process exit code.' }
}
catch {
    Write-Error $_
    exit 1
}
exit $exitCode
