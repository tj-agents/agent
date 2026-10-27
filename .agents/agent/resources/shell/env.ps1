$env:MSBUILDDISABLENODEREUSE = '1'
$env:DOTNET_CLI_USE_MSBUILD_SERVER = '0'
$env:DOTNET_PROCESSOR_COUNT = '4'

# Ensure the Aspire CLI is on PATH for every session (host-process env can go stale).
$aspireBin = "$env:USERPROFILE\.aspire\bin"
if ((Test-Path $aspireBin) -and ($env:PATH -notlike "*$aspireBin*")) {
    $env:PATH = "$aspireBin;$env:PATH"
}
