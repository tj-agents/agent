param([switch]$ApplyCodexHookTrust, [switch]$ApplyProfilePlan, [switch]$ApplyUserEnvironment)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Assert-RegularUnlinkedFile([string]$Path) {
    $item = Get-Item -Force -LiteralPath $Path -ErrorAction Stop
    if ($item.LinkType) { throw "File is a link: $Path" }
    if ($IsWindows) {
        $links = @(& fsutil hardlink list $Path 2>$null)
        if ($LASTEXITCODE -ne 0 -or $links.Count -ne 1) { throw "File is linked: $Path" }
        return
    }
    $links = (& stat -c %h -- $Path).Trim()
    if ($LASTEXITCODE -ne 0 -or $links -ne '1') { throw "File is linked: $Path" }
}

$generatedRoot = Join-Path $PSScriptRoot 'plugins'
$generatedRoot = Join-Path $generatedRoot 'agent'
$generatedRoot = Join-Path $generatedRoot '.agents'
$generatedRoot = Join-Path $generatedRoot 'agent'
$installerRoot = Join-Path $generatedRoot 'resources'
$installerRoot = Join-Path $installerRoot 'installer'
$generatedResource = Join-Path $installerRoot ('install' + '.' + 'ps1')
if (Test-Path -LiteralPath $generatedResource -PathType Leaf) {
    Assert-RegularUnlinkedFile $PSCommandPath
    Assert-RegularUnlinkedFile $generatedResource
    if ((Get-FileHash -LiteralPath $PSCommandPath -Algorithm SHA256).Hash -ne (Get-FileHash -LiteralPath $generatedResource -Algorithm SHA256).Hash) {
        throw 'The packaged installer does not match the root installer'
    }
}

if ($ApplyProfilePlan) {
    $payload = $input | Out-String | ConvertFrom-Json -AsHashtable
    $updates = @($payload.updates)
    if ($updates.Count -eq 0) { throw 'Profile plan has no updates' }
    $profileHome = [IO.Path]::GetFullPath([string]$payload.home)
    $documents = if ($null -eq $payload.documents) { $null } else { [IO.Path]::GetFullPath([string]$payload.documents) }
    $allowed = @(
        (Join-Path $profileHome '.bashrc'),
        (Join-Path $profileHome '.zshrc'),
        (Join-Path $profileHome '.config/powershell/Microsoft.PowerShell_profile.ps1')
    ) | ForEach-Object { [IO.Path]::GetFullPath($_) }
    if ($null -ne $documents) {
        $allowed += @(
            (Join-Path $documents 'WindowsPowerShell/Microsoft.PowerShell_profile.ps1'),
            (Join-Path $documents 'PowerShell/Microsoft.PowerShell_profile.ps1')
        ) | ForEach-Object { [IO.Path]::GetFullPath($_) }
    }
    foreach ($update in $updates) {
        if ((@($update.Keys | Sort-Object) -join ',') -ne 'expected,path,replacement') { throw 'Profile plan update is invalid' }
        $path = [IO.Path]::GetFullPath([string]$update.path)
        if ($allowed -notcontains $path) { throw "Profile path is not allowed: $path" }
        $parent = Split-Path -Parent $path
        while ($parent -and $parent.StartsWith($profileHome, [StringComparison]::OrdinalIgnoreCase)) {
            if ((Test-Path -LiteralPath $parent) -and (Get-Item -Force -LiteralPath $parent).LinkType) { throw "Profile parent is a link: $parent" }
            if ($parent -eq $profileHome) { break }
            $parent = Split-Path -Parent $parent
        }
        if (Test-Path -LiteralPath $path) { Assert-RegularUnlinkedFile $path }
        $expected = if ($null -eq $update.expected) { $null } else { [Convert]::FromBase64String([string]$update.expected) }
        $current = if (Test-Path -LiteralPath $path -PathType Leaf) { [IO.File]::ReadAllBytes($path) } else { $null }
        if (($null -eq $expected) -ne ($null -eq $current) -or ($null -ne $expected -and -not [Linq.Enumerable]::SequenceEqual[byte]([byte[]]$expected, [byte[]]$current))) { throw "Profile changed before update: $path" }
        $replacement = [Convert]::FromBase64String([string]$update.replacement)
        [IO.Directory]::CreateDirectory((Split-Path -Parent $path)) | Out-Null
        if ($null -ne $current) { [IO.File]::WriteAllBytes("$path.pre-tj-agents.bak", $current) }
        $temporary = Join-Path (Split-Path -Parent $path) ('.' + [IO.Path]::GetRandomFileName())
        try {
            [IO.File]::WriteAllBytes($temporary, $replacement)
            [IO.File]::Move($temporary, $path, $true)
        } finally {
            if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
        }
    }
    exit 0
}

if ($ApplyUserEnvironment) {
    if (-not $IsWindows) { throw 'User environment updates require Windows' }
    $payload = $input | Out-String | ConvertFrom-Json -AsHashtable
    $variables = $payload.variables
    $allowed = @('TJ_AGENTS_REAL_GIT_BASH', 'CLAUDE_CODE_GIT_BASH_PATH')
    if ($null -eq $variables -or @($variables.Keys | Where-Object { $_ -notin $allowed }).Count -ne 0) { throw 'User environment plan is invalid' }
    foreach ($name in $variables.Keys) {
        $value = $variables[$name]
        if ($value -isnot [string] -or -not $value) { throw 'User environment plan is invalid' }
        [Environment]::SetEnvironmentVariable($name, $value, 'User')
    }
    Add-Type -Namespace TjAgents -Name EnvironmentNotice -MemberDefinition '[DllImport("user32.dll", CharSet=CharSet.Unicode, SetLastError=true)] public static extern IntPtr SendMessageTimeout(IntPtr hWnd, uint Msg, UIntPtr wParam, string lParam, uint flags, uint timeout, out UIntPtr result);' -ErrorAction SilentlyContinue
    $result = [UIntPtr]::Zero
    [void][TjAgents.EnvironmentNotice]::SendMessageTimeout([IntPtr]0xffff, 0x001A, [UIntPtr]::Zero, 'Environment', 2, 5000, [ref]$result)
    exit 0
}

if ($ApplyCodexHookTrust) {
    $payload = $input | Out-String | ConvertFrom-Json -AsHashtable
    $projects = @($payload.projects)
    $updates = $payload.updates
    if ($projects.Count -eq 0 -or $updates -isnot [hashtable] -or $updates.Count -eq 0) { throw 'Codex hook trust payload is invalid' }
    $selected = @{}
    foreach ($project in $projects) {
        $root = [IO.Path]::GetFullPath([string]$project)
        $selection = Join-Path $root '.agents/plugins.json'
        if (-not (Test-Path -LiteralPath $selection -PathType Leaf)) { throw "Codex hook trust target is not selected: $root" }
        $top = (& git -C $root rev-parse --show-toplevel).Trim()
        if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($top) -ne $root) { throw "Codex hook trust target is not a repository root: $root" }
        $plugins = (Get-Content -Raw -LiteralPath $selection | ConvertFrom-Json -AsHashtable).plugins
        if ($plugins -isnot [Array]) { throw "Codex hook trust selection is invalid: $root" }
        foreach ($identity in $plugins) { if ($identity -is [string]) { $selected[$identity] = $true } }
    }
    foreach ($key in $updates.Keys) {
        if ($key -notmatch '^(?<identity>[^:]+@tj-agents):') { throw "Codex hook trust key is invalid: $key" }
        if (-not $selected.ContainsKey($Matches.identity)) { throw "Codex hook trust key is not selected: $key" }
        $update = $updates[$key]
        if ($update -isnot [hashtable] -or (@($update.Keys | Sort-Object) -join ',') -ne 'trusted_hash' -or $update.trusted_hash -isnot [string] -or -not $update.trusted_hash) { throw "Codex hook trust update is invalid: $key" }
    }
    $codex = Get-Command codex -ErrorAction Stop
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = [Diagnostics.ProcessStartInfo]::new($codex.Source, 'app-server')
    $process.StartInfo.UseShellExecute = $false
    $process.StartInfo.RedirectStandardInput = $true
    $process.StartInfo.RedirectStandardOutput = $true
    $process.StartInfo.CreateNoWindow = $true
    if (-not $process.Start()) { throw 'Codex app server did not start' }
    function Request([int]$Id, [string]$Method, $Params) {
        $process.StandardInput.WriteLine((@{ id = $Id; method = $Method; params = $Params } | ConvertTo-Json -Compress -Depth 10))
        $process.StandardInput.Flush()
        while ($true) {
            $line = $process.StandardOutput.ReadLine()
            if ($null -eq $line) { throw "Codex app server stopped during $Method" }
            $reply = $line | ConvertFrom-Json -AsHashtable
            if ($reply.id -ne $Id) { continue }
            if ($reply.ContainsKey('error')) { throw "Codex $Method failed: $($reply.error | ConvertTo-Json -Compress)" }
            return $reply.result
        }
    }
    try {
        [void](Request 1 'initialize' @{ clientInfo = @{ name = 'tj-agents-installer'; version = '1.0.0' }; capabilities = @{ experimentalApi = $true } })
        $process.StandardInput.WriteLine('{"method":"initialized"}')
        $process.StandardInput.Flush()
        $listing = Request 2 'hooks/list' @{ cwds = $projects }
        $found = @{}
        foreach ($entry in @($listing.data)) { foreach ($hook in @($entry.hooks)) { $found[$hook.key] = $hook } }
        foreach ($key in $updates.Keys) {
            $hook = $found[$key]
            if ($key -notmatch '^[^:]+@tj-agents:' -or $null -eq $hook -or $hook.currentHash -ne $updates[$key].trusted_hash -or $hook.trustStatus -notin @('untrusted', 'modified')) { throw "Codex hook trust plan is stale or invalid: $key" }
        }
        [void](Request 3 'config/batchWrite' @{ edits = @(@{ keyPath = 'hooks.state'; value = $updates; mergeStrategy = 'upsert' }); reloadUserConfig = $true })
        $listing = Request 4 'hooks/list' @{ cwds = $projects }
        $found = @{}
        foreach ($entry in @($listing.data)) { foreach ($hook in @($entry.hooks)) { $found[$hook.key] = $hook } }
        foreach ($key in $updates.Keys) {
            $hook = $found[$key]
            if ($null -eq $hook -or $hook.trustStatus -ne 'trusted' -or $hook.currentHash -ne $updates[$key].trusted_hash) { throw "Codex hook trust did not converge: $key" }
        }
    } finally {
        if (-not $process.HasExited) { $process.Kill() }
        $process.Dispose()
    }
    exit 0
}

$python = if ($IsWindows -or $env:OS -eq 'Windows_NT') { 'python' } else { 'python3' }
$generatedRuntime = Join-Path $generatedRoot 'runtime'
$generatedRuntime = Join-Path $generatedRuntime ('install' + '.' + 'py')
$packagedRuntime = Join-Path $PSScriptRoot '../../runtime'
$packagedRuntime = Join-Path $packagedRuntime ('install' + '.' + 'py')
$runtime = if (Test-Path -LiteralPath $generatedRuntime -PathType Leaf) { $generatedRuntime } else { $packagedRuntime }
if (-not (Test-Path -LiteralPath $runtime -PathType Leaf)) { throw "The packaged installer runtime is unavailable: $runtime" }
& $python -B $runtime @args
exit $LASTEXITCODE
