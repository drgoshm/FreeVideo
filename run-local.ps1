param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
. (Join-Path $PSScriptRoot 'env-local.ps1')
if ($Arguments.Count -gt 0 -and $Arguments[0] -in @('encode', 'generate')) {
    if ($Arguments -notcontains '--model-paths') {
        $Arguments += @('--model-paths', (Join-Path $PSScriptRoot 'encoder-paths.yaml'))
    }
    if ($Arguments[0] -eq 'generate' -and $Arguments -notcontains '--cache') {
        $Arguments += @('--cache', 'E:\freeVideo\FreeVideo-engine\prepared\edge-5e6ecc39f472f98c\cache')
    }
}
& (Join-Path $PSScriptRoot '.venv\Scripts\python.exe') -c 'from freevideo_engine.cli import main; main()' @Arguments
exit $LASTEXITCODE
