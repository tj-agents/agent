$python = if ($IsWindows -or $env:OS -eq 'Windows_NT') { 'python' } else { 'python3' }
& $python -B (Join-Path $PSScriptRoot 'plugins/agent/hooks/install.py') @args
exit $LASTEXITCODE
