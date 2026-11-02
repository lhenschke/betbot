# BetBot installer for a fresh Windows PC. Open PowerShell (Start menu, type "powershell") and paste:
#
#   irm https://raw.githubusercontent.com/lhenschke/betbot/claude/eager-hypatia-jf6af6/install.ps1 | iex
#
# It installs what's missing (Python, Git, Google Chrome), downloads BetBot to your user folder
# (C:\Users\<you>\BetBot), asks a few setup questions, adds a "BetBot" shortcut to the desktop and
# starts it. Running it again on a PC that already has BetBot just updates it.
#
# Options, set before pasting the line above:
#   $env:BETBOT_DIR    = "D:\BetBot"     # install somewhere else
#   $env:BETBOT_BRANCH = "some-branch"   # the GitHub branch this PC runs and updates from

# Each step's result is checked by hand: with "Stop", Windows PowerShell 5.1 treats Git's and pip's
# progress messages (written to stderr) as errors.
$ErrorActionPreference = "Continue"
$Repo   = "https://github.com/lhenschke/betbot.git"
$Branch = if ($env:BETBOT_BRANCH) { $env:BETBOT_BRANCH } else { "claude/eager-hypatia-jf6af6" }
$Dir    = if ($env:BETBOT_DIR) { $env:BETBOT_DIR } else { Join-Path $HOME "BetBot" }

function Say($msg) { Write-Host "`n== $msg" -ForegroundColor Cyan }
function Fail($msg) {
    Write-Host "`n$msg" -ForegroundColor Red
    Write-Host "Setup stopped here. Fix that, then paste the install line again: it picks up where it left off."
    throw "BetBot install stopped"
}

function Update-PathFromSystem {
    # Programs installed a moment ago add themselves to PATH for new windows only; pick that up here.
    $env:Path = [Environment]::GetEnvironmentVariable("Path", "Machine") + ";" +
                [Environment]::GetEnvironmentVariable("Path", "User")
}

function Install-WithWinget($id, $name, [string]$scope = "") {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
        Fail ("$name is needed, and this PC can't install it by itself (no 'winget'). Install $name by hand, " +
              "or update 'App Installer' from the Microsoft Store, which adds winget.")
    }
    Say "Installing $name (a Windows prompt may ask for permission: click Yes)..."
    $wingetArgs = @("install", "--id", $id, "-e", "--silent",
                    "--accept-source-agreements", "--accept-package-agreements")
    if ($scope) { $wingetArgs += @("--scope", $scope) }
    & winget @wingetArgs
    Update-PathFromSystem
}

function Find-Python {
    # A real Python 3.12 or newer. "python" on a fresh PC is often only a shortcut to the Microsoft
    # Store (in WindowsApps), which isn't a usable Python, so that one is skipped.
    $candidates = @()
    if (Get-Command py -ErrorAction SilentlyContinue) {
        try { $candidates += (& py -3 -c "import sys; print(sys.executable)" 2>$null) } catch { }
    }
    $candidates += Get-ChildItem -ErrorAction SilentlyContinue `
        "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe", "$env:ProgramFiles\Python3*\python.exe" |
        Sort-Object FullName -Descending | ForEach-Object { $_.FullName }
    $cmd = Get-Command python -ErrorAction SilentlyContinue
    if ($cmd -and $cmd.Source -notlike "*WindowsApps*") { $candidates += $cmd.Source }
    foreach ($p in $candidates) {
        if ($p -and (Test-Path $p)) {
            & $p -c "import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)" 2>$null
            if ($LASTEXITCODE -eq 0) { return $p }
        }
    }
    return $null
}

function Find-Chrome {
    foreach ($p in @("$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
                     "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe",
                     "$env:LOCALAPPDATA\Google\Chrome\Application\chrome.exe")) {
        if ($p -and (Test-Path $p)) { return $p }
    }
    return $null
}

Write-Host "BetBot installer: installs to $Dir, from branch $Branch" -ForegroundColor Green

# 1. What BetBot needs -------------------------------------------------------------------
$python = Find-Python
if (-not $python) {
    Install-WithWinget "Python.Python.3.12" "Python 3.12" "user"
    $python = Find-Python
    if (-not $python) { Fail "Python was installed but can't be found yet. Close PowerShell, open a new one and paste the line again." }
}
Write-Host "Python: $python"

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Install-WithWinget "Git.Git" "Git"
    if (-not (Get-Command git -ErrorAction SilentlyContinue) -and (Test-Path "$env:ProgramFiles\Git\cmd\git.exe")) {
        $env:Path += ";$env:ProgramFiles\Git\cmd"
    }
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { Fail "Git didn't install. Install it from https://git-scm.com/download/win" }
}
Write-Host "Git: $((Get-Command git).Source)"

if (-not (Find-Chrome)) {
    Install-WithWinget "Google.Chrome" "Google Chrome"
    if (-not (Find-Chrome)) { Fail "Google Chrome didn't install. Install it from https://www.google.com/chrome" }
}
Write-Host "Chrome: $(Find-Chrome)"

# 2. BetBot itself -------------------------------------------------------------------------
if (Test-Path (Join-Path $Dir ".git")) {
    Say "BetBot is already in ${Dir}: updating it..."
    & git -C $Dir pull --ff-only
    if ($LASTEXITCODE -ne 0) { Write-Host "Couldn't update; carrying on with the version that's there." -ForegroundColor Yellow }
} elseif ((Test-Path $Dir) -and (Get-ChildItem $Dir -Force | Select-Object -First 1)) {
    Fail "$Dir already exists and isn't a BetBot download. Move or rename it, or set `$env:BETBOT_DIR to another folder."
} else {
    Say "Downloading BetBot to $Dir..."
    & git clone -b $Branch $Repo $Dir
    if ($LASTEXITCODE -ne 0) { Fail "Downloading from GitHub failed (see the message above)." }
}

$venvPython = Join-Path $Dir ".venv\Scripts\python.exe"
if (-not (Test-Path $venvPython)) {
    Say "Setting up BetBot's own Python environment..."
    & $python -m venv (Join-Path $Dir ".venv")
    if ($LASTEXITCODE -ne 0) { Fail "Creating the Python environment failed." }
}
Say "Installing BetBot's packages (a minute or two the first time)..."
& $venvPython -m pip install -q --upgrade pip
& $venvPython -m pip install -q -e $Dir
if ($LASTEXITCODE -ne 0) { Fail "Installing packages failed (see the messages above)." }

# 3. Settings and logins ---------------------------------------------------------------------
Push-Location $Dir
try {
    if (-not (Test-Path (Join-Path $Dir "config.yaml"))) {
        Say "A few questions to set this PC up"
        & $venvPython -m betbot setup
    } else {
        Write-Host "Keeping this PC's settings (config.yaml). To answer the questions again, double-click setup.bat."
    }
} finally { Pop-Location }

# 4. Desktop shortcut ------------------------------------------------------------------------
$desktop = [Environment]::GetFolderPath("Desktop")
$shell = New-Object -ComObject WScript.Shell
$link = $shell.CreateShortcut((Join-Path $desktop "BetBot.lnk"))
$link.TargetPath = Join-Path $Dir "run.bat"
$link.WorkingDirectory = $Dir
$link.Description = "Start BetBot"
$link.Save()
Write-Host "Added a 'BetBot' shortcut to the desktop."

Say "Done."
$answer = Read-Host "Start BetBot now? (Y/n)"
if ($answer -notmatch '^[nN]') {
    Start-Process -FilePath (Join-Path $Dir "run.bat") -WorkingDirectory $Dir
    Write-Host "Starting: Chrome opens on the sportsbook and the dashboard at http://127.0.0.1:8765"
} else {
    Write-Host "Start it any time with the BetBot shortcut on the desktop."
}
