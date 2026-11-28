# dwookit's own QA: builds the test Blueprints in tests/fixtures/native/lab into the QA project
# (.qa/selftest/project), through the QA-only DwoQA plugin (tests/qa-plugin/DwoQA). dwookit never installs DwoQA
# into user projects.
#   powershell -File tests\qa-lab.ps1 [-Project <dir>] [-Engine <UE root>] [-Spec <name.build.json> ...]
param(
    [string]$Project = (Join-Path $PSScriptRoot '..\.qa\selftest\project'),
    [string]$Engine = 'C:\Program Files\Epic Games\UE_5.7',
    [string[]]$Spec = @()
)
$ErrorActionPreference = 'Stop'
$Project = (Resolve-Path $Project).Path
$uproject = Get-ChildItem $Project -Filter *.uproject | Select-Object -First 1
$src = Join-Path $PSScriptRoot 'qa-plugin\DwoQA'
$dest = Join-Path $Project 'Plugins\__DwoQA'
$work = Join-Path $env:TEMP 'dwookit-qa-plugin'

# build once per source change (BuildPlugin into a staging folder, then binaries + descriptor into the project)
$stamp = (Get-ChildItem -Recurse -File $src | Sort-Object FullName | ForEach-Object { (Get-FileHash $_.FullName).Hash }) -join ''
$hash = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($stamp))).Replace('-', '').Substring(0, 12)
if (-not (Test-Path (Join-Path $dest ".built-$hash"))) {
    Write-Host "Building DwoQA ($hash)..."
    if (Test-Path $work) { Remove-Item -Recurse -Force $work }
    Copy-Item -Recurse $src (Join-Path $work 'src')
    $uat = Join-Path $Engine 'Engine\Build\BatchFiles\RunUAT.bat'
    & cmd /c "`"$uat`" BuildPlugin -Plugin=`"$work\src\DwoQA.uplugin`" -Package=`"$work\out`" -HostPlatforms=Win64 -TargetPlatforms=Win64 > `"$work\build.log`" 2>&1"
    if (-not (Test-Path "$work\out\Binaries\Win64\UnrealEditor-DwoQA.dll")) { throw "DwoQA build failed: $work\build.log" }
    if (Test-Path $dest) { Remove-Item -Recurse -Force $dest }
    New-Item -ItemType Directory -Force $dest | Out-Null
    Copy-Item "$work\out\DwoQA.uplugin" $dest
    # BuildPlugin drops "EnabledByDefault": false; without it the project's build would look for DwoQA's source
    $up = Join-Path $dest 'DwoQA.uplugin'
    $j = Get-Content -Raw -Encoding UTF8 $up | ConvertFrom-Json
    $j | Add-Member -NotePropertyName EnabledByDefault -NotePropertyValue $false -Force
    [IO.File]::WriteAllText($up, ($j | ConvertTo-Json -Depth 10), (New-Object Text.UTF8Encoding $false))
    Copy-Item -Recurse "$work\out\Binaries" $dest
    New-Item -ItemType File (Join-Path $dest ".built-$hash") | Out-Null
}

$specs = if ($Spec.Count) { $Spec | ForEach-Object { if (Test-Path $_) { (Resolve-Path $_).Path } else { Join-Path $PSScriptRoot "fixtures\native\lab\$_" } } } else { Get-ChildItem (Join-Path $PSScriptRoot 'fixtures\native\lab') -Filter *.build.json | ForEach-Object FullName }
$cmd = Join-Path $Engine 'Engine\Binaries\Win64\UnrealEditor-Cmd.exe'
$failed = 0
foreach ($s in $specs) {
    $name = [IO.Path]::GetFileNameWithoutExtension([IO.Path]::GetFileNameWithoutExtension($s))
    $path = (Get-Content -Raw -Encoding UTF8 $s | ConvertFrom-Json).path
    $asset = Join-Path $Project ('Content' + $path.Substring(5).Replace('/', '\') + '.uasset')
    if (Test-Path $asset) { Remove-Item -Force $asset }
    $log = Join-Path $work "lab-$name.log"
    & cmd /c "`"$cmd`" `"$($uproject.FullName)`" -run=DwoQA -EnablePlugins=DwoQA -QaBuildBlueprint=`"$s`" -unattended -nullrhi -nosplash -nopause -NoSound -stdout > `"$log`" 2>&1"
    $lines = Select-String -Path $log -Pattern 'LogDwoBuild: ' | Where-Object { $_.Line -notmatch 'LogInit' } | ForEach-Object { $_.Line -replace '^\[[^\]]*\]\[[^\]]*\]', '' }
    if ($LASTEXITCODE -ne 0) { $failed++; Write-Host "FAILED $name"; $lines | ForEach-Object { "  $_" } } else { Write-Host "built  $name" }
}
exit $failed
