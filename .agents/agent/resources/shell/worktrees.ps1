# Build an active-first picker over the current repo's worktrees and return the chosen path (or $null).
# A row is "active" when it has uncommitted changes or commits ahead of the remote default; those sort
# to the top marked '*', the tree you're currently in is marked '>', and each row shows dirty count
# (~N), commits ahead (+N) and how long since its last commit — so the story you're working on stands
# out. All signals are local git, no network, so the picker is instant. Skips the harness's own agent
# scratch trees (.claude\worktrees\). Shared by cw and fw. An optional arg jumps straight to a
# uniquely-matching row, otherwise pre-fills the picker.
function Select-Worktree {
    param([string]$Query)

    if (-not (Get-Command fzf -ErrorAction SilentlyContinue)) {
        Write-Host "Missing dependency: fzf" -ForegroundColor Red; return $null
    }

    $porcelain = git worktree list --porcelain 2>$null
    if (-not $porcelain) { Write-Host "Not inside a git repo." -ForegroundColor Red; return $null }

    $here = (Get-Location).Path.TrimEnd('\')
    $def = git symbolic-ref --quiet --short refs/remotes/origin/HEAD 2>$null
    if ($def) { $def = $def.Trim() } else { $def = 'origin/main' }

    $trees = @(); $path = $null; $branch = $null
    foreach ($line in $porcelain) {
        if     ($line -like 'worktree *') { $path = ($line.Substring(9) -replace '/', '\').TrimEnd('\') }
        elseif ($line -like 'branch *')   { $branch = $line -replace '^branch refs/heads/', '' }
        elseif ($line -eq '') {
            if ($path -and $path -notmatch '\\\.claude\\worktrees\\') {
                $trees += [pscustomobject]@{ Path = $path; Branch = $(if ($branch) { $branch } else { '(detached)' }) }
            }
            $path = $null; $branch = $null
        }
    }
    if ($path -and $path -notmatch '\\\.claude\\worktrees\\') {
        $trees += [pscustomobject]@{ Path = $path; Branch = $(if ($branch) { $branch } else { '(detached)' }) }
    }
    if ($trees.Count -eq 0) { Write-Host "No worktrees found." -ForegroundColor Red; return $null }

    $rows = foreach ($t in $trees) {
        $dirty = @(git -C $t.Path status --porcelain 2>$null).Count
        $ahead = 0
        $a = git -C $t.Path rev-list --count "$def..HEAD" 2>$null
        if ($LASTEXITCODE -eq 0 -and $a) { $ahead = [int]$a }
        $ago = git -C $t.Path log -1 --format=%cr 2>$null
        $ts  = git -C $t.Path log -1 --format=%ct 2>$null
        $active = ($dirty -gt 0 -or $ahead -gt 0)
        $isHere = ($here -ieq $t.Path -or $here.StartsWith($t.Path + '\', [StringComparison]::OrdinalIgnoreCase))
        $mark = if ($isHere) { '>' } elseif ($active) { '*' } else { ' ' }
        $flags = @()
        if ($dirty -gt 0) { $flags += "~$dirty" }
        if ($ahead -gt 0) { $flags += "+$ahead" }
        [pscustomobject]@{
            Active = $active
            Sort   = $(if ($ts) { [long]$ts } else { 0 })
            Path   = $t.Path
            Label  = ("{0} {1} {2} {3}" -f $mark, $t.Branch.PadRight(32), (($flags -join ' ')).PadRight(9), $ago)
        }
    }

    $ordered = $rows | Sort-Object @{ Expression = 'Active'; Descending = $true }, @{ Expression = 'Sort'; Descending = $true }
    $items = $ordered | ForEach-Object { "{0}`t{1}" -f $_.Label, $_.Path }

    $q = $Query.Trim()
    $selection = $null
    if ($q) {
        $hit = @($ordered | Where-Object { $_.Label -like "*$q*" })
        if ($hit.Count -eq 1) { $selection = "{0}`t{1}" -f $hit[0].Label, $hit[0].Path }
    }
    if (-not $selection) {
        $fzfArgs = @('--prompt', 'worktree> ', '--height', '60%', '--border', '--delimiter', "`t", '--with-nth', '1')
        if ($q) { $fzfArgs += @('--query', $q) }
        $selection = $items | & fzf @fzfArgs
    }
    if ([string]::IsNullOrWhiteSpace($selection)) { return $null }
    return ($selection -split "`t")[1]
}

# Jump to one of this repo's worktrees and open an agent there - a session is rooted where it
# launches, so one session per tree. Run from anywhere inside the repo.
#   cw                  pick Claude or Codex, then pick the worktree
#   cw -Claude / -Codex skip the agent picker
function cw {
    param(
        [Parameter(ValueFromRemainingArguments = $true)][string[]]$Query,
        [switch]$Claude,
        [switch]$Codex
    )

    if (-not (Get-Command fzf -ErrorAction SilentlyContinue)) {
        Write-Host "Missing dependency: fzf" -ForegroundColor Red; return
    }

    $agent = 'Claude'
    if ($Codex) { $agent = 'Codex' }
    elseif (-not $Claude) {
        $pick = @('Claude', 'Codex') | & fzf --prompt 'agent> ' --height '30%' --border --header 'open which agent? ESC = Claude'
        if (-not [string]::IsNullOrWhiteSpace($pick)) { $agent = $pick.Trim() }
    }

    $target = Select-Worktree -Query ($Query -join ' ')
    if (-not $target) { return }
    Write-Host "-> $target ($agent)" -ForegroundColor DarkGray
    Set-Location -LiteralPath $target
    if ($agent -eq 'Codex') { codex } else { claude }
}

# Like cw, but only cd into the picked worktree — no Claude launch. For when kandev (or you) starts the
# CLI at the tree, or you just want to be there.
function fw {
    param([Parameter(ValueFromRemainingArguments = $true)][string[]]$Query)
    $target = Select-Worktree -Query ($Query -join ' ')
    if (-not $target) { return }
    Write-Host "-> $target" -ForegroundColor DarkGray
    Set-Location -LiteralPath $target
}
