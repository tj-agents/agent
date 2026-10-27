# JSON unescape for transcript text. The escaped backslash is parked on a sentinel first because -replace
# is case-insensitive, so a plain '\\t' rule eats the T out of every C:\Users\TommySeery in the index.
function Expand-CrEscapes {
    param([string]$Text)
    if (-not $Text) { return '' }
    $sentinel = [char]1
    $t = $Text -creplace '\\\\', $sentinel
    $t = $t -creplace '\\n', ' ' -creplace '\\t', ' ' -creplace '\\r', ' ' -creplace '\\"', '"' -creplace '\\/', '/'
    $t = $t.Replace($sentinel, '\')
    $t = $t -replace '</?(local-)?command-[a-z]+>', ' '
    return ($t -replace '[\t\r\n]', ' ' -replace '\s+', ' ').Trim()
}

# Wrap one long string to pane width, dropping whatever the console codepage can't print.
function Split-CrLines {
    param([string]$Text)
    $t = $Text -replace '[^\x20-\x7E]', ' '
    return @(([regex]::Replace($t, '(.{1,96})(?:\s+|$)', "`$1`n") -split "`n") | Where-Object { $_.Trim() })
}

# Same unescaping as Expand-CrEscapes, but the message keeps its own line breaks. The haystack wants the
# opposite - one flat line per session for findstr - which is why the two cannot be the same function.
function Expand-CrBlock {
    param([string]$Text)
    if (-not $Text) { return '' }
    $sentinel = [char]1
    $t = $Text -creplace '\\\\', $sentinel
    $t = $t -creplace '\\n', "`n" -creplace '\\t', '    ' -creplace '\\r', '' -creplace '\\"', '"' -creplace '\\/', '/'
    $t = $t.Replace($sentinel, '\')
    $t = $t -replace '</?(local-)?command-[a-z]+>', ''
    return $t.Trim()
}

# Columns the pane will actually have: 55% of the window, less its border and the gutter fzf keeps for
# the scroll indicator. Panes are written during indexing, which runs inside cr, so the live width is
# known at the only moment it can be baked in.
function Get-CrPaneWidth {
    $cols = 120
    try { if ($Host.UI.RawUI.WindowSize.Width -gt 0) { $cols = $Host.UI.RawUI.WindowSize.Width } } catch { }
    return [Math]::Max(40, [int]($cols * 0.55) - 8)
}

# Word wrap for one already-plain line. Splitting before the SGR goes on keeps the width arithmetic
# honest - an escape sequence costs bytes and no columns, so wrapping styled text measures it wrong.
function Split-CrWrap {
    param([string]$Text, [int]$Width)
    if ($Text.Length -le $Width) { return @($Text) }
    $out = New-Object System.Collections.Generic.List[string]
    $line = ''
    foreach ($w in ($Text -split ' ')) {
        if (-not $line) { $line = $w; continue }
        if (($line.Length + 1 + $w.Length) -le $Width) { $line += ' ' + $w; continue }
        [void]$out.Add($line)
        $line = $w
    }
    if ($line) { [void]$out.Add($line) }
    # A single token longer than the pane - a path, a URL - still has to be cut, or it drags the column
    # out and fzf truncates everything after it.
    $final = New-Object System.Collections.Generic.List[string]
    foreach ($l in $out) {
        $s = $l
        while ($s.Length -gt $Width) { [void]$final.Add($s.Substring(0, $Width)); $s = $s.Substring($Width) }
        [void]$final.Add($s)
    }
    return $final.ToArray()
}

# Tool calls, rendered the way Claude Code writes them: the tool's name and the one argument that says
# what it acted on. Each call's input is read from its own slice of the line, because a message carrying
# several calls would otherwise hand the first one's name the last one's path.
function Get-CrToolCalls {
    param([string]$Line)
    $calls = New-Object System.Collections.Generic.List[string]
    $ms = [regex]::Matches($Line, '"type":"tool_use","id":"[^"]*","name":"([^"]+)","input":')
    for ($i = 0; $i -lt $ms.Count; $i++) {
        $start = $ms[$i].Index + $ms[$i].Length
        $end = if ($i + 1 -lt $ms.Count) { $ms[$i + 1].Index } else { $Line.Length }
        $seg = $Line.Substring($start, $end - $start)
        $arg = ''
        foreach ($k in @('command', 'file_path', 'pattern', 'notebook_path', 'path', 'url', 'skill', 'query', 'description', 'prompt')) {
            $m = [regex]::Match($seg, '"' + $k + '":"((?:[^"\\]|\\.)*)"')
            if ($m.Success) { $arg = Expand-CrEscapes $m.Groups[1].Value; break }
        }
        if ($arg -match '^(?:[A-Za-z]:[\\/]|[\\/])[^\s]*$') { $arg = ($arg -split '[\\/]')[-1] }
        [void]$calls.Add($ms[$i].Groups[1].Value + '(' + $arg + ')')
    }
    return $calls.ToArray()
}

# The same two extractions against Codex's rollout format, where a shell call arrives as JS wrapping the
# command rather than as the command itself - the wrapper is unwrapped so the pane shows what was run.
function Get-CodexToolCalls {
    param([string]$Line)
    $calls = New-Object System.Collections.Generic.List[string]
    foreach ($m in [regex]::Matches($Line, '"type":"(?:function_call|custom_tool_call|local_shell_call)"(.{0,1200}?)("arguments"|"input"):"((?:[^"\\]|\\.)*)"')) {
        $seg = $m.Groups[1].Value
        $body = $m.Groups[3].Value
        $name = ''
        $nm = [regex]::Match($seg, '"name":"([^"]+)"')
        if ($nm.Success) { $name = $nm.Groups[1].Value }
        if (-not $name) { $name = 'tool' }
        $cmd = [regex]::Match($body, '\\"cmd\\":\\"((?:[^"\\]|\\.)*?)\\"')
        # Escaped twice over - once into the JS wrapper, once into the rollout line - so the backslash
        # pairs left standing after one unescape are collapsed explicitly. A second full pass would eat
        # the n out of any path like C:\notes.
        $arg = if ($cmd.Success) { (Expand-CrEscapes ($cmd.Groups[1].Value -replace '\\\\"', '"')) -replace '\\\\', '\' } else { Expand-CrEscapes $body }
        [void]$calls.Add($name + '(' + $arg + ')')
    }
    return $calls.ToArray()
}

function Get-CodexToolResults {
    param([string]$Line)
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($m in [regex]::Matches($Line, '"type":"(?:function_call_output|custom_tool_call_output|local_shell_call_output)".{0,400}?"output":"((?:[^"\\]|\\.)*)"')) {
        $lines = @((Expand-CrBlock $m.Groups[1].Value) -split "`n" | Where-Object { $_.Trim() })
        if ($lines.Count -eq 0) { continue }
        # Long outputs still collapse, the way Claude Code collapses them - one file dump would
        # otherwise evict the conversation from the pane. Anything short enough to read stays whole.
        $keep = @($lines | Select-Object -First 20 | ForEach-Object { $_.TrimEnd() })
        if ($lines.Count -gt 20) { $keep += ([string][char]0x2026 + ' +' + ($lines.Count - 20) + ' lines') }
        [void]$out.Add($keep -join "`n")
    }
    return $out.ToArray()
}

# What a tool printed back.
function Get-CrToolResults {
    param([string]$Line)
    $raw = New-Object System.Collections.Generic.List[string]
    foreach ($m in [regex]::Matches($Line, '"type":"tool_result","content":"((?:[^"\\]|\\.)*)"')) { [void]$raw.Add($m.Groups[1].Value) }
    foreach ($m in [regex]::Matches($Line, '"type":"tool_result","content":\[\{"type":"text","text":"((?:[^"\\]|\\.)*)"')) { [void]$raw.Add($m.Groups[1].Value) }
    $out = New-Object System.Collections.Generic.List[string]
    foreach ($c in $raw) {
        $lines = @((Expand-CrBlock $c) -split "`n" | Where-Object { $_.Trim() })
        if ($lines.Count -eq 0) { continue }
        # Long outputs still collapse, the way Claude Code collapses them - one file dump would
        # otherwise evict the conversation from the pane. Anything short enough to read stays whole.
        $keep = @($lines | Select-Object -First 20 | ForEach-Object { $_.TrimEnd() })
        if ($lines.Count -gt 20) { $keep += ([string][char]0x2026 + ' +' + ($lines.Count - 20) + ' lines') }
        [void]$out.Add($keep -join "`n")
    }
    return $out.ToArray()
}

# The pane is typed straight into the terminal by cmd, so anything the console codepage cannot render has
# to go - but the punctuation an agent actually writes folds to ASCII rather than being blanked out. The
# folds are keyed by code point because this profile is read as ANSI, which would mangle the literals.
$script:CrAsciiFolds = @{
    0x2018 = "'"; 0x2019 = "'"; 0x201A = "'"; 0x201B = "'"; 0x2032 = "'"
    0x201C = '"'; 0x201D = '"'; 0x201E = '"'; 0x2033 = '"'
    0x2010 = '-'; 0x2011 = '-'; 0x2012 = '-'; 0x2013 = '-'; 0x2014 = '-'; 0x2015 = '-'
    0x2026 = '...'; 0x00A0 = ' '; 0x2007 = ' '; 0x2009 = ' '; 0x202F = ' '; 0x200B = ''
    0x2022 = '*'; 0x25CF = '*'; 0x25AA = '*'; 0x25E6 = '*'; 0x00B7 = '*'
    0x2713 = 'v'; 0x2714 = 'v'; 0x2717 = 'x'; 0x2718 = 'x'; 0x2705 = 'v'
    0x2192 = '->'; 0x21D2 = '=>'; 0x2190 = '<-'; 0x21D0 = '<='; 0x2794 = '->'
}

function ConvertTo-CrAscii {
    param([string]$Text)
    $sb = New-Object System.Text.StringBuilder $Text.Length
    foreach ($c in $Text.ToCharArray()) {
        $n = [int]$c
        if (($n -ge 0x20 -and $n -le 0x7E) -or $n -eq 10) { [void]$sb.Append($c); continue }
        $fold = $script:CrAsciiFolds[$n]
        if ($null -ne $fold) { [void]$sb.Append($fold) }
    }
    return $sb.ToString()
}

# fzf pipes the pane straight through as bytes, so the SGR the agents' own CLIs paint with survives here
# too - markdown is rendered rather than stripped. Code spans are parked on sentinels first: styling them
# last would let the emphasis rules chew through the one span whose contents are meant to stay literal.
function Format-CrInline {
    param([string]$Text)
    $e = [char]27
    $slots = New-Object System.Collections.Generic.List[string]
    $t = [regex]::Replace($Text, '`([^`]+)`', {
            param($m)
            [void]$slots.Add("$e[38;5;180m" + $m.Groups[1].Value + "$e[39m")
            [char]2 + ($slots.Count - 1).ToString() + [char]3
        })
    $t = [regex]::Replace($t, '\[([^\]]+)\]\(([^)\s]+)\)', "$e[4m`$1$e[24m $e[38;5;240m`$2$e[39m")
    $t = [regex]::Replace($t, '\*\*\*([^*]+)\*\*\*', "$e[1;3m`$1$e[23;22m")
    $t = [regex]::Replace($t, '\*\*([^*]+)\*\*', "$e[1m`$1$e[22m")
    $t = [regex]::Replace($t, '(?<![\w*])\*([^*\s][^*]*)\*(?![\w*])', "$e[3m`$1$e[23m")
    $t = [regex]::Replace($t, '(?<![\w_])__([^_]+)__(?![\w_])', "$e[1m`$1$e[22m")
    $t = [regex]::Replace($t, [char]2 + '(\d+)' + [char]3, { param($m) $slots[[int]$m.Groups[1].Value] })
    return $t
}

# Line-level markdown: the structure a heading or a list marker was carrying becomes the thing you can
# see, so the syntax itself does not have to be read.
function Format-CrMarkdown {
    param([string]$Line)
    $e = [char]27
    $l = $Line.TrimEnd()
    if ($l -match '^\s*[-=_*]{3,}\s*$') { return "$e[38;5;240m" + ([string][char]0x2500 * 12) + "$e[39m" }
    if ($l -match '^(\s*)#{1,6}\s+(.+)$') { return $Matches[1] + "$e[1;97m" + (Format-CrInline $Matches[2]) + "$e[0m" }
    if ($l -match '^(\s*)>\s?(.*)$') { return $Matches[1] + "$e[38;5;240m" + [char]0x2502 + "$e[39m $e[2m" + (Format-CrInline $Matches[2]) + "$e[22m" }
    if ($l -match '^(\s*)[-*+]\s+(.*)$') { return $Matches[1] + "$e[38;5;245m" + [char]0x2022 + "$e[39m " + (Format-CrInline $Matches[2]) }
    if ($l -match '^(\s*)(\d+)[.)]\s+(.*)$') { return $Matches[1] + "$e[38;5;245m" + $Matches[2] + ".$e[39m " + (Format-CrInline $Matches[3]) }
    return (Format-CrInline $l)
}

# One turn, laid out the way its own CLI lays it out: Claude's orange bullet with the reply running from
# it and the prompt caret over dimmed input, Codex's labelled speaker with the body beneath. Nothing is
# wrapped here - fzf word-wraps to the live pane width, and a wrap baked in now breaks on the next resize.
function Format-CrTurn {
    param([string]$Who, [string]$Text, [int]$Width = 96)
    $e = [char]27
    $dot = [char]0x25CF
    $elbow = [char]0x23BF
    $accent = "$e[38;5;173m"
    $muted = "$e[38;5;245m"
    $body = New-Object System.Collections.Generic.List[string]

    # Prefix for the turn's first line, and the indent every later line and every wrap lands on. Wrapping
    # happens here rather than in fzf because fzf's wrap indicator is one fixed string for the whole pane,
    # so a result indented five columns and a paragraph indented two cannot both hang correctly under it.
    switch ($Who) {
        'YOU' { $lead = "$muted>$e[0m "; $pad = '  ' }
        'UX' { $lead = ''; $pad = '  ' }
        'CX' { $lead = ''; $pad = '  ' }
        'TOOL' { $lead = "$accent$dot$e[0m "; $pad = '  ' }
        'RES' { $lead = "  $muted$elbow  "; $pad = '     ' }
        'CTOOL' { $lead = "$e[38;5;75m" + [char]0x2022 + "$e[0m "; $pad = '  ' }
        'CRES' { $lead = "  $muted" + [char]0x2514 + ' '; $pad = '    ' }
        default { $lead = "$accent$dot$e[0m "; $pad = '  ' }
    }
    $plain = $Who -eq 'YOU' -or $Who -eq 'UX'
    $verbatim = $Who -eq 'TOOL' -or $Who -eq 'RES' -or $Who -eq 'CTOOL' -or $Who -eq 'CRES'
    $result = $Who -eq 'RES' -or $Who -eq 'CRES'
    $room = [Math]::Max(24, $Width - $pad.Length)

    $inCode = $false
    $blank = $false
    foreach ($raw in ((ConvertTo-CrAscii $Text) -split "`n")) {
        if (-not $verbatim -and $raw -match '^\s*(```|~~~)') { $inCode = -not $inCode; continue }
        if (-not $raw.Trim()) { $blank = $body.Count -gt 0; continue }
        if ($blank) { [void]$body.Add(''); $blank = $false }
        foreach ($seg in (Split-CrWrap -Text $raw.TrimEnd() -Width $room)) {
            if ($result) { [void]$body.Add("$muted$seg$e[0m"); continue }
            if ($verbatim) { [void]$body.Add($seg); continue }
            if ($inCode) { [void]$body.Add("$e[38;5;109m$seg$e[0m"); continue }
            if ($plain) { [void]$body.Add("$e[38;5;252m$seg$e[0m"); continue }
            [void]$body.Add((Format-CrMarkdown $seg))
        }
    }
    if ($body.Count -eq 0) { return @() }

    # A tool call and its result are one visual unit, so the call takes no trailing blank line and its
    # result carries one - otherwise every Read would sit a line away from what it read.
    if ($Who -eq 'TOOL' -or $Who -eq 'CTOOL') {
        $m = [regex]::Match($Text, '^([^(]+)\((.*)\)$')
        $name = if ($m.Success) { $m.Groups[1].Value } else { $Text }
        $arg = if ($m.Success) { $m.Groups[2].Value } else { '' }
        $head = "$lead$e[1m$name$e[22m"
        if ($arg) { $head += "($muted" + (ConvertTo-CrAscii $arg) + "$e[39m)" }
        $wrapped = @(Split-CrWrap -Text ((ConvertTo-CrAscii $Text)) -Width $room)
        if ($wrapped.Count -le 1) { return @($head) }
        $rest = @($wrapped | Select-Object -Skip 1 | ForEach-Object { "$pad$muted$_$e[0m" })
        return @($head) + $rest
    }

    $out = New-Object System.Collections.Generic.List[string]
    if ($Who -eq 'UX') { $out.Add("$e[1;38;5;245muser$e[0m") }
    elseif ($Who -eq 'CX') { $out.Add("$e[1;38;5;75mcodex$e[0m") }
    if ($lead) {
        $out.Add($lead + $body[0])
        for ($i = 1; $i -lt $body.Count; $i++) { $out.Add($(if ($body[$i]) { $pad + $body[$i] } else { '' })) }
    }
    else {
        foreach ($l in $body) { $out.Add($(if ($l) { $pad + $l } else { '' })) }
    }
    $out.Add('')
    return $out.ToArray()
}

# Tagged turns in, rendered blocks out - one block per turn so the pane can drop whole turns to fit
# rather than opening halfway through somebody's paragraph.
function ConvertTo-CrTurnBlocks {
    param([string[]]$Turns, [int]$Width = 96)
    $blocks = New-Object System.Collections.Generic.List[object]
    foreach ($t in @($Turns)) {
        $f = $t -split "`t", 3
        if ($f.Count -lt 3) { continue }
        if ($f[0] -eq 'CMD') { continue }
        $txt = $f[2]
        if ($txt -match 'The messages below were generated by the user while running local commands') { continue }
        $rendered = @(Format-CrTurn -Who $f[0] -Text $txt -Width $Width)
        if ($rendered.Count -gt 0) { [void]$blocks.Add([pscustomobject]@{ Seq = [int]$f[1]; Lines = $rendered }) }
    }
    return $blocks
}

# Flatten a transcript into (preview, story, branch, haystack, tail). The haystack is every message's text
# collapsed onto one line so the cr bar can filter on anything that was said. Only genuine message text
# counts - thinking blocks, signatures, tool_use arguments and tool results are all skipped, which is what
# keeps a whole multi-MB session inside the cap instead of spending it on the first few file dumps.
# The tail is a separate rolling buffer of the newest turns, each tagged with who spoke. It has to be its
# own buffer: a long session blows the cap hours before it ends, so the end of the capped haystack is the
# middle of the session, not how it finished - and how it finished is the part that identifies it.
function Get-CrText {
    param([string]$Path)
    $preview = ''
    $cmdPreview = ''
    $branch = ''
    $head = New-Object System.Collections.Generic.List[object]
    $sb = [System.Text.StringBuilder]::new()
    $cap = 300000
    $tailCap = 100000
    $tail = New-Object System.Collections.Generic.Queue[object]
    $tailLen = 0
    $reader = $null
    try {
        # ReadWrite share: a session still open in another window holds its own transcript for append,
        # and a lock failure here would cache an empty haystack for the session most likely to be wanted.
        $fs = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $reader = New-Object IO.StreamReader($fs)
        while ($true) {
            $line = $reader.ReadLine()
            if ($null -eq $line) { break }
            if (-not $branch -and $line.Contains('"gitBranch"')) {
                $b = [regex]::Match($line, '"gitBranch":"([^"]+)"')
                if ($b.Success) { $branch = $b.Groups[1].Value }
            }
            if (-not ($line.Contains('"type":"user"') -or $line.Contains('"type":"assistant"'))) { continue }

            # Tool calls and their results are most of what a session looks like on screen, so they are
            # carried as their own turns. They never reach the haystack - one file dump would swallow the
            # whole cap - which is why they are enqueued here and the text below is what feeds $sb.
            if ($line.Contains('"tool_result"')) {
                foreach ($r in (Get-CrToolResults -Line $line)) {
                    $seq++; $rec = [pscustomobject]@{ Who = 'RES'; Seq = $seq; Text = $r }
                    if ($head.Count -lt 6) { [void]$head.Add($rec) }
                    $tail.Enqueue($rec)
                    $tailLen += $r.Length
                    while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
                }
                continue
            }

            $said = New-Object System.Collections.Generic.List[string]
            foreach ($mm in [regex]::Matches($line, '"role":"user","content":"((?:[^"\\]|\\.)*)"')) { $said.Add($mm.Groups[1].Value) }
            foreach ($mm in [regex]::Matches($line, '"type":"text","text":"((?:[^"\\]|\\.)*)"')) { $said.Add($mm.Groups[1].Value) }
            $calls = @(Get-CrToolCalls -Line $line)
            if ($said.Count -eq 0 -and $calls.Count -eq 0) { continue }
            if ($said.Count -eq 0) {
                foreach ($c in $calls) {
                    $seq++; $rec = [pscustomobject]@{ Who = 'TOOL'; Seq = $seq; Text = $c }
                    if ($head.Count -lt 6) { [void]$head.Add($rec) }
                    $tail.Enqueue($rec)
                    $tailLen += $c.Length
                    while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
                }
                continue
            }

            # Injected context (isMeta) is not something that was "said". Slash-command markup IS real and
            # stays in the haystack so `cr /techdebt` finds it; only the preview skips it, to show the
            # typed prompt.
            if ($line.Contains('"isMeta":true')) { continue }
            $isCmd = $line.Contains('<command-name>') -or $line.Contains('<command-message>')
            $isUser = $line.Contains('"type":"user"')
            if (-not $preview -and $isUser) {
                if ($isCmd) { if (-not $cmdPreview) { $cmdPreview = $said[0] } }
                elseif (-not $said[0].StartsWith('<local-command-')) { $preview = $said[0] }
            }

            # A slash command and its echoed stdout stay in the haystack, so `cr /techdebt` still finds
            # them, but they identify no session - tagged CMD, the rendered pane leaves them out.
            $turn = $said -join ' '
            $plumbing = $isCmd -or $line.Contains('<local-command-stdout>') -or $line.Contains('<task-notification>') -or
                        $line.Contains('<bash-input>') -or $line.Contains('<bash-stdout>') -or $line.Contains('<system-reminder>')
            $who = if ($plumbing) { 'CMD' } elseif ($isUser) { 'YOU' } else { 'CC' }
            $seq++; $rec = [pscustomobject]@{ Who = $who; Seq = $seq; Text = $turn }
            if ($who -ne 'CMD' -and $head.Count -lt 6) { [void]$head.Add($rec) }
            $tail.Enqueue($rec)
            $tailLen += $turn.Length
            while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
            foreach ($c in $calls) {
                $seq++; $rec = [pscustomobject]@{ Who = 'TOOL'; Seq = $seq; Text = $c }
                if ($head.Count -lt 6) { [void]$head.Add($rec) }
                $tail.Enqueue($rec)
                $tailLen += $c.Length
                while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
            }

            if ($sb.Length -lt $cap) {
                foreach ($s in $said) {
                    [void]$sb.Append($s)
                    [void]$sb.Append(' ')
                    if ($sb.Length -ge $cap) { break }
                }
            }
        }
    }
    catch { }
    finally { if ($reader) { $reader.Dispose() } }

    if (-not $preview) { $preview = $cmdPreview }
    $story = ''
    $m = [regex]::Match($preview, '_workitems/edit/(\d{4,7})|AB#(\d{4,7})|workitem=(\d{4,7})')
    if ($m.Success) {
        foreach ($g in 1..3) { if ($m.Groups[$g].Success) { $story = 'AB#' + $m.Groups[$g].Value; break } }
    }
    elseif ($branch -match '^AB#\d+$') { $story = $branch }

    $preview = Expand-CrEscapes $preview
    if ($preview.Length -gt 78) { $preview = $preview.Substring(0, 78) }
    if (-not $preview) { $preview = '(no prompt)' }

    # Two shapes of the same turns: flat for the haystack findstr greps, and TAB-tagged blocks that keep
    # their own line breaks for the pane, which renders them back into a conversation.
    $tailTurns = @()
    $tailText = [System.Text.StringBuilder]::new()
    foreach ($t in $tail) {
        $txt = Expand-CrEscapes $t.Text
        if (-not $txt) { continue }
        $tailTurns += ($t.Who + "`t" + $t.Seq + "`t" + (Expand-CrBlock $t.Text))
        [void]$tailText.Append($txt); [void]$tailText.Append(' ')
    }

    # Past the cap the head stops growing, so append the rolling tail: without it nothing said in the back
    # half of a long session is searchable at all, which is its own way of losing a conversation.
    $headTurns = @()
    foreach ($t in $head) { $headTurns += ($t.Who + "`t" + $t.Seq + "`t" + (Expand-CrBlock $t.Text)) }

    $hay = Expand-CrEscapes $sb.ToString()
    if ($sb.Length -ge $cap) { $hay = ($hay + ' ... ' + $tailText.ToString()).Trim() }

    return @{ Preview = $preview; Story = $story; Branch = $branch; Haystack = $hay; Tail = $tailTurns; Head = $headTurns }
}

# Pane content for the cr bar. fzf runs previews through cmd on Windows, which can neither slice a file
# nor hold a 200k-char argument, so each session gets two small files: the wrapped body findstr greps for
# the live query, and a pre-rendered transcript the pane types verbatim.
# The transcript runs oldest to newest and the pane opens on its end (fzf's preview `follow`), so the last
# exchange - the part that says which session this is - is what you land on, and scrolling up walks back
# towards the opening. It is built backwards from the newest turn for the same reason: when the budget
# runs out it is the oldest turns that fall off.
function Write-CrPreview {
    param([string]$Sid, [string]$Haystack, [string[]]$Tail, [string[]]$Head, [string]$Dir, [int]$Width = 96, [int]$MaxLines = 400)
    $e = [char]27
    $lines = Split-CrLines $Haystack
    if ($lines.Count -eq 0) { $lines = @('(nothing said)') }

    $blocks = New-Object System.Collections.Generic.List[object]
    $seen = @{}
    foreach ($blk in @(ConvertTo-CrTurnBlocks -Turns $Head -Width $Width) + @(ConvertTo-CrTurnBlocks -Turns @($Tail | Select-Object -Last 40) -Width $Width)) {
        if ($seen.ContainsKey($blk.Seq)) { continue }
        $seen[$blk.Seq] = $true
        [void]$blocks.Add($blk)
    }
    $blocks = @($blocks | Sort-Object Seq)

    # Whole turns only, newest first: a pane that opens halfway through somebody's paragraph reads as
    # noise, so the budget is spent backwards and whatever no longer fits is dropped off the top.
    $kept = New-Object System.Collections.Generic.List[object]
    $used = 0
    for ($b = $blocks.Count - 1; $b -ge 0; $b--) {
        $n = @($blocks[$b].Lines).Count
        if ($kept.Count -gt 0 -and ($used + $n) -gt $MaxLines) { break }
        $kept.Insert(0, $blocks[$b])
        $used += $n
    }

    $ends = New-Object System.Collections.Generic.List[string]
    if ($kept.Count -eq 0) {
        foreach ($l in @($lines | Select-Object -First 6)) { $ends.Add('  ' + $l) }
    }
    else {
        # Against the earliest turn actually available, not against 1 - a session opening on a filtered
        # slash command would otherwise always claim there is something above it.
        if ($kept[0].Seq -gt $blocks[0].Seq) { $ends.Add("$e[38;5;240m  " + ([string][char]0x2026) + " earlier turns$e[0m"); $ends.Add('') }
        for ($i = 0; $i -lt $kept.Count; $i++) {
            if ($i -gt 0 -and $kept[$i].Seq -gt ($kept[$i - 1].Seq + 1)) {
                $ends.Add("$e[38;5;240m  " + ([string][char]0x2026) + ' ' + ($kept[$i].Seq - $kept[$i - 1].Seq - 1) + " turns not shown$e[0m")
                $ends.Add('')
            }
            $ends.AddRange([string[]]$kept[$i].Lines)
        }
    }
    while ($ends.Count -gt 0 -and -not $ends[$ends.Count - 1]) { $ends.RemoveAt($ends.Count - 1) }
    try {
        [IO.File]::WriteAllLines((Join-Path $Dir "$Sid.txt"), [string[]]$lines)
        [IO.File]::WriteAllLines((Join-Path $Dir "$Sid.ends.txt"), $ends.ToArray())
    }
    catch { }
}

# The searchable index behind the cr bar: one TSV record per session (sid, mtime, size, branch, story,
# preview, haystack) cached at ~/.claude/cr-index.tsv. Only new or modified transcripts are re-parsed, so
# the first run is slow and every run after is instant. Returns a sid-keyed map for the requested files.
function Update-CrIndex {
    param([System.IO.FileInfo[]]$Files, [switch]$Rebuild, [int]$Width = 96)
    $cachePath = Join-Path $env:USERPROFILE '.claude\cr-index.tsv'
    # Bump when Get-CrText changes shape - a stale header forces a full re-parse instead of serving an
    # index built by the old (broken) logic, so fixes land without anyone remembering to run -Rebuild.
    $ver = 'crindex-v11'
    $prevDir = Join-Path $env:USERPROFILE '.claude\cr-preview'
    if (-not (Test-Path $prevDir)) { [void](New-Item -ItemType Directory -Path $prevDir -Force) }
    $cache = @{}
    if (-not $Rebuild -and (Test-Path $cachePath)) {
        try {
            $all = [IO.File]::ReadAllLines($cachePath)
            if ($all.Count -ge 1 -and $all[0] -eq $ver) {
                for ($i = 1; $i -lt $all.Count; $i++) {
                    $line = $all[$i]
                    if (-not $line) { continue }
                    $p = $line -split "`t", 7
                    if ($p.Count -eq 7) { $cache[$p[0]] = $p }
                }
            }
        }
        catch { $cache = @{} }
    }

    $result = @{}
    $parsed = 0
    foreach ($f in $Files) {
        $sid = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        $mt = $f.LastWriteTime.Ticks.ToString()
        $c = $cache[$sid]
        # A valid row with no pane file on disk would leave TAB blank forever, so the pane's absence
        # re-parses too.
        if ($c -and $c[1] -eq $mt -and (Test-Path (Join-Path $prevDir "$sid.ends.txt"))) { $result[$sid] = $c; continue }
        $t = Get-CrText -Path $f.FullName
        $rec = @($sid, $mt, $f.Length.ToString(), $t.Branch, $t.Story, $t.Preview, $t.Haystack)
        Write-CrPreview -Sid $sid -Haystack $t.Haystack -Tail $t.Tail -Head $t.Head -Dir $prevDir -Width $Width
        $cache[$sid] = $rec
        $result[$sid] = $rec
        $parsed++
    }

    if ($parsed -gt 0) {
        $lines = New-Object System.Collections.Generic.List[string]
        $lines.Add($ver)
        foreach ($k in $cache.Keys) { $lines.Add(($cache[$k] -join "`t")) }
        try { [IO.File]::WriteAllLines($cachePath, $lines.ToArray()) } catch { }
        if ($parsed -ge 5) { Write-Host "cr: indexed $parsed new/changed session(s)" -ForegroundColor DarkGray }
    }
    return $result
}

# Shared by cr (both agents): the repo's main path, whether we're in a repo, and which worktree to
# search - 'ALL', 'MAIN', or one other worktree's full path. Only asks (via fzf) when sitting in the main
# worktree with other worktrees present; otherwise a repo with several in-flight branches would get mixed
# together by default. -All short-circuits to 'ALL' without asking.
function Select-CrScope {
    param([switch]$All)

    $base = (Get-Location).Path
    $isRepo = $false
    $top = git rev-parse --show-toplevel 2>$null
    if ($LASTEXITCODE -eq 0 -and $top) { $base = ($top -replace '/', '\'); $isRepo = $true }
    $inMain = $isRepo -and ($base -notmatch '\.worktrees[\\/]')
    if ($base -match '^(?<b>.*?)\.worktrees[\\/]') { $base = $Matches['b'] }
    $base = $base.TrimEnd('\')

    $choice = 'ALL'
    $others = @()
    if ($isRepo -and $inMain -and -not $All) {
        $porcelain = git worktree list --porcelain 2>$null
        $wp = $null; $wb = $null
        foreach ($line in $porcelain) {
            if     ($line -like 'worktree *') { $wp = ($line.Substring(9) -replace '/', '\').TrimEnd('\') }
            elseif ($line -like 'branch *')   { $wb = $line -replace '^branch refs/heads/', '' }
            elseif ($line -eq '') {
                if ($wp -and $wp -ne $base -and $wp -notmatch '\\\.claude\\worktrees\\') {
                    $others += [pscustomobject]@{ Path = $wp; Branch = $(if ($wb) { $wb } else { '(detached)' }) }
                }
                $wp = $null; $wb = $null
            }
        }
        if ($wp -and $wp -ne $base -and $wp -notmatch '\\\.claude\\worktrees\\') {
            $others += [pscustomobject]@{ Path = $wp; Branch = $(if ($wb) { $wb } else { '(detached)' }) }
        }

        if ($others.Count -gt 0) {
            $items = @('All worktrees' + "`tALL")
            $items += (Split-Path $base -Leaf) + ' (main)' + "`tMAIN"
            for ($i = 0; $i -lt $others.Count; $i++) { $items += "$($others[$i].Branch)`t$i" }

            $pick = $items | & fzf --prompt 'scope> ' --height '40%' --border --delimiter "`t" --with-nth 1 `
                --header 'which worktree to search? ENTER picks / ESC = all'
            if ($pick) {
                $sel = ($pick -split "`t")[1]
                if ($sel -eq 'MAIN') { $choice = 'MAIN' }
                elseif ($sel -ne 'ALL') { $choice = $others[[int]$sel].Path }
            }
        }
    }

    return [pscustomobject]@{ Base = $base; IsRepo = $isRepo; Choice = $choice; Others = $others }
}

# Claude buckets a session under a folder named for its cwd with every non-alphanumeric flattened to a
# dash, so C:\...\Concertable\.worktrees\Fix-X and C:\...\Concertable-worktrees-Fix-X collide by design.
# Anything narrower (dropping only : \ /) leaves a dot in the key that the real folder name never has.
function Get-CrProjectKey {
    param([string]$Path)
    return ($Path -replace '[^a-zA-Z0-9]', '-')
}

# The folder name is a lossy flattening of the cwd, so the transcript's own recorded cwd is the only
# thing that can put the shell back where a session has to be resumed from.
function Get-CrSessionCwd {
    param([string]$Path)
    try {
        $fs = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $reader = New-Object IO.StreamReader($fs)
        try {
            for ($i = 0; $i -lt 40; $i++) {
                $line = $reader.ReadLine()
                if ($null -eq $line) { break }
                $m = [regex]::Match($line, '"cwd":"((?:[^"\\]|\\.)*)"')
                if ($m.Success) { return ($m.Groups[1].Value -replace '\\\\', '\') }
            }
        }
        finally { $reader.Dispose() }
    }
    catch { }
    return $null
}

# The cr search bar: type `cr` to open an fzf picker over this repo's Claude or Codex sessions, then keep
# typing to filter live across EVERYTHING said in them - every message, not just the opening prompt - and
# press ENTER to resume. Each row is a label (date, story, opening prompt) followed by the flattened body;
# fzf searches both, so any word spoken in a session finds it. Bodies are cached (~/.claude/cr-index.tsv,
# ~/.codex/cr-index.tsv) and only new/changed sessions are re-read, so the first run is slow and the rest
# are instant. PLAN marks a Claude session that entered plan mode and never finished one.
#   cr                  pick Claude or Codex, then open the bar for this repo (most recent 500 sessions)
#   cr tech debt        open it pre-filtered to sessions where "tech" AND "debt" were said
#   cr -Claude / -Codex skip the agent picker
#   cr -All             every project, not just this repo (CTRL-A does the same from inside the bar,
#                       keeping whatever is typed - and an empty scope widens to it on its own)
#   cr -Rebuild         discard the index and rebuild it from scratch
#   cr -List            print rows instead of opening the picker
function cr {
    param(
        [Parameter(Position = 0, ValueFromRemainingArguments = $true)][string[]]$Query,
        [string]$Text,
        [int]$Count = 500,
        [switch]$All,
        [switch]$List,
        [switch]$Rebuild,
        [switch]$Claude,
        [switch]$Codex
    )

    if (-not (Get-Command fzf -ErrorAction SilentlyContinue)) {
        Write-Host "Missing dependency: fzf" -ForegroundColor Red; return
    }

    $agent = 'Claude'
    if ($Codex) { $agent = 'Codex' }
    elseif (-not $Claude) {
        $pick = @('Claude', 'Codex') | & fzf --prompt 'agent> ' --height '30%' --border --header 'search whose sessions? ESC = Claude'
        if (-not [string]::IsNullOrWhiteSpace($pick)) { $agent = $pick.Trim() }
    }

    if ($agent -eq 'Codex') {
        Invoke-CodexCr -Query $Query -Text $Text -Count $Count -All:$All -List:$List -Rebuild:$Rebuild
        return
    }

    $root = Join-Path $env:USERPROFILE '.claude\projects'
    if (-not (Test-Path $root)) { Write-Host "No Claude history at $root" -ForegroundColor Red; return }

    $scope = Select-CrScope -All:$All
    $base = $scope.Base
    $key = Get-CrProjectKey $base
    $scopeExclude = @()
    if ($scope.Choice -eq 'MAIN') {
        $scopeExclude = @($scope.Others | ForEach-Object { Get-CrProjectKey $_.Path })
    }
    elseif ($scope.Choice -ne 'ALL') {
        $key = Get-CrProjectKey $scope.Choice
    }

    # Scope is this directory and everything that has ever been under it, repo or not. A polyrepo
    # container like Concertable holds b2b/pipeline/payment, and a folder since split up or deleted
    # still owns its sessions - gating descent on a repo being present today is what made them
    # unreachable. Breadth costs a longer list that fzf filters anyway; narrowness hides sessions.
    $allDirs = @(Get-ChildItem $root -Directory -ErrorAction SilentlyContinue)
    $dirs = @($allDirs | Where-Object { $_.Name -eq $key -or $_.Name -like "$key-*" })
    foreach ($ex in $scopeExclude) {
        $dirs = @($dirs | Where-Object { $_.Name -ne $ex -and $_.Name -notlike "$ex-*" })
    }
    # A session rooted at the container (Concertable, alongside Concertable\b2b) is invisible from every
    # checkout inside it - which is exactly how one goes missing. Fold in the container itself, and only
    # it, when it has sessions.
    if ($scope.IsRepo) {
        $parent = Split-Path $base -Parent
        if ($parent) {
            $pkey = Get-CrProjectKey $parent
            $dirs += @($allDirs | Where-Object { $_.Name -eq $pkey })
        }
    }
    $dirs = @($dirs | Sort-Object FullName -Unique)
    if ($All) { $dirs = $allDirs }
    if ($dirs.Count -eq 0) {
        Write-Host "No conversations for $(Split-Path $base -Leaf) - widening to every project" -ForegroundColor DarkYellow
        $dirs = $allDirs
        $All = $true
    }
    if ($dirs.Count -eq 0) { Write-Host "No Claude conversations at all." -ForegroundColor Red; return }

    # Subagent transcripts live in per-session subfolders and are not resumable sessions.
    $files = @($dirs | ForEach-Object { Get-ChildItem $_.FullName -Filter *.jsonl -File -ErrorAction SilentlyContinue } |
        Sort-Object LastWriteTime -Descending)
    if ($Count -gt 0) { $files = @($files | Select-Object -First $Count) }
    if ($files.Count -eq 0) { Write-Host "No matching conversations." -ForegroundColor Red; return }

    $inflight = @(Get-ChildItem (Join-Path $env:USERPROFILE '.claude\plans') -Filter '_inflight-*.md' -Recurse -ErrorAction SilentlyContinue |
        ForEach-Object { $_.Name })

    $index = Update-CrIndex -Files $files -Rebuild:$Rebuild -Width (Get-CrPaneWidth)

    $pathBySid = @{}
    foreach ($f in $files) { $pathBySid[[IO.Path]::GetFileNameWithoutExtension($f.Name)] = $f.FullName }

    # Row = "<label>  ||  <body>" TAB "<sid>".  fzf searches only what it displays - --nth applies after
    # --with-nth, so a hidden body field would be unsearchable and the body has to ride in the shown
    # column. TAB opens a pane over the hidden sid, which also drives the resume.
    $today = (Get-Date).Date
    $rows = foreach ($f in $files) {
        $sid = [IO.Path]::GetFileNameWithoutExtension($f.Name)
        $e = $index[$sid]
        if (-not $e) { continue }
        $mt = [datetime]([long]$e[1])
        $when = $mt.ToString('MM-dd HH:mm')
        if ($mt.Date -eq $today) { $when = '  today ' + $mt.ToString('HH:mm') }
        $flag = '    '
        foreach ($n in $inflight) { if ($n -like ("*" + $sid.Substring(0, 8) + "*")) { $flag = 'PLAN'; break } }
        $label = "{0}  {1}  {2,7} KB  {3,-9} {4}" -f $when, $flag, [int]([long]$e[2] / 1KB), $e[4], $e[5]
        "{0}  ||  {1}`t{2}" -f $label, $e[6], $sid
    }
    $rows = @($rows)
    if ($rows.Count -eq 0) { Write-Host "No matching conversations." -ForegroundColor Red; return }

    if ($List) {
        foreach ($i in $rows) { (($i -split "`t")[0] -split '  \|\|  ', 2)[0] }
        return
    }

    $q = if ($Text) { $Text } else { ($Query -join ' ').Trim() }

    $scopeName = if ($All) { 'every project' } else { "$(Split-Path $base -Leaf) ($($dirs.Count) project folder(s), $($rows.Count) sessions)" }
    $pane = '"' + (Join-Path $env:USERPROFILE '.claude\bin\cr-preview.cmd') + '" "' + (Join-Path $env:USERPROFILE '.claude\cr-preview') + '" {2} {q}'
    $fzfArgs = @('--prompt', 'search> ', '--height', '70%', '--border', '--exact', '--no-sort',
                 '--delimiter', "`t", '--with-nth', '1', '--print-query', '--expect', 'ctrl-a',
                 '--preview', $pane, '--preview-window', 'right:55%:wrap-word:follow:hidden', '--preview-wrap-sign', '  ', '--bind', 'tab:toggle-preview',
                 '--header', "scope: $scopeName / TAB = how it ended / ENTER resumes / CTRL-A = search every project / PLAN = unfinished plan")
    if ($q) { $fzfArgs += @('--query', $q) }
    $out = @($rows | & fzf @fzfArgs)
    if ($out.Count -lt 2) { return }

    # --print-query puts the typed query on line 1 and the --expect key on line 2, so the row is line 3.
    if ($out[1].Trim() -eq 'ctrl-a') {
        if ($All) { return }
        cr -Claude -All -Text $out[0]
        return
    }
    if ($out.Count -lt 3 -or [string]::IsNullOrWhiteSpace($out[2])) { return }

    $sid = ($out[2] -split "`t")[1]
    # A session found outside the current project resumes only from the directory it was started in.
    $sessionCwd = $null
    if ($pathBySid.ContainsKey($sid)) { $sessionCwd = Get-CrSessionCwd -Path $pathBySid[$sid] }
    if ($sessionCwd -and (Test-Path -LiteralPath $sessionCwd) -and $sessionCwd -ne (Get-Location).Path) {
        Write-Host "-> cd $sessionCwd; claude --resume $sid" -ForegroundColor DarkGray
        Push-Location -LiteralPath $sessionCwd
        try { claude --resume $sid } finally { Pop-Location }
        return
    }
    Write-Host "-> claude --resume $sid" -ForegroundColor DarkGray
    claude --resume $sid
}

# Cheap per-file peek used to scope Codex sessions to a repo: Codex doesn't bucket sessions by project
# directory the way Claude does, so every session's own recorded cwd is the only thing that says which
# repo it belongs to. Reading just the first line (session_meta) is enough - no need to scan the rest.
function Get-CodexMeta {
    param([string]$Path)
    $cwd = ''; $sid = ''
    try {
        # ReadWrite share: the active session's own file is still open for append by Codex itself.
        $fs = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $reader = New-Object IO.StreamReader($fs)
        $first = $reader.ReadLine()
        $reader.Dispose()
        if ($first) {
            $c = [regex]::Match($first, '"cwd":"((?:[^"\\]|\\.)*)"')
            if ($c.Success) { $cwd = ($c.Groups[1].Value -replace '\\\\', '\') }
            $s = [regex]::Match($first, '"session_id":"([^"]+)"')
            if ($s.Success) { $sid = $s.Groups[1].Value }
        }
    }
    catch { }
    return @{ Cwd = $cwd; SessionId = $sid }
}

# Lightweight cache (fileKey, mtime, sessionId, cwd) covering every Codex session on the machine, so
# scoping to a repo never has to fully reopen a file it already peeked. Separate from the heavier text
# cache below, which only ever touches the (much smaller) set of sessions actually in scope.
function Update-CodexMetaIndex {
    param([System.IO.FileInfo[]]$Files, [switch]$Rebuild)
    $cachePath = Join-Path $env:USERPROFILE '.codex\cr-meta-index.tsv'
    $ver = 'codexmeta-v1'
    $cache = @{}
    if (-not $Rebuild -and (Test-Path $cachePath)) {
        try {
            $all = [IO.File]::ReadAllLines($cachePath)
            if ($all.Count -ge 1 -and $all[0] -eq $ver) {
                for ($i = 1; $i -lt $all.Count; $i++) {
                    $line = $all[$i]
                    if (-not $line) { continue }
                    $p = $line -split "`t", 4
                    if ($p.Count -eq 4) { $cache[$p[0]] = $p }
                }
            }
        }
        catch { $cache = @{} }
    }

    $result = @{}
    $parsed = 0
    foreach ($f in $Files) {
        $fileKey = $f.Name
        $mt = $f.LastWriteTime.Ticks.ToString()
        $c = $cache[$fileKey]
        if ($c -and $c[1] -eq $mt) { $result[$fileKey] = $c; continue }
        $m = Get-CodexMeta -Path $f.FullName
        $rec = @($fileKey, $mt, $m.SessionId, $m.Cwd)
        $cache[$fileKey] = $rec
        $result[$fileKey] = $rec
        $parsed++
    }

    if ($parsed -gt 0) {
        $lines = New-Object System.Collections.Generic.List[string]
        $lines.Add($ver)
        foreach ($k in $cache.Keys) { $lines.Add(($cache[$k] -join "`t")) }
        try { [IO.File]::WriteAllLines($cachePath, $lines.ToArray()) } catch { }
    }
    return $result
}

# Flatten a Codex transcript into (preview, haystack, tail), the same shape Get-CrText produces for Claude.
# Codex opens every turn by re-injecting AGENTS.md/environment context as a genuine "user" message rather
# than a marked isMeta one, so those are matched out by content instead. Reasoning/tool-call payloads are
# skipped entirely - only role":"user"/"assistant" message text counts as something that was "said".
function Get-CodexCrText {
    param([string]$Path)
    $preview = ''
    $head = New-Object System.Collections.Generic.List[object]
    $sb = [System.Text.StringBuilder]::new()
    $cap = 300000
    $tailCap = 100000
    $tail = New-Object System.Collections.Generic.Queue[object]
    $tailLen = 0
    $reader = $null
    try {
        $fs = New-Object IO.FileStream($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
        $reader = New-Object IO.StreamReader($fs)
        $lineNo = 0
        while ($true) {
            $line = $reader.ReadLine()
            if ($null -eq $line) { break }
            $lineNo++
            if ($lineNo -eq 1) { continue }
            if ($line.Length -ge 200000) { continue }
            # Codex's own tool traffic, on the same terms as Claude's: enqueued for the pane, kept out of
            # the haystack.
            foreach ($c in (Get-CodexToolCalls -Line $line)) {
                $seq++; $rec = [pscustomobject]@{ Who = 'CTOOL'; Seq = $seq; Text = $c }
                if ($head.Count -lt 6) { [void]$head.Add($rec) }
                $tail.Enqueue($rec)
                $tailLen += $c.Length
                while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
            }
            foreach ($o in (Get-CodexToolResults -Line $line)) {
                $seq++; $rec = [pscustomobject]@{ Who = 'CRES'; Seq = $seq; Text = $o }
                if ($head.Count -lt 6) { [void]$head.Add($rec) }
                $tail.Enqueue($rec)
                $tailLen += $o.Length
                while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
            }
            if (-not ($line.Contains('"role":"user"') -or $line.Contains('"role":"assistant"'))) { continue }
            $isUser = $line.Contains('"role":"user"')
            foreach ($mm in [regex]::Matches($line, '"text":"((?:[^"\\]|\\.)*)"')) {
                $txt = $mm.Groups[1].Value
                if ($txt -match '<INSTRUCTIONS>|<environment_context|^# AGENTS\.md instructions|<skills_instructions>|<recommended_plugins>') { continue }
                # The approval reviewer's traffic rides the same transcript and, being per tool call, is
                # the last thing in most sessions - it would bury the conversation in the pane.
                if ($txt -match '>>> (APPROVAL REQUEST|TRANSCRIPT)|^Planned action JSON:|^Assess the exact planned action|^Reviewed Codex session id:|^Some conversation entries were omitted|^The Codex agent has requested|^\{"risk_level"') { continue }
                # The reviewer's own traffic is often wrapped in a leading \n, which is why these patterns
                # are unanchored where the ones above are not.
                if ($txt -match '\[\d+\] (tool \w+ (call|result)|user:|assistant:)|\\?"risk_level\\?":|\\?"command\\?":\s*\[|The following is the Codex agent history|Some conversation entries were omitted|<no retained transcript delta entries>') { continue }
                if (-not $preview -and $isUser) { $preview = $txt }
                $seq++; $rec = [pscustomobject]@{ Who = $(if ($isUser) { 'UX' } else { 'CX' }); Seq = $seq; Text = $txt }
                if ($head.Count -lt 3) { [void]$head.Add($rec) }
                $tail.Enqueue($rec)
                $tailLen += $txt.Length
                while ($tailLen -gt $tailCap -and $tail.Count -gt 1) { $tailLen -= $tail.Dequeue().Text.Length }
                if ($sb.Length -lt $cap) { [void]$sb.Append($txt); [void]$sb.Append(' ') }
            }
        }
    }
    catch { }
    finally { if ($reader) { $reader.Dispose() } }

    $preview = Expand-CrEscapes $preview
    if ($preview.Length -gt 78) { $preview = $preview.Substring(0, 78) }
    if (-not $preview) { $preview = '(no prompt)' }

    $tailTurns = @()
    $tailText = [System.Text.StringBuilder]::new()
    foreach ($t in $tail) {
        $txt = Expand-CrEscapes $t.Text
        if (-not $txt) { continue }
        $tailTurns += ($t.Who + "`t" + $t.Seq + "`t" + (Expand-CrBlock $t.Text))
        [void]$tailText.Append($txt); [void]$tailText.Append(' ')
    }

    $headTurns = @()
    foreach ($t in $head) { $headTurns += ($t.Who + "`t" + $t.Seq + "`t" + (Expand-CrBlock $t.Text)) }

    $hay = Expand-CrEscapes $sb.ToString()
    if ($sb.Length -ge $cap) { $hay = ($hay + ' ... ' + $tailText.ToString()).Trim() }

    return @{ Preview = $preview; Haystack = $hay; Tail = $tailTurns; Head = $headTurns }
}

# Same shape and caching contract as Update-CrIndex, just against ~/.codex/cr-index.tsv and Codex's
# transcript format. Only called with the (small) in-scope, count-limited file list - never all sessions.
function Update-CodexTextIndex {
    param([System.IO.FileInfo[]]$Files, [switch]$Rebuild, [int]$Width = 96)
    $cachePath = Join-Path $env:USERPROFILE '.codex\cr-index.tsv'
    $ver = 'codexindex-v9'
    $prevDir = Join-Path $env:USERPROFILE '.codex\cr-preview'
    if (-not (Test-Path $prevDir)) { [void](New-Item -ItemType Directory -Path $prevDir -Force) }
    $cache = @{}
    if (-not $Rebuild -and (Test-Path $cachePath)) {
        try {
            $all = [IO.File]::ReadAllLines($cachePath)
            if ($all.Count -ge 1 -and $all[0] -eq $ver) {
                for ($i = 1; $i -lt $all.Count; $i++) {
                    $line = $all[$i]
                    if (-not $line) { continue }
                    $p = $line -split "`t", 5
                    if ($p.Count -eq 5) { $cache[$p[0]] = $p }
                }
            }
        }
        catch { $cache = @{} }
    }

    $result = @{}
    $parsed = 0
    foreach ($f in $Files) {
        $fileKey = $f.Name
        $mt = $f.LastWriteTime.Ticks.ToString()
        $c = $cache[$fileKey]
        if ($c -and $c[1] -eq $mt -and (Test-Path (Join-Path $prevDir "$fileKey.ends.txt"))) { $result[$fileKey] = $c; continue }
        $t = Get-CodexCrText -Path $f.FullName
        $rec = @($fileKey, $mt, $f.Length.ToString(), $t.Preview, $t.Haystack)
        Write-CrPreview -Sid $fileKey -Haystack $t.Haystack -Tail $t.Tail -Head $t.Head -Dir $prevDir -Width $Width
        $cache[$fileKey] = $rec
        $result[$fileKey] = $rec
        $parsed++
    }

    if ($parsed -gt 0) {
        $lines = New-Object System.Collections.Generic.List[string]
        $lines.Add($ver)
        foreach ($k in $cache.Keys) { $lines.Add(($cache[$k] -join "`t")) }
        try { [IO.File]::WriteAllLines($cachePath, $lines.ToArray()) } catch { }
        if ($parsed -ge 5) { Write-Host "cr: indexed $parsed new/changed Codex session(s)" -ForegroundColor DarkGray }
    }
    return $result
}

# The Codex half of cr - same search bar, sourced from ~/.codex/sessions instead of ~/.claude/projects.
# Codex sessions aren't grouped by project on disk, so every session on the machine gets a cheap header
# peek (cached) to learn its cwd, THEN gets scoped to this repo/worktree, THEN only the count-limited
# survivors get their full text indexed - the expensive step never runs over sessions outside scope.
function Invoke-CodexCr {
    param(
        [string[]]$Query,
        [string]$Text,
        [int]$Count = 500,
        [switch]$All,
        [switch]$List,
        [switch]$Rebuild
    )

    $root = Join-Path $env:USERPROFILE '.codex\sessions'
    if (-not (Test-Path $root)) { Write-Host "No Codex history at $root" -ForegroundColor Red; return }

    $scope = Select-CrScope -All:$All

    $allFiles = @(Get-ChildItem $root -Filter *.jsonl -File -Recurse -ErrorAction SilentlyContinue)
    if ($allFiles.Count -eq 0) { Write-Host "No Codex sessions found." -ForegroundColor Red; return }
    $meta = Update-CodexMetaIndex -Files $allFiles -Rebuild:$Rebuild

    function Test-CodexCwdInScope {
        param([string]$Cwd)
        if (-not $Cwd) { return $false }
        if ($scope.Choice -eq 'ALL') {
            if ($Cwd -eq $scope.Base -or $Cwd.StartsWith($scope.Base + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
            foreach ($o in $scope.Others) {
                if ($Cwd -eq $o.Path -or $Cwd.StartsWith($o.Path + '\', [StringComparison]::OrdinalIgnoreCase)) { return $true }
            }
            return $false
        }
        if ($scope.Choice -eq 'MAIN') {
            $inMain = ($Cwd -eq $scope.Base -or $Cwd.StartsWith($scope.Base + '\', [StringComparison]::OrdinalIgnoreCase))
            if (-not $inMain) { return $false }
            foreach ($o in $scope.Others) {
                if ($Cwd -eq $o.Path -or $Cwd.StartsWith($o.Path + '\', [StringComparison]::OrdinalIgnoreCase)) { return $false }
            }
            return $true
        }
        return ($Cwd -eq $scope.Choice -or $Cwd.StartsWith($scope.Choice + '\', [StringComparison]::OrdinalIgnoreCase))
    }

    $inScope = @()
    if ($All) {
        $inScope = @($allFiles | Where-Object { $meta[$_.Name] -and $meta[$_.Name][2] })
    }
    else {
        foreach ($f in $allFiles) {
            $e = $meta[$f.Name]
            if (-not $e -or -not $e[2]) { continue }
            if (Test-CodexCwdInScope -Cwd $e[3]) { $inScope += $f }
        }
    }

    if ($inScope.Count -eq 0 -and -not $All) {
        Write-Host "No Codex conversations for $(Split-Path $scope.Base -Leaf) - widening to every session" -ForegroundColor DarkYellow
        $inScope = @($allFiles | Where-Object { $meta[$_.Name] -and $meta[$_.Name][2] })
        $All = $true
    }
    $files = @($inScope | Sort-Object LastWriteTime -Descending)
    if ($Count -gt 0) { $files = @($files | Select-Object -First $Count) }
    if ($files.Count -eq 0) { Write-Host "No Codex conversations at all." -ForegroundColor Red; return }

    $index = Update-CodexTextIndex -Files $files -Rebuild:$Rebuild -Width (Get-CrPaneWidth)

    $today = (Get-Date).Date
    $rows = foreach ($f in $files) {
        $e = $index[$f.Name]
        if (-not $e) { continue }
        $mt = [datetime]([long]$e[1])
        $when = $mt.ToString('MM-dd HH:mm')
        if ($mt.Date -eq $today) { $when = '  today ' + $mt.ToString('HH:mm') }
        $label = "{0}  {1,7} KB  {2}" -f $when, [int]([long]$e[2] / 1KB), $e[3]
        "{0}  ||  {1}`t{2}" -f $label, $e[4], $f.Name
    }
    $rows = @($rows)
    if ($rows.Count -eq 0) { Write-Host "No matching conversations." -ForegroundColor Red; return }

    if ($List) {
        foreach ($i in $rows) { (($i -split "`t")[0] -split '  \|\|  ', 2)[0] }
        return
    }

    $q = if ($Text) { $Text } else { ($Query -join ' ').Trim() }

    $scopeName = if ($All) { 'every session' } else { "$(Split-Path $scope.Base -Leaf) ($($rows.Count) sessions)" }
    $pane = '"' + (Join-Path $env:USERPROFILE '.claude\bin\cr-preview.cmd') + '" "' + (Join-Path $env:USERPROFILE '.codex\cr-preview') + '" {2} {q}'
    $fzfArgs = @('--prompt', 'search> ', '--height', '70%', '--border', '--exact', '--no-sort', '--delimiter', "`t", '--with-nth', '1',
                 '--print-query', '--expect', 'ctrl-a',
                 '--preview', $pane, '--preview-window', 'right:55%:wrap-word:follow:hidden', '--preview-wrap-sign', '  ', '--bind', 'tab:toggle-preview',
                 '--header', "scope: $scopeName / TAB = how it ended / ENTER resumes / CTRL-A = search every session")
    if ($q) { $fzfArgs += @('--query', $q) }
    $out = @($rows | & fzf @fzfArgs)
    if ($out.Count -lt 2) { return }

    if ($out[1].Trim() -eq 'ctrl-a') {
        if ($All) { return }
        Invoke-CodexCr -Text $out[0] -Count $Count -All -Rebuild:$Rebuild
        return
    }
    if ($out.Count -lt 3 -or [string]::IsNullOrWhiteSpace($out[2])) { return }

    $fileKey = ($out[2] -split "`t")[1]
    $m = $meta[$fileKey]
    if (-not $m -or -not $m[2]) { Write-Host "Could not resolve a session id for that row." -ForegroundColor Red; return }
    $sid = $m[2]
    $cwd = $m[3]
    if ($cwd -and (Test-Path -LiteralPath $cwd)) {
        Write-Host "-> $cwd" -ForegroundColor DarkGray
        Set-Location -LiteralPath $cwd
    }
    Write-Host "-> codex resume $sid" -ForegroundColor DarkGray
    codex resume $sid
}
