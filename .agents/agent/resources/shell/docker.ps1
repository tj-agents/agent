# Nuke all of Docker: stop the watchdog service, force-kill every process tree,
# shut down the WSL backend. Self-elevates via UAC (one "Yes" click).
function killdocker {
    $script = @'
Stop-Service com.docker.service -Force -ErrorAction SilentlyContinue
foreach ($n in 'Docker Desktop','com.docker.backend','com.docker.build','com.docker.dev-envs','dockerd','vpnkit') {
    taskkill /F /T /IM "$n.exe" 2>$null
}
Get-Process *docker*,*vpnkit* -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
wsl --shutdown
Start-Sleep -Milliseconds 500
$left = Get-Process *docker* -ErrorAction SilentlyContinue
if ($left) { Write-Host "STILL ALIVE: $($left.Id -join ', ')" -ForegroundColor Red }
else       { Write-Host "Docker fully killed." -ForegroundColor Green }
Start-Sleep -Seconds 3
'@
    $enc = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes($script))
    Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile','-EncodedCommand',$enc
}
