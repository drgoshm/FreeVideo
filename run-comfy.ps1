param([int]$Port = 8188, [string]$ListenAddress = '0.0.0.0', [switch]$OpenBrowser)

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
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') (Join-Path $PSScriptRoot 'vendor\ComfyUI\main.py') --listen $ListenAddress --port $Port --extra-model-paths-config (Join-Path $PSScriptRoot 'encoder-paths.yaml') $browserOption
exit $LASTEXITCODE
