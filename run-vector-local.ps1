#Requires -Version 5.1

<#
.SYNOPSIS
    Bring the whole Vector stack up locally, in one command, against a
    throwaway PostgreSQL container that is not and can never be Neon.

.DESCRIPTION
    PostgreSQL 18 (Docker) -> FastAPI on 127.0.0.1:8000 -> Vite on :5173 ->
    a browser at http://localhost:5173/issues.

    Windows PowerShell 5.1 is the target, deliberately: it is what ships with
    Windows and what this machine runs. Nothing here uses `??`, `? :`,
    `ForEach-Object -Parallel`, `Get-Error`, `$PSStyle` or any other 7-only
    construct.

    ## The three promises this script makes

    1. **It never touches the owner's database.** The DSN handed to the
       backend is built from the constants below and from nothing else. The
       repository's `.env` is never read, sourced, copied or passed through,
       and any `DATABASE_URL` already in the shell is overwritten in the
       child rather than inherited. See `New-BackendWindowScript`.

    2. **It never kills anything it did not start.** A port held by an
       unknown process stops the launcher with an explanation; it is not
       freed. `-Stop` kills only PIDs this launcher recorded, and only after
       checking that the PID still belongs to the same process it recorded
       (name plus start time), because PIDs are reused.

    3. **It migrates only through the repository's own runner, and only to
       the repository's head.** Every file in `migrations/` is applied by
       `python -m scripts.apply_migration`, in filename order, so the ledger,
       the advisory lock and the per-file checksum all apply exactly as they
       would anywhere else. No version is named anywhere in this script: the
       set is whatever is on disk. If the ledger and the directory disagree
       afterwards, it refuses to run rather than repairing anything -- see
       `Assert-SchemaMatchesRepository`.

.PARAMETER Stop
    Stop the processes and container this launcher started, then remove its
    state file.

.PARAMETER ResetDb
    Destroy and recreate the dedicated `vector-ui-dev` container (and only
    that container) before starting. Use this when the launcher reports that
    the local database and the repository's migrations disagree.

.PARAMETER NoBrowser
    Start everything but do not open a browser window.

.EXAMPLE
    .\run-vector-local.ps1
    .\run-vector-local.ps1 -Stop
    .\run-vector-local.ps1 -ResetDb
    .\run-vector-local.ps1 -NoBrowser
#>

[CmdletBinding()]
param(
    [switch]$Stop,
    [switch]$ResetDb,
    [switch]$NoBrowser
)

Set-StrictMode -Version 2.0

# Cmdlet failures are terminating. Native commands (docker, npm, taskkill)
# are *not* covered by this in 5.1 -- $LASTEXITCODE is the only signal they
# give -- which is why every native call below goes through one of the two
# helpers that check it explicitly.
$ErrorActionPreference = 'Stop'


# ---------------------------------------------------------------------------
# Constants. Everything the stack is addressed by lives here and nowhere else.
# ---------------------------------------------------------------------------

$RepoRoot = $PSScriptRoot

$ContainerName = 'vector-ui-dev'
$PostgresImage = 'postgres:18'
$DbName        = 'vector_ui'
$DbUser        = 'vector'
$DbPassword    = 'vector'
$DbHostPort    = 5433

$BackendPort  = 8000
$FrontendPort = 5173

# Built here, from the constants above, and passed to the backend explicitly.
# Concatenated rather than interpolated because `$DbUser:` would be parsed as
# a scope qualifier, not as a variable followed by a colon.
$DatabaseUrl = 'postgresql://' + $DbUser + ':' + $DbPassword +
    '@127.0.0.1:' + $DbHostPort + '/' + $DbName

# The same DSN with the password removed, for anything that gets printed.
$DatabaseUrlForDisplay = 'postgresql://' + $DbUser +
    '@127.0.0.1:' + $DbHostPort + '/' + $DbName

$VenvPython    = Join-Path (Join-Path $RepoRoot '.venv') 'Scripts\python.exe'
$FrontendDir   = Join-Path $RepoRoot 'frontend'
$MigrationsDir = Join-Path $RepoRoot 'migrations'

# Deliberately outside the repository: a state file inside it would show up
# as an untracked change on every run.
$StateFile = Join-Path $env:TEMP 'vector-local-dev-state.json'

$BackendHealthUrl  = 'http://127.0.0.1:' + $BackendPort + '/healthz'

# `localhost`, not `127.0.0.1`, and this is load-bearing. Vite 8 binds IPv6
# only by default, so `http://127.0.0.1:5173/` connects to nothing while
# `http://localhost:5173/` (which resolves to ::1 first) answers 200. Polling
# the IPv4 literal makes a perfectly healthy dev server look dead.
$FrontendUrl        = 'http://localhost:' + $FrontendPort + '/'
$FrontendGraphqlUrl = 'http://localhost:' + $FrontendPort + '/graphql'
$AppUrl             = 'http://localhost:' + $FrontendPort + '/issues'

$BackendWindowTitle  = 'Vector Backend'
$FrontendWindowTitle = 'Vector Frontend'

$BackendReadyTimeoutSeconds  = 30
$FrontendReadyTimeoutSeconds = 60
$DatabaseReadyTimeoutSeconds = 60


# ---------------------------------------------------------------------------
# Console output
# ---------------------------------------------------------------------------

function Write-Step {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ''
    Write-Host ('==> ' + $Message) -ForegroundColor Cyan
}

function Write-Detail {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Message)
    Write-Host ('    ' + $Message) -ForegroundColor Gray
}

function Write-Good {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ('    ' + $Message) -ForegroundColor Green
}

function Write-Note {
    param([Parameter(Mandatory = $true)][string]$Message)
    Write-Host ('    ' + $Message) -ForegroundColor Yellow
}

<#
    Abandon the run with an explanation, never a stack trace.

    Every refusal in this script is a diagnosis -- a port is taken, the
    database is the wrong shape, the GraphQL contract is stale -- and the
    reader needs the sentence, not the line number it was thrown from.
#>
function Stop-Launcher {
    param(
        [Parameter(Mandatory = $true)][string]$Reason,
        [string[]]$Details = @()
    )

    Write-Host ''
    Write-Host '---------------------------------------------------------------' -ForegroundColor Red
    Write-Host ('STOPPED: ' + $Reason) -ForegroundColor Red

    foreach ($line in $Details) {
        Write-Host ('  ' + $line) -ForegroundColor Red
    }

    Write-Host '---------------------------------------------------------------' -ForegroundColor Red
    Write-Host ''

    exit 1
}


# ---------------------------------------------------------------------------
# Native commands
#
# In 5.1 a native command that writes to stderr raises NativeCommandError
# when $ErrorActionPreference is 'Stop' and its stderr is redirected. Both
# helpers drop the preference for the duration of the call and put it back,
# so `docker inspect` on a missing container is an exit code to read rather
# than an exception to catch.
# ---------------------------------------------------------------------------

<# Run a native command, capture its combined output, and return both. #>
function Invoke-Native {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @(),

        # Optional, and restored afterwards even on a throw. `python -m` only
        # resolves this repository's packages from the repository root, so the
        # migration runner needs it; every other caller is unaffected.
        [string]$WorkingDirectory
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    $code = $null
    $raw = $null

    if ($WorkingDirectory) {
        Push-Location -LiteralPath $WorkingDirectory
    }

    try {
        $raw = & $FilePath @Arguments 2>&1
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous

        if ($WorkingDirectory) {
            Pop-Location
        }
    }

    $text = ''

    if ($null -ne $raw) {
        $text = (@($raw) | ForEach-Object { [string]$_ }) -join [Environment]::NewLine
    }

    return [pscustomobject]@{
        ExitCode = $code
        Output   = $text.Trim()
    }
}

<#
    Run a native command with its output going straight to this console,
    and return only its exit code.

    `| Out-Host` is the load-bearing part. A bare `& npm ...` inside a
    function writes npm's every line to the *function's* output stream, so
    the caller receives those lines and the exit code as one array -- and
    `$result -ne 0` against an array of strings is a non-empty array, which
    is truthy. That turns a passing gate into a failing one. Out-Host sends
    the lines to the console instead, leaving the return value alone.
#>
function Invoke-NativeStreaming {
    param(
        [Parameter(Mandatory = $true)][string]$FilePath,
        [string[]]$Arguments = @()
    )

    $previous = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'

    $code = $null

    try {
        & $FilePath @Arguments 2>&1 | Out-Host
        $code = $LASTEXITCODE
    } finally {
        $ErrorActionPreference = $previous
    }

    return $code
}


# ---------------------------------------------------------------------------
# Ports and HTTP
# ---------------------------------------------------------------------------

<#
    Who, if anyone, is listening on a port.

    Returns objects with LocalAddress / OwningProcess, or nothing.
    Get-NetTCPConnection sees IPv4 and IPv6 listeners alike, which matters
    for Vite's IPv6-only bind.

    Every caller wraps the result in `@( )`, and must: PowerShell unrolls a
    collection on its way out of a function, so an empty result arrives as
    `$null` and a single result as a bare object. Reading `.Count` off either
    is an error under Set-StrictMode.
#>
function Get-PortListener {
    param([Parameter(Mandatory = $true)][int]$Port)

    if ($null -eq (Get-Command -Name 'Get-NetTCPConnection' -ErrorAction SilentlyContinue)) {
        # Ancient or stripped-down Windows. Fall back to "can I connect?",
        # which is weaker (it cannot name the owner) but never wrong about
        # whether the port is in use.
        if (Test-TcpPort -TargetHost '127.0.0.1' -Port $Port) {
            return @([pscustomobject]@{ LocalAddress = '127.0.0.1'; OwningProcess = 0 })
        }

        return @()
    }

    return @(Get-NetTCPConnection -State Listen -LocalPort $Port -ErrorAction SilentlyContinue)
}

<# A one-line description of a port's owner, for an error message. #>
function Get-PortOwnerDescription {
    param([Parameter(Mandatory = $true)][int]$Port)

    $listeners = @(Get-PortListener -Port $Port)

    if ($listeners.Count -eq 0) {
        return 'nothing'
    }

    $descriptions = @()

    foreach ($listener in $listeners) {
        $processId = $listener.OwningProcess
        $name = 'unknown'

        if ($processId -gt 0) {
            $owner = Get-Process -Id $processId -ErrorAction SilentlyContinue

            if ($null -ne $owner) {
                $name = $owner.ProcessName
            }
        }

        $descriptions += ($name + ' (PID ' + $processId + ', bound ' + $listener.LocalAddress + ')')
    }

    return ($descriptions | Select-Object -Unique) -join ', '
}

<# A bounded TCP connect. `$TargetHost`, because `$Host` is reserved. #>
function Test-TcpPort {
    param(
        [Parameter(Mandatory = $true)][string]$TargetHost,
        [Parameter(Mandatory = $true)][int]$Port,
        [int]$TimeoutMilliseconds = 1000
    )

    $client = New-Object System.Net.Sockets.TcpClient

    try {
        $async = $client.BeginConnect($TargetHost, $Port, $null, $null)

        if (-not $async.AsyncWaitHandle.WaitOne($TimeoutMilliseconds, $false)) {
            return $false
        }

        $client.EndConnect($async)

        return $true
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

<# A GET that answers 2xx/3xx. Any failure at all is `$false`. #>
function Test-HttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [int]$TimeoutSeconds = 3
    )

    try {
        $response = Invoke-WebRequest -Uri $Url -UseBasicParsing -Method Get -TimeoutSec $TimeoutSeconds

        return ($response.StatusCode -ge 200 -and $response.StatusCode -lt 400)
    } catch {
        return $false
    }
}

<#
    Poll until an endpoint answers, or until the budget runs out.

    Polling rather than sleeping a fixed interval: the backend takes as long
    as it takes to import Strawberry and open a pool, and a fixed sleep is
    either too short (and reports a working stack as broken) or too long on
    every single run.

    Returns 'ok', 'timeout', or 'process-exited'.
#>
function Wait-ForHttpEndpoint {
    param(
        [Parameter(Mandatory = $true)][string]$Url,
        [Parameter(Mandatory = $true)][int]$TimeoutSeconds,
        $Process = $null
    )

    $deadline = (Get-Date).AddSeconds($TimeoutSeconds)

    Write-Host '    waiting' -NoNewline -ForegroundColor Gray

    while ((Get-Date) -lt $deadline) {
        if ($null -ne $Process -and $Process.HasExited) {
            Write-Host ''
            return 'process-exited'
        }

        if (Test-HttpEndpoint -Url $Url) {
            Write-Host ''
            return 'ok'
        }

        Write-Host '.' -NoNewline -ForegroundColor Gray
        Start-Sleep -Milliseconds 500
    }

    Write-Host ''

    return 'timeout'
}


# ---------------------------------------------------------------------------
# Docker
# ---------------------------------------------------------------------------

<# 'absent', or the container's docker state ('running', 'exited', ...). #>
function Get-ContainerState {
    $result = Invoke-Native -FilePath 'docker' -Arguments @(
        'inspect', '--format', '{{.State.Status}}', $ContainerName
    )

    if ($result.ExitCode -ne 0) {
        return 'absent'
    }

    return $result.Output.Trim()
}

<# What the container publishes 5432 on, e.g. '127.0.0.1:5433', or ''. #>
function Get-ContainerPublishedPort {
    $result = Invoke-Native -FilePath 'docker' -Arguments @('port', $ContainerName, '5432/tcp')

    if ($result.ExitCode -ne 0) {
        return ''
    }

    return $result.Output.Trim()
}

<#
    psql inside the dedicated container.

    Inside, rather than on the host, because psql is not installed on this
    machine and requiring it would put a second thing between the owner and
    a working stack. ON_ERROR_STOP is set for every invocation, so a failing
    statement is a non-zero exit rather than a warning scrolling past.
#>
function Invoke-ContainerPsql {
    param([Parameter(Mandatory = $true)][string[]]$PsqlArguments)

    $dockerArguments = @(
        'exec',
        '-e', ('PGPASSWORD=' + $DbPassword),
        $ContainerName,
        'psql',
        '-v', 'ON_ERROR_STOP=1',
        '--username', $DbUser,
        '--dbname', $DbName
    ) + $PsqlArguments

    return Invoke-Native -FilePath 'docker' -Arguments $dockerArguments
}

<# One scalar, unaligned and untitled, as a trimmed string. #>
function Get-ScalarFromDatabase {
    param([Parameter(Mandatory = $true)][string]$Sql)

    $result = Invoke-ContainerPsql -PsqlArguments @('-tAc', $Sql)

    if ($result.ExitCode -ne 0) {
        Stop-Launcher -Reason 'A query against the local database failed.' -Details @(
            $result.Output
        )
    }

    return $result.Output.Trim()
}


# ---------------------------------------------------------------------------
# Launcher state
#
# Recorded outside the repository, in $env:TEMP, so that a running stack
# leaves the working tree exactly as clean as it found it.
# ---------------------------------------------------------------------------

function Get-LauncherState {
    if (-not (Test-Path -LiteralPath $StateFile)) {
        return $null
    }

    try {
        return (Get-Content -LiteralPath $StateFile -Raw | ConvertFrom-Json)
    } catch {
        # A truncated or hand-edited state file is not worth failing over;
        # it just means there is nothing reliable to stop.
        Write-Note ('Ignoring unreadable state file: ' + $StateFile)
        return $null
    }
}

function Save-LauncherState {
    param([Parameter(Mandatory = $true)][array]$Processes)

    $state = [ordered]@{
        schemaVersion = 1
        startedAt     = (Get-Date).ToUniversalTime().ToString('o')
        repoRoot      = $RepoRoot
        containerName = $ContainerName
        databasePort  = $DbHostPort
        backendPort   = $BackendPort
        frontendPort  = $FrontendPort
        processes     = $Processes
    }

    $state | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath $StateFile -Encoding UTF8
}

<#
    Turn a started process into the record `-Stop` will act on.

    The start time is the part that matters. Windows reuses PIDs, so a state
    file that survives a reboot can name a PID that now belongs to something
    else entirely; `Test-RecordedProcessIdentity` compares this timestamp
    before anything is killed.
#>
function New-ProcessRecord {
    param(
        [Parameter(Mandatory = $true)][string]$Role,
        [Parameter(Mandatory = $true)]$Process
    )

    return [ordered]@{
        role      = $Role
        pid       = $Process.Id
        name      = $Process.ProcessName
        startedAt = $Process.StartTime.ToString('o')
    }
}

<#
    Is the process holding this PID still the one that was recorded?

    Name and start time together. Without this check, `-Stop` after a reboot
    is a `taskkill /F` aimed at an arbitrary PID -- which is exactly the
    "kill whatever is in the way" behaviour this launcher refuses to have.
#>
<#
    Whether a record carries every field the stop path will dereference.

    Set-StrictMode makes reading an absent property a terminating error, so a
    state file that is valid JSON but structurally incomplete -- hand-edited,
    written by an older version of this script, truncated at a record boundary
    -- would abort the run partway through. In -Stop that is a wedge rather
    than an inconvenience: the abort happens before the container is stopped
    and before the state file is removed, so the one action that would clear
    the bad file never runs and every subsequent invocation fails the same
    way, with nothing in the output naming the file to delete.

    Parsing succeeded, so the file is not obviously broken; only the shape is.
    Records that fail this are skipped, which is the same treatment a record
    for a process that has already exited gets.
#>
function Test-ProcessRecordShape {
    param($Record)

    if ($null -eq $Record) {
        return $false
    }

    $present = @($Record.PSObject.Properties.Name)

    foreach ($field in @('role', 'pid', 'name', 'startedAt')) {
        if ($present -notcontains $field) {
            return $false
        }
    }

    return $true
}

function Test-RecordedProcessIdentity {
    param(
        [Parameter(Mandatory = $true)]$Record,
        [Parameter(Mandatory = $true)]$Process
    )

    if ($Process.ProcessName -ne $Record.name) {
        return $false
    }

    try {
        $recordedStart = [datetime]::Parse(
            $Record.startedAt,
            [System.Globalization.CultureInfo]::InvariantCulture,
            [System.Globalization.DateTimeStyles]::RoundtripKind
        )
    } catch {
        return $false
    }

    return ([Math]::Abs(($Process.StartTime - $recordedStart).TotalSeconds) -le 2)
}

<# Every recorded process that is still alive and still itself. #>
function Get-LiveRecordedProcess {
    param($State)

    $live = @()

    if ($null -eq $State) {
        return $live
    }

    if (-not ($State.PSObject.Properties.Name -contains 'processes')) {
        return $live
    }

    foreach ($record in @($State.processes)) {
        if (-not (Test-ProcessRecordShape -Record $record)) {
            continue
        }

        $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue

        if ($null -eq $process) {
            continue
        }

        if (-not (Test-RecordedProcessIdentity -Record $record -Process $process)) {
            continue
        }

        $live += [pscustomobject]@{ Record = $record; Process = $process }
    }

    return $live
}

<#
    Stop the recorded processes, and only those.

    A recorded process that is already gone is normal, not a failure: the
    owner may have closed the window by hand. A recorded PID that now belongs
    to something else is left strictly alone and reported.
#>
function Stop-RecordedProcess {
    param($State)

    if ($null -eq $State) {
        Write-Detail 'No recorded processes.'
        return
    }

    if (-not ($State.PSObject.Properties.Name -contains 'processes')) {
        Write-Detail 'No recorded processes.'
        return
    }

    $records = @($State.processes)

    if ($records.Count -eq 0) {
        Write-Detail 'No recorded processes.'
        return
    }

    foreach ($record in $records) {
        if (-not (Test-ProcessRecordShape -Record $record)) {
            Write-Note (
                'Ignoring a malformed record in the state file. The rest of ' +
                'the stop still runs, and the file is removed at the end.'
            )
            continue
        }

        $label = [string]$record.role + ' (PID ' + $record.pid + ')'
        $process = Get-Process -Id $record.pid -ErrorAction SilentlyContinue

        if ($null -eq $process) {
            Write-Detail ($label + ' was already stopped.')
            continue
        }

        if (-not (Test-RecordedProcessIdentity -Record $record -Process $process)) {
            Write-Note (
                $label + ' is now held by an unrelated process (' +
                $process.ProcessName + '). Left running.'
            )
            continue
        }

        # /T because uvicorn --reload runs the server in a child of the
        # window's shell, and killing only the shell would orphan it.
        $result = Invoke-Native -FilePath 'taskkill' -Arguments @(
            '/PID', [string]$record.pid, '/T', '/F'
        )

        if ($result.ExitCode -eq 0) {
            Write-Good ($label + ' stopped.')
        } else {
            Write-Note ($label + ' could not be stopped: ' + $result.Output)
        }
    }
}

<# Stop the dedicated container. Never any other container. #>
function Stop-DedicatedContainer {
    $state = Get-ContainerState

    if ($state -eq 'absent') {
        Write-Detail ('Container ' + $ContainerName + ' does not exist.')
        return
    }

    if ($state -ne 'running') {
        Write-Detail ('Container ' + $ContainerName + ' is already ' + $state + '.')
        return
    }

    $result = Invoke-Native -FilePath 'docker' -Arguments @('stop', '-t', '10', $ContainerName)

    if ($result.ExitCode -eq 0) {
        Write-Good ('Container ' + $ContainerName + ' stopped.')
    } else {
        Write-Note ('Could not stop ' + $ContainerName + ': ' + $result.Output)
    }
}

function Remove-LauncherState {
    if (Test-Path -LiteralPath $StateFile) {
        Remove-Item -LiteralPath $StateFile -Force
        Write-Detail ('Removed ' + $StateFile)
    }
}


# ---------------------------------------------------------------------------
# Child windows
# ---------------------------------------------------------------------------

<# A string as a PowerShell single-quoted literal, apostrophes and all. #>
function ConvertTo-PowerShellLiteral {
    param([Parameter(Mandatory = $true)][AllowEmptyString()][string]$Value)

    return "'" + $Value.Replace("'", "''") + "'"
}

<#
    Open a visible PowerShell window running the given script body.

    `-EncodedCommand` rather than `-Command`, because Start-Process on 5.1
    joins -ArgumentList with spaces and adds no quoting of its own: a repo
    path containing a space would arrive at the child as two arguments. A
    base64 blob has neither spaces nor quotes, so it survives that join
    unaltered. The script it encodes is built in plain text immediately
    above each call site and is not hidden from anyone.

    `-NoExit` so that a backend that fails to start leaves its traceback on
    screen instead of closing the window on top of it.
#>
function Start-ChildWindow {
    param(
        [Parameter(Mandatory = $true)][string]$ScriptBody,
        [Parameter(Mandatory = $true)][string]$WorkingDirectory
    )

    $encoded = [Convert]::ToBase64String([System.Text.Encoding]::Unicode.GetBytes($ScriptBody))

    return Start-Process -FilePath 'powershell.exe' `
        -ArgumentList @('-NoProfile', '-NoExit', '-EncodedCommand', $encoded) `
        -WorkingDirectory $WorkingDirectory `
        -PassThru
}

<#
    The backend window's script.

    DATABASE_URL is assigned here, from the constant at the top of this file.
    That assignment is the whole tenancy guarantee: pydantic-settings ranks a
    real environment variable above any value in `.env`, so whatever the
    repository's `.env` points at -- Neon included -- is overridden before
    the application reads it. This script never opens `.env` at all.
#>
function New-BackendWindowScript {
    $template = @'
$Host.UI.RawUI.WindowTitle = __TITLE__
Set-Location -LiteralPath __REPO__

# Explicit, and overriding anything inherited from the parent shell or
# resolved from .env by pydantic-settings.
$env:DATABASE_URL = __DSN__
$env:ENVIRONMENT = 'development'

Write-Host ''
Write-Host '==================================================' -ForegroundColor Green
Write-Host '  Vector Backend' -ForegroundColor Green
Write-Host '' -ForegroundColor Green
Write-Host '  LOCAL DATABASE ONLY' -ForegroundColor Green
Write-Host '  NO NEON' -ForegroundColor Green
Write-Host '' -ForegroundColor Green
Write-Host ('  ' + __DSN_DISPLAY__) -ForegroundColor Green
Write-Host '==================================================' -ForegroundColor Green
Write-Host ''

& __PYTHON__ -m uvicorn app.main:create_app --factory --reload --host 127.0.0.1 --port __PORT__

Write-Host ''
Write-Host '**************************************************' -ForegroundColor Red
Write-Host '  The Vector backend exited. The output above is' -ForegroundColor Red
Write-Host '  why. This window stays open on purpose.' -ForegroundColor Red
Write-Host '**************************************************' -ForegroundColor Red
'@

    $body = $template
    $body = $body.Replace('__TITLE__', (ConvertTo-PowerShellLiteral $BackendWindowTitle))
    $body = $body.Replace('__REPO__', (ConvertTo-PowerShellLiteral $RepoRoot))
    $body = $body.Replace('__DSN_DISPLAY__', (ConvertTo-PowerShellLiteral $DatabaseUrlForDisplay))
    $body = $body.Replace('__DSN__', (ConvertTo-PowerShellLiteral $DatabaseUrl))
    $body = $body.Replace('__PYTHON__', (ConvertTo-PowerShellLiteral $VenvPython))
    $body = $body.Replace('__PORT__', [string]$BackendPort)

    return $body
}

<#
    The frontend window's script.

    VITE_GRAPHQL_URL is a path, never an absolute backend URL. The backend
    installs no CORS middleware, so a page on Vite's origin that called
    http://127.0.0.1:8000/graphql directly would have every response
    discarded by the browser while curl against the same URL kept working.
    The path goes through Vite's proxy and stays same-origin.
#>
function New-FrontendWindowScript {
    $template = @'
$Host.UI.RawUI.WindowTitle = __TITLE__
Set-Location -LiteralPath __FRONTEND__

# A path, not an origin: the Vite proxy forwards it, and the request stays
# same-origin. See frontend/vite.config.ts.
$env:VITE_GRAPHQL_URL = '/graphql'

Write-Host ''
Write-Host '==================================================' -ForegroundColor Cyan
Write-Host '  Vector Frontend' -ForegroundColor Cyan
Write-Host '  /graphql is proxied to 127.0.0.1:__BACKEND_PORT__' -ForegroundColor Cyan
Write-Host '==================================================' -ForegroundColor Cyan
Write-Host ''

npm run dev

Write-Host ''
Write-Host '**************************************************' -ForegroundColor Red
Write-Host '  The Vector frontend exited. The output above is' -ForegroundColor Red
Write-Host '  why. This window stays open on purpose.' -ForegroundColor Red
Write-Host '**************************************************' -ForegroundColor Red
'@

    $body = $template
    $body = $body.Replace('__TITLE__', (ConvertTo-PowerShellLiteral $FrontendWindowTitle))
    $body = $body.Replace('__FRONTEND__', (ConvertTo-PowerShellLiteral $FrontendDir))
    $body = $body.Replace('__BACKEND_PORT__', [string]$BackendPort)

    return $body
}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

function Assert-Prerequisites {
    Write-Step 'Preflight'

    $required = @(
        @{ Path = $VenvPython;   What = 'the backend virtualenv interpreter' },
        @{ Path = $MigrationsDir; What = 'the migrations directory' },
        @{ Path = (Join-Path $FrontendDir 'package.json');      What = 'the frontend manifest' },
        @{ Path = (Join-Path $FrontendDir 'package-lock.json'); What = 'the frontend lockfile' },
        @{ Path = (Join-Path $FrontendDir 'vite.config.ts');    What = 'the Vite config' }
    )

    foreach ($entry in $required) {
        if (-not (Test-Path -LiteralPath $entry.Path)) {
            Stop-Launcher -Reason ('Cannot find ' + $entry.What + '.') -Details @(
                ('Expected: ' + $entry.Path),
                'Run this script from a complete checkout with the virtualenv created.'
            )
        }
    }

    Write-Good 'Repository layout looks complete.'

    foreach ($tool in @('docker', 'node', 'npm')) {
        if ($null -eq (Get-Command -Name $tool -ErrorAction SilentlyContinue)) {
            Stop-Launcher -Reason ($tool + ' is not on PATH.') -Details @(
                'Install it, or open a shell where it is available, and try again.'
            )
        }
    }

    $dockerInfo = Invoke-Native -FilePath 'docker' -Arguments @('info', '--format', '{{.ServerVersion}}')

    if ($dockerInfo.ExitCode -ne 0) {
        Stop-Launcher -Reason 'The Docker daemon is not responding.' -Details @(
            'Start Docker Desktop and wait for it to report "running", then try again.',
            $dockerInfo.Output
        )
    }

    Write-Good ('Docker daemon reachable (server ' + $dockerInfo.Output + ').')
}

<#
    Refuse to start on top of a stack that is already up.

    Refuse, rather than adopt or restart: the owner may be mid-debug in one
    of those windows, and silently replacing them would throw that away.
#>
function Assert-NoRunningStack {
    $state = Get-LauncherState

    if ($null -eq $state) {
        return
    }

    # `@( )` for the same unrolling reason documented at Get-PortListener.
    $live = @(Get-LiveRecordedProcess -State $state)

    if ($live.Count -gt 0) {
        $lines = @()

        foreach ($entry in $live) {
            $lines += ($entry.Record.role + ': PID ' + $entry.Record.pid + ' (' + $entry.Process.ProcessName + ')')
        }

        $lines += ''
        $lines += 'Run  .\run-vector-local.ps1 -Stop  first.'

        Stop-Launcher -Reason 'A Vector local stack started by this launcher is already running.' -Details $lines
    }

    Write-Detail 'Found a stale state file from a previous run; discarding it.'
    Remove-LauncherState
}

<#
    Check the three ports, and free none of them.

    A port is a claim by a running process, and a launcher that breaks such a
    claim to make room for itself will eventually take down something that
    mattered. So: 5433 is acceptable only when the dedicated container holds
    it, and 8000 or 5173 in use is the end of the run.
#>
function Assert-PortsAvailable {
    Write-Step 'Checking ports'

    $containerState = Get-ContainerState
    $databaseListeners = @(Get-PortListener -Port $DbHostPort)

    if ($databaseListeners.Count -gt 0 -and $containerState -ne 'running') {
        Stop-Launcher -Reason ('Port ' + $DbHostPort + ' is in use by something other than ' + $ContainerName + '.') -Details @(
            ('Holder: ' + (Get-PortOwnerDescription -Port $DbHostPort)),
            'This launcher will not free a port it did not take.',
            'Stop that process yourself, or change $DbHostPort in this script.'
        )
    }

    if ($databaseListeners.Count -gt 0) {
        Write-Good ('Port ' + $DbHostPort + ' is held by ' + $ContainerName + ', which is expected.')
    } else {
        Write-Good ('Port ' + $DbHostPort + ' is free.')
    }

    foreach ($port in @($BackendPort, $FrontendPort)) {
        if (@(Get-PortListener -Port $port).Count -gt 0) {
            Stop-Launcher -Reason ('Port ' + $port + ' is already in use.') -Details @(
                ('Holder: ' + (Get-PortOwnerDescription -Port $port)),
                'Nothing has been started, and nothing has been killed.',
                'If that is a previous run of this launcher, stop it with:',
                '  .\run-vector-local.ps1 -Stop',
                'Otherwise stop the process yourself and run this again.'
            )
        }

        Write-Good ('Port ' + $port + ' is free.')
    }
}


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

function Start-DedicatedContainer {
    Write-Step 'Local PostgreSQL'

    $state = Get-ContainerState

    if ($state -eq 'running') {
        Write-Good ('Container ' + $ContainerName + ' is already running.')
    } elseif ($state -eq 'absent') {
        Write-Detail ('Creating ' + $ContainerName + ' from ' + $PostgresImage + '...')

        # 127.0.0.1: bound to the loopback interface only, so the database is
        # not offered to anything else on the network.
        $result = Invoke-Native -FilePath 'docker' -Arguments @(
            'run', '--detach',
            '--name', $ContainerName,
            '--env', ('POSTGRES_USER=' + $DbUser),
            '--env', ('POSTGRES_PASSWORD=' + $DbPassword),
            '--env', ('POSTGRES_DB=' + $DbName),
            '--publish', ('127.0.0.1:' + $DbHostPort + ':5432'),
            $PostgresImage
        )

        if ($result.ExitCode -ne 0) {
            Stop-Launcher -Reason 'Could not create the local PostgreSQL container.' -Details @(
                $result.Output
            )
        }

        Write-Good ('Created ' + $ContainerName + '.')
    } else {
        Write-Detail ('Container ' + $ContainerName + ' is ' + $state + '; starting it...')

        $result = Invoke-Native -FilePath 'docker' -Arguments @('start', $ContainerName)

        if ($result.ExitCode -ne 0) {
            Stop-Launcher -Reason ('Could not start ' + $ContainerName + '.') -Details @(
                $result.Output,
                'If it is damaged, recreate it with:  .\run-vector-local.ps1 -ResetDb'
            )
        }

        Write-Good ('Started ' + $ContainerName + '.')
    }

    # A container reused from an older run could have been created with a
    # different publish mapping, in which case the DSN this script hands the
    # backend points at nothing. Better to say so than to let the backend
    # fail with a connection error that names no cause.
    $published = Get-ContainerPublishedPort
    $expected = '127.0.0.1:' + $DbHostPort

    if ($published -ne $expected) {
        Stop-Launcher -Reason ($ContainerName + ' does not publish PostgreSQL where this launcher expects it.') -Details @(
            ('Expected: ' + $expected),
            ('Actual:   ' + $published),
            'Recreate it with:  .\run-vector-local.ps1 -ResetDb'
        )
    }
}

<#
    Wait until PostgreSQL is accepting real connections.

    `pg_isready -h 127.0.0.1` inside the container, not the default socket
    check: the official image runs a temporary server on the unix socket
    while it initialises the data directory, and that server answers
    pg_isready. Requiring TCP means the answer arrives only once the real
    server is up with the database created.

    Then a connect from the host as well, because the backend reaches
    PostgreSQL through the published port, not from inside the container.
#>
function Wait-ForDatabase {
    $deadline = (Get-Date).AddSeconds($DatabaseReadyTimeoutSeconds)

    Write-Host '    waiting' -NoNewline -ForegroundColor Gray

    while ((Get-Date) -lt $deadline) {
        $ready = Invoke-Native -FilePath 'docker' -Arguments @(
            'exec', $ContainerName,
            'pg_isready', '-h', '127.0.0.1', '-p', '5432',
            '-U', $DbUser, '-d', $DbName
        )

        if ($ready.ExitCode -eq 0 -and (Test-TcpPort -TargetHost '127.0.0.1' -Port $DbHostPort)) {
            Write-Host ''
            Write-Good 'PostgreSQL is accepting connections.'
            return
        }

        Write-Host '.' -NoNewline -ForegroundColor Gray
        Start-Sleep -Milliseconds 500
    }

    Write-Host ''

    Stop-Launcher -Reason 'PostgreSQL did not become ready in time.' -Details @(
        ('Inspect it with:  docker logs ' + $ContainerName),
        'Or start over with:  .\run-vector-local.ps1 -ResetDb'
    )
}

<#
    Bring the local database up to whatever `migrations/` currently holds.

    Through the repository's own runner -- `python -m scripts.apply_migration`
    -- rather than by piping SQL at psql. The runner takes an advisory lock,
    wraps each file in one transaction, records a ledger row and refuses a
    file whose text no longer matches the checksum it was applied under. A
    launcher that shelled SQL straight into the database would reproduce none
    of that, and would leave a local database the runner then considers
    untracked.

    Every file is offered every run. An already-applied migration is a no-op
    the runner reports and exits 0 for, so this is safe to repeat and needs
    no "which ones are pending" logic of its own.

    Nothing here names a version. The set is whatever is on disk, in filename
    order, so a migration added tomorrow is applied tomorrow without this
    function changing -- which is the point: the previous version of this file
    hardcoded 001, and every migration since would have needed an edit here.

    DATABASE_URL is set for the runner's process only, from the constant at
    the top of this file. pydantic-settings ranks a real environment variable
    above anything in `.env`, so the runner cannot reach Neon even if `.env`
    points there. `.env` is never opened by this script.
#>
function Initialize-Schema {
    Write-Step 'Schema'

    $files = @(Get-ChildItem -LiteralPath $MigrationsDir -Filter '*.sql' |
        Sort-Object -Property Name)

    if ($files.Count -eq 0) {
        Stop-Launcher -Reason 'No migrations found.' -Details @(
            ('Looked in: ' + $MigrationsDir)
        )
    }

    Write-Detail ('Applying ' + $files.Count + ' migration(s) through scripts.apply_migration...')

    # Saved and restored around the loop so the launcher does not leave the
    # owner's shell pointing at the throwaway container after it exits.
    $previousDatabaseUrl = $env:DATABASE_URL
    $previousEnvironment = $env:ENVIRONMENT

    try {
        $env:DATABASE_URL = $DatabaseUrl
        $env:ENVIRONMENT  = 'development'

        foreach ($file in $files) {
            $result = Invoke-Native -FilePath $VenvPython -Arguments @(
                '-m', 'scripts.apply_migration', $file.FullName
            ) -WorkingDirectory $RepoRoot

            if ($result.ExitCode -ne 0) {
                Stop-Launcher -Reason ('Migration ' + $file.Name + ' failed.') -Details @(
                    $result.Output,
                    '',
                    'Nothing else has been started. Start from a clean database with:',
                    '  .
un-vector-local.ps1 -ResetDb'
                )
            }

            Write-Detail ('  ' + $result.Output.Trim())
        }
    } finally {
        $env:DATABASE_URL = $previousDatabaseUrl
        $env:ENVIRONMENT  = $previousEnvironment
    }

    Assert-SchemaMatchesRepository
}

<#
    Refuse to run against a database the repository does not agree with.

    This replaces an older check that asserted the database was *001-shaped*
    -- that `issues` carried no tenancy columns. That check existed because
    the application was not tenancy-aware and a 002 database broke issue
    creation; it is obsolete now that the application is, and keeping it
    would refuse every correctly migrated database.

    What replaces it is not another shape assertion. Enumerating expected
    tables or columns would be a second, hand-maintained copy of the schema
    that goes stale on the next migration and fails in a way that names the
    launcher rather than the cause. The ledger already answers the question
    exactly: the runner records what has been applied, and `--status` reports
    anything on disk that has not been, plus any applied file whose text has
    since changed.

    So the assertion is: nothing pending, nothing mismatched. That stays
    correct for every migration this repository will ever add, without this
    function knowing any of their names.

    The check is also run through psql against the container, not only
    through the runner, because the two reach the database by different
    routes -- if DATABASE_URL had somehow pointed elsewhere, the runner would
    report a happy ledger for a database this launcher never started.
#>
function Assert-SchemaMatchesRepository {
    $previousDatabaseUrl = $env:DATABASE_URL
    $previousEnvironment = $env:ENVIRONMENT

    try {
        $env:DATABASE_URL = $DatabaseUrl
        $env:ENVIRONMENT  = 'development'

        $status = Invoke-Native -FilePath $VenvPython -Arguments @(
            '-m', 'scripts.apply_migration', '--status'
        ) -WorkingDirectory $RepoRoot
    } finally {
        $env:DATABASE_URL = $previousDatabaseUrl
        $env:ENVIRONMENT  = $previousEnvironment
    }

    if ($status.ExitCode -ne 0) {
        Stop-Launcher -Reason 'The migration ledger does not match the repository.' -Details @(
            $status.Output,
            '',
            'An applied migration''s text has changed, or a migration could not',
            'be read. This launcher will not repair a ledger. If the local',
            'database is disposable, start over with:',
            '  .
un-vector-local.ps1 -ResetDb'
        )
    }

    # The same question asked of the container directly: every file on disk
    # has a ledger row. Counted rather than listed, because the names are the
    # runner's business and the count is what this needs to compare.
    $onDisk = @(Get-ChildItem -LiteralPath $MigrationsDir -Filter '*.sql').Count
    $recorded = Get-ScalarFromDatabase -Sql 'SELECT count(*) FROM schema_migrations'

    if ([int]$recorded -ne $onDisk) {
        Stop-Launcher -Reason 'The local database is not fully migrated.' -Details @(
            ('Migrations on disk: ' + $onDisk),
            ('Recorded in this container''s ledger: ' + $recorded),
            '',
            'The runner reported success, so the two are looking at different',
            'databases. Start over with:',
            '  .
un-vector-local.ps1 -ResetDb'
        )
    }

    Write-Good ('Database is migrated to the repository''s head (' + $onDisk + ' migrations).')
}


# ---------------------------------------------------------------------------
# Frontend dependencies and contract gates
# ---------------------------------------------------------------------------

function Install-FrontendDependencies {
    Write-Step 'Frontend dependencies'

    if (Test-Path -LiteralPath (Join-Path $FrontendDir 'node_modules')) {
        Write-Good 'node_modules present; reusing it.'
        return
    }

    Write-Detail 'node_modules is missing. Running npm ci (this takes a few minutes)...'

    Push-Location -LiteralPath $FrontendDir

    try {
        # `ci`, never `install`: `install` is allowed to update
        # package-lock.json, and a launcher that rewrites the lockfile as a
        # side effect of starting the app is a launcher that dirties the
        # working tree behind the owner's back.
        $code = Invoke-NativeStreaming -FilePath 'npm' -Arguments @('ci')
    } finally {
        Pop-Location
    }

    if ($code -ne 0) {
        Stop-Launcher -Reason ('npm ci failed (exit ' + $code + ').') -Details @(
            'The output above is npm''s.'
        )
    }

    Write-Good 'Dependencies installed.'
}

<#
    Check the GraphQL contract. Check only -- never regenerate.

    Regenerating here would make the gate meaningless: the launcher would
    quietly rewrite the checked-in schema and types to match whatever the
    backend happens to be today, and the drift it exists to report would
    become invisible.

    $env:PYTHON is the interesting part. `graphql:schema:check` shells out to
    Python to build the real Strawberry schema, trying $env:PYTHON, then
    `python`, then `python3`. In a fresh shell where the virtualenv has not
    been activated, `python` on this machine resolves to the system
    interpreter at %LOCALAPPDATA%\Programs\Python\Python312, which has no
    strawberry installed -- so the check fails with ModuleNotFoundError and
    looks like a broken schema rather than a missing dependency. Naming the
    interpreter removes that trap; the value is restored afterwards so the
    owner's shell is left as it was found.
#>
function Assert-GraphqlContractCurrent {
    Write-Step 'GraphQL contract'

    $previousPython = $env:PYTHON
    $env:PYTHON = $VenvPython

    Write-Detail ('PYTHON=' + $VenvPython)

    Push-Location -LiteralPath $FrontendDir

    try {
        $schemaCode = Invoke-NativeStreaming -FilePath 'npm' -Arguments @('run', 'graphql:schema:check')
        $codegenCode = Invoke-NativeStreaming -FilePath 'npm' -Arguments @('run', 'graphql:check')
    } finally {
        Pop-Location

        if ($null -eq $previousPython) {
            Remove-Item -LiteralPath 'Env:\PYTHON' -ErrorAction SilentlyContinue
        } else {
            $env:PYTHON = $previousPython
        }
    }

    if ($schemaCode -ne 0 -or $codegenCode -ne 0) {
        Stop-Launcher -Reason 'The GraphQL contract is stale.' -Details @(
            'frontend/schema.graphql or frontend/src/generated/ no longer matches',
            'the backend schema and the operation documents.',
            '',
            'This launcher checks and never regenerates. Regenerate deliberately:',
            '  cd frontend',
            '  npm run graphql:schema',
            '  npm run graphql:codegen',
            '',
            'Then review the diff before starting the app.'
        )
    }

    Write-Good 'schema.graphql and src/generated/ are current.'
}


# ---------------------------------------------------------------------------
# Modes
# ---------------------------------------------------------------------------

function Invoke-StopMode {
    Write-Host ''
    Write-Host 'Vector local stack -- stopping' -ForegroundColor Cyan

    Write-Step 'Processes'
    $state = Get-LauncherState

    if ($null -eq $state) {
        Write-Detail ('No state file at ' + $StateFile + '.')
    }

    Stop-RecordedProcess -State $state

    Write-Step 'Database container'
    Stop-DedicatedContainer

    Write-Step 'State'
    Remove-LauncherState

    Write-Host ''
    Write-Host 'Stopped.' -ForegroundColor Green
    Write-Host ''
}

<#
    Throw away the dedicated container and start over.

    `docker rm -f` is aimed at exactly one name, which this launcher created.
    No filter, no prune, no sweep: nothing else on the machine is a candidate.
#>
function Invoke-DatabaseReset {
    Write-Step 'Resetting the local database'

    $state = Get-LauncherState

    if ($null -ne $state) {
        Write-Detail 'Stopping anything this launcher had running...'
        Stop-RecordedProcess -State $state
        Remove-LauncherState
    }

    $containerState = Get-ContainerState

    if ($containerState -eq 'absent') {
        Write-Detail ('Container ' + $ContainerName + ' does not exist; nothing to remove.')
        return
    }

    $result = Invoke-Native -FilePath 'docker' -Arguments @('rm', '--force', $ContainerName)

    if ($result.ExitCode -ne 0) {
        Stop-Launcher -Reason ('Could not remove ' + $ContainerName + '.') -Details @($result.Output)
    }

    Write-Good ('Removed ' + $ContainerName + ' and its data.')
}

function Invoke-StartMode {
    Write-Host ''
    Write-Host 'Vector local stack -- starting' -ForegroundColor Cyan
    Write-Detail ('Repository: ' + $RepoRoot)
    Write-Detail ('Database:   ' + $DatabaseUrlForDisplay + '  (local container only)')

    Assert-Prerequisites

    if ($ResetDb) {
        Invoke-DatabaseReset
    } else {
        Assert-NoRunningStack
    }

    Assert-PortsAvailable

    Start-DedicatedContainer
    Wait-ForDatabase
    Initialize-Schema

    Install-FrontendDependencies
    Assert-GraphqlContractCurrent

    # --- backend -----------------------------------------------------------

    Write-Step ('Backend (' + $BackendWindowTitle + ')')

    $backend = Start-ChildWindow -ScriptBody (New-BackendWindowScript) -WorkingDirectory $RepoRoot

    # Recorded before the health poll, so that a backend which starts but
    # never becomes healthy is still something `-Stop` can clean up.
    Save-LauncherState -Processes @((New-ProcessRecord -Role 'backend' -Process $backend))

    Write-Detail ('PID ' + $backend.Id + '. Polling ' + $BackendHealthUrl)

    $backendStatus = Wait-ForHttpEndpoint -Url $BackendHealthUrl `
        -TimeoutSeconds $BackendReadyTimeoutSeconds -Process $backend

    if ($backendStatus -ne 'ok') {
        $reason = 'The backend did not become healthy within ' + $BackendReadyTimeoutSeconds + ' seconds.'

        if ($backendStatus -eq 'process-exited') {
            $reason = 'The backend window exited before the API became healthy.'
        }

        Stop-Launcher -Reason $reason -Details @(
            'The frontend has deliberately NOT been started.',
            '',
            ('Look at the "' + $BackendWindowTitle + '" window: the reason is on screen there.'),
            'Common causes: an import error, or a database the app cannot reach.',
            '',
            'When you have read it, clean up with:',
            '  .\run-vector-local.ps1 -Stop'
        )
    }

    Write-Good ('Backend healthy at ' + $BackendHealthUrl)

    # --- frontend ----------------------------------------------------------

    Write-Step ('Frontend (' + $FrontendWindowTitle + ')')

    $frontend = Start-ChildWindow -ScriptBody (New-FrontendWindowScript) -WorkingDirectory $FrontendDir

    Save-LauncherState -Processes @(
        (New-ProcessRecord -Role 'backend' -Process $backend),
        (New-ProcessRecord -Role 'frontend' -Process $frontend)
    )

    Write-Detail ('PID ' + $frontend.Id + '. Polling ' + $FrontendUrl)

    $frontendStatus = Wait-ForHttpEndpoint -Url $FrontendUrl `
        -TimeoutSeconds $FrontendReadyTimeoutSeconds -Process $frontend

    if ($frontendStatus -ne 'ok') {
        $reason = 'The dev server did not answer within ' + $FrontendReadyTimeoutSeconds + ' seconds.'

        if ($frontendStatus -eq 'process-exited') {
            $reason = 'The frontend window exited before the dev server answered.'
        }

        Stop-Launcher -Reason $reason -Details @(
            'The backend is healthy; only Vite is unaccounted for.',
            ('Look at the "' + $FrontendWindowTitle + '" window.'),
            '',
            'Clean up with:  .\run-vector-local.ps1 -Stop'
        )
    }

    Write-Good ('Frontend answering at ' + $FrontendUrl)

    Test-GraphqlThroughProxy

    # --- browser -----------------------------------------------------------

    if ($NoBrowser) {
        Write-Step 'Ready'
        Write-Detail ('Browser not opened (-NoBrowser). Open ' + $AppUrl + ' yourself.')
    } else {
        Write-Step 'Opening the app'
        Start-Process $AppUrl | Out-Null
        Write-Good $AppUrl
    }

    Write-Host ''
    Write-Host '---------------------------------------------------------------' -ForegroundColor Green
    Write-Host '  Vector is running.' -ForegroundColor Green
    Write-Host ('    app       ' + $AppUrl) -ForegroundColor Green
    Write-Host ('    api       http://127.0.0.1:' + $BackendPort + '/graphql') -ForegroundColor Green
    Write-Host ('    database  ' + $DatabaseUrlForDisplay) -ForegroundColor Green
    Write-Host '' -ForegroundColor Green
    Write-Host '  Stop it with:  .\run-vector-local.ps1 -Stop' -ForegroundColor Green
    Write-Host '---------------------------------------------------------------' -ForegroundColor Green
    Write-Host ''
}

<#
    Prove the Vite proxy actually forwards GraphQL.

    `{ __typename }` executes no resolver and touches no database, so it
    fails only if the transport is broken. A warning rather than a refusal:
    both servers have already answered on their own, the app is up, and the
    owner is better served by a named suspicion than by a launcher that
    refuses to open a browser onto a working stack.
#>
function Test-GraphqlThroughProxy {
    Write-Step 'GraphQL through the Vite proxy'

    try {
        $response = Invoke-WebRequest -Uri $FrontendGraphqlUrl -UseBasicParsing -Method Post `
            -ContentType 'application/json' -Body '{"query":"{ __typename }"}' -TimeoutSec 10

        if ($response.Content -like '*__typename*') {
            Write-Good ('POST ' + $FrontendGraphqlUrl + ' -> ' + $response.StatusCode)
            return
        }

        Write-Note ('Unexpected response body from ' + $FrontendGraphqlUrl + ': ' + $response.Content)
    } catch {
        Write-Note ('Could not reach GraphQL through the proxy: ' + $_.Exception.Message)
        Write-Note 'The app may fail to load data. Check both windows.'
    }
}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if ($Stop -and $ResetDb) {
    Stop-Launcher -Reason '-Stop and -ResetDb do not go together.' -Details @(
        '-Stop shuts the stack down. -ResetDb recreates the database and starts it.'
    )
}

if ($Stop) {
    Invoke-StopMode
} else {
    Invoke-StartMode
}
