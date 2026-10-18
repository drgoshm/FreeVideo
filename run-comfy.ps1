param([int]$Port = 8188, [string]$ListenAddress = '127.0.0.1', [switch]$OpenBrowser)

$ErrorActionPreference = 'Stop'
$lanPath = Join-Path $PSScriptRoot 'lan-access\config.json'
$lan = if (Test-Path -LiteralPath $lanPath) { Get-Content -LiteralPath $lanPath -Raw | ConvertFrom-Json } else { $null }
if ($lan.enabled -and $ListenAddress -ne '127.0.0.1') {
    throw 'With LAN authentication enabled, ComfyUI must listen on 127.0.0.1. Use the HTTPS gateway for network access.'
}

if ($OpenBrowser) {
    $url = "http://127.0.0.1:$Port/"
    $stats = $null
    try {
        $stats = Invoke-RestMethod -Uri "${url}system_stats" -TimeoutSec 3 -ErrorAction Stop
    } catch {
        # No ready ComfyUI instance; start one below.
    }
    if ($stats.system.comfyui_version) {
        Write-Host "ComfyUI is already running. Opening $url"
        Start-Process $url -ErrorAction Stop
        exit 0
    }
}

. (Join-Path $PSScriptRoot 'env-local.ps1')
$browserOption = if ($OpenBrowser) { '--auto-launch' } else { '--disable-auto-launch' }
$python = Join-Path $PSScriptRoot '.venv\Scripts\python.exe'
if ($lan.enabled) {
    $extra = if ($OpenBrowser) { @('--open-browser') } else { @() }
    & $python -m freevideo_engine.lan_server --root $PSScriptRoot --port $Port @extra
} else {
    & $python (Join-Path $PSScriptRoot 'vendor\ComfyUI\main.py') --listen $ListenAddress --port $Port --extra-model-paths-config (Join-Path $PSScriptRoot 'encoder-paths.yaml') $browserOption
}
exit $LASTEXITCODE
