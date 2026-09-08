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

    4. **It leaves the browser looking at a product, not an empty list.** A
       demo workspace is seeded through the application's own GraphQL API --
       `register`, `issueCreate`, `issueUpdate` -- by
       `python -m scripts.seed_local_demo`, which prints the demo sign-in. A
       seed that wrote rows straight into PostgreSQL would bypass every rule
       the services own and would stop proving the write path works.

.PARAMETER Stop
    Stop the processes and container this launcher started, then remove its
    state file. Also what to run after closing the launcher's own window
    instead of interrupting it.

.PARAMETER Fresh
    Destroy and recreate the dedicated `vector-ui-dev` container (and only
    that container) before starting, so 001..N are applied to an empty
    database and can be seen to apply cleanly. Also the way out of the
    launcher's report that the local database and the repository's migrations
    disagree. `-ResetDb` is the same switch under its older name.

.PARAMETER Preview
    Serve the production bundle -- `npm run build` then `npm run preview` --
    instead of the dev server, on the same port. Use it to look at what
    actually ships: the dev server transforms modules on demand and applies
    neither minification, tree-shaking nor `import.meta.env.PROD`, so a bug
    that only the real bundle has is invisible under `npm run dev`.

.PARAMETER NoBrowser
    Start everything but do not open a browser window.

.EXAMPLE
    .\run-vector-local.ps1
    .\run-vector-local.ps1 -Fresh
    .\run-vector-local.ps1 -Preview
    .\run-vector-local.ps1 -NoBrowser
    .\run-vector-local.ps1 -Stop
#>

[CmdletBinding()]
param(
    [switch]$Stop,

    # `-ResetDb` was this switch's original name and still works, because it
    # is in the owner's fingers and in three of this script's own error
    # messages. One switch with two spellings, never two switches.
    [Alias('ResetDb')]
    [switch]$Fresh,

    [switch]$Preview,
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

# The official postgres:18 with pgvector compiled in, and the pin is two pins
# in one. `uuidv7()` in migrations/001_issues.sql is native to PostgreSQL 18
# and rejected by 16 and 17; the `vector` extension that
# migrations/025_semantic_search.sql creates is not in stock postgres:18.
#
# This was `postgres:18` until 025 landed, and the failure it produced is the
# reason the check further down exists: the migration runner got as far as 024
# and then stopped on `extension "vector" is not available`, which reads as a
# broken migration rather than as a server missing an extension. Every other
# path in this repository already pins this image -- tests/conftest.py,
# docker-compose.yml and the CI workflow -- so the launcher was the only place
# a developer could get a server the test suite never sees.
$PostgresImage = 'pgvector/pgvector:pg18'
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

$VenvDir       = Join-Path $RepoRoot '.venv'
$VenvPython    = Join-Path $VenvDir 'Scripts\python.exe'
$FrontendDir   = Join-Path $RepoRoot 'frontend'
$MigrationsDir = Join-Path $RepoRoot 'migrations'

# The compiled lock, not requirements.in. It is hash-pinned, so a cold install
# resolves to exactly the versions CI and the other machines have.
$RequirementsFile = Join-Path $RepoRoot 'requirements.txt'

# requirements.txt is compiled `--python-version 3.12`, and uuidv7() in
# migration 001 needs PostgreSQL 18; the Python floor is the one this script
# can check before it installs anything.
$MinimumPythonMinor = 12

# What to tell someone who does not have a prerequisite. A URL and a name,
# because "install docker" is advice they had already worked out for
# themselves by the time they read it.
$Prerequisites = @(
    @{
        Tool    = 'docker'
        Install = 'Docker Desktop -- https://docs.docker.com/desktop/setup/install/windows-install/'
    },
    @{
        Tool    = 'node'
        Install = 'Node.js 20.19 or newer -- https://nodejs.org/en/download'
    },
    @{
        Tool    = 'npm'
        Install = 'npm, which ships with Node.js -- https://nodejs.org/en/download'
    }
)

# Deliberately outside the repository: a state file inside it would show up
# as an untracked change on every run.
$StateFile = Join-Path $env:TEMP 'vector-local-dev-state.json'

$BackendHealthUrl  = 'http://127.0.0.1:' + $BackendPort + '/healthz'

# Reached directly rather than through the Vite proxy, by both the seeder and
# the banner. The seeder runs before the frontend is started, so the proxy does
# not exist yet; the loopback literal is also what `scripts.seed_local_demo`
# will accept, and it refuses anything else.
$BackendGraphqlUrl = 'http://127.0.0.1:' + $BackendPort + '/graphql'

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

<# The image the container was CREATED from, or '' if it cannot be read. #>
function Get-ContainerImage {
    $result = Invoke-Native -FilePath 'docker' -Arguments @(
        'inspect', '--format', '{{.Config.Image}}', $ContainerName
    )

    if ($result.ExitCode -ne 0) {
        return ''
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
    The path goes through Vite's proxy and stays same-origin. `vite preview`
    proxies it too -- see the shared table in frontend/vite.config.ts.

    Under -Preview the window runs `npm run preview` and nothing else: the
    build has already been run and gated by Build-FrontendBundle, so a
    TypeScript error is a refusal with the compiler's output attached rather
    than a window that never answers and a poll that times out 60 seconds
    later naming nothing.

    `--strictPort` matters for the same reason. Vite's preview server moves to
    the next free port when the one it asked for is taken; without the flag a
    port taken between Assert-PortsAvailable and this line puts the app on
    5174 while the launcher polls 5173, which reads as a dead frontend.
#>
function New-FrontendWindowScript {
    param([switch]$UseBuild)

    $template = @'
$Host.UI.RawUI.WindowTitle = __TITLE__
Set-Location -LiteralPath __FRONTEND__

# A path, not an origin: the Vite proxy forwards it, and the request stays
# same-origin. See frontend/vite.config.ts.
$env:VITE_GRAPHQL_URL = '/graphql'

Write-Host ''
Write-Host '==================================================' -ForegroundColor Cyan
Write-Host '  Vector Frontend (__MODE__)' -ForegroundColor Cyan
Write-Host '  /graphql is proxied to 127.0.0.1:__BACKEND_PORT__' -ForegroundColor Cyan
Write-Host '==================================================' -ForegroundColor Cyan
Write-Host ''

__COMMAND__

Write-Host ''
Write-Host '**************************************************' -ForegroundColor Red
Write-Host '  The Vector frontend exited. The output above is' -ForegroundColor Red
Write-Host '  why. This window stays open on purpose.' -ForegroundColor Red
Write-Host '**************************************************' -ForegroundColor Red
'@

    if ($UseBuild) {
        $mode = 'production build'
        $command = 'npm run preview -- --port ' + $FrontendPort + ' --strictPort'
    } else {
        $mode = 'dev server'
        $command = 'npm run dev'
    }

    $body = $template
    $body = $body.Replace('__TITLE__', (ConvertTo-PowerShellLiteral $FrontendWindowTitle))
    $body = $body.Replace('__FRONTEND__', (ConvertTo-PowerShellLiteral $FrontendDir))
    $body = $body.Replace('__BACKEND_PORT__', [string]$BackendPort)
    $body = $body.Replace('__MODE__', $mode)
    $body = $body.Replace('__COMMAND__', $command)

    return $body
}


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------

function Assert-Prerequisites {
    Write-Step 'Preflight'

    $required = @(
        @{ Path = $MigrationsDir;    What = 'the migrations directory' },
        @{ Path = $RequirementsFile; What = 'the Python lockfile' },
        @{ Path = (Join-Path $RepoRoot 'scripts\seed_local_demo.py'); What = 'the demo seeder' },
        @{ Path = (Join-Path $FrontendDir 'package.json');      What = 'the frontend manifest' },
        @{ Path = (Join-Path $FrontendDir 'package-lock.json'); What = 'the frontend lockfile' },
        @{ Path = (Join-Path $FrontendDir 'vite.config.ts');    What = 'the Vite config' }
    )

    foreach ($entry in $required) {
        if (-not (Test-Path -LiteralPath $entry.Path)) {
            Stop-Launcher -Reason ('Cannot find ' + $entry.What + '.') -Details @(
                ('Expected: ' + $entry.Path),
                'This does not look like a complete Vector checkout. Clone it again.'
            )
        }
    }

    Write-Good 'Repository layout looks complete.'

    # Every missing tool is collected before any of them is reported. Someone
    # setting up a new machine typically has none of the three, and a launcher
    # that names one, is fixed, then names the next has made them run three
    # installs and four launches to learn what one message could have said.
    $missing = @()

    foreach ($entry in $Prerequisites) {
        if ($null -eq (Get-Command -Name $entry.Tool -ErrorAction SilentlyContinue)) {
            $missing += ('  ' + $entry.Tool + ' -- install ' + $entry.Install)
        }
    }

    if ($missing.Count -gt 0) {
        Stop-Launcher -Reason 'Missing prerequisites.' -Details (
            @('Not on PATH:') + $missing + @(
                '',
                'Install them, open a new shell so PATH is picked up, and run this again.'
            )
        )
    }

    $dockerInfo = Invoke-Native -FilePath 'docker' -Arguments @('info', '--format', '{{.ServerVersion}}')

    if ($dockerInfo.ExitCode -ne 0) {
        Stop-Launcher -Reason 'The Docker daemon is not responding.' -Details @(
            'Docker is installed but not running. Start Docker Desktop and wait',
            'for it to report "running", then try again.',
            $dockerInfo.Output
        )
    }

    Write-Good ('Docker daemon reachable (server ' + $dockerInfo.Output + ').')
}

<#
    The first interpreter on this machine new enough to build the virtualenv,
    or $null.

    `py` is tried before `python` deliberately. A bare `python` on a Windows
    machine that has never installed one resolves to the App Execution Alias:
    a zero-byte stub that opens the Microsoft Store and exits without running
    anything. `Get-Command python` finds it, so presence is not evidence, and
    the version probe below is what actually separates the two.
#>
function Find-BasePython {
    $candidates = @(
        @{ File = 'py';      Arguments = @('-3.12') },
        @{ File = 'py';      Arguments = @('-3') },
        @{ File = 'python';  Arguments = @() },
        @{ File = 'python3'; Arguments = @() }
    )

    foreach ($candidate in $candidates) {
        if ($null -eq (Get-Command -Name $candidate.File -ErrorAction SilentlyContinue)) {
            continue
        }

        # The probe contains no quote character, and that is a requirement
        # rather than a style. Windows PowerShell 5.1 re-parses the arguments
        # it hands a native command and eats embedded double quotes, so the
        # obvious `print("%d.%d" % sys.version_info[:2])` reaches Python as
        # `print(%d.%d % sys.version_info[:2])` and dies of a SyntaxError --
        # which this function would read as "not a usable interpreter" and
        # skip, on a machine with a perfectly good Python on it.
        $probe = Invoke-Native -FilePath $candidate.File -Arguments (
            $candidate.Arguments + @('-c', 'import sys; print(sys.version_info[0], sys.version_info[1])')
        )

        if ($probe.ExitCode -ne 0) {
            continue
        }

        $parts = @($probe.Output.Trim() -split '\s+')

        if ($parts.Count -lt 2) {
            continue
        }

        $major = 0
        $minor = 0

        # TryParse rather than a cast: a stub or a wrapper can answer with
        # anything at all on stdout, and a cast to [int] of that would throw
        # out of a function whose whole job is to keep looking.
        if (-not [int]::TryParse($parts[0], [ref]$major)) { continue }
        if (-not [int]::TryParse($parts[1], [ref]$minor)) { continue }

        if ($major -ne 3 -or $minor -lt $MinimumPythonMinor) {
            continue
        }

        return [pscustomobject]@{
            File      = $candidate.File
            Arguments = $candidate.Arguments
            Version   = ('3.' + $minor)
        }
    }

    return $null
}

<#
    Make sure `.venv` exists, building it from the lockfile if it does not.

    The symmetric counterpart of Install-FrontendDependencies, and there for
    the same reason: a clone has neither `node_modules` nor `.venv`, and a
    launcher that installs one but instructs the owner to install the other by
    hand is a launcher that does not actually start from a clone.

    Only ever creates. An existing `.venv` is reused untouched -- no upgrade,
    no sync, no `pip install` over the top of it. The owner may be holding a
    deliberately patched dependency in there, and a launcher that quietly
    reverted it while starting the app would be very hard to suspect.
#>
function Install-BackendVirtualenv {
    Write-Step 'Backend virtualenv'

    if (Test-Path -LiteralPath $VenvPython) {
        Write-Good '.venv present; reusing it.'
        return
    }

    $base = Find-BasePython

    if ($null -eq $base) {
        Stop-Launcher -Reason ('No Python 3.' + $MinimumPythonMinor + ' or newer was found.') -Details @(
            ('Install Python 3.' + $MinimumPythonMinor + ' or newer -- ' +
                'https://www.python.org/downloads/windows/'),
            'Tick "Add python.exe to PATH" in the installer, then open a new',
            'shell and run this again.',
            '',
            ('Tried: py -3.' + $MinimumPythonMinor + ', py -3, python, python3.')
        )
    }

    Write-Detail ('Creating .venv with ' + $base.File + ' (Python ' + $base.Version + ')...')

    $code = Invoke-NativeStreaming -FilePath $base.File -Arguments (
        $base.Arguments + @('-m', 'venv', $VenvDir)
    )

    if ($code -ne 0) {
        Stop-Launcher -Reason ('Could not create ' + $VenvDir + ' (exit ' + $code + ').') -Details @(
            'The output above is Python''s.'
        )
    }

    Write-Detail 'Installing backend dependencies from requirements.txt (a few minutes)...'

    # requirements.txt, never requirements.in: the compiled lock is hash-pinned,
    # so this resolves to the same versions CI has rather than to whatever is
    # newest today.
    $code = Invoke-NativeStreaming -FilePath $VenvPython -Arguments @(
        '-m', 'pip', 'install', '--disable-pip-version-check',
        '--requirement', $RequirementsFile
    )

    if ($code -ne 0) {
        # Removed rather than left behind. A half-populated .venv passes the
        # Test-Path above on the next run, so keeping it would turn one clear
        # failure into an import error somewhere downstream on every run after.
        Remove-Item -LiteralPath $VenvDir -Recurse -Force -ErrorAction SilentlyContinue

        Stop-Launcher -Reason ('Installing backend dependencies failed (exit ' + $code + ').') -Details @(
            'The output above is pip''s. The incomplete .venv has been removed.'
        )
    }

    Write-Good 'Virtualenv created and dependencies installed.'
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
                'If it is damaged, recreate it with:  .\run-vector-local.ps1 -Fresh'
            )
        }

        Write-Good ('Started ' + $ContainerName + '.')
    }

    # A container reused from an older run could have been created from the
    # image this launcher used BEFORE migration 025 needed pgvector. Changing
    # the constant above does not recreate an existing container, so without
    # this check the only symptom is the migration runner stopping at 025 on
    # `extension "vector" is not available` -- an error that names the
    # extension but not the reason the server lacks it, and that reads as a
    # broken migration rather than as a container built from the wrong image.
    #
    # Checked before the port mapping because it is the one a developer who
    # has run this launcher before will actually hit.
    $image = Get-ContainerImage

    if ($image -ne $PostgresImage) {
        Stop-Launcher -Reason ($ContainerName + ' was created from a different PostgreSQL image.') -Details @(
            ('Expected: ' + $PostgresImage),
            ('Actual:   ' + $image),
            'migrations/025_semantic_search.sql needs the pgvector extension,',
            'which stock postgres:18 does not ship. Recreate the container:',
            '  .\run-vector-local.ps1 -Fresh'
        )
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
            'Recreate it with:  .\run-vector-local.ps1 -Fresh'
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
        'Or start over with:  .\run-vector-local.ps1 -Fresh'
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
                    '  .\run-vector-local.ps1 -Fresh'
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
            '  .\run-vector-local.ps1 -Fresh'
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
            '  .\run-vector-local.ps1 -Fresh'
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

<#
    Under -Preview, build the production bundle before anything is started.

    Here rather than in the frontend window, and that placement is the point.
    `npm run build` is `tsc -b && vite build`, so it fails on any type error in
    the repository -- and a build that failed inside the child window would
    leave the launcher polling a port nothing will ever listen on, reporting a
    60-second timeout whose actual cause scrolled past in another window. Run
    here, a type error is a refusal with the compiler's own output above it.

    A no-op without -Preview: the dev server compiles on demand and needs no
    dist/ at all, and building one anyway would add a minute to every ordinary
    run for an artefact nothing then reads.
#>
function Build-FrontendBundle {
    if (-not $Preview) {
        return
    }

    Write-Step 'Frontend production build'
    Write-Detail 'npm run build (tsc -b && vite build)...'

    Push-Location -LiteralPath $FrontendDir

    try {
        $code = Invoke-NativeStreaming -FilePath 'npm' -Arguments @('run', 'build')
    } finally {
        Pop-Location
    }

    if ($code -ne 0) {
        Stop-Launcher -Reason ('npm run build failed (exit ' + $code + ').') -Details @(
            'The output above is the compiler''s. Nothing has been started.',
            '',
            'The dev server does not type-check, so this can fail on a tree',
            'that `npm run dev` serves quite happily.'
        )
    }

    Write-Good 'dist/ built.'
}

<#
    Put a demo workspace in the local database, through the product's own API.

    Delegated to `python -m scripts.seed_local_demo` rather than done here in
    psql, and the reason is the same one that sends migrations through the
    repository's runner: a seed written in SQL bypasses every rule the services
    own -- the priority range, the workflow-state derivation of completed_at,
    the issue-number allocation -- and so stops being evidence that the write
    path works. The seeder speaks GraphQL over loopback and holds no database
    credentials at all.

    Run after the backend is healthy and before the frontend starts, so that
    the first page the browser renders already has issues on it.

    Its output is passed straight through, including the demo sign-in it
    prints. That is the one credential this launcher deliberately shows: a
    throwaway account, with a password committed in plain sight, in a database
    `-Fresh` destroys. Every other credential here stays out of the terminal.

    A failure warns rather than stops. The stack is up and usable at this
    point, and an empty issue list is a worse outcome than no launch only if
    you wanted the demo data; refusing to open a working application over it
    would be the launcher substituting its priorities for the owner's.
#>
function Initialize-DemoData {
    Write-Step 'Demo data'

    $result = Invoke-Native -FilePath $VenvPython -Arguments @(
        '-m', 'scripts.seed_local_demo', '--api-url', $BackendGraphqlUrl
    ) -WorkingDirectory $RepoRoot

    Write-Host $result.Output -ForegroundColor Gray

    if ($result.ExitCode -ne 0) {
        Write-Note 'Seeding failed. The stack is still starting; the issue list may be empty.'
        return
    }

    Write-Good 'Local demo workspace ready.'
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
    Install-BackendVirtualenv

    if ($Fresh) {
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
    Build-FrontendBundle

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

    Initialize-DemoData

    # --- frontend ----------------------------------------------------------

    Write-Step ('Frontend (' + $FrontendWindowTitle + ')')

    $frontend = Start-ChildWindow -ScriptBody (New-FrontendWindowScript -UseBuild:$Preview) `
        -WorkingDirectory $FrontendDir

    Save-LauncherState -Processes @(
        (New-ProcessRecord -Role 'backend' -Process $backend),
        (New-ProcessRecord -Role 'frontend' -Process $frontend)
    )

    Write-Detail ('PID ' + $frontend.Id + '. Polling ' + $FrontendUrl)

    $frontendStatus = Wait-ForHttpEndpoint -Url $FrontendUrl `
        -TimeoutSeconds $FrontendReadyTimeoutSeconds -Process $frontend

    if ($frontendStatus -ne 'ok') {
        $server = if ($Preview) { 'preview server' } else { 'dev server' }
        $reason = 'The ' + $server + ' did not answer within ' + $FrontendReadyTimeoutSeconds + ' seconds.'

        if ($frontendStatus -eq 'process-exited') {
            $reason = 'The frontend window exited before the ' + $server + ' answered.'
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

    $servedBy = if ($Preview) { 'production build (npm run preview)' } else { 'dev server (npm run dev)' }

    Write-Host ''
    Write-Host '---------------------------------------------------------------' -ForegroundColor Green
    Write-Host '  Vector is running.' -ForegroundColor Green
    Write-Host ('    app       ' + $AppUrl) -ForegroundColor Green
    Write-Host ('    api       ' + $BackendGraphqlUrl) -ForegroundColor Green
    Write-Host ('    database  ' + $DatabaseUrlForDisplay) -ForegroundColor Green
    Write-Host ('    frontend  ' + $servedBy) -ForegroundColor Green
    Write-Host '' -ForegroundColor Green
    Write-Host '  Press Ctrl+C here to stop everything this launcher started.' -ForegroundColor Green
    Write-Host '  Or close this window and stop it later with:' -ForegroundColor Green
    Write-Host '    .\run-vector-local.ps1 -Stop' -ForegroundColor Green
    Write-Host '---------------------------------------------------------------' -ForegroundColor Green
    Write-Host ''

    Wait-UntilInterrupted
}

<#
    Hold the launcher in the foreground until Ctrl+C.

    This is what makes Ctrl+C mean anything. The servers run in their own
    windows, so without something to interrupt there is no process for the key
    to reach and the only shutdown is `-Stop` at some later point -- easy to
    forget, and a forgotten backend is what holds port 8000 against the next
    run.

    Sleeping in short slices rather than one long one: PowerShell delivers the
    interrupt at a statement boundary, so a single `Start-Sleep -Seconds 3600`
    would swallow Ctrl+C for as long as it felt like.

    The caller's `finally` does the stopping. Nothing is torn down here,
    because this function is also what a normal `exit` unwinds through.
#>
function Wait-UntilInterrupted {
    $script:AttachedAndRunning = $true

    while ($true) {
        Start-Sleep -Milliseconds 500
    }
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

if ($Stop -and $Fresh) {
    Stop-Launcher -Reason '-Stop and -Fresh do not go together.' -Details @(
        '-Stop shuts the stack down. -Fresh recreates the database and starts it.'
    )
}

if ($Stop -and $Preview) {
    Stop-Launcher -Reason '-Stop and -Preview do not go together.' -Details @(
        '-Preview chooses how the frontend is served, which -Stop does not start.'
    )
}

if ($Stop) {
    Invoke-StopMode
    return
}

# Set inside Wait-UntilInterrupted, and read only by the finally below. It is
# the difference between "the owner interrupted a running stack" and every
# other way out of Invoke-StartMode.
$script:AttachedAndRunning = $false

try {
    Invoke-StartMode
} finally {
    # Ctrl+C, and only Ctrl+C.
    #
    # `finally` also runs for the `exit` inside Stop-Launcher, which is why
    # this is gated rather than unconditional. Almost every one of those
    # refusals ends by telling the owner to go and read the backend or
    # frontend window, and a teardown here would close the window they were
    # sent to read -- turning a diagnosis into a blank screen. Those paths
    # leave the state file behind on purpose, and `-Stop` clears them.
    if ($script:AttachedAndRunning) {
        $script:AttachedAndRunning = $false

        Write-Host ''
        Write-Host 'Interrupted. Stopping what this launcher started...' -ForegroundColor Cyan

        Invoke-StopMode
    }
}
