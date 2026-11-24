# dwo-Forge entry point, kept so what dwo-Forge wrote keeps working: __dwo-forge.bat launchers in connected
# projects, DwoLensInspector plugins built before the rename, DwoBuilder git hooks and old shortcuts. dwookit runs
# from tools\dwookit.ps1; this file only forwards to it. The first command run through it migrates the project
# (__dwo-forge -> __dwookit, launcher, git exclude block, agent pointer files, git hooks).
param(
    [Parameter(Mandatory = $true)][string]$Command,
    [Parameter(Mandatory = $true)][string]$Project,
    [Parameter(ValueFromRemainingArguments = $true)][string[]]$Rest
)
$kit = Join-Path $PSScriptRoot 'dwookit.ps1'
if ($Rest) { & $kit -Command $Command -Project $Project @Rest } else { & $kit -Command $Command -Project $Project }
exit $LASTEXITCODE
