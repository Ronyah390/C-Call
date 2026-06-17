# C-Call IVR - Windows launcher
# Installs/checks common runtime dependencies, starts Docker/PostgreSQL if
# configured, otherwise falls back to SQLite.
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "  ============================" -ForegroundColor Cyan
Write-Host "        C-Call IVR Console" -ForegroundColor Cyan
Write-Host "  ============================" -ForegroundColor Cyan
Write-Host ""

# --- 0. Runtime dependency helpers ------------------------------------------
function Refresh-Path {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = "$machine;$user"
}

function Try-WingetInstall($id, $label) {
    if (-not (Get-Command "winget" -ErrorAction SilentlyContinue)) {
        Write-Host "      winget not found. Please install $label manually." -ForegroundColor Red
        return $false
    }
    Write-Host "      Installing $label with winget..." -ForegroundColor Yellow
    & winget install --id $id --exact --silent --accept-source-agreements --accept-package-agreements
    Refresh-Path
    return $LASTEXITCODE -eq 0
}

Write-Host "[0/7] Checking Windows runtime dependencies..." -ForegroundColor Yellow
if (-not (Get-Command "py" -ErrorAction SilentlyContinue) -and -not (Get-Command "python" -ErrorAction SilentlyContinue)) {
    $null = Try-WingetInstall "Python.Python.3.12" "Python 3.12"
}
if (-not (Get-Command "ffmpeg" -ErrorAction SilentlyContinue)) {
    $installed = Try-WingetInstall "Gyan.FFmpeg" "FFmpeg"
    if (-not $installed -and -not (Get-Command "ffmpeg" -ErrorAction SilentlyContinue)) {
        Write-Host "      WARNING: FFmpeg is required for audio conversion and recordings." -ForegroundColor Red
        Write-Host "      Install FFmpeg or set FFMPEG_PATH in .env before using calls." -ForegroundColor Red
    }
}
else {
    Write-Host "      Python/FFmpeg checks passed." -ForegroundColor Green
}

# --- 1. Database selection ---------------------------------------------------
# Deterministic: SQLite by default (zero setup). PostgreSQL ONLY if you opt in
# by setting a real DATABASE_URL in .env. This prevents settings from being
# split across two databases between runs.
$usePostgres = $false
$wantPostgres = $false

if (Test-Path ".env") {
    $envLines = Get-Content ".env"
    foreach ($line in $envLines) {
        if ($line -match "^\s*DATABASE_URL\s*=\s*(\S.*)$") {
            $wantPostgres = $true
        }
    }
}

if (-not $wantPostgres) {
    Write-Host "[1/7] Database: SQLite (default, no setup needed)." -ForegroundColor Yellow
    Write-Host "      To use PostgreSQL instead, set DATABASE_URL in .env and restart." -ForegroundColor DarkGray
}
elseif (Get-Command "docker" -ErrorAction SilentlyContinue) {
    Write-Host "[1/7] DATABASE_URL is set - bringing up Docker/PostgreSQL..." -ForegroundColor Yellow
    $null = & docker info 2>&1

    if ($LASTEXITCODE -ne 0) {
        Write-Host "      Docker Desktop is not running. Attempting to start it..." -ForegroundColor Yellow
        $dockerPaths = @(
            "C:\Program Files\Docker\Docker\Docker Desktop.exe",
            "$env:LOCALAPPDATA\Docker\Docker Desktop.exe"
        )
        $launched = $false
        foreach ($p in $dockerPaths) {
            if (Test-Path $p) {
                Start-Process $p
                $launched = $true
                break
            }
        }
        if ($launched) {
            Write-Host "      Waiting for Docker daemon (up to 90s)..." -ForegroundColor Yellow
            $waited = 0
            while ($waited -lt 90) {
                Start-Sleep 3
                $waited += 3
                $null = & docker info 2>&1
                if ($LASTEXITCODE -eq 0) { break }
                Write-Host "      Still waiting... $waited s" -ForegroundColor DarkGray
            }
        }
    }

    $null = & docker info 2>&1
    if ($LASTEXITCODE -eq 0) {
        & docker compose up -d 2>&1 | ForEach-Object { Write-Host "      $_" -ForegroundColor DarkGray }
        if ($LASTEXITCODE -eq 0) {
            $usePostgres = $true
            Write-Host "      PostgreSQL container is up." -ForegroundColor Green
        }
        else {
            Write-Host "      docker compose failed. Set DATABASE_URL blank in .env to use SQLite." -ForegroundColor Red
        }
    }
    else {
        Write-Host "      Docker not ready. Set DATABASE_URL blank in .env to use SQLite." -ForegroundColor Red
    }
}
else {
    Write-Host "[1/7] DATABASE_URL is set but Docker is not installed." -ForegroundColor Red
    Write-Host "      Install Docker, or blank out DATABASE_URL in .env to use SQLite." -ForegroundColor Red
}

# --- 2. Python virtual environment -------------------------------------------
Write-Host "[2/7] Checking Python virtual environment..." -ForegroundColor Yellow
if (-not (Test-Path ".venv")) {
    if (Get-Command "py" -ErrorAction SilentlyContinue) {
        & py -3 -m venv .venv
    }
    elseif (Get-Command "python" -ErrorAction SilentlyContinue) {
        & python -m venv .venv
    }
    else {
        Write-Host "      ERROR: Python not found. Install Python 3.10+ from python.org, then run run.bat again." -ForegroundColor Red
        Read-Host "Press Enter to exit"
        exit 1
    }
    Write-Host "      Virtual environment created." -ForegroundColor Green
}
$py = ".\.venv\Scripts\python.exe"

# --- 3. Install / update dependencies ----------------------------------------
Write-Host "[3/7] Installing Python dependencies..." -ForegroundColor Yellow
& $py -m pip install --upgrade pip --quiet
& $py -m pip install -r requirements.txt --quiet
Write-Host "      Dependencies ready." -ForegroundColor Green

# --- 4. .env file ------------------------------------------------------------
Write-Host "[4/7] Checking .env..." -ForegroundColor Yellow
if (-not (Test-Path ".env")) {
    if (Test-Path ".env.example") {
        Copy-Item ".env.example" ".env"
        Write-Host "      Created .env from .env.example" -ForegroundColor Yellow
    }
    else {
        $key = [guid]::NewGuid().ToString('N')
        "FLASK_SECRET_KEY=$key`nPORT=5000`nHOST=127.0.0.1`nCALL_WORKERS=4`n" |
            Out-File -FilePath ".env" -Encoding utf8
        Write-Host "      Created .env with defaults." -ForegroundColor Yellow
    }
}

# --- 5. Align preflight with .env (the single source of truth) ---------------
Write-Host "[5/7] Configuring database..." -ForegroundColor Yellow

# Extract DATABASE_URL from .env so the preflight init hits the SAME database
# the app will use (app.py reads .env via load_dotenv).
$dbUrl = ""
foreach ($line in (Get-Content ".env")) {
    if ($line -match "^\s*DATABASE_URL\s*=\s*(\S.*)$") {
        $dbUrl = $Matches[1].Trim()
    }
}
$env:DATABASE_URL = $dbUrl

if ($usePostgres) {
    Write-Host "      Waiting for PostgreSQL to accept connections..." -ForegroundColor Yellow
    $pgReady = $false
    for ($i = 0; $i -lt 20; $i++) {
        $check = & $py -c "import psycopg; psycopg.connect('$dbUrl', connect_timeout=2).close(); print('OK')" 2>&1
        if ("$check" -like "OK*") {
            $pgReady = $true
            Write-Host "      PostgreSQL is ready." -ForegroundColor Green
            break
        }
        Write-Host "      Not ready yet ($i/20)" -ForegroundColor DarkGray
        Start-Sleep 3
    }
    if (-not $pgReady) {
        Write-Host "      WARNING: PostgreSQL not reachable. The app may fail to start." -ForegroundColor Red
        Write-Host "      Fix Docker, or blank out DATABASE_URL in .env to use SQLite." -ForegroundColor Red
    }
}
else {
    Write-Host "      Using SQLite at instance\c_call_ivr.db" -ForegroundColor Yellow
}

# --- 6. Init database and start ----------------------------------------------
Write-Host "[6/7] Initializing database tables..." -ForegroundColor Yellow
$initResult = & $py -c "import db; db.init_db(); print('DB OK')" 2>&1
if ("$initResult" -like "*DB OK*") {
    Write-Host "      Database initialized." -ForegroundColor Green
}
else {
    Write-Host "      WARNING: $initResult" -ForegroundColor Red
}

if ($usePostgres) { $dbMode = "PostgreSQL (Docker)" } else { $dbMode = "SQLite (local)" }
Write-Host ""
Write-Host "[7/7] Starting C-Call IVR - Database: $dbMode" -ForegroundColor Cyan
Write-Host "  Open: http://127.0.0.1:5000" -ForegroundColor Green
Write-Host ""

& $py app.py
