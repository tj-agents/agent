# Noise we never want to navigate into.
$script:FuzzyExcludes = @(
    '.git', 'node_modules', 'target', 'bin', 'obj', 'build',
    'cmake-build-debug', 'cmake-build-release', 'out', 'dist',
    '.vs', '.vscode', '.idea', '__pycache__', '.cache', '.next',
    '.venv', 'venv', 'packages', 'Debug', 'Release',
    '.pytest_cache', '.mypy_cache', 'worktrees'
)

# Score a candidate relative path against a query, ignoring separators and case
# so "cppnote" matches "cpp\note". Higher is better; -1 means no match.
function Get-FuzzyScore {
    param([string]$Query, [string]$Path)

    $nq = ($Query.ToLower() -replace '[\\/ _\-.]', '')
    if ([string]::IsNullOrEmpty($nq)) { return -1 }

    $segs  = $Path.ToLower() -split '\\'
    $nf    = ($Path.ToLower() -replace '[\\/ _\-.]', '')
    $nl    = ($segs[-1]       -replace '[\\/ _\-.]', '')
    $depth = $segs.Count

    if ($nf -eq $nq)         { return 1000 - $depth }                       # whole path collapses to query
    if ($nl -eq $nq)         { return  950 - $depth }                       # leaf folder is the query
    if ($nf.EndsWith($nq))   { return  850 - $depth }                       # query is the tail of the path
    if ($nl.StartsWith($nq)) { return  800 - $depth }                       # leaf starts with query
    if ($nf.Contains($nq))   { return  700 - $depth - $nf.IndexOf($nq) }    # query appears somewhere

    # Last resort: characters of the query appear in order (subsequence).
    $qi = 0
    foreach ($c in $nf.ToCharArray()) {
        if ($qi -lt $nq.Length -and $c -eq $nq[$qi]) { $qi++ }
    }
    if ($qi -eq $nq.Length) { return 300 - $depth - $nf.Length }
    return -1
}

function Invoke-Fuzzycd {
    param([string]$Root, [string]$Prompt, [string]$Query)

    if (!(Test-Path -LiteralPath $Root)) {
        Write-Host "Root path not found: $Root" -ForegroundColor Red
        return
    }
    foreach ($dep in 'fd', 'fzf') {
        if (-not (Get-Command $dep -ErrorAction SilentlyContinue)) {
            Write-Host "Missing dependency: $dep" -ForegroundColor Red
            return
        }
    }

    $rootFull = (Resolve-Path -LiteralPath $Root).Path.TrimEnd('\')

    # --no-ignore on purpose: project folders nested under a config repo (e.g.
    # cpp/ whose .gitignore is "/*") are gitignored but we still navigate them.
    $fdArgs = @('.', $rootFull, '--type', 'd', '--hidden', '--no-ignore')
    foreach ($e in $script:FuzzyExcludes) { $fdArgs += @('--exclude', $e) }
    $subdirs = & fd @fdArgs

    # Map the relative path (what we match and show) to the full path (where we cd).
    $map = [ordered]@{}
    $map['.'] = $rootFull
    foreach ($d in $subdirs) {
        $full = ($d -replace '/', '\').TrimEnd('\')
        $rel  = $full
        if ($full.Length -gt $rootFull.Length -and
            $full.Substring(0, $rootFull.Length).Equals($rootFull, [StringComparison]::OrdinalIgnoreCase)) {
            $rel = $full.Substring($rootFull.Length).TrimStart('\')
        }
        if (-not [string]::IsNullOrEmpty($rel)) { $map[$rel] = $full }
    }
    $choices = @($map.Keys) | Sort-Object

    # No query: interactive picker (path-aware scheme ranks folder names better).
    if ([string]::IsNullOrWhiteSpace($Query)) {
        $selection = $choices | & fzf --prompt $Prompt --height '60%' --border --scheme=path
        if ([string]::IsNullOrWhiteSpace($selection)) { return }
        Set-Location -LiteralPath $map[$selection]
        return
    }

    # Query: jump straight to the closest match.
    $best = $null; $bestScore = -1
    foreach ($rel in $choices) {
        $s = Get-FuzzyScore $Query $rel
        if ($s -gt $bestScore) { $bestScore = $s; $best = $rel }
    }

    # Nothing matched: fall back to the picker, pre-filled with the query.
    if ($bestScore -lt 0) {
        Write-Host "No match for '$Query' - opening picker" -ForegroundColor Yellow
        $selection = $choices | & fzf --prompt $Prompt --height '60%' --border --scheme=path --query $Query
        if ([string]::IsNullOrWhiteSpace($selection)) { return }
        Set-Location -LiteralPath $map[$selection]
        return
    }

    Write-Host "-> $best" -ForegroundColor DarkGray
    Set-Location -LiteralPath $map[$best]
}

function f  { param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Query) Invoke-Fuzzycd (Join-Path $env:USERPROFILE 'source\repos')   'Repos folder> ' ($Query -join ' ') }
function fp { param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Query) Invoke-Fuzzycd (Join-Path $env:USERPROFILE 'CLionProjects')  'CLion folder> ' ($Query -join ' ') }
