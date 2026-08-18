<#
.SYNOPSIS
    NauroLabs cross-project leak audit. Scans repo files for personal
    identifiers that must never be committed.

.DESCRIPTION
    Generic, central version of the audit. Designed to be copied into every
    NauroLabs project's `scripts/` folder by `.github/scripts/install-leak-audit.ps1`
    and wired as a `pre-push` git hook.

    Pattern source resolution (first hit wins for the BASE list):
      1. -PatternsFile <path>                              (explicit override)
      2. <repo>/scripts/.leak-patterns.txt                 (synced from central, committed)
      3. <workspace>/.github/config/personal-info-patterns.txt  (when running from workspace)

    Additional patterns are merged on top:
      - <repo>/scripts/.leak-patterns.local.txt   (gitignored, per-developer overrides)
      - -ExtraPatterns @('foo','bar')             (CLI overrides)

    Exits 0 if clean, 1 if leaks found.

.PARAMETER Staged
    Scan only files staged for commit (use from pre-commit hook).

.PARAMETER PrePushRange
    Scan the diff for an outgoing push. Provide "<local_sha>..<remote_sha>" or
    let the pre-push hook supply it via stdin. When set, only changed files
    in the range are scanned.

.PARAMETER PatternsFile
    Explicit path to a pattern file (overrides default resolution).

.PARAMETER LocalPatternsFile
    Explicit path to per-developer local pattern overrides. Default:
    <repo>/scripts/.leak-patterns.local.txt

.PARAMETER ExtraPatterns
    Additional inline patterns to merge.

.PARAMETER Quiet
    Suppress non-essential output. Only print failures.

.EXAMPLE
    ./scripts/audit-leaks.ps1
    Scan all tracked-eligible files in the repo.

.EXAMPLE
    ./scripts/audit-leaks.ps1 -Staged
    Pre-commit usage — only staged files.

.EXAMPLE
    ./scripts/audit-leaks.ps1 -ExtraPatterns @('my-project-rg','custom-secret')
    Add ad-hoc patterns on top of the base list.

.NOTES
    Source of truth lives in samoletovs/nauroLabs-github at
    .github/scripts/audit-leaks.ps1. Per-project copies should NEVER be edited
    in place — re-run .github/scripts/install-leak-audit.ps1 to refresh.
#>
[CmdletBinding()]
param(
    [switch]$Staged,
    [string]$PrePushRange,
    [string]$PatternsFile,
    [string]$LocalPatternsFile,
    [string[]]$ExtraPatterns = @(),
    [switch]$Quiet
)

$ErrorActionPreference = 'Stop'

function Write-Info([string]$msg, [string]$color = 'Cyan') {
    if (-not $Quiet) { Write-Host $msg -ForegroundColor $color }
}

function Test-IsBinary([string]$path) {
    try {
        $fs = [System.IO.File]::OpenRead($path)
        try {
            $buf = New-Object byte[] 8192
            $read = $fs.Read($buf, 0, $buf.Length)
            for ($i = 0; $i -lt $read; $i++) { if ($buf[$i] -eq 0) { return $true } }
        } finally { $fs.Dispose() }
    } catch { return $false }
    return $false
}

function Resolve-RepoRoot {
    $r = (& git rev-parse --show-toplevel 2>$null)
    if ($LASTEXITCODE -eq 0 -and $r) { return (Resolve-Path $r).Path }
    return (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
}

function Resolve-WorkspaceCentralFile([string]$repoRoot) {
    # Walk up from the repo root looking for a sibling `.github/config/personal-info-patterns.txt`.
    # This works on a developer laptop where projects live next to `.github/`.
    $dir = Split-Path -Parent $repoRoot
    while ($dir -and (Test-Path $dir)) {
        $candidate = Join-Path $dir '.github\config\personal-info-patterns.txt'
        if (Test-Path -LiteralPath $candidate) { return (Resolve-Path $candidate).Path }
        $parent = Split-Path -Parent $dir
        if ($parent -eq $dir) { break }
        $dir = $parent
    }
    return $null
}

function Read-PatternFile([string]$path) {
    if (-not (Test-Path -LiteralPath $path)) { return @() }
    $raw = Get-Content -LiteralPath $path -ErrorAction Stop
    $out = @()
    foreach ($line in $raw) {
        if ($null -eq $line) { continue }
        $trimmed = $line.Trim()
        if (-not $trimmed) { continue }
        if ($trimmed.StartsWith('#')) { continue }
        # strip trailing "  # comment" (require at least two spaces before #)
        $cleaned = ($trimmed -split '\s{2,}#', 2)[0].Trim()
        if ($cleaned) { $out += $cleaned }
    }
    return ,$out
}

# ─────────────────────────────────────────────────────────────────────────────
$repoRoot = Resolve-RepoRoot
Push-Location $repoRoot
try {
    # ── Resolve pattern sources ──
    $basePath = $null
    if ($PatternsFile) {
        $basePath = (Resolve-Path -LiteralPath $PatternsFile -ErrorAction Stop).Path
    }
    elseif (Test-Path -LiteralPath (Join-Path $repoRoot 'scripts\.leak-patterns.txt')) {
        $basePath = (Resolve-Path (Join-Path $repoRoot 'scripts\.leak-patterns.txt')).Path
    }
    else {
        $basePath = Resolve-WorkspaceCentralFile $repoRoot
    }

    if (-not $basePath) {
        Write-Host "FAIL: no pattern file found. Looked for -PatternsFile, scripts/.leak-patterns.txt, and the workspace central file." -ForegroundColor Red
        Write-Host "      Run .github/scripts/install-leak-audit.ps1 to install patterns into this repo." -ForegroundColor Yellow
        exit 2
    }

    $basePatterns = Read-PatternFile $basePath
    $localPath = if ($LocalPatternsFile) { $LocalPatternsFile } else { Join-Path $repoRoot 'scripts\.leak-patterns.local.txt' }
    $localPatterns = if (Test-Path -LiteralPath $localPath) { Read-PatternFile $localPath } else { @() }

    $allPatterns = @($basePatterns) + @($localPatterns) + @($ExtraPatterns) | Where-Object { $_ } | Sort-Object -Unique

    if (-not $allPatterns -or $allPatterns.Count -eq 0) {
        Write-Host "FAIL: pattern set is empty after merge." -ForegroundColor Red
        exit 2
    }

    $baseName = [System.IO.Path]::GetFileName($basePath)
    $srcMsg   = "Pattern sources: base=$baseName ($($basePatterns.Count))"
    if ($localPatterns.Count -gt 0) { $srcMsg += ", local=.leak-patterns.local.txt ($($localPatterns.Count))" }
    if ($ExtraPatterns.Count -gt 0) { $srcMsg += ", extra=$($ExtraPatterns.Count)" }
    Write-Info $srcMsg

    # ── Resolve files to scan ──
    $files = $null
    if ($PrePushRange) {
        $changed = git diff --name-only $PrePushRange --diff-filter=ACM 2>$null
        if (-not $changed) {
            Write-Info "Nothing changed in range '$PrePushRange'." 'Yellow'
            exit 0
        }
        $files = $changed | ForEach-Object { Get-Item -LiteralPath (Join-Path $repoRoot $_) -ErrorAction SilentlyContinue } |
                 Where-Object { $_ -and -not $_.PSIsContainer }
    }
    elseif ($Staged) {
        $stagedFiles = git diff --cached --name-only --diff-filter=ACM 2>$null
        if (-not $stagedFiles) {
            Write-Info "No staged files to scan." 'Yellow'
            exit 0
        }
        $files = $stagedFiles | ForEach-Object { Get-Item -LiteralPath (Join-Path $repoRoot $_) -ErrorAction SilentlyContinue } |
                 Where-Object { $_ -and -not $_.PSIsContainer }
    }
    else {
        # All tracked-eligible files: everything except build/runtime noise, .env files,
        # the pattern files themselves, and the audit script itself.
        $selfName  = [System.IO.Path]::GetFileName($PSCommandPath)
        $files = Get-ChildItem -Recurse -File | Where-Object {
            $_.FullName -notmatch '\\(\.venv|venv|node_modules|\.git|__pycache__|bin|obj|dist|build|\.next|coverage|test-results|playwright-report|\.pytest_cache|\.mypy_cache|\.ruff_cache|\.playwright-mcp)\\' `
                -and $_.Name -notmatch '^\.env(\..+)?$' `
                -and $_.Name -ne $selfName `
                -and $_.Name -ne '.leak-patterns.txt' `
                -and $_.Name -ne '.leak-patterns.local.txt'
        }

        # A git-ignored file cannot reach the remote, so it cannot leak - and scanning one
        # produces findings nobody can action. Derived binaries (a search index, a cache) are
        # the common case, and random bytes reliably match an IBAN-shaped pattern, which
        # blocks the push on pure noise.
        $ignored = @(git ls-files --others --ignored --exclude-standard 2>$null)
        if ($ignored.Count) {
            $ignoredFull = [System.Collections.Generic.HashSet[string]]::new(
                [string[]]($ignored | ForEach-Object { (Join-Path $repoRoot $_) }),
                [StringComparer]::OrdinalIgnoreCase)
            $files = $files | Where-Object { -not $ignoredFull.Contains($_.FullName) }
        }
    }

    # Text patterns against binary content are meaningless in both directions: false hits on
    # random bytes, and no real coverage. Sniff for a NUL in the first 8 KB.
    $files = $files | Where-Object { -not (Test-IsBinary $_.FullName) }

    if (-not $files -or $files.Count -eq 0) {
        Write-Info "No files to scan." 'Yellow'
        exit 0
    }

    Write-Info "Scanning $($files.Count) file(s) for $($allPatterns.Count) pattern(s)..."

    # Split into substring vs regex patterns. Regex lines are prefixed with "re:".
    $literalPatterns = @()
    $regexPatterns   = @()
    foreach ($pat in $allPatterns) {
        if ($pat -match '^re:(.+)$') {
            $regexPatterns += $Matches[1]
        }
        else {
            $literalPatterns += $pat
        }
    }

    $leaks = 0
    function Write-LeakHits([string]$label, $hits) {
        if (-not $hits) { return 0 }
        Write-Host ""
        Write-Host "[LEAK $label]" -ForegroundColor Red
        foreach ($h in $hits) {
            $rel = try { Resolve-Path -Relative $h.Path } catch { $h.Path }
            Write-Host ("  {0}:{1}  {2}" -f $rel, $h.LineNumber, $h.Line.Trim()) -ForegroundColor Yellow
        }
        return $hits.Count
    }

    foreach ($p in $literalPatterns) {
        $hits = $files | Select-String -Pattern $p -SimpleMatch -ErrorAction SilentlyContinue
        $leaks += (Write-LeakHits "'$p'" $hits)
    }
    foreach ($p in $regexPatterns) {
        $hits = $files | Select-String -Pattern $p -ErrorAction SilentlyContinue
        $leaks += (Write-LeakHits "re:'$p'" $hits)
    }

    # ── Commit metadata ──
    # Publishing a repository publishes its commit headers too, and nothing above reads
    # them: this script scans working-tree files, and `git grep` only ever reads blobs.
    # That blind spot is not theoretical - it put a corporate address into 151 of
    # golazo's 243 commits, and golazo is already public, while every content scan over
    # it returned clean. Author and committer identity and the message body (where
    # Co-authored-by trailers live) are checked here against the same patterns.
    #
    # Note what this cannot fix: once a commit is pushed, GitHub keeps it reachable via
    # refs/pull/*/head, which the repo owner cannot rewrite or delete. Catching an
    # identity here - before the push - is the only cheap moment.
    if (-not $Staged) {
        $logArgs = if ($PrePushRange) { @('log', $PrePushRange) } else { @('log', '--all') }
        $metaLines = & git @logArgs --format='%h author %an <%ae>%n%h committer %cn <%ce>%n%h message %B' 2>$null

        if ($metaLines) {
            $metaText = @($metaLines | Where-Object { $_ -and $_.Trim() })
            Write-Info "Scanning commit metadata for $($allPatterns.Count) pattern(s)..."

            function Write-MetaHits([string]$label, $hits) {
                if (-not $hits) { return 0 }
                # One line per commit field, so the same commit can match repeatedly.
                # Report distinct commits - that is the number that decides whether a
                # history rewrite is needed.
                $shas = $hits | ForEach-Object {
                    if ($_.Line -match '^([0-9a-f]{7,40})\s') { $Matches[1] }
                } | Where-Object { $_ } | Sort-Object -Unique
                Write-Host ""
                Write-Host "[LEAK commit-metadata $label]" -ForegroundColor Red
                Write-Host ("  {0} commit(s) affected: {1}" -f $shas.Count,
                            (($shas | Select-Object -First 8) -join ', ')) -ForegroundColor Yellow
                if ($shas.Count -gt 8) { Write-Host "  ..." -ForegroundColor Yellow }
                return $shas.Count
            }

            foreach ($p in $literalPatterns) {
                $hits = $metaText | Select-String -Pattern $p -SimpleMatch -ErrorAction SilentlyContinue
                $leaks += (Write-MetaHits "'$p'" $hits)
            }
            foreach ($p in $regexPatterns) {
                $hits = $metaText | Select-String -Pattern $p -ErrorAction SilentlyContinue
                $leaks += (Write-MetaHits "re:'$p'" $hits)
            }
        }
    }

    Write-Host ""
    if ($leaks -gt 0) {
        Write-Host "FAIL: $leaks leak(s) found across $($allPatterns.Count) pattern(s)." -ForegroundColor Red
        Write-Host "      In file content: move the value to .env (gitignored) or refactor it out, then re-stage." -ForegroundColor Red
        Write-Host "      In commit metadata: fix the identity BEFORE pushing - `git config user.email` to your" -ForegroundColor Red
        Write-Host "      <id>+<user>@users.noreply.github.com alias, then rebase to rewrite the affected commits." -ForegroundColor Red
        Write-Host "      Once pushed, GitHub pins those commits behind refs/pull/* that you cannot rewrite." -ForegroundColor Red
        Write-Host "      To bypass once (NOT recommended): git push --no-verify" -ForegroundColor DarkGray
        exit 1
    }
    else {
        Write-Info "OK: clean across $($allPatterns.Count) pattern(s)." 'Green'
        exit 0
    }
}
finally {
    Pop-Location
}
