param(
    [ValidateSet('check','run','test')][string]$Command = 'check',
    [string]$InputFile,
    [string]$OutputDirectory,
    [switch]$Live,
    [int]$MaxCalls = 100
)
$ErrorActionPreference = 'Stop'
$env:PYTHONIOENCODING = 'utf-8'
Push-Location -LiteralPath $PSScriptRoot
try {
    if ($Command -eq 'test') {
        python -m unittest discover -s tests -v
    } elseif ($Command -eq 'check') {
        python -m english_difficulty.cli check
    } else {
        $pipelineArgs = @('-m','english_difficulty.cli','run','--input',$InputFile,'--max-calls',"$MaxCalls")
        if ($OutputDirectory) { $pipelineArgs += @('--output',$OutputDirectory) }
        if ($Live) { $pipelineArgs += '--live' }
        python @pipelineArgs
    }
    if ($LASTEXITCODE -ne 0) { throw "Pipeline exited with code $LASTEXITCODE" }
} finally {
    Pop-Location
}
