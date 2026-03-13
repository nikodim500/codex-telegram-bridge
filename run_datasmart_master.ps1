param(
    [string]$Profile = "default",
    [string]$BotToken,
    [switch]$PersistToken,
    [switch]$NoPrompt
)

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$profilePath = Join-Path $root "profiles\$Profile.json"
if (-not (Test-Path -LiteralPath $profilePath)) {
    throw "[bridge] Profile not found: $profilePath"
}

$profile = Get-Content -LiteralPath $profilePath -Raw | ConvertFrom-Json
$tokenEnvName = if ([string]::IsNullOrWhiteSpace([string]$profile.telegram_bot_token_env)) {
    "TELEGRAM_BOT_TOKEN"
} else {
    [string]$profile.telegram_bot_token_env
}

function Set-BridgeTokenInSession {
    param([Parameter(Mandatory = $true)][string]$Value)
    Set-Item -Path "Env:$tokenEnvName" -Value $Value
}

function Persist-BridgeTokenForUser {
    param([Parameter(Mandatory = $true)][string]$Value)
    [System.Environment]::SetEnvironmentVariable($tokenEnvName, $Value, "User")
    Write-Host "[bridge] Token saved to user env var $tokenEnvName"
}

$existingToken = [System.Environment]::GetEnvironmentVariable($tokenEnvName)

if (-not [string]::IsNullOrWhiteSpace($BotToken)) {
    Set-BridgeTokenInSession -Value $BotToken
    if ($PersistToken) {
        Persist-BridgeTokenForUser -Value $BotToken
    } else {
        Write-Host "[bridge] Token set for current PowerShell session only"
    }
} elseif ([string]::IsNullOrWhiteSpace($existingToken)) {
    if ($NoPrompt) {
        throw "[bridge] $tokenEnvName is not set. Pass -BotToken or run without -NoPrompt."
    }

    $secureToken = Read-Host "Enter Telegram bot token (input hidden)" -AsSecureString
    $tokenPtr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureToken)
    try {
        $plainToken = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($tokenPtr)
    } finally {
        [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($tokenPtr)
    }

    if ([string]::IsNullOrWhiteSpace($plainToken)) {
        throw "[bridge] Empty token provided."
    }

    Set-BridgeTokenInSession -Value $plainToken
    $persistAnswer = Read-Host "Persist token to user env for future launches? (y/N)"
    if ($persistAnswer -match "^(y|yes)$") {
        Persist-BridgeTokenForUser -Value $plainToken
    } else {
        Write-Host "[bridge] Token set for current PowerShell session only"
    }
}

$projectPathRaw = if ([string]::IsNullOrWhiteSpace([string]$profile.project_path)) { "." } else { [string]$profile.project_path }
$projectPath = if ([System.IO.Path]::IsPathRooted($projectPathRaw)) {
    $projectPathRaw
} else {
    Join-Path $root $projectPathRaw
}

Write-Host "[bridge] profile=$Profile"
Write-Host "[bridge] project=$projectPath"

python .\bridge_native.py --profile $Profile
