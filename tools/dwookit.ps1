# dwookit control script. Runs from the dwookit install folder (a git clone or an extracted zip);
# invoked by dwookit.bat, the GUI, or the project's __dwookit.bat launcher.
# Windows PowerShell 5.1 compatible. Never modifies tracked project files.
#
# dwookit is a launcher: 'connect' prepares a project, then tools are installed, updated and removed one by one
# ('tool install|update|remove <id>', 'tools' lists versions and updates):
#   lens          DwoLens: analysis lines (analyze, verify, logs, smoke, perf, sizes, trace) + DwoLensInspector commandlet
#   statemonitor  DwoStateMonitor: live runtime state viewer plugin (also sm-install, sm-remove)
#   canvas        DwoCanvas: design file -> UMG Widget Blueprint importer (also canvas-install, canvas-remove, canvas-import)
#   ci            DwoBuilder: compile and package (run here, need nothing installed) + Jenkins job and git hooks for
#                 automatic builds (tools\ci.ps1; the Jenkins itself is PC-wide)
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('connect', 'tool', 'attach', 'update', 'status', 'analyze', 'verify', 'logs', 'smoke', 'perf', 'sizes', 'trace', 'play', 'tools', 'gui',
                 'sm-install', 'sm-remove', 'canvas-install', 'canvas-remove', 'canvas-import', 'canvas-validate',
                 'doctor', 'uninstall', 'detach', 'compile', 'package', 'bp', 'agent', 'live', 'live-ingest', 'library', 'native',
                 'ci-job', 'ci-trigger', 'ci-hook', 'ci-hooks', 'ci-status', 'ci-install', 'ci-start', 'ci-stop', 'ci-remove', 'ci-bundle')]
    [string]$Command,
    [Parameter(Mandatory = $true)]
    [string]$Project,
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$Rest
)

$ErrorActionPreference = 'Stop'
$Utf8 = New-Object System.Text.UTF8Encoding($false)

$SystemDir   = Split-Path -Parent $PSScriptRoot
$Project     = (Resolve-Path $Project).Path.TrimEnd('\')
$ForgeDir    = Join-Path $Project '__dwookit'
$DataDir     = Join-Path $ForgeDir 'data'
$StatePath   = Join-Path $ForgeDir 'state.json'
$PluginDest  = Join-Path $Project 'Plugins\__DwoLensInspector'
$Marker      = '.dwookit-managed'
$Markers     = @('.dwookit-managed', '.dwo-forge-managed', '.dwo-lens-managed')   # dwo-Forge 2.x and dwo-Lens 1.x wrote the older ones
$BlockEnd    = '# <<< dwookit managed <<<'
$SmDest      = Join-Path $Project 'Plugins\__DwoStateMonitor'
$SmLegacyDest = Join-Path $Project 'Plugins\__StateMonitor'      # dwo-Lens 1.x install location
$CanvasDest  = Join-Path $Project 'Plugins\__DwoCanvas'
$CanvasOldDest = Join-Path $Project 'Plugins\DwoCanvas'           # 2.0.0 installed it here, visible to git
$CanvasOldMarker = '.dwo-forge-installed'                      # ... with this marker
# Legacy names stay listed so leftovers of a migration in progress never show up in git.
# CLAUDE.local.md and the Copilot instructions files point coding agents at __dwookit\context\AGENT.md and the
# user's own project.instructions.md (tools\lens_agent.py). A file committed to git is never changed or removed.
$ManagedPaths = @('__dwookit/', '__dwookit.bat', 'Plugins/__DwoLensInspector/', 'Plugins/__DwoStateMonitor/',
                  'Plugins/__DwoCanvas/', '__dwo-forge/', '__dwo-forge.bat', '__dwo-lens/', '__dwo-lens.bat', 'Plugins/__StateMonitor/',
                  '.github/instructions/dwo-forge.instructions.md',
                  'CLAUDE.local.md', '.github/instructions/dwookit.instructions.md',
                  '.github/instructions/project.instructions.md')
$LauncherPath = Join-Path $Project '__dwookit.bat'
$LedgerPath  = Join-Path $ForgeDir 'created-files.txt'   # files dwookit caused outside __dwookit (removed by uninstall)
$AppDataDir  = Join-Path $env:LOCALAPPDATA 'dwookit'
# Earlier names, migrated on the first command run against the project: dwo-Forge 2.x, then dwo-Lens 1.x.
# FromBat: that version's launcher is the one running this command (it cannot be deleted while cmd reads it).
$LegacyGens = @(
    [pscustomobject]@{ Name = 'dwo-Forge'; Block = 'dwo-forge'; Dir = (Join-Path $Project '__dwo-forge')
                       Launcher = (Join-Path $Project '__dwo-forge.bat'); AppData = (Join-Path $env:LOCALAPPDATA 'dwo-Forge')
                       FromBat = ($env:DWO_FORGE_FROM_BAT -eq '1') -and ($env:DWOOKIT_FROM_BAT -ne '1') }
    [pscustomobject]@{ Name = 'dwo-Lens 1.x'; Block = 'dwo-lens'; Dir = (Join-Path $Project '__dwo-lens')
                       Launcher = (Join-Path $Project '__dwo-lens.bat'); AppData = (Join-Path $env:LOCALAPPDATA 'dwo-Lens')
                       FromBat = ($env:DWO_LENS_FROM_BAT -eq '1') -and ($env:DWO_FORGE_FROM_BAT -ne '1') -and ($env:DWOOKIT_FROM_BAT -ne '1') }
)
$BlockNames = '(dwookit|dwo-forge|dwo-lens)'

function Say([string]$Message) { Write-Host "[dwookit] $Message" }
function Fail([string]$Message) { Write-Host "[dwookit] ERROR: $Message" -ForegroundColor Red; exit 1 }

function Write-Json($Path, $Object) {
    $dir = Split-Path -Parent $Path
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    [IO.File]::WriteAllText($Path, ($Object | ConvertTo-Json -Depth 10), $Utf8)
}

function Read-State {
    if (Test-Path $StatePath) { return Get-Content -Raw -Encoding UTF8 $StatePath | ConvertFrom-Json }
    return $null
}

# ---------------------------------------------------------------- project / engine
function Get-UProject {
    $files = @(Get-ChildItem -Path $Project -Filter *.uproject -File)
    if ($files.Count -ne 1) { Fail "Expected exactly one .uproject in $Project, found $($files.Count)." }
    $json = Get-Content -Raw -Encoding UTF8 $files[0].FullName | ConvertFrom-Json
    return [pscustomobject]@{ Path = $files[0].FullName; Name = $files[0].BaseName; Json = $json;
                              IsCode = (Test-Path (Join-Path $Project 'Source')) }
}

function Resolve-Engine([string]$Association) {
    $e = Find-Engine $Association
    if (-not $e) { Fail "Cannot find the engine for EngineAssociation '$Association'." }
    return $e
}

function Get-InstalledEngines {
    # Every engine this PC knows: launcher installs and source builds registered by UnrealVersionSelector.
    $list = @()
    $dat = Join-Path $env:ProgramData 'Epic\UnrealEngineLauncher\LauncherInstalled.dat'
    if (Test-Path $dat) {
        foreach ($i in @((Get-Content -Raw $dat | ConvertFrom-Json).InstallationList | Where-Object { $_.AppName -like 'UE_*' })) {
            $list += [ordered]@{ association = $i.AppName.Substring(3); root = $i.InstallLocation }
        }
    }
    $key = 'HKCU:\Software\Epic Games\Unreal Engine\Builds'
    if (Test-Path $key) {
        foreach ($p in (Get-ItemProperty $key).PSObject.Properties | Where-Object { $_.Name -match '^\{.+\}$' }) {
            $list += [ordered]@{ association = $p.Name; root = "$($p.Value)" }
        }
    }
    foreach ($e in $list) {
        $f = Join-Path $e.root 'Engine\Build\Build.version'
        $e['version'] = $(if (Test-Path $f) { $v = Get-Content -Raw $f | ConvertFrom-Json; "$($v.MajorVersion).$($v.MinorVersion).$($v.PatchVersion)" } else { '' })
    }
    return @($list | Where-Object { $_.version })
}

function Find-Engine([string]$Association) {
    # Like Resolve-Engine, but $null instead of stopping (status and tool listings must work without an engine).
    $root = $null
    if ($Association -match '^\{.+\}$') {
        $key = 'HKCU:\Software\Epic Games\Unreal Engine\Builds'
        if (Test-Path $key) { $root = (Get-ItemProperty $key).$Association }
    } else {
        $dat = Join-Path $env:ProgramData 'Epic\UnrealEngineLauncher\LauncherInstalled.dat'
        if (Test-Path $dat) {
            $entry = (Get-Content -Raw $dat | ConvertFrom-Json).InstallationList |
                     Where-Object { $_.AppName -eq "UE_$Association" } | Select-Object -First 1
            if ($entry) { $root = $entry.InstallLocation }
        }
        if (-not $root) {
            $key = "HKLM:\SOFTWARE\EpicGames\Unreal Engine\$Association"
            if (Test-Path $key) { $root = (Get-ItemProperty $key).InstalledDirectory }
        }
    }
    if (-not $root -or -not (Test-Path $root)) { return $null }
    $root = (Resolve-Path $root).Path
    $versionFile = Join-Path $root 'Engine\Build\Build.version'
    if (-not (Test-Path $versionFile)) { return $null }
    $v = Get-Content -Raw $versionFile | ConvertFrom-Json
    return [pscustomobject]@{
        Root    = $root
        Version = "$($v.MajorVersion).$($v.MinorVersion).$($v.PatchVersion)"
        Cmd     = Join-Path $root 'Engine\Binaries\Win64\UnrealEditor-Cmd.exe'
        RunUAT  = Join-Path $root 'Engine\Build\BatchFiles\RunUAT.bat'
    }
}

# ---------------------------------------------------------------- git
function Invoke-Utf8([scriptblock]$Block) {
    # git and Python (PYTHONUTF8) print UTF-8, but Windows PowerShell decodes captured native output with the
    # console code page (949 on Korean Windows), which garbles non-ASCII paths. Decode as UTF-8 for this call only.
    $prev = [Console]::OutputEncoding
    try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false) } catch { }
    try { & $Block } finally { try { [Console]::OutputEncoding = $prev } catch { } }
}
function Invoke-Git([string[]]$GitArgs) {
    # git is optional: a project without version control (or a PC without git) simply has no repository.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $null }
    $ErrorActionPreference = 'Continue'
    $out = Invoke-Utf8 { & git -C $Project @GitArgs 2>$null }
    if ($LASTEXITCODE -ne 0) { return $null }
    return $out
}

# How the project is version controlled:
#   own    - the project folder is a git repository root
#   parent - the project sits inside a larger git repository (paths are prefixed in that repository's exclude)
#   none   - no git; nothing to hide, so no exclude is written
function Get-Vcs {
    $top = Invoke-Git @('rev-parse', '--show-toplevel')
    if (-not $top) { return [pscustomobject]@{ Kind = 'none'; Top = $null; Prefix = ''; Exclude = $null } }
    # Prefix comparison stays correct through junctions, symlinks and redirected profile paths.
    $prefix = ((Invoke-Utf8 { & git -C $Project rev-parse --show-prefix 2>$null }) | Out-String).Trim()
    $excl = Invoke-Git @('rev-parse', '--path-format=absolute', '--git-path', 'info/exclude')
    return [pscustomobject]@{ Kind = $(if ($prefix) { 'parent' } else { 'own' }); Top = "$top"; Prefix = $prefix
                              Exclude = $(if ($excl) { ("$excl" -replace '/', '\') } else { $null }) }
}

function Get-ExcludeLines([string]$Path) {
    if (-not (Test-Path $Path)) { return @() }
    return @([IO.File]::ReadAllLines($Path))
}

# One block per project, so several projects inside one parent repository keep separate entries.
function Get-BlockId($Vcs) { if ($Vcs.Prefix) { return $Vcs.Prefix } else { return '.' } }

function Set-ExcludeBlock {
    $vcs = Get-Vcs
    if ($vcs.Kind -eq 'none' -or -not $vcs.Exclude) { return $vcs }
    $id = Get-BlockId $vcs
    $lines = Get-ExcludeLines $vcs.Exclude
    $kept = Remove-Block $lines $id
    $block = @("# >>> dwookit managed: $id (do not edit) >>>") + @(@($ManagedPaths) + @(Get-InstalledPaths) | ForEach-Object { '/' + $vcs.Prefix + $_ }) + @($BlockEnd)
    $all = @($kept | Where-Object { $_ -ne $null }) + @('') + $block
    $dir = Split-Path -Parent $vcs.Exclude
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force $dir | Out-Null }
    [IO.File]::WriteAllText($vcs.Exclude, (($all -join "`n").Trim() + "`n"), $Utf8)
    return $vcs
}

# Removes this project's block, whether written by dwookit or an earlier name ('# >>> dwo-forge managed: <id> ...',
# '# >>> dwo-lens managed: <id> ...').
# The unnamed block of the oldest versions belongs to the repository root project.
function Remove-Block([string[]]$Lines, [string]$Id) {
    $out = New-Object System.Collections.Generic.List[string]
    $inside = $false
    foreach ($line in $Lines) {
        if ($line -match "^# >>> $BlockNames managed") {
            $legacy = $line -notmatch "^# >>> $BlockNames managed: "
            $mine = ($legacy -and $Id -eq '.') -or $line -match ("^# >>> $BlockNames managed: " + [regex]::Escape($Id) + ' ')
            if ($mine) { $inside = $true; continue }
        }
        if ($inside) {
            if ($line -match "^# <<< $BlockNames managed") { $inside = $false }
            continue
        }
        $out.Add($line)
    }
    while ($out.Count -gt 0 -and [string]::IsNullOrWhiteSpace($out[$out.Count - 1])) { $out.RemoveAt($out.Count - 1) }
    return ,$out.ToArray()
}

function Clear-ExcludeBlock {
    $vcs = Get-Vcs
    if ($vcs.Kind -eq 'none' -or -not $vcs.Exclude -or -not (Test-Path $vcs.Exclude)) { return $false }
    $lines = Get-ExcludeLines $vcs.Exclude
    $kept = Remove-Block $lines (Get-BlockId $vcs)
    if ($kept.Count -eq ($lines | Where-Object { $_ -ne $null }).Count) { return $false }
    $text = ''
    if ($kept.Count -gt 0) { $text = ($kept -join "`n") + "`n" }
    [IO.File]::WriteAllText($vcs.Exclude, $text, $Utf8)
    return $true
}

function Test-BlockPresent([switch]$Legacy) {
    # Default: the current dwookit block. -Legacy: a block written by an earlier name (dwo-Forge, dwo-Lens 1.x).
    $vcs = Get-Vcs
    if ($vcs.Kind -eq 'none' -or -not $vcs.Exclude -or -not (Test-Path $vcs.Exclude)) { return $false }
    $id = Get-BlockId $vcs
    $text = Get-Content -Raw $vcs.Exclude
    if (-not $text) { return $false }
    if ($Legacy) {
        foreach ($g in $LegacyGens) {
            if (($text -like "*# >>> $($g.Block) managed: $id *") -or ($id -eq '.' -and $text -like "*# >>> $($g.Block) managed (do not edit)*")) { return $true }
        }
        return $false
    }
    return ($text -like "*# >>> dwookit managed: $id *")
}

# Folders the engine itself rewrites whenever an editor or commandlet runs (ignored by the standard UE .gitignore).
$VolatileDirs = @('Saved/', 'Intermediate/', 'DerivedDataCache/', 'Binaries/', '.vs/')

function Get-ProjectStatus {
    # Porcelain status of the project folder, used to prove attach/uninstall leave no trace.
    # Engine work folders are excluded: running the editor always touches them.
    # Porcelain paths are relative to the repository root; strip the project prefix (parent repository case).
    $prefix = ((Invoke-Utf8 { & git -C $Project rev-parse --show-prefix 2>$null }) | Out-String).Trim()
    $s = Invoke-Git @('status', '--porcelain', '--untracked-files=all', '--', '.')
    if (-not $s) { return '' }
    $kept = @($s) | Where-Object {
        $path = $_.Substring(3).Trim('"')
        if ($prefix -and $path.StartsWith($prefix)) { $path = $path.Substring($prefix.Length) }
        -not ($VolatileDirs | Where-Object { $path.StartsWith($_) })
    }
    return (@($kept) -join "`n")
}

# ---------------------------------------------------------------- plugin builds (shared by every provided tool)
# Build products a developer may have next to the plugin source; never part of the hash or the build input.
$PluginBuildDirs = @('Binaries', 'Intermediate', 'Saved', '__pycache__')

function Get-PluginSourceFiles([string]$Src) {
    return Get-ChildItem -Recurse -File $Src | Where-Object {
        $rel = $_.FullName.Substring($Src.Length + 1)
        -not ($PluginBuildDirs | Where-Object { $rel -like "$_\*" -or $rel -like "*\$_\*" })
    }
}

function Get-PluginSourceHash([string]$Name = 'DwoLensInspector') {
    $src = Join-Path $SystemDir "plugin\$Name"
    $sha = [Security.Cryptography.SHA256]::Create()
    $sb = New-Object System.Text.StringBuilder
    Get-PluginSourceFiles $src | Sort-Object FullName | ForEach-Object {
        $rel = $_.FullName.Substring($src.Length)
        $h = (Get-FileHash -Algorithm SHA256 $_.FullName).Hash
        [void]$sb.Append("$rel=$h;")
    }
    $bytes = $sha.ComputeHash([Text.Encoding]::UTF8.GetBytes($sb.ToString()))
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 12)
}

# Builds plugin\<Name> with BuildPlugin once per engine version and source hash, cached under
# %LOCALAPPDATA%\dwookit\plugin-cache. $Module is the editor module whose DLL proves the build worked.
function Get-PluginBuild($Engine, [string]$Name = 'DwoLensInspector', [string]$Module = '') {
    if (-not $Module) { $Module = $Name }
    $hash = Get-PluginSourceHash $Name
    $cacheRoot = Join-Path $AppDataDir 'plugin-cache'
    $cache = Join-Path $cacheRoot "$($Engine.Version)-$Name-$hash"
    $dll = Join-Path $cache "Binaries\Win64\UnrealEditor-$Module.dll"
    if (Test-Path $dll) { return [pscustomobject]@{ Dir = $cache; Hash = $hash } }

    Say "Building $Name for UE $($Engine.Version) (first time for this engine/plugin version, takes a few minutes)..."
    $staging = Join-Path $cacheRoot "staging-$Name-$hash"
    if (Test-Path $staging) { Remove-Item -Recurse -Force $staging }
    $logDir = if (Test-Path $ForgeDir) { Join-Path $DataDir 'logs' } else { Join-Path $cacheRoot 'logs' }
    $log = Join-Path $logDir "build-$Name.log"
    New-Item -ItemType Directory -Force $logDir | Out-Null
    # Build from a copy: BuildPlugin writes files (Config/FilterPlugin.ini) into the plugin folder,
    # and the install folder must stay as shipped.
    $src = Join-Path $SystemDir "plugin\$Name"
    $srcCopy = Join-Path $cacheRoot "src-$Name-$hash"
    if (Test-Path $srcCopy) { Remove-Item -Recurse -Force $srcCopy }
    foreach ($f in (Get-PluginSourceFiles $src)) {
        $target = Join-Path $srcCopy $f.FullName.Substring($src.Length + 1)
        New-Item -ItemType Directory -Force (Split-Path -Parent $target) | Out-Null
        Copy-Item $f.FullName $target
    }
    $uplugin = Join-Path $srcCopy "$Name.uplugin"
    & cmd /c "`"$($Engine.RunUAT)`" BuildPlugin -Plugin=`"$uplugin`" -Package=`"$staging`" -HostPlatforms=Win64 -TargetPlatforms=Win64 > `"$log`" 2>&1"
    $code = $LASTEXITCODE
    # Another UnrealBuildTool (an editor, another build) can hold its shared rules assembly for a moment: try once more.
    if ($code -ne 0 -and (Test-Path $log) -and (Select-String -Path $log -Pattern 'being used by another process' -Quiet)) {
        Say "The build tools are busy with another build; trying again in 30 seconds..."
        Start-Sleep -Seconds 30
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
        & cmd /c "`"$($Engine.RunUAT)`" BuildPlugin -Plugin=`"$uplugin`" -Package=`"$staging`" -HostPlatforms=Win64 -TargetPlatforms=Win64 > `"$log`" 2>&1"
        $code = $LASTEXITCODE
    }
    Remove-Item -Recurse -Force $srcCopy -ErrorAction SilentlyContinue
    if ($code -ne 0 -or -not (Test-Path (Join-Path $staging "Binaries\Win64\UnrealEditor-$Module.dll"))) {
        if (Test-Path $log) { Select-String -Path $log -Pattern ': (fatal )?error ' | Select-Object -First 5 | ForEach-Object { Write-Host "    $($_.Line.Trim())" -ForegroundColor Red } }
        Remove-Item -Recurse -Force $staging -ErrorAction SilentlyContinue
        Fail "Plugin build failed. See $log"
    }
    if (Test-Path $cache) { Remove-Item -Recurse -Force $cache }
    Move-Item $staging $cache
    return [pscustomobject]@{ Dir = $cache; Hash = $hash }
}

function Copy-PluginExtras([string]$Name, [string]$Dest) {
    # BuildPlugin packages only Binaries, Source, Content, Resources and what FilterPlugin.ini lists. Everything else
    # the plugin ships next to them is needed at run time too: Config\Engine.ini carries [CoreRedirects],
    # DwoCanvas reads Schema\*.json while importing, and README/Samples belong with the plugin. Copy whatever the
    # build output lacks from the shipped source (never overwriting built files).
    $src = Join-Path $SystemDir "plugin\$Name"
    foreach ($item in (Get-ChildItem -Force $src)) {
        if ($PluginBuildDirs -contains $item.Name) { continue }
        $target = Join-Path $Dest $item.Name
        if (-not $item.PSIsContainer) { if (-not (Test-Path $target)) { Copy-Item $item.FullName $target }; continue }
        foreach ($f in (Get-PluginSourceFiles $item.FullName)) {
            $t = Join-Path $target $f.FullName.Substring($item.FullName.Length + 1)
            if (Test-Path $t) { continue }
            New-Item -ItemType Directory -Force (Split-Path -Parent $t) | Out-Null
            Copy-Item $f.FullName $t
        }
    }
}

function Read-SharedText([string]$Path) {
    # Reads a log that a lingering engine child process may still hold open for writing.
    $share = [IO.FileShare]([int][IO.FileShare]::ReadWrite -bor [int][IO.FileShare]::Delete)
    $fs = New-Object IO.FileStream -ArgumentList $Path, ([IO.FileMode]::Open), ([IO.FileAccess]::Read), $share
    try { $sr = New-Object IO.StreamReader($fs, [Text.Encoding]::UTF8); return $sr.ReadToEnd() } finally { $fs.Dispose() }
}

function Test-Managed([string]$Dir) {
    # A plugin folder dwookit (or dwo-Lens 1.x) installed: it carries one of the marker files.
    foreach ($m in $Markers) { if (Test-Path (Join-Path $Dir $m)) { return $true } }
    return $false
}

function Install-Plugin($Build) {
    Remove-Plugin
    New-Item -ItemType Directory -Force $PluginDest | Out-Null
    # Prebuilt binaries only: the project's own build never compiles this plugin.
    Copy-Item (Join-Path $Build.Dir 'DwoLensInspector.uplugin') $PluginDest
    # Disabled by default: the editor, cooks and packages never load or stage it. dwookit enables it only
    # for its own commandlet runs (-EnablePlugins=DwoLensInspector), so the project needs no .uproject entry.
    $upluginPath = Join-Path $PluginDest 'DwoLensInspector.uplugin'
    $uplugin = Get-Content -Raw -Encoding UTF8 $upluginPath | ConvertFrom-Json
    # Live Lens on: the editor loads it too (still no .uproject entry; this copy is git-excluded).
    $uplugin | Add-Member -NotePropertyName EnabledByDefault -NotePropertyValue (Test-LiveEnabled) -Force
    [IO.File]::WriteAllText($upluginPath, ($uplugin | ConvertTo-Json -Depth 10), $Utf8)
    Copy-Item -Recurse (Join-Path $Build.Dir 'Binaries') $PluginDest
    if (Test-Path (Join-Path $Build.Dir 'Resources')) { Copy-Item -Recurse (Join-Path $Build.Dir 'Resources') $PluginDest }
    [IO.File]::WriteAllText((Join-Path $PluginDest $Marker), "Managed by dwookit (DwoLens inspector). Removed by '__dwookit.bat uninstall'.`n", $Utf8)
}

function Remove-EmptyPluginsFolder {
    # Remove Plugins\ only when attach created it (it did not exist before) and nothing else is in it now.
    $plugins = Join-Path $Project 'Plugins'
    $state = Read-State
    $existed = $state -and $state.PSObject.Properties['pluginsFolderExisted'] -and [bool]$state.pluginsFolderExisted
    if (-not $existed -and (Test-Path $plugins) -and -not (Get-ChildItem -Force $plugins)) { Remove-Item -Force $plugins }
}

function Remove-Plugin {
    if (-not (Test-Path $PluginDest)) { return }
    if (-not (Test-Managed $PluginDest)) {
        Fail "$PluginDest exists but is not managed by dwookit. Leaving it untouched."
    }
    Remove-Folder $PluginDest 'Plugins\__DwoLensInspector'
    Remove-EmptyPluginsFolder
}

function Remove-Folder([string]$Path, [string]$What) {
    # Removes a folder; a file held open (an editor using a plugin DLL) stops the command with a clear message.
    try { Remove-Item -Recurse -Force $Path -ErrorAction Stop }
    catch { Fail "Cannot remove $What ($Path): $($_.Exception.Message) Close the Unreal Editor and try again." }
}

# ---------------------------------------------------------------- DwoStateMonitor (provided tool)
function Get-StateMonitorReferences {
    # Project code that includes or calls the state monitor. Removing the plugin would break those builds.
    $refs = @()
    foreach ($dir in @('Source', 'Plugins')) {
        $path = Join-Path $Project $dir
        if (-not (Test-Path $path)) { continue }
        $refs += Get-ChildItem -Path $path -Recurse -File -Include *.h, *.cpp, *.cs -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -notlike '*\Plugins\__*' } |
                 Select-String -Pattern 'StateMonitor' -List | ForEach-Object { $_.Path.Substring($Project.Length + 1) + ':' + $_.LineNumber }
    }
    return $refs
}

function Get-StaleStateMonitorDeps {
    # Places that still name the module/plugin 'StateMonitor' (dwo-Lens 1.x). With DwoStateMonitor they must say
    # 'DwoStateMonitor'; class names and headers did not change, so nothing else in the code needs editing.
    $hits = @()
    foreach ($dir in @('Source', 'Plugins')) {
        $path = Join-Path $Project $dir
        if (-not (Test-Path $path)) { continue }
        $hits += Get-ChildItem -Path $path -Recurse -File -Filter *.Build.cs -ErrorAction SilentlyContinue |
                 Where-Object { $_.FullName -notlike '*\Plugins\__*' -and $_.Name -ne 'StateMonitor.Build.cs' } |
                 Select-String -Pattern '"StateMonitor"' | ForEach-Object { $_.Path.Substring($Project.Length + 1) + ':' + $_.LineNumber + '  (Build.cs dependency)' }
    }
    $files = @(Get-ChildItem -Path $Project -Filter *.uproject -File)
    if ($files.Count -eq 1) {
        $json = Get-Content -Raw -Encoding UTF8 $files[0].FullName | ConvertFrom-Json
        if (@($json.Plugins | Where-Object { $_.Name -eq 'StateMonitor' }).Count -gt 0) { $hits += "$($files[0].Name)  (Plugins entry)" }
    }
    return $hits
}

function Write-StaleDepsWarning {
    # Returns $true when something still names the old module.
    if (Find-OwnStateMonitor) { return $false }   # the project ships its own plugin; its names are its business
    $stale = @(Get-StaleStateMonitorDeps)
    if ($stale.Count -eq 0) { return $false }
    Write-Host '[dwookit] WARNING: the plugin is now named DwoStateMonitor, but the project still refers to "StateMonitor":' -ForegroundColor Yellow
    $stale | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    Say '  Replace "StateMonitor" with "DwoStateMonitor" in those Build.cs dependency lists / .uproject Plugins entries.'
    Say '  Class names (UStateMonitorSubsystem, ...) and includes stay the same; saved Blueprints load through CoreRedirects.'
    return $true
}

function Find-OwnStateMonitor {
    # A state monitor that belongs to the project itself (not installed by dwookit/dwo-Lens).
    $plugins = Join-Path $Project 'Plugins'
    if (-not (Test-Path $plugins)) { return $null }
    return Get-ChildItem -Path $plugins -Recurse -Include 'DwoStateMonitor.uplugin', 'StateMonitor.uplugin' -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -notlike '*\Plugins\__*' } | Select-Object -First 1
}

function Get-InstalledStateMonitor {
    # The managed install: DwoStateMonitor (current) or __StateMonitor (dwo-Lens 1.x, migrated by update/sm-install).
    if (Test-Path $SmDest) { return [pscustomobject]@{ Dir = $SmDest; Legacy = $false; Uplugin = (Join-Path $SmDest 'DwoStateMonitor.uplugin') } }
    if (Test-Path $SmLegacyDest) { return [pscustomobject]@{ Dir = $SmLegacyDest; Legacy = $true; Uplugin = (Join-Path $SmLegacyDest 'StateMonitor.uplugin') } }
    return $null
}

function Test-RuntimeMode([string]$UpluginPath) {
    if (-not (Test-Path $UpluginPath)) { return $false }
    return -not ((Get-Content -Raw -Encoding UTF8 $UpluginPath) -match 'TargetAllowList')
}

function Install-StateMonitor($Engine, [bool]$Runtime) {
    $build = Get-PluginBuild $Engine 'DwoStateMonitor'
    if (Test-Path $SmDest) { Remove-Folder $SmDest 'Plugins\__DwoStateMonitor' }
    New-Item -ItemType Directory -Force $SmDest | Out-Null
    # Installed as a source plugin with prebuilt editor binaries: the editor opens without compiling, and
    # packaging (including Blueprint-only projects) builds a game target that contains DwoStateMonitor.
    Get-ChildItem $build.Dir | Where-Object { $_.Name -ne 'Intermediate' } | ForEach-Object { Copy-Item -Recurse $_.FullName $SmDest }
    Copy-PluginExtras 'DwoStateMonitor' $SmDest   # Config\Engine.ini: CoreRedirects /Script/StateMonitor -> /Script/DwoStateMonitor
    $upluginPath = Join-Path $SmDest 'DwoStateMonitor.uplugin'
    $uplugin = Get-Content -Raw -Encoding UTF8 $upluginPath | ConvertFrom-Json
    $uplugin | Add-Member -NotePropertyName EnabledByDefault -NotePropertyValue $true -Force
    $uplugin | Add-Member -NotePropertyName Installed -NotePropertyValue $false -Force
    if (-not $Runtime) {
        # Default: editor and PIE only. Packaged games never load the module, so installing it cannot
        # change or break a package. (A Blueprint-only package uses the stock UnrealGame executable,
        # which cannot contain a plugin the .uproject does not list.)
        foreach ($m in $uplugin.Modules) { $m | Add-Member -NotePropertyName TargetAllowList -NotePropertyValue @('Editor') -Force }
    }
    [IO.File]::WriteAllText($upluginPath, ($uplugin | ConvertTo-Json -Depth 10), $Utf8)
    [IO.File]::WriteAllText((Join-Path $SmDest $Marker), "DwoStateMonitor installed by dwookit. Remove with '__dwookit.bat sm-remove'.`n", $Utf8)
    Set-ToolInstalled 'statemonitor' @{ engine = $Engine.Version; mode = $(if ($Runtime) { 'runtime' } else { 'editor' }) }
}

function Move-LegacyStateMonitor($Engine) {
    # dwo-Lens 1.x installed the plugin as Plugins\__StateMonitor (module StateMonitor). Replace it with
    # Plugins\__DwoStateMonitor in the same mode. Saved assets resolve through the plugin's CoreRedirects.
    if (-not (Test-Path $SmLegacyDest)) { return $false }
    if (-not (Test-Managed $SmLegacyDest)) { Say 'Plugins\__StateMonitor is not managed by dwookit; leaving it.'; return $false }
    $runtime = Test-RuntimeMode (Join-Path $SmLegacyDest 'StateMonitor.uplugin')
    Say "Migrating Plugins\__StateMonitor (dwo-Lens 1.x) to Plugins\__DwoStateMonitor ($(if ($runtime) { 'runtime' } else { 'editor and PIE' }))..."
    Install-StateMonitor $Engine $runtime
    Remove-Folder $SmLegacyDest 'Plugins\__StateMonitor'
    Say 'StateMonitor migrated to DwoStateMonitor.'
    $null = Write-StaleDepsWarning
    return $true
}

function Invoke-SmInstall {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $own = Find-OwnStateMonitor
    if ($own) { Fail "The project already contains a state monitor plugin ($($own.FullName.Substring($Project.Length + 1))). Nothing to install." }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $null = Set-ExcludeBlock
    $runtime = $Rest -contains '--runtime'
    $legacy = (Test-Path $SmLegacyDest) -and (Test-Managed $SmLegacyDest)
    if ($legacy -and -not $runtime) { $runtime = Test-RuntimeMode (Join-Path $SmLegacyDest 'StateMonitor.uplugin') }
    Install-StateMonitor $engine $runtime
    if ($legacy) { Remove-Folder $SmLegacyDest 'Plugins\__StateMonitor'; Say 'Replaced the dwo-Lens 1.x install (Plugins\__StateMonitor).' }
    if ($runtime) {
        Say "DwoStateMonitor installed for runtime: Plugins\__DwoStateMonitor (UE $($engine.Version))"
        Say 'Packaged games include it only when the project declares it:'
        if ($uproject.IsCode) { Say '  C++ project: add "DwoStateMonitor" to the Build.cs of the module that calls it.' }
        else { Say '  Blueprint-only project: add {"Name": "DwoStateMonitor", "Enabled": true} to Plugins in the .uproject.' }
        Say 'Remove those entries before sm-remove or uninstall.'
    } else {
        Say "DwoStateMonitor installed for editor and PIE: Plugins\__DwoStateMonitor (UE $($engine.Version))"
        Say 'Packaged builds are not affected. For packaged games use: __dwookit.bat sm-install --runtime'
    }
    Say 'Live view: open dwookit > DwoStateMonitor, then Play in the editor or run __dwookit.bat play. No project code needed.'
    Say 'Optional: Blueprint Watch State Object / Submit State Snapshot. C++: UStateMonitorSubsystem::Get(this)->Submit(TEXT("Id"), State).'
    $null = Write-StaleDepsWarning
}

function Remove-StateMonitor([bool]$Force) {
    $installed = @(@($SmDest, $SmLegacyDest) | Where-Object { Test-Path $_ })
    if ($installed.Count -eq 0) { return $true }
    foreach ($dir in $installed) { if (-not (Test-Managed $dir)) { Fail "$dir is not managed by dwookit. Leaving it untouched." } }
    $refs = @(Get-StateMonitorReferences)
    if ($refs.Count -gt 0 -and -not $Force) {
        Say 'Project code still uses the state monitor. Removing the plugin would break the build:'
        $refs | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" }
        Say 'Remove those references first, or run again with --force.'
        return $false
    }
    foreach ($dir in $installed) { Remove-Folder $dir (Split-Path -Leaf $dir) }
    Remove-EmptyPluginsFolder
    Save-ToolRecord 'statemonitor' $null
    Say 'DwoStateMonitor removed.'
    return $true
}

function Invoke-SmRemove {
    if (-not (Get-InstalledStateMonitor)) { Say 'DwoStateMonitor is not installed by dwookit.'; return }
    if (-not (Remove-StateMonitor ($Rest -contains '--force'))) { exit 1 }
}

# ---------------------------------------------------------------- DwoCanvas (provided tool)
# Managed like DwoStateMonitor: the source stays in dwookit, canvas-install copies it to Plugins\__DwoCanvas
# (git-excluded), and canvas-remove / uninstall delete it. Widget Blueprints made by canvas-import use the
# DwoCanvasRuntime module, so removal is refused while such assets exist (unless --force).
function Get-CanvasState {
    # none | managed (installed by dwookit) | own (the project's own copy)
    if (Test-Path (Join-Path $CanvasDest 'DwoCanvas.uplugin')) {
        if (Test-Managed $CanvasDest) { return 'managed' }
        return 'own'
    }
    if (Test-Path (Join-Path $CanvasOldDest $CanvasOldMarker)) { return 'managed' }   # 2.0.0 install, not moved yet
    $plugins = Join-Path $Project 'Plugins'
    if ((Test-Path $plugins) -and (Get-ChildItem -Path $plugins -Recurse -Filter 'DwoCanvas.uplugin' -ErrorAction SilentlyContinue |
                                   Where-Object { $_.FullName -notlike '*\Plugins\__*' } | Select-Object -First 1)) { return 'own' }
    return 'none'
}

function Get-CanvasUsers {
    # Assets that reference DwoCanvasRuntime (classes of widgets generated by canvas-import).
    $content = Join-Path $Project 'Content'
    if (-not (Test-Path $content)) { return @() }
    $users = New-Object System.Collections.Generic.List[string]
    $latin1 = [Text.Encoding]::GetEncoding(28591)   # one char per byte
    foreach ($f in (Get-ChildItem -Path $content -Recurse -File -Include *.uasset, *.umap -ErrorAction SilentlyContinue)) {
        # Imported class and package names live in the package header (name and import tables) at the start
        # of the file; the first 8 MB cover it for any realistic widget, and the bulk data after it never matters.
        $stream = [IO.File]::OpenRead($f.FullName)
        try {
            $buffer = New-Object byte[] ([Math]::Min($stream.Length, 8MB))
            $read = $stream.Read($buffer, 0, $buffer.Length)
        } finally { $stream.Dispose() }
        if ($latin1.GetString($buffer, 0, $read).Contains('DwoCanvasRuntime')) { $users.Add($f.FullName.Substring($Project.Length + 1)) }
    }
    return $users.ToArray()
}

function Invoke-CanvasInstall {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $state = Get-CanvasState
    if ($state -eq 'own') { Fail 'The project already contains its own DwoCanvas plugin. Nothing to install.' }
    $src = Join-Path $SystemDir 'plugin\DwoCanvas\DwoCanvas.uplugin'
    if (-not (Test-Path $src)) { Fail "DwoCanvas is not part of this dwookit install ($src missing)." }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    # Prebuilt editor binaries, like DwoStateMonitor: Blueprint-only projects open without compiling.
    $build = Get-PluginBuild $engine 'DwoCanvas' 'DwoCanvasEditor'
    $null = Set-ExcludeBlock
    if (Test-Path $CanvasDest) { Remove-Folder $CanvasDest 'Plugins\__DwoCanvas' }
    if (Test-Path (Join-Path $CanvasOldDest $CanvasOldMarker)) { Remove-Folder $CanvasOldDest 'Plugins\DwoCanvas (2.0.0 install)' }
    New-Item -ItemType Directory -Force $CanvasDest | Out-Null
    Get-ChildItem $build.Dir | Where-Object { $_.Name -ne 'Intermediate' } | ForEach-Object { Copy-Item -Recurse $_.FullName $CanvasDest }
    Copy-PluginExtras 'DwoCanvas' $CanvasDest     # Schema\, Config\, Samples\, README
    $upluginPath = Join-Path $CanvasDest 'DwoCanvas.uplugin'
    $uplugin = Get-Content -Raw -Encoding UTF8 $upluginPath | ConvertFrom-Json
    $uplugin | Add-Member -NotePropertyName EnabledByDefault -NotePropertyValue $true -Force
    $uplugin | Add-Member -NotePropertyName Installed -NotePropertyValue $false -Force
    [IO.File]::WriteAllText($upluginPath, ($uplugin | ConvertTo-Json -Depth 10), $Utf8)
    [IO.File]::WriteAllText((Join-Path $CanvasDest $Marker),
        "DwoCanvas $($uplugin.VersionName) installed by dwookit ($(Get-SystemCommit)). Removed by '__dwookit.bat canvas-remove' or uninstall.`n", $Utf8)
    Set-ToolInstalled 'canvas' @{ engine = $engine.Version }
    Say "DwoCanvas installed: Plugins\__DwoCanvas (UE $($engine.Version)), hidden from git like the other dwookit plugins."
    if (-not $uproject.IsCode) { Say 'Blueprint-only project: prebuilt editor binaries are included, so the editor opens without compiling.' }
    Say 'Widget Blueprints made by canvas-import use DwoCanvasRuntime: anyone opening them needs DwoCanvas installed too.'
    Say 'Next: __dwookit.bat canvas-validate <file.dwocanvas.json>, then canvas-import <file>.'
}

function Move-OldCanvasInstall {
    # 2.0.0 installed DwoCanvas visibly as Plugins\DwoCanvas; move it to the managed, git-excluded location.
    if (-not (Test-Path (Join-Path $CanvasOldDest $CanvasOldMarker))) { return }
    if (Test-Path $CanvasDest) { return }
    try { Move-Item $CanvasOldDest $CanvasDest -ErrorAction Stop }
    catch { Say "Plugins\DwoCanvas (2.0.0 install) could not be moved to Plugins\__DwoCanvas: $($_.Exception.Message) Close the editor and run any command again."; return }
    Remove-Item -Force (Join-Path $CanvasDest $CanvasOldMarker) -ErrorAction SilentlyContinue
    [IO.File]::WriteAllText((Join-Path $CanvasDest $Marker), "DwoCanvas installed by dwookit. Removed by '__dwookit.bat canvas-remove' or uninstall.`n", $Utf8)
    $null = Set-ExcludeBlock
    Say 'Moved Plugins\DwoCanvas to Plugins\__DwoCanvas (managed by dwookit, hidden from git).'
}

function Remove-Canvas([bool]$Force) {
    # $true when nothing managed is left. The project's own DwoCanvas is never touched.
    $dirs = @(@($CanvasDest, $CanvasOldDest) | Where-Object { (Test-Path $_) -and ((Test-Managed $_) -or (Test-Path (Join-Path $_ $CanvasOldMarker))) })
    if ($dirs.Count -eq 0) { return $true }
    $users = @(Get-CanvasUsers)
    if ($users.Count -gt 0 -and -not $Force) {
        Say "$($users.Count) asset(s) still use DwoCanvasRuntime (generated widgets). Removing the plugin would break them:"
        $users | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" }
        Say 'Delete or rebuild those widgets first, or run again with --force.'
        return $false
    }
    foreach ($dir in $dirs) { Remove-Folder $dir (Split-Path -Leaf $dir) }
    Remove-EmptyPluginsFolder
    Save-ToolRecord 'canvas' $null
    Say 'DwoCanvas removed.'
    if ($Command -ne 'uninstall') { Say 'Generated widgets, import records (DwoCanvas\Records) and reports (Saved\DwoCanvas) stay; uninstall removes them on request.' }
    return $true
}

function Invoke-CanvasRemove {
    $state = Get-CanvasState
    if ($state -eq 'none') { Say 'DwoCanvas is not installed.'; return }
    if ($state -eq 'own') { Fail 'DwoCanvas in this project was not installed by dwookit. Leaving it untouched.' }
    if (-not (Remove-Canvas ($Rest -contains '--force'))) { exit 1 }
}

function Get-FileArg {
    # First argument that is not an option (and not the value of --doc).
    $skip = $false
    foreach ($a in $Rest) {
        if ($skip) { $skip = $false; continue }
        if (-not $a) { continue }
        if ($a -eq '--doc') { $skip = $true; continue }
        if ($a.StartsWith('--')) { continue }
        return $a
    }
    return $null
}

function Resolve-DesignFile {
    $file = Get-FileArg
    if (-not $file) { Fail "Usage: $Command <file.dwocanvas.json>" }
    foreach ($candidate in @($file, (Join-Path $Project $file))) {
        if ($candidate -and (Test-Path -LiteralPath $candidate -PathType Leaf)) { return (Resolve-Path -LiteralPath $candidate).Path }
    }
    Fail "Design file not found: $file"
}

function Get-CanvasReports([datetime]$Since) {
    $dir = Join-Path $Project 'Saved\DwoCanvas\Reports'
    if (-not (Test-Path $dir)) { return @() }
    return @(Get-ChildItem $dir -Filter *.txt -File | Where-Object { $_.LastWriteTime -ge $Since } | Sort-Object LastWriteTime -Descending)
}

function Invoke-CanvasImport {
    $file = Resolve-DesignFile
    if ((Get-CanvasState) -eq 'none') { Fail 'DwoCanvas is not installed in this project. Run canvas-install first.' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $reset = ($Rest -contains '--reset') -or ($Rest -contains '--reset-overrides')
    $doc = $null
    $list = @($Rest)
    for ($k = 0; $k -lt $list.Count - 1; $k++) { if ($list[$k] -eq '--doc') { $doc = $list[$k + 1] } }
    $logDir = if (Test-Path $ForgeDir) { Join-Path $DataDir 'canvas' } else { Join-Path $Project 'Saved\DwoCanvas\Logs' }
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $log = Join-Path $logDir ('import-' + (Get-Date).ToString('yyyyMMdd-HHmmss') + '.log')
    $arguments = "`"$($uproject.Path)`" -run=DwoCanvasImport -File=`"$file`""
    if ($reset) { $arguments += ' -ResetOverrides' }
    if ($doc) { $arguments += " -Doc=`"$doc`"" }
    Say "canvas-import $(Split-Path -Leaf $file)$(if ($reset) { ' (reset overrides)' })$(if ($doc) { " (document $doc)" })"
    Say 'Close the Unreal Editor first: the import saves Widget Blueprints an open editor may hold.'
    $since = (Get-Date).AddSeconds(-2)
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & cmd /c "`"$($engine.Cmd)`" $arguments -unattended -nullrhi -nosplash -nopause -NoSound -stdout -FullStdOutLogOutput > `"$log`" 2>&1"
    $code = $LASTEXITCODE
    $report = $null
    if (Test-Path $log) {
        foreach ($line in ((Read-SharedText $log) -split "`r?`n")) { if ($line -match 'Report:\s*(.+\.txt)\s*$') { $report = $Matches[1].Trim() } }
    }
    # The commandlet prints the path relative to the engine's working folder (Engine\Binaries\Win64).
    if ($report -and -not [IO.Path]::IsPathRooted($report)) { $report = [IO.Path]::GetFullPath((Join-Path (Split-Path -Parent $engine.Cmd) $report)) }
    if (-not $report -or -not (Test-Path -LiteralPath $report)) { $r = Get-CanvasReports $since | Select-Object -First 1; $report = if ($r) { $r.FullName } else { $null } }
    if ($report) {
        $report = (Resolve-Path -LiteralPath $report).Path
        $lines = [IO.File]::ReadAllLines($report, [Text.Encoding]::UTF8)
        $lines | Select-Object -First 2 | ForEach-Object { Say $_ }
        $lines | Where-Object { $_ -like 'ERROR*' } | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Red }
        $lines | Where-Object { $_ -like 'WARN*' } | Select-Object -First 10 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    }
    if (Test-Path $ForgeDir) {
        Write-Json (Join-Path $DataDir 'canvas\last.json') ([ordered]@{
            schema = 'forge.canvas/0.1'; file = $file; reset = [bool]$reset; doc = $doc; exitCode = $code; report = $report
            log = $log; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1); finishedAt = (Get-Date).ToString('o') })
    }
    Say "canvas-import finished in $([math]::Round($sw.Elapsed.TotalSeconds)) s, exit code $code ($(if ($code -eq 0) { 'no errors' } else { 'errors' }))"
    if ($report) { Say "Report: $report" } else { Say "No report was written. Engine log: $log" }
    if ($code -ne 0) { exit $code }
}

function Invoke-CanvasValidate {
    $file = Resolve-DesignFile
    $pyRoot = Join-Path $SystemDir 'canvas\python'
    if (-not (Test-Path (Join-Path $pyRoot 'dwocanvas'))) { Fail "The DwoCanvas validator is not available in this install ($pyRoot\dwocanvas missing)." }
    if (-not (Get-Command python -ErrorAction SilentlyContinue)) { Fail 'Python 3.10+ is required for canvas-validate.' }
    $previous = $env:PYTHONPATH
    $env:PYTHONPATH = if ($previous) { "$pyRoot;$previous" } else { $pyRoot }
    $pyArgs = @('-m', 'dwocanvas', 'validate', $file)
    if ($Rest -contains '--json') { $pyArgs += '--json' } else { Say "canvas-validate $(Split-Path -Leaf $file)" }
    $ErrorActionPreference = 'Continue'
    & python @pyArgs
    $code = $LASTEXITCODE
    $env:PYTHONPATH = $previous
    if ($code -ne 0) { exit $code }
}

function Get-SystemCommit {
    # Version of the install folder: VERSION file, plus the commit when it is a git clone.
    $versionFile = Join-Path $SystemDir 'VERSION'
    $v = if (Test-Path $versionFile) { (Get-Content -Raw $versionFile).Trim() } else { 'dev' }
    if (Test-Path (Join-Path $SystemDir '.git')) {
        $c = & git -C $SystemDir rev-parse --short HEAD 2>$null
        if ($LASTEXITCODE -eq 0 -and $c) { return "$v+$c" }
    }
    return $v
}

function Add-Created([string]$Path) {
    # Remembers a file dwookit caused outside __dwookit so uninstall can offer to remove it.
    if (-not $Path -or -not (Test-Path $ForgeDir)) { return }
    [IO.File]::AppendAllText($LedgerPath, $Path.Substring($Project.Length + 1) + "`n", $Utf8)
}

function Test-LauncherOutdated {
    # The project's __dwookit.bat differs from the one this dwookit writes (older connection).
    if (-not (Test-Path $LauncherPath)) { return $false }
    $src = Join-Path $SystemDir 'tools\project-launcher.bat'
    return [Convert]::ToBase64String([IO.File]::ReadAllBytes($LauncherPath)) -ne [Convert]::ToBase64String([IO.File]::ReadAllBytes($src))
}

function Install-Launcher {
    # The project's __dwookit.bat: a small launcher that finds this install folder (home.txt) and runs dwookit.ps1.
    $src = Join-Path $SystemDir 'tools\project-launcher.bat'
    $existing = if (Test-Path $LauncherPath) { [IO.File]::ReadAllBytes($LauncherPath) } else { $null }
    $new = [IO.File]::ReadAllBytes($src)
    if (-not $existing -or [Convert]::ToBase64String($existing) -ne [Convert]::ToBase64String($new)) {
        # A running __dwookit.bat is read line by line by cmd; replace it only after it finished.
        if ($env:DWOOKIT_FROM_BAT -eq '1' -and $existing) { Copy-Item $src (Join-Path $ForgeDir 'launcher.next') -Force }
        else { Copy-Item $src $LauncherPath -Force }
    }
}

# ---------------------------------------------------------------- migration from earlier names (dwo-Forge 2.x, dwo-Lens 1.x)
function Merge-Folder([string]$From, [string]$To) {
    # Moves what is missing in $To; anything $To already has wins. Locked files are left for the next run.
    New-Item -ItemType Directory -Force $To | Out-Null
    foreach ($c in @(Get-ChildItem -Force $From -ErrorAction SilentlyContinue)) {
        if ($c.Name -eq 'launcher.next') { Remove-Item -Force $c.FullName -ErrorAction SilentlyContinue; continue }
        $target = Join-Path $To $c.Name
        try {
            if (-not (Test-Path $target)) { Move-Item $c.FullName $target -ErrorAction Stop }
            elseif ($c.PSIsContainer) { Merge-Folder $c.FullName $target }
            else { Remove-Item -Force $c.FullName -ErrorAction Stop }
        } catch { }
    }
    if (-not (Get-ChildItem -Force $From -ErrorAction SilentlyContinue)) { Remove-Item -Force -Recurse $From -ErrorAction SilentlyContinue }
}

function Move-LegacyAppData {
    # %LOCALAPPDATA%\dwo-Forge and \dwo-Lens (plugin and GUI caches, GUI settings, kept run histories) -> \dwookit.
    # 2.0.0's GUI launcher could leave a FILE named dwo-Forge (a copied gui.json); it blocks every cache below.
    foreach ($g in $LegacyGens) {
        if (Test-Path $g.AppData -PathType Leaf) { Remove-Item -Force $g.AppData -ErrorAction SilentlyContinue }
        if (-not (Test-Path $g.AppData)) { continue }
        $moved = $false
        if (-not (Test-Path $AppDataDir)) {
            try { Move-Item $g.AppData $AppDataDir -ErrorAction Stop; $moved = $true } catch { }
        }
        if (-not $moved) { Merge-Folder $g.AppData $AppDataDir }    # files in use (a running old window) stay for the next run
        if (-not (Test-Path $g.AppData)) { Say "Moved %LOCALAPPDATA%\$(Split-Path -Leaf $g.AppData) to %LOCALAPPDATA%\dwookit." }
    }
}

function Test-OurLauncher([string]$Path) {
    if (-not (Test-Path $Path)) { return $false }
    $text = [IO.File]::ReadAllText($Path)
    # 'dwo-Lens bootstrap': the 1.x file users copied into the project themselves (it cloned the system into __dwo-lens)
    return ($text -match '(dwo-Lens|dwo-Forge|dwookit) project launcher' -or $text -match '(dwo-Forge|dwookit) legacy launcher' -or $text -match 'dwo-Lens bootstrap')
}

function Update-LegacyHooks {
    # DwoBuilder git hooks written under the dwo-Forge name: same hook, new mark, state folder and kept-original name.
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return }
    $ErrorActionPreference = 'Continue'
    $d = & git -C $Project rev-parse --git-path hooks 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $d) { return }
    if (-not [IO.Path]::IsPathRooted($d)) { $d = Join-Path $Project $d }
    if (-not (Test-Path $d)) { return }
    $n = 0
    foreach ($f in @(Get-ChildItem -File $d -ErrorAction SilentlyContinue)) {
        if ($f.Name -like '*.dwo-forge-orig') {
            $to = $f.FullName -replace '\.dwo-forge-orig$', '.dwookit-orig'
            if (-not (Test-Path $to)) { Move-Item $f.FullName $to -ErrorAction SilentlyContinue }
            continue
        }
        $text = [IO.File]::ReadAllText($f.FullName)
        if ($text -notmatch 'dwo-forge-ci-hook') { continue }
        $text = $text.Replace('dwo-forge-ci-hook', 'dwookit-ci-hook').Replace('.dwo-forge-orig', '.dwookit-orig').Replace('__dwo-forge', '__dwookit').Replace('dwo-Forge', 'dwookit')
        [IO.File]::WriteAllText($f.FullName, $text, $Utf8)
        $n++
    }
    if ($n) { Say "DwoBuilder git hooks moved to the dwookit name ($n)." }
}

function Update-LegacyLedger {
    # created-files.txt names the Copilot pointer file by its dwo-Forge name; lens_agent.py now writes dwookit's.
    if (-not (Test-Path $LedgerPath)) { return }
    $text = [IO.File]::ReadAllText($LedgerPath)
    $new = $text.Replace('dwo-forge.instructions.md', 'dwookit.instructions.md')
    if ($new -ne $text) { [IO.File]::WriteAllText($LedgerPath, $new, $Utf8) }
}

function Invoke-Migration {
    # Renames what an earlier version (dwo-Forge 2.x, dwo-Lens 1.x) put into the project. Runs before every
    # command, so a project connected with an earlier version is migrated the first time any dwookit command (or
    # the GUI) touches it. Run history, settings and the connection are kept.
    Move-LegacyAppData
    Move-OldCanvasInstall
    $any = $false
    foreach ($g in $LegacyGens) {
        $dirName = Split-Path -Leaf $g.Dir
        $batName = Split-Path -Leaf $g.Launcher
        if (-not ((Test-Path $g.Dir) -or (Test-OurLauncher $g.Launcher))) { continue }
        $any = $true
        if (Test-Path $g.Dir) {
            $moved = $false
            if (-not (Test-Path $ForgeDir)) {
                try { Move-Item $g.Dir $ForgeDir -ErrorAction Stop; $moved = $true } catch { }
            }
            if (-not $moved) { Merge-Folder $g.Dir $ForgeDir }
            if (Test-Path $g.Dir) {
                $left = @(Get-ChildItem -Force -Recurse -File $g.Dir -ErrorAction SilentlyContinue | Where-Object { $_.Name -ne 'launcher.next' }).Count
                if ($left) { Say "Moved $dirName to __dwookit except $left file(s) in use (an Unreal editor or an old $($g.Name) window). They move on the next command." }
            } else { Say "Migrated $dirName to __dwookit (run history, settings and connection kept)." }
        }
        if (Test-OurLauncher $g.Launcher) {
            if ($g.FromBat) {
                # The old launcher is running (cmd reads it line by line), so it cannot be deleted now. It moves
                # <old folder>\launcher.next over itself when it finishes: hand it a forwarder to __dwookit.bat
                # that deletes itself on its next use.
                New-Item -ItemType Directory -Force $g.Dir | Out-Null
                Copy-Item (Join-Path $SystemDir 'tools\legacy-project-launcher.bat') (Join-Path $g.Dir 'launcher.next') -Force
            } else {
                Remove-Item -Force $g.Launcher -ErrorAction SilentlyContinue
            }
            if ((Test-Path $StatePath) -and -not (Test-Path $LauncherPath)) { Copy-Item (Join-Path $SystemDir 'tools\project-launcher.bat') $LauncherPath -Force }
            Say "Project launcher renamed: $batName -> __dwookit.bat."
        }
    }
    $attached = Test-Path $StatePath
    if ($attached -and ($any -or (Test-BlockPresent -Legacy))) {
        [IO.File]::WriteAllText((Join-Path $ForgeDir 'home.txt'), $SystemDir, $Utf8)
        if (Test-BlockPresent -Legacy) { $null = Set-ExcludeBlock; Say 'Replaced the git exclude block of the earlier name with the dwookit block.' }
    }
    if ($attached -and $any) {
        Update-LegacyLedger
        Update-LegacyHooks
        Update-AgentContext                                 # CLAUDE.local.md and the Copilot file under the new name
    }
    if (Test-Path $SmLegacyDest) { Say 'Plugins\__StateMonitor (dwo-Lens 1.x) is still installed. Run __dwookit.bat update to migrate it to DwoStateMonitor.' }
}

function Sync-ConnectionFiles {
    # The small files a connection puts into the project follow the dwookit that runs: the launcher and
    # home.txt are rewritten as soon as any command touches a project connected by an older version. Tool
    # plugins are not swapped here (the editor may have their DLLs loaded); they show as updates instead.
    if (-not (Test-Path $StatePath)) { return }
    if (Test-LauncherOutdated) {
        Install-Launcher
        Say 'Project launcher (__dwookit.bat) updated to this dwookit.'
    }
    $homeFile = Join-Path $ForgeDir 'home.txt'
    if (-not (Test-Path $homeFile) -or ([IO.File]::ReadAllText($homeFile).Trim() -ne $SystemDir)) { [IO.File]::WriteAllText($homeFile, $SystemDir, $Utf8) }
}

function Invoke-Attach([string]$Mode) {
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $previous = Read-State
    $baseline = $null
    if ($previous -and $previous.gitStatusBeforeAttach -ne $null) { $baseline = $previous.gitStatusBeforeAttach }
    $pluginsExisted = if ($previous -and $previous.PSObject.Properties['pluginsFolderExisted']) { [bool]$previous.pluginsFolderExisted } else { Test-Path (Join-Path $Project 'Plugins') }

    # Hide everything before it exists (git only).
    if ($baseline -eq $null -and (Get-Vcs).Kind -ne 'none') { $baseline = Get-ProjectStatus }
    $vcs = Set-ExcludeBlock
    switch ($vcs.Kind) {
        'own'    { Say "git repository: dwookit files are hidden through .git\info\exclude (local only, never committed)." }
        'parent' { Say "The project is inside the git repository $($vcs.Top). dwookit files are hidden there under /$($vcs.Prefix)." }
        'none'   { Say 'No version control: nothing to hide, so no exclude entry is written.' }
    }

    New-Item -ItemType Directory -Force $ForgeDir | Out-Null
    # Older versions kept a git clone of the system inside the project. The system now runs from the install folder.
    $legacy = Join-Path $ForgeDir 'system'
    if (Test-Path $legacy) {
        Get-ChildItem $legacy -Recurse -Force -File | ForEach-Object { if ($_.IsReadOnly) { $_.IsReadOnly = $false } }
        Remove-Item -Recurse -Force $legacy
        Say 'Removed the old system copy (__dwookit\system); the system now runs from the dwookit folder.'
    }
    [IO.File]::WriteAllText((Join-Path $ForgeDir 'home.txt'), $SystemDir, $Utf8)
    Install-Launcher

    # Connecting prepares the project only; tools are installed separately ('tool install <id>').
    # attach (dwo-Lens compatible) = connect + DwoLens; update = connect again + update every installed tool.
    $records = Get-ToolRecords
    $state = [ordered]@{
        schema                = 'forge.state/0.4'
        project               = $uproject.Name
        projectType           = $(if ($uproject.IsCode) { 'code' } else { 'blueprint-only' })
        engineVersion         = $engine.Version
        engineRoot            = $engine.Root
        systemCommit          = Get-SystemCommit
        systemHome            = $SystemDir
        vcs                   = $vcs.Kind
        vcsRoot               = $vcs.Top
        excludeFile           = $vcs.Exclude
        pluginsFolderExisted  = $pluginsExisted
        attachedAt            = $(if ($previous) { $previous.attachedAt } else { (Get-Date).ToString('o') })
        updatedAt             = (Get-Date).ToString('o')
        gitStatusBeforeAttach = $baseline
        tools                 = [pscustomobject]$records
    }
    Write-Json $StatePath $state
    Update-AgentContext -Selection
    Say "$(if ($Mode -eq 'update') { 'Connection refreshed' } else { 'Connected' }): $($uproject.Name) / UE $($engine.Version) / dwookit $($state.systemCommit)"
    if ($Mode -eq 'attach') { Install-Lens $engine }
    elseif ($Mode -eq 'update') { Update-AllTools }
    else { Say 'Install tools from the dwookit window or: __dwookit.bat tool install <lens|statemonitor|canvas|builder>' }
    Say "Open the control panel: dwookit.bat (or __dwookit.bat in the project). Disconnect and remove everything: __dwookit.bat uninstall"
    Invoke-Doctor | Out-Null
}

# ---------------------------------------------------------------- tools (dwookit is their launcher)
# Each tool is installed, updated and removed on its own. state.json 'tools' records, per tool, the dwookit
# version, the source hash and the engine it was installed from; a different hash or engine means "update".
# DwoBuilder keeps its record in __dwookit\ci.json (written by ci.ps1).
$ToolDefs = [ordered]@{
    lens         = @{ Name = 'DwoLens';            Scope = 'project' }
    statemonitor = @{ Name = 'DwoStateMonitor'; Scope = 'project' }
    canvas       = @{ Name = 'DwoCanvas';       Scope = 'project' }
    ci           = @{ Name = 'DwoBuilder';        Scope = 'project+pc' }
}

function Get-SystemVersion {
    $f = Join-Path $SystemDir 'VERSION'
    if (Test-Path $f) { return (Get-Content -Raw $f).Trim() }
    return 'dev'
}

function Get-TextHash([string]$Text) {
    $bytes = [Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($Text))
    return (($bytes | ForEach-Object { $_.ToString('x2') }) -join '').Substring(0, 12)
}

function Get-CiSourceHash {
    # Same formula as Get-CiSourceHash in ci.ps1: the CI job and git hooks are generated from these two files.
    $sb = New-Object System.Text.StringBuilder
    foreach ($f in @('tools\dwookit_ci.py', 'tools\ci.ps1')) { [void]$sb.Append("$f=$((Get-FileHash -Algorithm SHA256 (Join-Path $SystemDir $f)).Hash);") }
    return Get-TextHash $sb.ToString()
}

function Get-ToolSourceHash([string]$Id) {
    switch ($Id) {
        'lens'         { return Get-PluginSourceHash 'DwoLensInspector' }
        'statemonitor' { return Get-PluginSourceHash 'DwoStateMonitor' }
        'canvas'       { return Get-PluginSourceHash 'DwoCanvas' }
        'ci'           { return Get-CiSourceHash }
    }
}

function Test-ToolSource([string]$Id) {
    switch ($Id) {
        'lens'         { return Test-Path (Join-Path $SystemDir 'plugin\DwoLensInspector\DwoLensInspector.uplugin') }
        'statemonitor' { return Test-Path (Join-Path $SystemDir 'plugin\DwoStateMonitor\DwoStateMonitor.uplugin') }
        'canvas'       { return Test-Path (Join-Path $SystemDir 'plugin\DwoCanvas\DwoCanvas.uplugin') }
        'ci'           { return Test-Path (Join-Path $SystemDir 'tools\ci.ps1') }
    }
    return $false
}

function Get-ToolRecords {
    $s = Read-State
    $r = [ordered]@{}
    if ($s -and $s.PSObject.Properties['tools'] -and $s.tools) { foreach ($p in $s.tools.PSObject.Properties) { $r[$p.Name] = $p.Value } }
    # Projects connected before 3.0 recorded only the DwoLens inspector hash.
    if (-not $r.Contains('lens') -and $s -and $s.PSObject.Properties['pluginHash'] -and (Test-Path $PluginDest)) {
        $r['lens'] = [pscustomobject]@{ version = "$($s.systemCommit)"; hash = "$($s.pluginHash)"; engine = "$($s.engineVersion)" }
    }
    return $r
}

function Save-ToolRecord([string]$Id, $Record) {
    # $Record $null forgets the tool. Not connected: nothing to record.
    $s = Read-State
    if (-not $s) { return }
    $tools = [ordered]@{}
    $current = Get-ToolRecords
    foreach ($k in $current.Keys) { if ($k -ne $Id) { $tools[$k] = $current[$k] } }
    if ($Record) { $tools[$Id] = $Record }
    $s | Add-Member -NotePropertyName tools -NotePropertyValue ([pscustomobject]$tools) -Force
    if ($s.PSObject.Properties['pluginHash']) { $s.PSObject.Properties.Remove('pluginHash') }
    Write-Json $StatePath $s
}

function Set-ToolInstalled([string]$Id, [hashtable]$Extra = @{}) {
    $rec = [ordered]@{ version = Get-SystemVersion; hash = Get-ToolSourceHash $Id; installedAt = (Get-Date).ToString('o') }
    foreach ($k in $Extra.Keys) { $rec[$k] = $Extra[$k] }
    Save-ToolRecord $Id ([pscustomobject]$rec)
}

function Get-CiHome {
    # Same rule as ci.ps1: DWOOKIT_CI_HOME, else %LOCALAPPDATA%\dwookit\ci.
    if ($env:DWOOKIT_CI_HOME) { return $env:DWOOKIT_CI_HOME.TrimEnd('\') }
    return Join-Path $AppDataDir 'ci'
}

function Get-PcStatus {
    # Machine-wide parts the tools use: the local Jenkins (DwoBuilder), its offline bundle, the installed engines.
    $ciHome = Get-CiHome
    $st = $null
    $f = Join-Path $ciHome 'ci.json'
    if (Test-Path $f) { $st = Get-Content -Raw -Encoding UTF8 $f | ConvertFrom-Json }
    $bundle = Get-ChildItem (Join-Path $SystemDir 'bundles') -Filter 'dwookit-ci-bundle-*.zip' -File -ErrorAction SilentlyContinue |
              Sort-Object LastWriteTime -Descending | Select-Object -First 1
    $bundleVersion = if ($bundle -and $bundle.BaseName -match 'bundle-(.+)$') { $Matches[1] } else { '' }
    return [ordered]@{
        jenkins = [ordered]@{ installed = [bool](Test-Path (Join-Path $ciHome 'runtime\jenkins.war'))
                              version = $(if ($st) { "$($st.jenkinsVersion)" } else { '' })
                              port = $(if ($st -and $st.port) { [int]$st.port } else { 0 })
                              home = $ciHome }
        bundle  = [ordered]@{ file = $(if ($bundle) { $bundle.Name } else { '' }); version = $bundleVersion }
        engines = @(Get-InstalledEngines)
    }
}

function Get-ToolStatus([string]$Id, $EngineVersion) {
    # state: none | installed (up to date) | update | own (part of the project, never touched) | legacy (dwo-Lens 1.x install)
    $records = Get-ToolRecords
    $rec = $records[$Id]
    $state = 'none'
    $detail = ''
    switch ($Id) {
        'lens' {
            if (Test-Path (Join-Path $PluginDest 'DwoLensInspector.uplugin')) { $state = $(if (Test-Managed $PluginDest) { 'installed' } else { 'own' }) }
        }
        'statemonitor' {
            $sm = Get-InstalledStateMonitor
            if ($sm -and $sm.Legacy) { $state = 'legacy' }
            elseif ($sm) { $state = 'installed'; $detail = $(if (Test-RuntimeMode $sm.Uplugin) { 'runtime' } else { 'editor' }) }
            elseif (Find-OwnStateMonitor) { $state = 'own' }
        }
        'canvas' {
            switch (Get-CanvasState) { 'managed' { $state = 'installed' } 'own' { $state = 'own' } }
        }
        'ci' {
            $ciFile = Join-Path $ForgeDir 'ci.json'
            if (Test-Path $ciFile) { $state = 'installed'; $rec = Get-Content -Raw -Encoding UTF8 $ciFile | ConvertFrom-Json; $detail = "$($rec.job)" }
        }
    }
    $source = Test-ToolSource $Id
    $availableHash = if ($source) { Get-ToolSourceHash $Id } else { '' }
    $reason = ''
    if ($state -eq 'installed') {
        $hash = if ($rec -and $rec.PSObject.Properties['hash']) { "$($rec.hash)" } else { '' }
        $engine = if ($rec -and $rec.PSObject.Properties['engine']) { "$($rec.engine)" } else { '' }
        $version = if ($rec -and $rec.PSObject.Properties['version']) { "$($rec.version)".Split('+')[0] } else { '' }
        if (-not $hash) { $state = 'update'; $reason = 'unknown-version' }
        elseif ($source -and $hash -ne $availableHash) { $state = 'update'; $reason = 'new-version' }
        # Installed by an older release: same plugin source, but its files (descriptor, marker, record) are
        # rewritten so the project carries nothing from the old version.
        elseif ($version -and $version -ne (Get-SystemVersion)) { $state = 'update'; $reason = 'older-release' }
        elseif ($engine -and $EngineVersion -and $engine -ne $EngineVersion) { $state = 'update'; $reason = 'engine-changed' }
    }
    return [ordered]@{
        id = $Id; name = $ToolDefs[$Id].Name; scope = $ToolDefs[$Id].Scope; state = $state; reason = $reason; detail = $detail
        installedVersion = $(if ($rec -and $rec.PSObject.Properties['version']) { "$($rec.version)" } else { '' })
        availableVersion = Get-SystemVersion
        availableHash = $availableHash
        installedHash = $(if ($rec -and $rec.PSObject.Properties['hash']) { "$($rec.hash)" } else { '' })
        installedEngine = $(if ($rec -and $rec.PSObject.Properties['engine']) { "$($rec.engine)" } else { '' })
        installedAt = $(if ($rec -and $rec.PSObject.Properties['installedAt']) { "$($rec.installedAt)" } elseif ($rec -and $rec.PSObject.Properties['registeredAt']) { "$($rec.registeredAt)" } else { '' })
        available = $source
    }
}

function Get-ToolsDocument {
    $state = Read-State
    $files = @(Get-ChildItem -Path $Project -Filter *.uproject -File -ErrorAction SilentlyContinue)
    $association = ''
    if ($files.Count -eq 1) { $association = "$((Get-Content -Raw -Encoding UTF8 $files[0].FullName | ConvertFrom-Json).EngineAssociation)" }
    $engine = if ($association) { Find-Engine $association } else { $null }
    $engineVersion = if ($engine) { $engine.Version } else { '' }
    return [ordered]@{
        schema  = 'forge.tools/1'
        forge   = [ordered]@{ version = Get-SystemVersion; commit = Get-SystemCommit; home = $SystemDir }
        project = [ordered]@{
            name = $(if ($files.Count -eq 1) { $files[0].BaseName } else { '' }); path = $Project; connected = [bool]$state
            legacy = [bool]@($LegacyGens | Where-Object { Test-Path (Join-Path $_.Dir 'state.json') }).Count
            type = $(if (Test-Path (Join-Path $Project 'Source')) { 'code' } else { 'blueprint-only' })
            engineAssociation = $association; engine = $engineVersion
            connectedEngine = $(if ($state) { "$($state.engineVersion)" } else { '' })
            vcs = $(if ($state) { "$($state.vcs)" } else { '' })
            connectedAt = $(if ($state) { "$($state.attachedAt)" } else { '' })
            connectedWith = $(if ($state) { "$($state.systemCommit)" } else { '' })
            launcherOutdated = [bool]($state -and (Test-LauncherOutdated))
        }
        tools   = @($ToolDefs.Keys | ForEach-Object { Get-ToolStatus $_ $engineVersion })
        pc      = Get-PcStatus
    }
}

function Install-Lens($Engine) {
    $build = Get-PluginBuild $Engine
    Install-Plugin $build
    Set-ToolInstalled 'lens' @{ engine = $Engine.Version }
    Say "DwoLens installed: Plugins\__DwoLensInspector (UE $($Engine.Version), editor-only, disabled by default)."
}

function Update-StateMonitor($Engine) {
    if (Move-LegacyStateMonitor $Engine) { return }
    $sm = Get-InstalledStateMonitor
    if (-not $sm) { Fail 'DwoStateMonitor is not installed by dwookit. Install it: __dwookit.bat tool install statemonitor' }
    $runtime = Test-RuntimeMode $sm.Uplugin
    Install-StateMonitor $Engine $runtime
    Say "DwoStateMonitor updated ($(if ($runtime) { 'runtime' } else { 'editor and PIE' }), UE $($Engine.Version))."
}

function Invoke-CiTool([string]$Action) {
    $ci = Join-Path $SystemDir 'tools\ci.ps1'
    if ($Action -eq 'install' -and -not (Get-PcStatus).jenkins.installed) {
        Say 'DwoBuilder needs the PC-wide Jenkins first: installing it from the offline bundle...'
        & $ci -Command ci-install -Project $Project -Rest @()
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
    $rest = if ($Action -eq 'remove') { @('--remove') } else { @() }
    & $ci -Command ci-job -Project $Project -Rest $rest
    if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
}

function Invoke-ToolAction([string]$Action, [string]$Id) {
    if ($Action -ne 'remove' -and -not (Test-ToolSource $Id)) { Fail "$($ToolDefs[$Id].Name) is not part of this dwookit install." }
    switch ($Id) {
        'lens' {
            if ($Action -eq 'remove') {
                if (-not (Test-Path $PluginDest)) { Say 'DwoLens is not installed.'; Save-ToolRecord 'lens' $null; return }
                Remove-Plugin
                Save-ToolRecord 'lens' $null
                Say 'DwoLens removed (Plugins\__DwoLensInspector). Run history in __dwookit\data stays; uninstall removes it.'
                return
            }
            $engine = Resolve-Engine (Get-UProject).Json.EngineAssociation
            $null = Set-ExcludeBlock
            Install-Lens $engine
        }
        'statemonitor' {
            switch ($Action) {
                'install' { Invoke-SmInstall }
                'update'  { $null = Set-ExcludeBlock; Update-StateMonitor (Resolve-Engine (Get-UProject).Json.EngineAssociation) }
                'remove'  { Invoke-SmRemove }
            }
        }
        'canvas' {
            if ($Action -eq 'remove') { Invoke-CanvasRemove } else { Invoke-CanvasInstall }
        }
        'ci' { Invoke-CiTool $Action }
    }
}

function Update-AllTools {
    $engine = Find-Engine (Get-UProject).Json.EngineAssociation
    $done = 0
    foreach ($id in $ToolDefs.Keys) {
        $s = Get-ToolStatus $id $(if ($engine) { $engine.Version } else { '' })
        if ($s.state -in @('update', 'legacy')) { Say "updating $($s.name) ..."; Invoke-ToolAction 'update' $id; $done++ }
        elseif ($s.state -eq 'installed') { Say "$($s.name): up to date" }
    }
    if ((Read-State) -and (Test-LauncherOutdated)) { Install-Launcher; Say 'Project launcher (__dwookit.bat) refreshed.'; $done++ }
    if (-not $done) { Say 'Every installed tool is up to date.' }
}

function Invoke-Tool {
    $a = @($Rest)
    $action = if ($a.Count) { $a[0] } else { '' }
    $id = if ($a.Count -gt 1) { "$($a[1])".ToLowerInvariant() } else { '' }
    # Tool names work too: DwoLens, DwoStateMonitor, DwoCanvas, DwoBuilder (and builder for ci).
    $aliases = @{ dwolens = 'lens'; dwostatemonitor = 'statemonitor'; dwocanvas = 'canvas'; dwobuilder = 'ci'; builder = 'ci' }
    if ($aliases.ContainsKey($id)) { $id = $aliases[$id] }
    $usage = 'Usage: __dwookit.bat tool install|update|remove <lens|statemonitor|canvas|builder>   or   tool update --all'
    if ($action -notin @('install', 'update', 'remove')) { Fail $usage }
    if (-not (Read-State)) { Fail 'This project is not connected. Connect it first: __dwookit.bat connect' }
    if ($action -eq 'update' -and $id -eq '--all') { Update-AllTools; return }
    if (-not $ToolDefs.Contains($id)) { Fail "Unknown tool '$id'. $usage" }
    Invoke-ToolAction $action $id
}

function Invoke-ToolList {
    if ($Rest -contains '--catalog') { Invoke-Tools; return }
    $doc = Get-ToolsDocument
    if ($Rest -contains '--json') {
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        Write-Output (ConvertTo-Json -InputObject $doc -Depth 6 -Compress)
        return
    }
    Say "$($doc.project.name): $(if ($doc.project.connected) { "connected (dwookit $($doc.project.connectedWith))" } else { 'not connected' }) - dwookit $($doc.forge.version)"
    foreach ($t in $doc.tools) {
        $text = switch ($t.state) {
            'none'      { 'not installed' }
            'installed' { "up to date ($($t.installedVersion))" }
            'update'    { "update available ($(if ($t.installedVersion) { $t.installedVersion } else { 'unknown' }) -> $($t.availableVersion)$(if ($t.reason -eq 'engine-changed') { ', engine changed' }))" }
            'own'       { 'part of the project itself (not managed)' }
            'legacy'    { 'dwo-Lens 1.x install (update migrates it)' }
        }
        Say ("  {0,-16} {1}{2}" -f $t.name, $text, $(if ($t.detail) { " - $($t.detail)" } else { '' }))
    }
    if ($doc.project.launcherOutdated) { Say ("  {0,-16} {1}" -f 'Project launcher', 'older version (update refreshes __dwookit.bat)') }
    $j = $doc.pc.jenkins
    Say "  PC: Jenkins $(if ($j.installed) { "$($j.version) (port $($j.port))" } else { 'not installed' }); bundle $(if ($doc.pc.bundle.file) { $doc.pc.bundle.version } else { 'none' }); engines $(@($doc.pc.engines | ForEach-Object { $_.version }) -join ', ')"
}

function Get-ToolSummary {
    # One line per provided tool, shared by status and doctor.
    $sm = Get-InstalledStateMonitor
    $smText = if ($sm -and $sm.Legacy) { 'installed by dwo-Lens 1.x as Plugins\__StateMonitor (run update to migrate)' }
              elseif ($sm) { "installed (Plugins\__DwoStateMonitor, $(if (Test-RuntimeMode $sm.Uplugin) { 'runtime' } else { 'editor and PIE' }))" }
              elseif (Find-OwnStateMonitor) { 'part of the project itself' }
              else { 'not installed (optional: tool install statemonitor)' }
    $canvasText = switch (Get-CanvasState) {
        'managed' { 'installed (Plugins\__DwoCanvas)' }
        'own'     { 'part of the project itself' }
        default   { 'not installed (optional: tool install canvas)' }
    }
    return [pscustomobject]@{ StateMonitor = $smText; Canvas = $canvasText }
}

function Invoke-Status {
    $state = Read-State
    if (-not $state) {
        Say 'Not connected.'
        Say "DwoCanvas      : $((Get-ToolSummary).Canvas)"
        return
    }
    $tools = Get-ToolSummary
    Say "Project        : $($state.project) ($($state.projectType))"
    Say "Engine         : UE $($state.engineVersion)"
    Say "dwookit      : $(Get-SystemCommit) at $SystemDir"
    if (Test-LauncherOutdated) { Say "Project launcher: from an older dwookit ($($state.systemCommit)). Run update to refresh it." }
    Say "Version control: $((Get-Vcs).Kind)"
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    if ($engine.Version -ne $state.engineVersion) { Say "Engine changed to $($engine.Version). Run __dwookit.bat update." }
    Say "Attached at    : $($state.attachedAt)"
    Say "DwoLens           : $(if (Test-Path $PluginDest) { 'installed (Plugins\__DwoLensInspector)' } else { 'not installed (optional: tool install lens)' })"
    Say "DwoStateMonitor: $($tools.StateMonitor)"
    Say "DwoCanvas      : $($tools.Canvas)"
    Say "DwoBuilder     : $(if (Test-Path (Join-Path $ForgeDir 'ci.json')) { 'installed' } else { 'not installed (optional: tool install builder)' })"
    Say 'Versions and updates: __dwookit.bat tools'
    $null = Write-StaleDepsWarning
}

function Invoke-Doctor {
    function Check([string]$Name, [bool]$Pass, [string]$Hint) {
        if ($Pass) { Write-Host "  [ok]   $Name" } else { Write-Host "  [fail] $Name - $Hint" -ForegroundColor Yellow; $script:doctorFailed = $true }
    }
    $script:doctorFailed = $false
    Say 'doctor'
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    Check "engine UE $($engine.Version)" (Test-Path $engine.Cmd) 'UnrealEditor-Cmd.exe not found'
    $vcs = Get-Vcs
    if ($vcs.Kind -eq 'none') { Write-Host '  [info] no version control: exclude not needed' }
    else {
        if ($vcs.Kind -eq 'parent') { Write-Host "  [info] inside the git repository $($vcs.Top) (entries prefixed /$($vcs.Prefix))" }
        Check 'exclude block present' (Test-BlockPresent) 'run: __dwookit.bat connect'
        Check 'no exclude block of an earlier name left (dwo-Forge, dwo-Lens)' (-not (Test-BlockPresent -Legacy)) 'run update'
        $leaks = Invoke-Git @('status', '--porcelain', '--untracked-files=all', '--', '__dwookit', '__dwookit.bat', 'Plugins/__DwoLensInspector', 'Plugins/__DwoStateMonitor', 'Plugins/__DwoCanvas', '__dwo-forge', '__dwo-forge.bat', '__dwo-lens', '__dwo-lens.bat', 'Plugins/__StateMonitor')
        Check 'dwookit files hidden from git' (-not $leaks) "visible to git: $leaks"
    }
    if (Test-Path $PluginDest) { Check 'DwoLens inspector complete' (Test-Path (Join-Path $PluginDest 'Binaries\Win64\UnrealEditor-DwoLensInspector.dll')) 'run: tool update lens' }
    else { Write-Host '  [info] DwoLens: not installed (optional: tool install lens)' }
    Check 'project launcher' (Test-Path $LauncherPath) 'run: __dwookit.bat connect'
    $py = Get-Command python -ErrorAction SilentlyContinue
    Check 'python available' ($py -ne $null) 'install Python 3.10+'
    if ($uproject.IsCode) {
        Write-Host '  [note] code project: Plugins/__DwoLensInspector is prebuilt, editor-only and disabled by default.'
    }
    $tools = Get-ToolSummary
    Write-Host "  [info] DwoStateMonitor: $($tools.StateMonitor)"
    Check 'no "StateMonitor" module references left' (-not (Write-StaleDepsWarning)) 'rename them to DwoStateMonitor (see above)'
    Write-Host "  [info] DwoCanvas: $($tools.Canvas)"
    if ((Get-CanvasState) -ne 'none') {
        Check 'DwoCanvas validator available' (Test-Path (Join-Path $SystemDir 'canvas\python\dwocanvas')) 'canvas\python missing from the dwookit install'
    }
    return (-not $script:doctorFailed)
}

# ---------------------------------------------------------------- config / engine runs
function Get-ConfigFiles {
    $files = @(Join-Path $SystemDir 'config\default.json')
    $override = Join-Path $ForgeDir 'config.json'
    if (Test-Path $override) { $files += $override }
    return $files
}

function Get-Config {
    # Two-level merge: project config.json overrides keys inside each section of default.json.
    $cfg = Get-Content -Raw -Encoding UTF8 (Join-Path $SystemDir 'config\default.json') | ConvertFrom-Json
    $override = Join-Path $ForgeDir 'config.json'
    if (Test-Path $override) {
        $o = Get-Content -Raw -Encoding UTF8 $override | ConvertFrom-Json
        foreach ($section in $o.PSObject.Properties) {
            if ($cfg.PSObject.Properties[$section.Name] -and $section.Value -is [pscustomobject]) {
                foreach ($k in $section.Value.PSObject.Properties) {
                    $cfg.($section.Name) | Add-Member -NotePropertyName $k.Name -NotePropertyValue $k.Value -Force
                }
            } else {
                $cfg | Add-Member -NotePropertyName $section.Name -NotePropertyValue $section.Value -Force
            }
        }
    }
    return $cfg
}

function Invoke-Engine($Engine, [string]$Arguments, [string]$RunDir, [string]$Name, [int]$TimeoutMinutes) {
    # Runs UnrealEditor-Cmd headless with its log routed into the run folder. Returns exit code, -1 on timeout.
    $log = Join-Path $RunDir "$Name.log"
    $all = "$Arguments -unattended -nullrhi -nosplash -nopause -NoSound -ABSLOG=`"$log`""
    $p = Start-Process -FilePath $Engine.Cmd -ArgumentList $all -NoNewWindow -PassThru `
         -RedirectStandardOutput (Join-Path $RunDir "$Name.stdout.txt") -RedirectStandardError (Join-Path $RunDir "$Name.stderr.txt")
    $null = $p.Handle
    if (-not $p.WaitForExit($TimeoutMinutes * 60000)) {
        try { $p.Kill() } catch { }
        Say "$Name timed out after $TimeoutMinutes min"
        return -1
    }
    return $p.ExitCode
}

function Find-StateMonitor($Config) {
    # The state monitor plugin in any form (installed by dwookit, dwo-Lens 1.x, or the project's own copy).
    $names = @($Config.stateMonitor.pluginName) | Where-Object { $_ } | ForEach-Object { "$_.uplugin" }
    if (-not $names) { $names = @('DwoStateMonitor.uplugin', 'StateMonitor.uplugin') }
    $plugins = Join-Path $Project 'Plugins'
    if (-not (Test-Path $plugins)) { return $null }
    return Get-ChildItem -Path $plugins -Recurse -Include $names -ErrorAction SilentlyContinue |
           Where-Object { $_.FullName -notlike '*\__DwoLensInspector\*' } | Select-Object -First 1
}

function New-RunDir([string]$Prefix) {
    $runId = "$Prefix-" + (Get-Date).ToString('yyyyMMdd-HHmmss')
    $dir = Join-Path $DataDir "runs\$runId"
    New-Item -ItemType Directory -Force $dir | Out-Null
    return [pscustomobject]@{ Id = $runId; Dir = $dir }
}

function Get-PreviousOutput([string]$Prefix, [string]$File, [string]$ExceptId) {
    $runs = Join-Path $DataDir 'runs'
    if (-not (Test-Path $runs)) { return $null }
    return Get-ChildItem $runs -Directory -Filter "$Prefix-*" | Where-Object { $_.Name -ne $ExceptId } |
           Sort-Object Name -Descending | ForEach-Object { Join-Path $_.FullName "out\$File" } |
           Where-Object { Test-Path $_ } | Select-Object -First 1
}

function Write-Run($Run, [string]$Line, $Stations, [string]$Final, $Engine) {
    $state = Read-State
    Write-Json (Join-Path $Run.Dir 'run.json') ([ordered]@{
        schema = 'lens.run/0.1'; runId = $Run.Id; line = $Line; project = $state.project
        engineVersion = $Engine.Version; systemCommit = Get-SystemCommit
        finishedAt = (Get-Date).ToString('o'); stations = $Stations; final = $Final
    })
    Complete-RunOutputs $Run.Dir
}

function Complete-RunOutputs([string]$RunDir) {
    # Every finished run: JUnit + SARIF for CI (compared with the previous run of the same check), a copy into
    # DWOOKIT_CI_OUT when a CI job runs us, then the retention limit (oldest runs beyond it are removed).
    $id = Split-Path -Leaf $RunDir
    $prefix = $id.Split('-')[0]
    $pyArgs = @((Join-Path $SystemDir 'tools\lens_export.py'), '--run', $RunDir)
    $baseline = Get-PreviousOutput $prefix 'results.sarif' $id
    if ($baseline) { $pyArgs += @('--baseline', $baseline) }
    if ($env:DWOOKIT_CI_OUT) { $pyArgs += @('--copy-to', $env:DWOOKIT_CI_OUT) }
    & python @pyArgs
    & python (Join-Path $SystemDir 'tools\dwookit_ci.py') prune --runs (Join-Path $DataDir 'runs') --config @(Get-ConfigFiles)
}

# ---------------------------------------------------------------- build lines (compile, package)
function Get-EditorTarget($UProject) {
    # <Name>Editor.Target.cs under Source; the UE convention is <Project>Editor.
    $src = Join-Path $Project 'Source'
    $t = @(Get-ChildItem $src -Filter '*Editor.Target.cs' -File -ErrorAction SilentlyContinue | Select-Object -First 1)
    if ($t.Count) { return $t[0].Name -replace '\.Target\.cs$', '' }
    return "$($UProject.Name)Editor"
}

function Invoke-Logged([string]$Exe, [string]$Arguments, [string]$Log, [int]$TimeoutMinutes) {
    # Console tools (Build.bat, RunUAT.bat) with stdout+stderr in one log. Returns the exit code, -1 on timeout.
    $p = Start-Process -FilePath 'cmd.exe' -ArgumentList "/d /s /c `"`"$Exe`" $Arguments > `"$Log`" 2>&1`"" -NoNewWindow -PassThru
    $null = $p.Handle
    if (-not $p.WaitForExit($TimeoutMinutes * 60000)) {
        & taskkill /PID $p.Id /T /F 2>$null | Out-Null
        Say "timed out after $TimeoutMinutes min"
        return -1
    }
    return $p.ExitCode
}

function Invoke-Compile {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $run = New-RunDir 'compile'
    $raw = Join-Path $run.Dir 'raw'
    New-Item -ItemType Directory -Force $raw | Out-Null
    $py = Join-Path $SystemDir 'tools\lens_build.py'
    if (-not $uproject.IsCode) {
        Say "[$($run.Id)] B1 compile: Blueprint-only project, nothing to compile"
        & python $py compile --run $run.Dir --skipped blueprint-only
        Write-Run $run 'compile' @([ordered]@{ id = 'B1'; name = 'compile'; verdict = 'pass'; ms = 0 }) 'pass' $engine
        Say "[$($run.Id)] verdict: pass"
        return
    }
    $target = Get-EditorTarget $uproject
    $timeout = [int](Get-ConfigValue $cfg.build 'compileTimeoutMinutes' 60)
    $buildBat = Join-Path $engine.Root 'Engine\Build\BatchFiles\Build.bat'
    Say "[$($run.Id)] B1 compile: $target Win64 Development (UnrealBuildTool)"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = Invoke-Logged $buildBat "$target Win64 Development -Project=`"$($uproject.Path)`" -WaitMutex -NoHotReloadFromIDE" (Join-Path $raw 'compile.log') $timeout
    $sw.Stop()
    Write-Json (Join-Path $raw 'compile-meta.json') ([ordered]@{ target = $target; platform = 'Win64'; configuration = 'Development'
                                                                 exitCode = $code; seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1) })
    & python $py compile --run $run.Dir
    $result = Read-LineResult (Join-Path $run.Dir 'out\compile.json')
    Complete-Line $run 'compile' @([ordered]@{ id = 'B1'; name = $target; exitCode = $code; ms = $sw.ElapsedMilliseconds; verdict = $result.verdict; log = 'raw\compile.log' }) $result $engine
}

function Clear-SpaceForPackage($Retention) {
    # A packaged build can take many GB: below minFreeDiskGB, drop the oldest kept packages first.
    $min = [double](Get-ConfigValue $Retention 'minFreeDiskGB' 20)
    $drive = [IO.DriveInfo]::new([IO.Path]::GetPathRoot($Project))
    $free = $drive.AvailableFreeSpace / 1GB
    if ($free -ge $min) { return }
    $old = @(Get-ChildItem (Join-Path $DataDir 'runs') -Directory -Filter 'package-*' -ErrorAction SilentlyContinue | Sort-Object Name)
    foreach ($d in $old) {
        Say "Low disk space ($([math]::Round($free, 1)) GB free, minimum $min GB): removing old package run $($d.Name)"
        Remove-Item -Recurse -Force $d.FullName -ErrorAction SilentlyContinue
        $free = $drive.AvailableFreeSpace / 1GB
        if ($free -ge $min) { return }
    }
    Say "Warning: only $([math]::Round($free, 1)) GB free on $($drive.Name) (retention.minFreeDiskGB = $min)."
}

function Invoke-Package {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $platform = [string](Get-ConfigValue $cfg.build 'platform' 'Win64')
    $config = [string](Get-ConfigValue $cfg.build 'configuration' 'Development')
    $timeout = [int](Get-ConfigValue $cfg.build 'packageTimeoutMinutes' 180)
    Clear-SpaceForPackage $cfg.retention
    $run = New-RunDir 'package'
    $raw = Join-Path $run.Dir 'raw'
    $archive = Join-Path $run.Dir 'build'
    New-Item -ItemType Directory -Force $raw | Out-Null
    $flags = "-project=`"$($uproject.Path)`" -noP4 -platform=$platform -clientconfig=$config -cook -stage -pak -archive -archivedirectory=`"$archive`" -unattended -utf8output -nocompileeditor"
    if ($uproject.IsCode) { $flags += ' -build' }
    $extra = [string](Get-ConfigValue $cfg.build 'extraPackageArgs' '')
    if ($extra) { $flags += " $extra" }
    Say "[$($run.Id)] P1 package: $platform $config (RunUAT BuildCookRun)"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $code = Invoke-Logged $engine.RunUAT "BuildCookRun $flags" (Join-Path $raw 'package.log') $timeout
    $sw.Stop()
    Write-Json (Join-Path $raw 'package-meta.json') ([ordered]@{ platform = $platform; configuration = $config; exitCode = $code
                                                                 seconds = [math]::Round($sw.Elapsed.TotalSeconds, 1) })
    & python (Join-Path $SystemDir 'tools\lens_build.py') package --run $run.Dir --archive $archive
    $result = Read-LineResult (Join-Path $run.Dir 'out\package.json')
    Complete-Line $run 'package' @([ordered]@{ id = 'P1'; name = "$platform $config"; exitCode = $code; ms = $sw.ElapsedMilliseconds; verdict = $result.verdict; log = 'raw\package.log' }) $result $engine
}

function Invoke-Verify {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $v = $cfg.verify
    $run = New-RunDir 'verify'
    $stations = New-Object System.Collections.Generic.List[object]
    $up = "`"$($uproject.Path)`""

    if ($v.compileBlueprints) {
        Say "[$($run.Id)] V1 CompileAllBlueprints"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $code = Invoke-Engine $engine "$up -run=CompileAllBlueprints -ShowResultsOnly" $run.Dir 'compile' $v.timeoutMinutes
        $stations.Add([ordered]@{ id = 'V1'; name = 'compile-blueprints'; exitCode = $code; ms = $sw.ElapsedMilliseconds; verdict = $null })
    }
    if ($v.dataValidation) {
        Say "[$($run.Id)] V2 DataValidation"
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $code = Invoke-Engine $engine "$up -run=DataValidation" $run.Dir 'datavalidation' $v.timeoutMinutes
        $stations.Add([ordered]@{ id = 'V2'; name = 'data-validation'; exitCode = $code; ms = $sw.ElapsedMilliseconds; verdict = $null })
    }
    if ($v.automation) {
        $filters = @($v.automationFilters)
        $sm = Find-StateMonitor $cfg
        if ($sm) {
            $filters += $cfg.stateMonitor.testFilter
            Say "DwoStateMonitor detected ($($sm.FullName.Substring($Project.Length + 1))): adding '$($cfg.stateMonitor.testFilter)' tests"
        }
        $filterText = ($filters | Select-Object -Unique) -join '+'
        Say "[$($run.Id)] V3 Automation RunTests $filterText"
        $report = Join-Path $run.Dir 'automation'
        New-Item -ItemType Directory -Force $report | Out-Null
        $sw = [Diagnostics.Stopwatch]::StartNew()
        $code = Invoke-Engine $engine "$up -ExecCmds=`"Automation RunTests $filterText;Quit`" -testexit=`"Automation Test Queue Empty`" -ReportExportPath=`"$report`"" $run.Dir 'automation' $v.timeoutMinutes
        $stations.Add([ordered]@{ id = 'V3'; name = 'automation'; filter = $filterText; exitCode = $code; ms = $sw.ElapsedMilliseconds; verdict = $null })
    }

    & python (Join-Path $SystemDir 'tools\lens_verify.py') verify --run $run.Dir
    $result = Get-Content -Raw -Encoding UTF8 (Join-Path $run.Dir 'out\verify.json') | ConvertFrom-Json
    foreach ($s in $stations) {
        $step = $result.steps | Where-Object { $_.id -eq $s.id } | Select-Object -First 1
        if ($step) { $s.verdict = $step.verdict }
    }
    Write-Run $run 'verify' $stations $result.verdict $engine
    Say "[$($run.Id)] verdict: $($result.verdict)"
    Say "Report: $(Join-Path $run.Dir 'out\report.html')"
    if ($result.verdict -eq 'fail') { exit 2 }
}

function Invoke-Logs {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $run = New-RunDir 'logs'
    $logDir = Join-Path $Project 'Saved\Logs'
    $files = @()
    if (Test-Path $logDir) {
        $files = @(Get-ChildItem $logDir -Filter *.log -File | Sort-Object LastWriteTime -Descending |
                   Select-Object -First $cfg.logs.maxFiles | ForEach-Object { $_.FullName })
    }
    Say "[$($run.Id)] ST-4 logs: $($files.Count) log files"
    $pyArgs = @((Join-Path $SystemDir 'tools\lens_verify.py'), 'logs', '--run', $run.Dir, '--crashes', (Join-Path $Project 'Saved\Crashes'))
    if ($files.Count -gt 0) { $pyArgs += '--files'; $pyArgs += $files }
    $baseline = Get-PreviousOutput 'logs' 'logs.json' $run.Id
    if ($baseline) { $pyArgs += @('--baseline', $baseline) }
    $ignore = @($cfg.logs.ignoreCategories)
    if ($ignore.Count -gt 0) { $pyArgs += '--ignore'; $pyArgs += $ignore }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & python @pyArgs
    $sw.Stop()
    $summary = Get-Content -Raw -Encoding UTF8 (Join-Path $run.Dir 'out\logs.json') | ConvertFrom-Json
    Write-Run $run 'logs' @([ordered]@{ id = 'ST-4'; name = 'logs'; verdict = $summary.verdict; files = $files.Count; ms = $sw.ElapsedMilliseconds }) $summary.verdict $engine
    Say "[$($run.Id)] verdict: $($summary.verdict)"
    Say "Report: $(Join-Path $run.Dir 'out\report.html')"
    if ($summary.verdict -eq 'fail') { exit 2 }
}

# ---------------------------------------------------------------- runtime lines (smoke, perf, sizes, trace, play)
function Get-EditorExe($Engine) { return (Join-Path $Engine.Root 'Engine\Binaries\Win64\UnrealEditor.exe') }

function Get-MapArg([string]$Fallback) {
    # First non-option argument after the command, e.g. __dwookit.bat perf /Game/Maps/L_Main
    $m = @($Rest | Where-Object { $_ -and -not $_.StartsWith('--') }) | Select-Object -First 1
    if ($m) { return $m }
    if ($Fallback) { return $Fallback }
    return (Get-DefaultMap)
}

function Get-DefaultMap {
    # GameDefaultMap from Config\DefaultEngine.ini ("/Game/Maps/Main.Main" -> "/Game/Maps/Main").
    $ini = Join-Path $Project 'Config\DefaultEngine.ini'
    if (-not (Test-Path $ini)) { return '' }
    foreach ($line in [IO.File]::ReadAllLines($ini)) {
        if ($line -match '^\s*GameDefaultMap\s*=\s*(\S+)') { return ($Matches[1] -replace '\.[^/]*$', '') }
    }
    return ''
}

function Get-ConfigValue($Section, [string]$Key, $Default) {
    if ($Section -and $Section.PSObject.Properties[$Key] -and $Section.$Key -ne $null -and "$($Section.$Key)" -ne '') { return $Section.$Key }
    return $Default
}

function Invoke-Timed([string]$Exe, [string]$Arguments, [string]$RunDir, [string]$Name, [int]$Seconds, [switch]$Window) {
    # Runs a game process for a fixed time, then stops it: window close first (windowed runs), then the whole
    # process tree. ExitCode is $null when dwookit stopped the process, the real code when it exited on its own.
    $started = Get-Date
    $sp = @{ FilePath = $Exe; ArgumentList = $Arguments; PassThru = $true }
    if (-not $Window) {
        $sp.NoNewWindow = $true
        $sp.RedirectStandardOutput = Join-Path $RunDir "$Name.stdout.txt"
        $sp.RedirectStandardError = Join-Path $RunDir "$Name.stderr.txt"
    }
    $p = Start-Process @sp
    $null = $p.Handle
    $killed = $false
    if (-not $p.WaitForExit($Seconds * 1000)) {
        $killed = $true
        if ($Window) { try { [void]$p.CloseMainWindow() } catch { }; [void]$p.WaitForExit(15000) }
        if (-not $p.HasExited) { & taskkill /PID $p.Id /T /F 2>$null | Out-Null; [void]$p.WaitForExit(15000) }
    }
    $ended = Get-Date
    $code = $null
    if (-not $killed) { $code = $p.ExitCode }
    return [pscustomobject]@{
        ExitCode = $code; Killed = $killed; Seconds = ($ended - $started).TotalSeconds
        StartedAt = [DateTimeOffset]::new($started).ToUnixTimeSeconds(); EndedAt = [DateTimeOffset]::new($ended).ToUnixTimeSeconds()
    }
}

function Read-LineResult([string]$Path) {
    if (Test-Path $Path) { return (Get-Content -Raw -Encoding UTF8 $Path | ConvertFrom-Json) }
    return [pscustomobject]@{ verdict = 'fail' }
}

function Complete-Line($Run, [string]$Line, $Stations, $Result, $Engine) {
    Write-Run $Run $Line $Stations $Result.verdict $Engine
    Say "[$($Run.Id)] verdict: $($Result.verdict)"
    Say "Report: $(Join-Path $Run.Dir 'out\report.html')"
    if ($Result.verdict -eq 'fail') { exit 2 }
}

function Invoke-Smoke {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $seconds = [int](Get-ConfigValue $cfg.smoke 'seconds' 15)
    $run = New-RunDir 'smoke'
    $raw = Join-Path $run.Dir 'raw'
    New-Item -ItemType Directory -Force $raw | Out-Null
    $py = Join-Path $SystemDir 'tools\lens_runtime.py'
    $mapsFile = Join-Path $raw 'maps.json'
    & python $py maps --project $Project --out $mapsFile --config @(Get-ConfigFiles)
    $maps = @()
    if (Test-Path $mapsFile) { foreach ($m in (Get-Content -Raw -Encoding UTF8 $mapsFile | ConvertFrom-Json)) { $maps += [string]$m } }
    Say "[$($run.Id)] smoke: $($maps.Count) maps, $seconds s each"

    $entries = New-Object System.Collections.Generic.List[object]
    $stations = New-Object System.Collections.Generic.List[object]
    $i = 0
    foreach ($m in $maps) {
        $i++
        $name = "smoke-$i"
        $log = Join-Path $raw "$name.log"
        Say "[$($run.Id)] SM-$i $m"
        $r = Invoke-Timed $engine.Cmd "`"$($uproject.Path)`" `"$m`" -game -nullrhi -nosound -unattended -nosplash -nopause -log -ABSLOG=`"$log`"" $raw $name $seconds
        $entries.Add([ordered]@{ map = $m; log = $log; exitCode = $r.ExitCode; killed = $r.Killed; seconds = [math]::Round($r.Seconds, 1)
                                 startedAt = $r.StartedAt; endedAt = $r.EndedAt })
        $stations.Add([ordered]@{ id = "SM-$i"; name = $m; exitCode = $r.ExitCode; ms = [int]($r.Seconds * 1000); verdict = $null; log = "raw\$name.log" })
    }
    $runsFile = Join-Path $raw 'smoke-runs.json'
    if ($entries.Count -eq 0) { [IO.File]::WriteAllText($runsFile, '[]', $Utf8) }
    else { [IO.File]::WriteAllText($runsFile, (ConvertTo-Json -InputObject $entries.ToArray() -Depth 5), $Utf8) }

    & python $py smoke --run $run.Dir --project $Project --config @(Get-ConfigFiles)
    $result = Read-LineResult (Join-Path $run.Dir 'out\smoke.json')
    for ($k = 0; $k -lt $stations.Count; $k++) {
        if ($result.maps -and $k -lt @($result.maps).Count) { $stations[$k].verdict = @($result.maps)[$k].verdict }
    }
    Complete-Line $run 'smoke' $stations $result $engine
}

function Invoke-Perf {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $p = $cfg.perf
    $map = Get-MapArg (Get-ConfigValue $p 'map' '')
    $nullrhi = [bool](Get-ConfigValue $p 'nullrhi' $false)
    $frames = [int](Get-ConfigValue $p 'frames' 600) + [int](Get-ConfigValue $p 'warmupFrames' 60)
    $timeout = [int](Get-ConfigValue $p 'timeoutSeconds' 600)
    $run = New-RunDir 'perf'
    $raw = Join-Path $run.Dir 'raw'
    $out = Join-Path $run.Dir 'out'
    New-Item -ItemType Directory -Force $raw, $out | Out-Null
    $stations = New-Object System.Collections.Generic.List[object]

    # CSV profiler: the capture starts on the first frame after the map loaded (-ExecCmds run then) and the
    # game exits by itself when the frame count is reached (csvprofile exitoncompletion / -ExitAfterCsvProfiling).
    $csvName = "$($run.Id).csv"
    $exec = "csvprofile startfile=$csvName,csvprofile exitoncompletion,csvprofile frames=$frames"
    $log = Join-Path $raw 'perf.log'
    $common = "`"$($uproject.Path)`" `"$map`" -game -nosound -unattended -nosplash -nopause -log -ABSLOG=`"$log`" -csvCompression=0 -ExitAfterCsvProfiling -ExecCmds=`"$exec`""
    Say "[$($run.Id)] P1 capture $frames frames on $map ($(if ($nullrhi) { 'nullrhi' } else { 'rendered' }))"
    if ($nullrhi) { $r = Invoke-Timed $engine.Cmd "$common -nullrhi" $raw 'perf' $timeout }
    else {
        $resX = [int](Get-ConfigValue $p 'resX' 960); $resY = [int](Get-ConfigValue $p 'resY' 540)
        $r = Invoke-Timed (Get-EditorExe $engine) "$common -windowed -ResX=$resX -ResY=$resY" $raw 'perf' $timeout -Window
    }
    $csvDir = Join-Path $Project 'Saved\Profiling\CSV'
    $csv = Join-Path $csvDir $csvName
    if (-not (Test-Path $csv) -and (Test-Path $csvDir)) {
        $since = [DateTimeOffset]::FromUnixTimeSeconds($r.StartedAt).LocalDateTime
        $found = Get-ChildItem $csvDir -Filter *.csv -File | Where-Object { $_.LastWriteTime -ge $since } | Sort-Object LastWriteTime -Descending | Select-Object -First 1
        if ($found) { $csv = $found.FullName }
    }
    $copied = Join-Path $out 'perf.csv'
    if (Test-Path $csv) { Copy-Item $csv $copied -Force; Add-Created $csv }
    if ($r.Killed) { Say "P1 did not finish in $timeout s; stopped" }
    $stations.Add([ordered]@{ id = 'P1'; name = 'csv-capture'; exitCode = $r.ExitCode; ms = [int]($r.Seconds * 1000)
                              verdict = $(if (Test-Path $copied) { 'pass' } else { 'fail' }); log = 'raw\perf.log' })

    $sw = [Diagnostics.Stopwatch]::StartNew()
    $pyArgs = @((Join-Path $SystemDir 'tools\lens_runtime.py'), 'perf', '--run', $run.Dir, '--map', $map, '--config') + @(Get-ConfigFiles)
    if (Test-Path $copied) { $pyArgs += @('--csv', $copied) }
    if ($r.ExitCode -ne $null) { $pyArgs += @('--exit-code', "$($r.ExitCode)") }
    & python @pyArgs
    $result = Read-LineResult (Join-Path $out 'perf.json')
    $stations.Add([ordered]@{ id = 'P2'; name = 'frame-stats'; ms = $sw.ElapsedMilliseconds; verdict = $result.verdict })
    Complete-Line $run 'perf' $stations $result $engine
}

function Invoke-Sizes {
    $state = Read-State
    if (-not $state) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $run = New-RunDir 'sizes'
    $sw = [Diagnostics.Stopwatch]::StartNew()
    $pyArgs = @((Join-Path $SystemDir 'tools\lens_runtime.py'), 'sizes', '--run', $run.Dir, '--project', $Project, '--config') + @(Get-ConfigFiles)
    $graph = Get-PreviousOutput 'run' 'graph.json' ''
    if ($graph) { $pyArgs += @('--graph', $graph) } else { Say 'No analyze run yet: hard-reference closure weights skipped (run __dwookit.bat analyze).' }
    Say "[$($run.Id)] Z1 content sizes"
    & python @pyArgs
    $result = Read-LineResult (Join-Path $run.Dir 'out\sizes.json')
    $stations = @([ordered]@{ id = 'Z1'; name = 'content-sizes'; ms = $sw.ElapsedMilliseconds; verdict = $result.verdict; graph = $graph })
    # No engine run: the version recorded at attach is enough for run.json.
    Complete-Line $run 'sizes' $stations $result ([pscustomobject]@{ Version = $state.engineVersion })
}

function Invoke-Trace {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $cfg = Get-Config
    $t = $cfg.trace
    $map = Get-MapArg (Get-ConfigValue $cfg.perf 'map' '')
    $seconds = [int](Get-ConfigValue $t 'seconds' 20)
    $channels = [string](Get-ConfigValue $t 'channels' 'default,counters,loadtime,task')
    $nullrhi = [bool](Get-ConfigValue $t 'nullrhi' $false)
    $run = New-RunDir 'trace'
    $raw = Join-Path $run.Dir 'raw'
    $out = Join-Path $run.Dir 'out'
    New-Item -ItemType Directory -Force $raw, $out | Out-Null
    $file = Join-Path $out 'trace.utrace'
    $log = Join-Path $raw 'trace.log'
    $common = "`"$($uproject.Path)`" `"$map`" -game -nosound -unattended -nosplash -nopause -log -ABSLOG=`"$log`" -trace=$channels -tracefile=`"$file`" -statnamedevents"
    Say "[$($run.Id)] T1 trace $seconds s on $map (channels $channels)"
    if ($nullrhi) { $r = Invoke-Timed $engine.Cmd "$common -nullrhi" $raw 'trace' $seconds }
    else { $r = Invoke-Timed (Get-EditorExe $engine) "$common -windowed -ResX=960 -ResY=540" $raw 'trace' $seconds -Window }
    $insights = Join-Path $engine.Root 'Engine\Binaries\Win64\UnrealInsights.exe'
    $pyArgs = @((Join-Path $SystemDir 'tools\lens_runtime.py'), 'trace', '--run', $run.Dir, '--file', $file, '--seconds', "$([math]::Round($r.Seconds, 1))", '--config') + @(Get-ConfigFiles)
    if (Test-Path $insights) { $pyArgs += @('--insights', $insights) }
    if (-not $r.Killed) { $pyArgs += @('--exited', '--exit-code', "$($r.ExitCode)") }
    & python @pyArgs
    $result = Read-LineResult (Join-Path $out 'trace.json')
    $stations = @([ordered]@{ id = 'T1'; name = 'insights-trace'; exitCode = $r.ExitCode; ms = [int]($r.Seconds * 1000); verdict = $result.verdict; channels = $channels })
    Complete-Line $run 'trace' $stations $result $engine
}

function Invoke-Play {
    # Not a run: starts the game detached (e.g. for the StateMonitor viewer) and returns immediately.
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    $map = @($Rest | Where-Object { $_ -and -not $_.StartsWith('--') }) | Select-Object -First 1
    $arguments = "`"$($uproject.Path)`""
    if ($map) { $arguments += " `"$map`"" }
    $arguments += ' -game -windowed -ResX=1280 -ResY=720 -log'
    $p = Start-Process -FilePath (Get-EditorExe $engine) -ArgumentList $arguments -PassThru
    Say "play: started $(if ($map) { $map } else { 'default map' }) (pid $($p.Id))"
    Write-Output "pid=$($p.Id)"
}

function Invoke-Tools {
    $catalog = Get-Content -Raw -Encoding UTF8 (Join-Path $SystemDir 'tools\catalog.json') | ConvertFrom-Json
    foreach ($t in $catalog.tools) {
        Write-Host ("  {0,-5} {1,-24} {2,-6} {3,-10} {4}" -f $t.station, $t.id, $t.kind, $t.status, $t.command)
    }
}

function Invoke-Analyze {
    $state = Read-State
    if (-not $state) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    if (-not (Test-Path (Join-Path $PluginDest 'DwoLensInspector.uplugin'))) { Fail 'DwoLens is not installed in this project. Install it: __dwookit.bat tool install lens' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    Assert-ModulesBuilt $uproject
    $startedAt = Get-Date
    $runId = 'run-' + $startedAt.ToString('yyyyMMdd-HHmmss')
    $runDir = Join-Path $DataDir "runs\$runId"
    $rawDir = Join-Path $runDir 'raw'
    $outDir = Join-Path $runDir 'out'
    New-Item -ItemType Directory -Force $rawDir, $outDir | Out-Null
    $stations = New-Object System.Collections.Generic.List[object]

    Say "[$runId] ST-1 collect: running DwoLensInspector commandlet"
    $log = Join-Path $runDir 'commandlet.log'
    # Tracked Blueprints are refreshed in the same editor start.
    $bpArgs = ''
    if (@(Get-BpTracked).Count) {
        New-Item -ItemType Directory -Force $BpDir | Out-Null
        $bpArgs = " -LensBpList=`"$BpTrackPath`" -LensBpOut=`"$BpDir`""
    }
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & cmd /c "`"$($engine.Cmd)`" `"$($uproject.Path)`" -run=DwoLensInspector -EnablePlugins=DwoLensInspector -LensOut=`"$rawDir`"$bpArgs -ABSLOG=`"$(Join-Path $runDir 'editor.log')`" -unattended -nullrhi -nosplash -nopause -NoSound -stdout -FullStdOutLogOutput > `"$log`" 2>&1"
    $code = $LASTEXITCODE
    $sw.Stop()
    $collected = Test-Path (Join-Path $rawDir 'meta.json')
    # Output written but a non-zero exit (errors or ensures while loading assets): usable, but flag it.
    $collectVerdict = if (-not $collected) { 'fail' } elseif ($code -ne 0) { 'warn' } else { 'pass' }
    $stations.Add([ordered]@{ id = 'ST-1'; name = 'collect'; verdict = $collectVerdict; exitCode = $code; ms = $sw.ElapsedMilliseconds; log = 'commandlet.log' })
    if ($collected -and $code -ne 0) { Say "[$runId] ST-1 collect: the editor exited with code $code (errors while loading the project). See $log" }
    if (-not $collected) { Write-Json (Join-Path $runDir 'run.json') @{ runId = $runId; stations = $stations; final = 'fail' }; Fail "Commandlet produced no output. See $log" }

    Say "[$runId] ST-2/3 analyze and gate"
    $sw = [Diagnostics.Stopwatch]::StartNew()
    & python (Join-Path $SystemDir 'tools\lens_analyze.py') --raw $rawDir --project $uproject.Path --out $outDir --run-id $runId --config @(Get-ConfigFiles)
    $pyCode = $LASTEXITCODE
    $sw.Stop()
    $findingsPath = Join-Path $outDir 'findings.json'
    $verdict = 'fail'
    if ($pyCode -eq 0 -and (Test-Path $findingsPath)) { $verdict = (Get-Content -Raw -Encoding UTF8 $findingsPath | ConvertFrom-Json).verdict }
    $stations.Add([ordered]@{ id = 'ST-2'; name = 'analyze+gate'; verdict = $verdict; exitCode = $pyCode; ms = $sw.ElapsedMilliseconds })

    $run = [ordered]@{
        schema = 'lens.run/0.1'; runId = $runId; line = 'architecture-report'
        project = $state.project; engineVersion = $engine.Version; systemCommit = Get-SystemCommit
        startedAt = $startedAt.ToString('o'); finishedAt = (Get-Date).ToString('o'); stations = $stations; final = $verdict
    }
    Write-Json (Join-Path $runDir 'run.json') $run
    Complete-RunOutputs $runDir
    if ($bpArgs) { Invoke-BpMarkdown }
    Update-AgentContext
    Say "[$runId] verdict: $verdict"
    $report = Join-Path $outDir 'report.html'
    if (Test-Path $report) { Say "Report: $report" }
    else { Say "No report: the analyzer stopped (exit code $pyCode) before writing one. The error is printed above." }
    if ($verdict -eq 'fail') { exit 2 }
}

# ---------------------------------------------------------------- tracked Blueprints (bp)
# The user picks Blueprints (files or folders) to follow. 'bp export' runs the DwoLensInspector commandlet for
# them only and writes one folder per Blueprint under __dwookit\blueprints: <Name>.json (every graph, node, pin
# and link) and <Name>.md (compressed pseudocode for agents), plus index.md. 'analyze' refreshes them too.
$BpTrackPath = Join-Path $ForgeDir 'bp-track.txt'
$BpDir       = Join-Path $ForgeDir 'blueprints'

function Get-BpTracked {
    if (-not (Test-Path $BpTrackPath)) { return @() }
    return @(Get-Content -Encoding UTF8 $BpTrackPath | ForEach-Object { $_.Trim() } | Where-Object { $_ -and -not $_.StartsWith('#') })
}

function Set-BpTracked([string[]]$Entries) {
    $lines = @('# Blueprints followed by dwookit (bp add / bp remove). One package path or folder (ending with /) per line.') +
             @($Entries | Sort-Object -Unique)
    [IO.File]::WriteAllText($BpTrackPath, (($lines -join "`n") + "`n"), $Utf8)
}

function ConvertTo-BpEntry([string]$Arg) {
    # Accepts /Game/A/BP_X, /Game/A/BP_X.BP_X, /Game/A/ (folder), Content\A\BP_X.uasset, a folder on disk,
    # or a plugin's Content path. Returns the package path ('/Game/A/BP_X') or folder ('/Game/A/').
    $a = $Arg.Trim().Trim('"')
    if (-not $a) { return $null }
    if ($a.StartsWith('/')) {
        $a = $a -replace '\\', '/'
        if ($a.EndsWith('/')) { return $a }
        $leaf = $a.Substring($a.LastIndexOf('/') + 1)
        if ($leaf.Contains('.')) { $a = $a.Substring(0, $a.LastIndexOf('/') + 1) + $leaf.Substring(0, $leaf.IndexOf('.')) }
        return $a
    }
    $full = $null
    foreach ($candidate in @($a, (Join-Path $Project $a))) {
        if (Test-Path $candidate) { $full = (Resolve-Path $candidate).Path.TrimEnd('\'); break }
    }
    if (-not $full) { Fail "Not found: $Arg (give /Game/... or a path to a .uasset or folder in this project)" }
    if (-not $full.StartsWith($Project + '\', [StringComparison]::OrdinalIgnoreCase)) { Fail "$Arg is outside the project." }
    $rel = $full.Substring($Project.Length + 1) -replace '\\', '/'
    $isDir = (Get-Item $full).PSIsContainer
    if ($rel -match '^Content(/|$)(.*)$') { $mount = '/Game'; $inner = $Matches[2] }
    elseif ($rel -match '^(Plugins/(?:.+/)?)([^/]+)/Content(/|$)(.*)$') {
        $pluginDir = Join-Path $Project (($Matches[1] + $Matches[2]) -replace '/', '\')
        $uplugin = @(Get-ChildItem $pluginDir -Filter '*.uplugin' -File -ErrorAction SilentlyContinue | Select-Object -First 1)
        $mount = '/' + $(if ($uplugin.Count) { $uplugin[0].BaseName } else { $Matches[2] }); $inner = $Matches[4]
    }
    else { Fail "$Arg is not under Content\ or a plugin's Content\ folder." }
    if ($isDir) { return ($mount + '/' + $inner).TrimEnd('/') + '/' }
    if ($inner -notmatch '\.uasset$') { Fail "$Arg is not a .uasset file." }
    return $mount + '/' + ($inner -replace '\.uasset$', '')
}

function Test-BpEntryExists([string]$Entry) {
    # Only /Game paths can be checked on disk; plugin mounts are checked by the export.
    if (-not $Entry.StartsWith('/Game/')) { return $true }
    $rel = 'Content\' + ($Entry.Substring(6).TrimEnd('/') -replace '/', '\')
    if ($Entry.EndsWith('/')) { return Test-Path (Join-Path $Project $rel) -PathType Container }
    return Test-Path (Join-Path $Project "$rel.uasset")
}

function Assert-ModulesBuilt($UProject) {
    # The commandlet loads the project's own C++ modules; unbuilt ones make the editor stop with a dialog-less error.
    if (-not $UProject.IsCode) { return }
    $bin = Join-Path $Project 'Binaries\Win64'
    $missing = @()
    foreach ($m in @($UProject.Json.Modules)) {
        if (-not $m -or -not $m.Name) { continue }
        if ("$($m.Type)" -match 'Program|Server') { continue }
        if (-not (Get-ChildItem $bin -Filter "UnrealEditor-$($m.Name)*.dll" -ErrorAction SilentlyContinue)) { $missing += $m.Name }
    }
    if ($missing.Count) { Fail "The project's C++ modules are not built for the editor ($($missing -join ', ')). Build them first: __dwookit.bat compile (editor closed)" }
}

function Invoke-BpMarkdown {
    if (-not (Test-Path (Join-Path $BpDir 'export.json'))) { return }
    & python (Join-Path $SystemDir 'tools\lens_bp.py') md --dir $BpDir --project $Project
    if ($LASTEXITCODE -ne 0) { Say 'bp: writing the pseudocode (.md) failed; the .json files are there.' }
}

function Invoke-BpExport {
    $tracked = @(Get-BpTracked)
    if ($tracked.Count -eq 0) { Fail 'No Blueprint is tracked. Add some first: __dwookit.bat bp add <Content\...\BP_X.uasset | /Game/... | folder>' }
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    if (-not (Test-Path (Join-Path $PluginDest 'DwoLensInspector.uplugin'))) { Fail 'DwoLens is not installed in this project. Install it: __dwookit.bat tool install lens' }
    $uproject = Get-UProject
    $engine = Resolve-Engine $uproject.Json.EngineAssociation
    Assert-ModulesBuilt $uproject
    $logDir = Join-Path $DataDir 'logs'
    New-Item -ItemType Directory -Force $logDir, $BpDir | Out-Null
    $log = Join-Path $logDir 'bp-export.log'
    Say "bp export: $($tracked.Count) entr$(if ($tracked.Count -eq 1) { 'y' } else { 'ies' }) -> $BpDir"
    & cmd /c "`"$($engine.Cmd)`" `"$($uproject.Path)`" -run=DwoLensInspector -EnablePlugins=DwoLensInspector -LensBpList=`"$BpTrackPath`" -LensBpOut=`"$BpDir`" -ABSLOG=`"$(Join-Path $logDir 'bp-export-editor.log')`" -unattended -nullrhi -nosplash -nopause -NoSound -stdout -FullStdOutLogOutput > `"$log`" 2>&1"
    $code = $LASTEXITCODE
    if (-not (Test-Path (Join-Path $BpDir 'export.json'))) { Fail "The commandlet exported nothing (exit code $code). See $log" }
    Invoke-BpMarkdown
    Update-AgentContext
    $manifest = Get-Content -Raw -Encoding UTF8 (Join-Path $BpDir 'export.json') | ConvertFrom-Json
    foreach ($m in @($manifest.missing)) { if ($m) { Say "bp: no Blueprint found at $m" } }
    foreach ($f in @($manifest.failed)) { if ($f) { Say "bp: could not export $f (see $log)" } }
    Say "bp export: $(@($manifest.exported).Count) Blueprint(s). Start here: $(Join-Path $BpDir 'index.md')"
    if ($code -ne 0) { exit 2 }
}

function Invoke-Bp {
    $action = if ($Rest.Count) { $Rest[0] } else { 'list' }
    $args_ = @($Rest | Select-Object -Skip 1 | Where-Object { $_ })
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    switch ($action) {
        'add' {
            if (-not $args_.Count) { Fail 'Usage: bp add <Content\...\BP_X.uasset | /Game/.../BP_X | folder> [...]' }
            $tracked = @(Get-BpTracked)
            foreach ($a in $args_) {
                $e = ConvertTo-BpEntry $a
                if (-not (Test-BpEntryExists $e)) { Fail "No asset at $e" }
                if ($tracked -contains $e) { Say "already tracked: $e" } else { $tracked += $e; Say "tracking: $e" }
            }
            Set-BpTracked $tracked
            Say 'Run __dwookit.bat bp export (editor closed) to write the files.'
        }
        'remove' {
            if (-not $args_.Count) { Fail 'Usage: bp remove <path> [...]' }
            $tracked = @(Get-BpTracked)
            foreach ($a in $args_) {
                $e = if ($a.Trim().StartsWith('/')) { ConvertTo-BpEntry $a } else { try { ConvertTo-BpEntry $a } catch { $a } }
                if ($tracked -contains $e) { $tracked = @($tracked | Where-Object { $_ -ne $e }); Say "no longer tracked: $e" }
                else { Say "not tracked: $e" }
            }
            Set-BpTracked $tracked
            Say 'Its files are removed at the next bp export.'
        }
        'list' {
            $tracked = @(Get-BpTracked)
            if (-not $tracked.Count) { Say 'No Blueprint is tracked. Add: __dwookit.bat bp add <path>'; return }
            Say "tracked ($($tracked.Count)):"
            $tracked | ForEach-Object { Write-Host "  $_" }
            if (Test-Path (Join-Path $BpDir 'export.json')) {
                & python (Join-Path $SystemDir 'tools\lens_bp.py') status --dir $BpDir --project $Project
            } else {
                Say 'Not exported yet: __dwookit.bat bp export'
            }
        }
        'export' { Invoke-BpExport }
        default { Fail "Unknown bp action '$action'. Use: bp add | remove | list | export" }
    }
}

# ---------------------------------------------------------------- native migration (native)
# Blueprint -> C++ base class (tools\lens_native.py), then the Blueprint is rebased onto it by the DwoLensInspector
# commandlet (DwoLensNativeRebase.cpp), which verifies every class default and component value and saves only when
# nothing differs. Steps, editor closed:
#   native plan <bp>                 export it and show what moves to C++, and why the rest stays in Blueprint
#   native generate <bp> [--module M] [--class-name N]
#                                    Source\<Module>\DwoNative\<Name>\ (your code from now on: commit it) and the
#                                    modules it needs in <Module>.Build.cs (a backup is kept)
#   compile                          build the editor target with the new class
#   native rebase <bp> [--dry-run] [--force]
#                                    back up the .uasset, rebase, verify; the backup goes back when it fails
#   native restore <bp>              put the last backup of the .uasset back (and of Build.cs)
#   native verify <bp> [--frames N] [--no-probe] [--probe-offset X:Y:Z] [--call Function@Frame ...]
#                                    behaviour check: runs the rebased Blueprint and its pre-migration backup through
#                                    the same scenario in a headless world (DwoLensBehaviour.cpp) and compares them;
#                                    --probe-offset: where the probe lands, from the actor (cm);
#                                    --call runs an event or function on the actor at that frame (an implementable
#                                    event its C++ parent fires in the game), with default arguments
#   native status                    what was generated and rebased
$NativeDir = Join-Path $ForgeDir 'native'

function Get-NativeArg([string]$Name, [string]$Default = '') {
    $i = [array]::IndexOf(@($Rest), $Name)
    if ($i -ge 0 -and $i + 1 -lt @($Rest).Count) { return $Rest[$i + 1] }
    return $Default
}

function Get-GameModule($UProject) {
    $chosen = Get-NativeArg '--module'
    if ($chosen) { return $chosen }
    $m = @($UProject.Json.Modules | Where-Object { "$($_.Type)" -eq 'Runtime' } | Select-Object -First 1)
    if (-not $m.Count) { Fail 'This project has no runtime C++ module. Add C++ to it first (File > New C++ Class in the editor), then try again.' }
    return $m[0].Name
}

function Get-NativeTarget([string]$Arg) {
    $entry = ConvertTo-BpEntry $Arg
    if ($entry.EndsWith('/')) { Fail 'native works on one Blueprint at a time, not a folder.' }
    if (-not (Test-BpEntryExists $entry)) { Fail "No asset at $entry" }
    $name = Split-Path -Leaf $entry
    return [pscustomobject]@{ Entry = $entry; Name = $name
                              Export = Join-Path $BpDir ($entry.TrimStart('/') + '/' + $name + '.json')
                              Asset = Join-Path $Project ('Content' + $entry.Substring(5).Replace('/', '\') + '.uasset')
                              Map = Join-Path $NativeDir "$name.native.json" }
}

function Update-NativeExport($Target) {
    $tracked = @(Get-BpTracked)
    if ($tracked -notcontains $Target.Entry) { Set-BpTracked ($tracked + $Target.Entry); Say "tracking: $($Target.Entry)" }
    Invoke-BpExport
    if (-not (Test-Path $Target.Export)) { Fail "The export of $($Target.Entry) is missing: $($Target.Export)" }
}

function Invoke-NativeRebase($Target, $UProject) {
    if (-not (Test-Path $Target.Map)) { Fail "Generate first: __dwookit.bat native generate $($Target.Entry)" }
    $engine = Resolve-Engine $UProject.Json.EngineAssociation
    Assert-ModulesBuilt $UProject
    $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
    $backup = Join-Path $NativeDir "backup\$stamp"
    New-Item -ItemType Directory -Force $backup | Out-Null
    Copy-Item $Target.Asset (Join-Path $backup (Split-Path -Leaf $Target.Asset)) -Force
    [IO.File]::WriteAllText((Join-Path $NativeDir "$($Target.Name).last-backup.txt"), $backup, $Utf8)
    $report = Join-Path $NativeDir "$($Target.Name).rebase.json"
    Remove-Item -Force (Join-Path $NativeDir "$($Target.Name).restored.json") -ErrorAction SilentlyContinue
    Remove-Item -Force $report -ErrorAction SilentlyContinue
    $flags = @()
    if ($Rest -contains '--dry-run') { $flags += '-LensDryRun' }
    if ($Rest -contains '--force') { $flags += '-LensForce' }
    $logDir = Join-Path $DataDir 'logs'
    New-Item -ItemType Directory -Force $logDir | Out-Null
    $log = Join-Path $logDir "native-rebase-$($Target.Name).log"
    Say "native rebase: $($Target.Entry) (backup $backup)"
    & cmd /c "`"$($engine.Cmd)`" `"$($UProject.Path)`" -run=DwoLensInspector -EnablePlugins=DwoLensInspector -LensRebase=`"$($Target.Map)`" -LensRebaseReport=`"$report`" $($flags -join ' ') -unattended -nullrhi -nosplash -nopause -NoSound -stdout -FullStdOutLogOutput > `"$log`" 2>&1"
    if (-not (Test-Path $report)) { Fail "The rebase wrote no report (see $log). The Blueprint was not saved." }
    $r = Get-Content -Raw -Encoding UTF8 $report | ConvertFrom-Json
    foreach ($s_ in @($r.steps)) { Write-Host "  $s_" }
    foreach ($p_ in @($r.problems)) { if ($p_) { Write-Host "  [problem] $p_" -ForegroundColor Yellow } }
    $diffs = @($r.diffs)
    foreach ($d in ($diffs | Select-Object -First 20)) { if ($d) { Write-Host "  [differs] $($d.scope) $($d.property): $($d.before) -> $($d.after)" -ForegroundColor Yellow } }
    if ($diffs.Count -gt 20) { Write-Host "  ... $($diffs.Count - 20) more in $report" }
    if ($r.ok -and $r.saved) { Say "native rebase: $($Target.Name) now derives from $($r.class). Backup: $backup"; return }
    if ($r.ok -and $r.dryRun) { Say 'native rebase: dry run passed; nothing was saved. Run it again without --dry-run.'; return }
    # not saved: the file on disk is the original; put the backup back anyway in case a save happened half-way
    Copy-Item (Join-Path $backup (Split-Path -Leaf $Target.Asset)) $Target.Asset -Force
    Fail "native rebase failed; the Blueprint is unchanged (report: $report)."
}

function Invoke-BehaviourRun($UProject, $Engine, [string]$Scenario, [string]$Out, [string]$Log) {
    Remove-Item -Force $Out -ErrorAction SilentlyContinue
    & cmd /c "`"$($Engine.Cmd)`" `"$($UProject.Path)`" -run=DwoLensInspector -EnablePlugins=DwoLensInspector -LensBehave=`"$Scenario`" -LensBehaveOut=`"$Out`" -unattended -nullrhi -nosplash -nopause -NoSound -stdout -FullStdOutLogOutput > `"$Log`" 2>&1"
    if (-not (Test-Path $Out)) { return $false }
    return $true
}

function Invoke-NativeVerify($Target, $UProject) {
    $last = Join-Path $NativeDir "$($Target.Name).last-backup.txt"
    $rb = Join-Path $NativeDir "$($Target.Name).rebase.json"
    if (-not (Test-Path $last) -or -not (Test-Path $rb)) { Fail "Rebase it first: __dwookit.bat native rebase $($Target.Entry)" }
    $r = Get-Content -Raw -Encoding UTF8 $rb | ConvertFrom-Json
    if (-not $r.saved) { Fail "The last rebase of $($Target.Name) was not saved; nothing to compare." }
    $original = Join-Path ((Get-Content -Raw $last).Trim()) (Split-Path -Leaf $Target.Asset)
    if (-not (Test-Path $original)) { Fail "The pre-migration backup is missing: $original" }
    $engine = Resolve-Engine $UProject.Json.EngineAssociation
    Assert-ModulesBuilt $UProject
    $frames = [int](Get-NativeArg '--frames' '90')
    $dir = Join-Path $NativeDir "behaviour\$($Target.Name)"
    New-Item -ItemType Directory -Force $dir | Out-Null
    $scenario = Join-Path $dir 'scenario.json'
    $j = Get-Content -Raw -Encoding UTF8 $Target.Map | ConvertFrom-Json
    $doc = [ordered]@{ blueprint = $j.blueprint; frames = $frames; dt = 0.0333333; recordEvery = 10
                       probe = $(if ($Rest -contains '--no-probe') { 'none' } else { 'character' }); probeFrame = 20; probeOffset = @(0, 0, 0)
                       calls = @() }
    $offset = Get-NativeArg '--probe-offset'
    if ($offset) {
        $o = @($offset -split '[,:]' | ForEach-Object { [double]$_ })
        if ($o.Count -ne 3) { Fail "--probe-offset takes X:Y:Z, for example --probe-offset 0:200:50" }
        $doc.probeOffset = $o
    }
    for ($i = 0; $i -lt @($Rest).Count - 1; $i++) {
        if ($Rest[$i] -ne '--call') { continue }
        $c = "$($Rest[$i + 1])" -split '@'
        if ($c.Count -ne 2 -or $c[1] -notmatch '^\d+$') { Fail "--call takes Function@Frame, for example --call OnBoxDamaged@30" }
        $doc.calls += [ordered]@{ function = $c[0]; frame = [int]$c[1] }
    }
    Write-Json $scenario $doc
    $logDir = Join-Path $DataDir 'logs'
    New-Item -ItemType Directory -Force $logDir | Out-Null
    Say "native verify: $($Target.Name), $frames frames, probe $($doc.probe)"
    $after = Join-Path $dir 'after.json'
    $before = Join-Path $dir 'before.json'
    if (-not (Invoke-BehaviourRun $UProject $engine $scenario $after (Join-Path $logDir "native-verify-after-$($Target.Name).log"))) { Fail "The migrated run wrote no trace (see $logDir)." }
    # the original: its backup in place of the migrated asset for one run, then the migrated asset back no matter what
    $keep = Join-Path $dir 'migrated.uasset'
    Copy-Item $Target.Asset $keep -Force
    try {
        Copy-Item $original $Target.Asset -Force
        $ok = Invoke-BehaviourRun $UProject $engine $scenario $before (Join-Path $logDir "native-verify-before-$($Target.Name).log")
    } finally {
        Copy-Item $keep $Target.Asset -Force
        Remove-Item -Force $keep -ErrorAction SilentlyContinue
    }
    if (-not $ok) { Fail "The original run wrote no trace (see $logDir)." }
    & python (Join-Path $SystemDir 'tools\lens_native.py') compare $before $after --map $Target.Map --out (Join-Path $NativeDir "$($Target.Name).behaviour")
    $code = $LASTEXITCODE
    Say "  report: $(Join-Path $NativeDir "$($Target.Name).behaviour.md")"
    if ($code -ne 0) { exit 1 }
}

function Invoke-Native {
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $action = if ($Rest.Count) { $Rest[0] } else { 'status' }
    $uproject = Get-UProject
    New-Item -ItemType Directory -Force $NativeDir | Out-Null
    $py = Join-Path $SystemDir 'tools\lens_native.py'
    if ($action -eq 'status') {
        $maps = @(Get-ChildItem $NativeDir -Filter '*.native.json' -ErrorAction SilentlyContinue)
        if (-not $maps.Count) { Say 'native: nothing generated yet. Start with: __dwookit.bat native plan <Blueprint>'; return }
        foreach ($m in $maps) {
            $j = Get-Content -Raw -Encoding UTF8 $m.FullName | ConvertFrom-Json
            $rb = Join-Path $NativeDir ($m.Name -replace '\.native\.json$', '.rebase.json')
            $state = if (Test-Path (Join-Path $NativeDir ($m.Name -replace '\.native\.json$', '.restored.json')))  { 'restored' } elseif (Test-Path $rb) { $r = Get-Content -Raw -Encoding UTF8 $rb | ConvertFrom-Json; if ($r.saved) { 'rebased' } elseif ($r.dryRun -and $r.ok) { 'dry run passed' } else { 'rebase failed' } } else { 'generated' }
            Write-Host ("  {0,-34} {1,-30} {2}/{3} nodes  {4}" -f $j.name, $j.class, $j.coverage.migrated, $j.coverage.nodes, $state)
        }
        return
    }
    if ($Rest.Count -lt 2) { Fail "Usage: native $action <Blueprint: /Game/... or Content\...\X.uasset>" }
    $target = Get-NativeTarget $Rest[1]
    switch ($action) {
        'plan' {
            Update-NativeExport $target
            & python $py plan $target.Export --module (Get-GameModule $uproject)
        }
        'generate' {
            Update-NativeExport $target
            $module = Get-GameModule $uproject
            $out = Join-Path $Project "Source\$module\DwoNative\$($target.Name)"
            $pyArgs = @($py, 'generate', $target.Export, '--module', $module, '--out', $out)
            $cls = Get-NativeArg '--class-name'
            if ($cls) { $pyArgs += @('--class-name', $cls) }
            & python @pyArgs
            if ($LASTEXITCODE -ne 0) { Fail 'native generate failed (see above).' }
            Copy-Item (Join-Path $out "$($target.Name).native.json") $target.Map -Force
            $j = Get-Content -Raw -Encoding UTF8 $target.Map | ConvertFrom-Json
            $mods = @($j.modules | Where-Object { $_ })
            if ($mods.Count) {
                $buildCs = Join-Path $Project "Source\$module\$module.Build.cs"
                $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
                $bk = Join-Path $NativeDir "backup\$stamp"
                New-Item -ItemType Directory -Force $bk | Out-Null
                Copy-Item $buildCs (Join-Path $bk "$module.Build.cs") -Force
                & python $py modules $buildCs ($mods -join ',')
            }
            Say "native generate: $out"
            Say "  report: $(Join-Path $out "$($target.Name).native.md")"
            Say "  next: __dwookit.bat compile (editor closed), then __dwookit.bat native rebase $($target.Entry) [--dry-run]"
        }
        'rebase' { Invoke-NativeRebase $target $uproject }
        'verify' { Invoke-NativeVerify $target $uproject }
        'restore' {
            $last = Join-Path $NativeDir "$($target.Name).last-backup.txt"
            if (-not (Test-Path $last)) { Fail "No backup of $($target.Name)." }
            $dir = (Get-Content -Raw $last).Trim()
            Copy-Item (Join-Path $dir (Split-Path -Leaf $target.Asset)) $target.Asset -Force
            $rb = Join-Path $NativeDir "$($target.Name).rebase.json"
            if (Test-Path $rb) { Move-Item -Force $rb (Join-Path $NativeDir "$($target.Name).restored.json") }
            Say "native restore: $($target.Asset) is back as it was before the rebase ($dir). The generated C++ stays; delete Source\...\DwoNative\$($target.Name) yourself if you no longer want it."
        }
        default { Fail "Unknown native action '$action'. Use: native plan | generate | rebase | verify | restore | status" }
    }
}

# ---------------------------------------------------------------- agent context (agent)
# __dwookit\context\AGENT.md tells coding agents what dwookit knows and how to work here (read the tracked
# Blueprints' pseudocode cheaply, deliver C++ or an editor checklist, short output).
# .github\instructions\project.instructions.md is the user's own (created as an empty shell, never overwritten).
# CLAUDE.local.md (a managed block) and .github\instructions\dwookit.instructions.md make Claude Code and VS
# Code Copilot read both. Uninstall backs the user's file up before removing it. Rewritten on connect, update, analyze and bp export.
# Instruction files and skills from the dwookit library (tools\lens_library.py) go to the agents the project picked
# (Claude Code, GitHub Copilot): connect --instructions a,b --skills x --targets claude,copilot, or 'agent use', or the
# GUI. Without a choice a newly connected project gets the library defaults.
function Get-SelectionArgs {
    $out = @()
    foreach ($k in @('--instructions', '--skills', '--targets')) {
        $i = [array]::IndexOf(@($Rest), $k)
        if ($i -ge 0 -and $i + 1 -lt @($Rest).Count) { $out += @($k, $Rest[$i + 1]) }
    }
    return $out
}

function Get-InstalledPaths {
    # Files and folders written outside __dwookit for the library selection (lens_agent.py keeps the list).
    $f = Join-Path $ForgeDir 'context\installed.json'
    if (-not (Test-Path $f)) { return @() }
    try { return @((Get-Content -Raw -Encoding UTF8 $f | ConvertFrom-Json).paths) } catch { return @() }
}

function Update-AgentContext([switch]$Selection) {
    if (-not (Read-State)) { return }
    $sel = if ($Selection) { @(Get-SelectionArgs) } else { @() }
    $script = Join-Path $SystemDir 'tools\lens_agent.py'
    if ($sel.Count) { & python $script use --project $Project @sel }
    else { & python $script write --project $Project }
    if ($LASTEXITCODE -ne 0) { Say 'agent: writing the agent context failed (see above).' }
    $null = Set-ExcludeBlock          # after the write: the block lists what the selection put outside __dwookit
}

function Invoke-Library {
    # The library needs no project: <dwookit>\library (git-ignored). list, add-instruction, add-skill, remove, defaults.
    $pyArgs = @((Join-Path $SystemDir 'tools\lens_library.py')) + @($Rest)
    if (-not $Rest.Count) { $pyArgs += 'list' }
    & python @pyArgs
    exit $LASTEXITCODE
}

function Invoke-Agent {
    $action = if ($Rest.Count) { $Rest[0] } else { 'refresh' }
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $script = Join-Path $SystemDir 'tools\lens_agent.py'
    switch ($action) {
        'refresh' { Update-AgentContext; Say "agent: $(Join-Path $ForgeDir 'context\AGENT.md') (your instructions: $(Join-Path $Project '.github\instructions\project.instructions.md'))" }
        'status'  { & python $script status --project $Project }
        'on'      { $null = Set-ExcludeBlock; & python $script on --project $Project }
        'off'     { & python $script off --project $Project; Say 'agent: pointer files removed; AGENT.md stays in __dwookit\context.' }
        'use'     { $sel = @(Get-SelectionArgs); & python $script use --project $Project @sel; $null = Set-ExcludeBlock }
        default   { Fail "Unknown agent action '$action'. Use: agent [refresh] | status | on | off | use [--instructions a,b|none] [--skills x|none] [--targets claude,copilot|none]" }
    }
}

# ---------------------------------------------------------------- Live Lens (live)
# The DwoLensInspector plugin also runs inside the editor (DwoLensLive.cpp) and collects raw data per editor
# session; after the editor closes, live-ingest merges it into __dwookit\live\base and analyses it like
# 'analyze' (a run with source 'live'). Settings: __dwookit\live-lens.json.
$LiveDir = Join-Path $ForgeDir 'live'
$LiveConfigPath = Join-Path $ForgeDir 'live-lens.json'
$LiveTimings = @('changes', 'exit', 'save')
$LiveKeeps = @('latest', 'history', 'snapshot')
$LiveCollect = @('structure', 'logs', 'pie', 'work')

function Get-LiveConfig {
    $c = [ordered]@{ enabled = $false; timing = 'changes'; keep = 'history'
                     collect = [ordered]@{ structure = $true; logs = $true; pie = $true; work = $true } }
    if (Test-Path $LiveConfigPath) {
        try {
            $j = Get-Content -Raw -Encoding UTF8 $LiveConfigPath | ConvertFrom-Json
            if ($j.PSObject.Properties['enabled']) { $c.enabled = [bool]$j.enabled }
            if ($LiveTimings -contains "$($j.timing)") { $c.timing = "$($j.timing)" }
            if ($LiveKeeps -contains "$($j.keep)") { $c.keep = "$($j.keep)" }
            if ($j.collect) { foreach ($k in $LiveCollect) { if ($j.collect.PSObject.Properties[$k]) { $c.collect[$k] = [bool]$j.collect.$k } } }
        } catch { }
    }
    return $c
}

function Test-LiveEnabled { return [bool](Get-LiveConfig).enabled }

function Set-LivePluginEnabled([bool]$On) {
    $path = Join-Path $PluginDest 'DwoLensInspector.uplugin'
    if (-not (Test-Path $path)) { return $false }
    $u = Get-Content -Raw -Encoding UTF8 $path | ConvertFrom-Json
    $u | Add-Member -NotePropertyName EnabledByDefault -NotePropertyValue $On -Force
    [IO.File]::WriteAllText($path, ($u | ConvertTo-Json -Depth 10), $Utf8)
    return $true
}

function Invoke-Live {
    $action = if ($Rest.Count) { $Rest[0] } else { 'status' }
    if (-not (Read-State)) { Fail 'Not connected. Connect the project first: __dwookit.bat connect' }
    $c = Get-LiveConfig
    # options: --timing changes|exit|save  --keep latest|history|snapshot  --collect structure,logs,pie,work
    for ($i = 1; $i -lt $Rest.Count; $i++) {
        switch ($Rest[$i]) {
            '--timing'  { $v = $Rest[++$i]; if ($LiveTimings -notcontains $v) { Fail "--timing: $($LiveTimings -join ' | ')" }; $c.timing = $v }
            '--keep'    { $v = $Rest[++$i]; if ($LiveKeeps -notcontains $v) { Fail "--keep: $($LiveKeeps -join ' | ')" }; $c.keep = $v }
            '--collect' { $v = @("$($Rest[++$i])".Split(',') | Where-Object { $_ }); foreach ($k in $LiveCollect) { $c.collect[$k] = $v -contains $k } }
        }
    }
    switch ($action) {
        'on' {
            if (-not (Test-Path (Join-Path $PluginDest 'DwoLensInspector.uplugin'))) { Fail 'DwoLens is not installed. Install it first: __dwookit.bat tool install lens' }
            $c.enabled = $true
            Write-Json $LiveConfigPath $c
            $null = Set-LivePluginEnabled $true
            Say "Live Lens on (collect: $(@($LiveCollect | Where-Object { $c.collect[$_] }) -join ', '); timing: $($c.timing); keep: $($c.keep))."
            Say 'Restart the Unreal Editor. Every editor run is collected; results are processed in the background after it closes.'
            if (-not (Test-Path (Join-Path $LiveDir 'base\meta.json'))) { Say 'The first session collects everything when the editor closes (this takes longer once).' }
        }
        'off' {
            $c.enabled = $false
            Write-Json $LiveConfigPath $c
            $null = Set-LivePluginEnabled $false
            Say 'Live Lens off. The editor stops loading DwoLens after its next restart; the cache stays.'
        }
        'config' { Write-Json $LiveConfigPath $c; Say "Live Lens settings saved (timing: $($c.timing); keep: $($c.keep)). They apply from the next editor start." }
        'rebuild' {
            Remove-Item -Recurse -Force (Join-Path $LiveDir 'base') -ErrorAction SilentlyContinue
            Say 'Live Lens cache dropped: the next editor session collects everything again.'
        }
        'status' {
            Say "Live Lens: $(if ($c.enabled) { 'on' } else { 'off' })  timing=$($c.timing)  keep=$($c.keep)  collect=$(@($LiveCollect | Where-Object { $c.collect[$_] }) -join ',')"
            Say "cache: $(if (Test-Path (Join-Path $LiveDir 'base\meta.json')) { 'ready' } else { 'empty (next session collects everything)' })"
            $sessions = @(Get-ChildItem (Join-Path $LiveDir 'sessions') -Directory -ErrorAction SilentlyContinue | Sort-Object Name -Descending | Select-Object -First 8)
            foreach ($d in $sessions) {
                $j = $null
                if (Test-Path (Join-Path $d.FullName 'session.json')) { $j = Get-Content -Raw -Encoding UTF8 (Join-Path $d.FullName 'session.json') | ConvertFrom-Json }
                $state = if (-not $j) { 'running' } elseif ($j.ingested) { "-> $($j.ingested)" } elseif ($j.ended -eq 'crashed') { 'crashed' } else { 'waiting for processing' }
                Write-Host ("  {0}  {1}  collected={2}  exit={3:N1}s  {4}" -f $d.Name, $(if ($j) { $j.ended } else { '' }), $(if ($j) { $j.collected } else { '' }), $(if ($j -and $j.exitSeconds) { [double]$j.exitSeconds } else { 0 }), $state)
            }
        }
        default { Fail "Unknown live action '$action'. Use: live on | off | status | config | rebuild  [--timing changes|exit|save] [--keep latest|history|snapshot] [--collect structure,logs,pie,work]" }
    }
}

function Invoke-LiveIngest {
    # Started by the editor plugin at exit (hidden, detached): wait for the editor, then process every session.
    $waitPid = 0
    for ($i = 0; $i -lt $Rest.Count; $i++) { if ($Rest[$i] -eq '--wait-pid') { $waitPid = [int]$Rest[++$i] } }
    New-Item -ItemType Directory -Force $LiveDir | Out-Null
    $log = Join-Path $LiveDir 'ingest.log'
    $lock = Join-Path $LiveDir 'ingest.lock'
    try { $lockStream = [IO.File]::Open($lock, 'OpenOrCreate', 'ReadWrite', 'None') } catch { Say 'live-ingest: already running.'; return }
    try {
        Start-Transcript -Path $log -Append | Out-Null
        if ($waitPid) { try { Wait-Process -Id $waitPid -Timeout 900 -ErrorAction Stop } catch { } }
        $c = Get-LiveConfig
        $uproject = Get-UProject
        $engine = Resolve-Engine $uproject.Json.EngineAssociation
        $sessions = @(Get-ChildItem (Join-Path $LiveDir 'sessions') -Directory -ErrorAction SilentlyContinue | Sort-Object Name)
        foreach ($d in $sessions) {
            $sj = Join-Path $d.FullName 'session.json'
            if (-not (Test-Path $sj)) { continue }                       # still running (or crashed and not yet closed)
            $summary = Get-Content -Raw -Encoding UTF8 $sj | ConvertFrom-Json
            if ($summary.PSObject.Properties['ingested'] -and $summary.ingested) { continue }
            Say "live-ingest: session $($d.Name) ($($summary.ended))"
            $base = Join-Path $LiveDir 'base'
            & python (Join-Path $SystemDir 'tools\lens_live.py') merge --session $d.FullName --base $base
            $merged = $LASTEXITCODE
            if ($merged -ne 0 -or -not (Test-Path (Join-Path $base 'meta.json'))) {
                # nothing structural to process (crashed session, structure off): keep the activity record only
                $summary | Add-Member -NotePropertyName ingested -NotePropertyValue 'activity-only' -Force
                Write-Json $sj $summary
                continue
            }
            $startedAt = Get-Date
            $runId = 'run-' + $startedAt.ToString('yyyyMMdd-HHmmss')
            $runDir = Join-Path $DataDir "runs\$runId"
            $rawDir = Join-Path $runDir 'raw'
            $outDir = Join-Path $runDir 'out'
            New-Item -ItemType Directory -Force $rawDir, $outDir | Out-Null
            Copy-Item (Join-Path $base '*') $rawDir
            $sw = [Diagnostics.Stopwatch]::StartNew()
            & python (Join-Path $SystemDir 'tools\lens_analyze.py') --raw $rawDir --project $uproject.Path --out $outDir --run-id $runId --config @(Get-ConfigFiles)
            $pyCode = $LASTEXITCODE
            $sw.Stop()
            $verdict = 'fail'
            $findings = Join-Path $outDir 'findings.json'
            if ($pyCode -eq 0 -and (Test-Path $findings)) { $verdict = (Get-Content -Raw -Encoding UTF8 $findings | ConvertFrom-Json).verdict }
            $state = Read-State
            Write-Json (Join-Path $runDir 'run.json') ([ordered]@{
                schema = 'lens.run/0.1'; runId = $runId; line = 'architecture-report'; source = 'live'; session = $d.Name
                project = $state.project; engineVersion = $engine.Version; systemCommit = Get-SystemCommit
                startedAt = $startedAt.ToString('o'); finishedAt = (Get-Date).ToString('o'); final = $verdict
                stations = @([ordered]@{ id = 'ST-1'; name = "collect (Live Lens, $($summary.collected))"; verdict = 'pass'; ms = [int]([double]$summary.exitSeconds * 1000) },
                             [ordered]@{ id = 'ST-2'; name = 'analyze+gate'; verdict = $verdict; exitCode = $pyCode; ms = $sw.ElapsedMilliseconds })
            })
            Complete-RunOutputs $runDir
            & python (Join-Path $SystemDir 'tools\lens_live.py') accumulate --project $Project --run $runDir --session $d.FullName --keep $c.keep
            # the flow again, now with this session's play-tested classes (runtime.json was just updated)
            & python (Join-Path $SystemDir 'tools\lens_flow.py') --raw $rawDir --out $outDir --project $Project | Out-Null
            & python (Join-Path $SystemDir 'tools\lens_report.py') --out $outDir --project $Project | Out-Null
            Invoke-BpMarkdown
            Update-AgentContext
            Say "live-ingest: $runId ($verdict)"
        }
        # session logs and crashes, compared with the previous logs run (its own process: it may stop on errors)
        if ($c.collect.logs -and $sessions.Count) {
            & powershell -NoProfile -ExecutionPolicy Bypass -File (Join-Path $SystemDir 'tools\dwookit.ps1') -Command logs -Project $Project
        }
    } finally {
        try { Stop-Transcript | Out-Null } catch { }
        $lockStream.Dispose()
    }
}

# ---------------------------------------------------------------- uninstall
# One command removes everything dwookit put into the project or caused there. Nothing has to be removed
# first (no sm-remove / canvas-remove step). It works in two passes: a scan that lists every trace, grouped by
# what the user has to decide, then the removal. The scan is also printed as JSON (uninstall --plan-json) for
# the GUI dialog.

# Plugin and module names dwookit provides now or provided before (dwo-Lens 1.x, DesignBridge).
$OurPlugins = @('DwoStateMonitor', 'StateMonitor', 'DwoCanvas', 'DesignBridge', 'DwoLensInspector')
$OurModules = @('DwoStateMonitor', 'StateMonitor', 'DwoCanvasRuntime', 'DwoCanvasEditor', 'DesignBridgeRuntime', 'DesignBridgeEditor', 'DwoLensInspector')
# Folders only dwookit tools write: reports, logs and test leftovers. Removed with the run history choice.
$OurArtifacts = @('Saved\DwoCanvas', 'Saved\DwoCanvasTests', 'Saved\DesignBridge', 'Saved\DesignBridgeTests',
                  'Content\DwoCanvasTests', 'Content\DesignBridgeTests')
# Import records: committed next to generated widgets, so they follow the generated-widgets choice.
$OurRecords = @('DwoCanvas\Records', 'DesignBridge\Records')
# Project settings files the plugins create when their settings page is edited.
$OurConfigs = @('Config\DefaultDwoCanvas.ini', 'Config\DefaultDesignBridge.ini')

function Get-ForeignNames {
    # Names that belong to the project itself (its own copy of a plugin) must never be touched.
    $skip = @()
    if (Find-OwnStateMonitor) { $skip += @('DwoStateMonitor', 'StateMonitor') }
    if ((Get-CanvasState) -eq 'own') { $skip += @('DwoCanvas', 'DwoCanvasRuntime', 'DwoCanvasEditor') }
    return $skip
}

function Get-ProjectCodeFiles([string[]]$Include) {
    # The project's own code: Source\ and plugins that are not dwookit installs.
    $files = @()
    foreach ($dir in @('Source', 'Plugins')) {
        $path = Join-Path $Project $dir
        if (-not (Test-Path $path)) { continue }
        $files += Get-ChildItem -Path $path -Recurse -File -Include $Include -ErrorAction SilentlyContinue |
                  Where-Object { $_.FullName -notlike '*\Plugins\__*' -and $_.FullName -notmatch '\\(Intermediate|Binaries)\\' }
    }
    return $files
}

function Find-UprojectEntries([string[]]$Plugins) {
    $files = @(Get-ChildItem -Path $Project -Filter *.uproject -File)
    if ($files.Count -ne 1) { return @() }
    $json = Get-Content -Raw -Encoding UTF8 $files[0].FullName | ConvertFrom-Json
    return @($json.Plugins | Where-Object { $Plugins -contains $_.Name } | ForEach-Object { $_.Name })
}

function Find-BuildCsDeps([string[]]$Modules) {
    # "Module" strings in Build.cs files (dependency lists). One entry per file and module.
    $hits = @()
    foreach ($f in (Get-ProjectCodeFiles @('*.Build.cs'))) {
        $text = [IO.File]::ReadAllText($f.FullName)
        foreach ($m in $Modules) {
            if ($f.Name -eq "$m.Build.cs") { continue }
            if ($text -match ('"' + [regex]::Escape($m) + '"')) {
                $hits += [pscustomobject]@{ File = $f.FullName.Substring($Project.Length + 1); Module = $m }
            }
        }
    }
    return $hits
}

function Find-CodeUses([string[]]$Skip) {
    # Includes and types of the plugins in project code. These cannot be removed automatically.
    $patterns = @()
    if ($Skip -notcontains 'DwoStateMonitor') { $patterns += @('StateMonitor\w*\.h"', '\b[UFS]StateMonitor\w+') }
    if ($Skip -notcontains 'DwoCanvas') { $patterns += @('DwoCanvas\w*\.h"', '\b[UFIS]DwoCanvas\w+', 'DesignBridge\w*\.h"', '\b[UFIS]DesignBridge\w+') }
    if ($patterns.Count -eq 0) { return @() }
    $regex = ($patterns -join '|')
    $hits = @()
    foreach ($f in (Get-ProjectCodeFiles @('*.h', '*.hpp', '*.cpp', '*.inl'))) {
        foreach ($m in (Select-String -Path $f.FullName -Pattern $regex)) {
            $hits += "$($f.FullName.Substring($Project.Length + 1)):$($m.LineNumber): $($m.Line.Trim())"
        }
    }
    return $hits
}

function Find-GeneratedWidgets {
    # Assets made by canvas-import: they reference the DwoCanvasRuntime (or older DesignBridgeRuntime) module.
    if ((Get-CanvasState) -eq 'own') { return @() }
    $content = Join-Path $Project 'Content'
    if (-not (Test-Path $content)) { return @() }
    $users = New-Object System.Collections.Generic.List[string]
    $latin1 = [Text.Encoding]::GetEncoding(28591)
    foreach ($f in (Get-ChildItem -Path $content -Recurse -File -Include *.uasset, *.umap -ErrorAction SilentlyContinue)) {
        if ($f.FullName -match '\\Content\\(DwoCanvasTests|DesignBridgeTests)\\') { continue }   # test leftovers, removed anyway
        # Images the importer created: T_DB_<first 16 hex of the content hash> (DwoCanvasImporter.cpp).
        if ($f.Name -cmatch '^T_DB_[0-9A-F]{16}\.uasset$') { $users.Add($f.FullName.Substring($Project.Length + 1)); continue }
        $stream = [IO.File]::OpenRead($f.FullName)
        try {
            $buffer = New-Object byte[] ([Math]::Min($stream.Length, 8MB))
            $read = $stream.Read($buffer, 0, $buffer.Length)
        } finally { $stream.Dispose() }
        $head = $latin1.GetString($buffer, 0, $read)
        if ($head.Contains('DwoCanvasRuntime') -or $head.Contains('DesignBridgeRuntime')) { $users.Add($f.FullName.Substring($Project.Length + 1)) }
    }
    # Widgets an import made without DwoCanvas widgets inside (plain UMG) are known only from the import records.
    foreach ($dir in $OurRecords) {
        $records = Join-Path $Project $dir
        if (-not (Test-Path $records)) { continue }
        foreach ($r in (Get-ChildItem $records -Recurse -File -Filter *.json -ErrorAction SilentlyContinue)) {
            try { $asset = (Get-Content -Raw -Encoding UTF8 $r.FullName | ConvertFrom-Json).asset } catch { continue }
            if ("$asset" -notlike '/Game/*') { continue }
            $rel = 'Content\' + ("$asset".Substring(6) -replace '/', '\') + '.uasset'
            if ((Test-Path (Join-Path $Project $rel)) -and -not $users.Contains($rel)) { $users.Add($rel) }
        }
    }
    return $users.ToArray()
}

function Get-EditorIniRegex {
    # Module time stamps / settings keys, and the plugin browser's "seen plugins" records.
    $keys = '^(' + (($OurModules + @('DwoCanvasSettings', 'DesignBridgeSettings')) -join '|') + ')[.=]'
    $seen = '^InstalledPlugins=(' + ($OurPlugins -join '|') + ')\s*$'
    return "$keys|$seen"
}

function Find-EditorIniLines {
    # Per-user editor settings the engine writes for loaded plugin modules (compile time stamps, panel state).
    $hits = @()
    $saved = Join-Path $Project 'Saved\Config'
    if (-not (Test-Path $saved)) { return @() }
    $regex = Get-EditorIniRegex
    foreach ($f in (Get-ChildItem $saved -Recurse -File -Filter *.ini -ErrorAction SilentlyContinue)) {
        $n = @([IO.File]::ReadAllLines($f.FullName) | Where-Object { $_ -match $regex }).Count
        if ($n -gt 0) { $hits += [pscustomobject]@{ File = $f.FullName.Substring($Project.Length + 1); Lines = $n } }
    }
    return $hits
}

function Get-FolderBytes([string]$Path) {
    if (-not (Test-Path $Path)) { return 0 }
    return [long](Get-ChildItem $Path -Recurse -File -Force -ErrorAction SilentlyContinue | Measure-Object Length -Sum).Sum
}

function Get-AgentPointers {
    # Agent pointer files written by lens_agent.py (only dwookit's: a block or the managed marker inside).
    $found = @()
    $claude = Join-Path $Project 'CLAUDE.local.md'
    if ((Test-Path $claude) -and ([IO.File]::ReadAllText($claude) -like '*>>> dwookit managed (removed by*')) { $found += 'CLAUDE.local.md (block)' }
    $copilot = Join-Path $Project '.github\instructions\dwookit.instructions.md'
    if ((Test-Path $copilot) -and ([IO.File]::ReadAllText($copilot) -like '*dwookit managed: removed by*')) { $found += '.github\instructions\dwookit.instructions.md' }
    $user = Join-Path $Project '.github\instructions\project.instructions.md'
    if ((Test-Path $user) -and ([IO.File]::ReadAllText($user) -like '*<!-- dwookit: project instructions*')) { $found += '.github\instructions\project.instructions.md (yours: backed up, then removed)' }
    foreach ($rel in @(Get-InstalledPaths)) { if (Test-Path (Join-Path $Project $rel)) { $found += ($rel -replace '/', '\') } }
    return $found
}

function Get-UninstallScan {
    $skip = @(Get-ForeignNames)
    $plugins = @($OurPlugins | Where-Object { $skip -notcontains $_ })
    $modules = @($OurModules | Where-Object { $skip -notcontains $_ })
    $runs = @()
    $runsDir = Join-Path $DataDir 'runs'
    if (Test-Path $runsDir) { $runs = @(Get-ChildItem $runsDir -Directory) }
    $created = @()
    if (Test-Path $LedgerPath) {
        $created = @(Get-Content -Encoding UTF8 $LedgerPath | Where-Object { $_ } | Select-Object -Unique |
                     Where-Object { Test-Path (Join-Path $Project $_) })
    }
    $canvas = Get-CanvasState
    $installed = @(@($PluginDest, $SmDest, $SmLegacyDest) | Where-Object { Test-Path $_ })
    if ($canvas -eq 'managed') { $installed += @(@($CanvasDest, $CanvasOldDest) | Where-Object { Test-Path $_ }) }
    return [pscustomobject]@{
        Plugins     = @($installed | ForEach-Object { $_.Substring($Project.Length + 1) })
        Launchers   = @(@(@($LauncherPath) + @($LegacyGens | ForEach-Object { $_.Launcher })) | Where-Object { Test-Path $_ } | ForEach-Object { Split-Path -Leaf $_ })
        Exclude     = $(if ((Test-BlockPresent) -or (Test-BlockPresent -Legacy)) { (Get-Vcs).Exclude } else { $null })
        Runs        = $runs.Count
        DataBytes   = (Get-FolderBytes $DataDir)
        Created     = $created
        Artifacts   = @($OurArtifacts | Where-Object { Test-Path (Join-Path $Project $_) })
        Records     = @($OurRecords | Where-Object { Test-Path (Join-Path $Project $_) })
        Generated   = @(Find-GeneratedWidgets)
        Uproject    = @(Find-UprojectEntries $plugins)
        BuildCs     = @(Find-BuildCsDeps $modules)
        Configs     = @($OurConfigs | Where-Object { Test-Path (Join-Path $Project $_) })
        EditorIni   = @(Find-EditorIniLines)
        CodeUses    = @(Find-CodeUses $skip)
        Agent       = @(Get-AgentPointers)
        SkipNames   = $skip
    }
}

function Write-UninstallScan($Scan) {
    Say 'uninstall: removes everything dwookit added to this project or created in it'
    Write-Host "  always removed"
    Write-Host "    plugins          : $(if ($Scan.Plugins.Count) { $Scan.Plugins -join ', ' } else { 'none' })"
    Write-Host "    launcher, state  : $((@($Scan.Launchers) + @('__dwookit\')) -join ', ')"
    Write-Host "    git exclude      : $(if ($Scan.Exclude) { "dwookit block in $($Scan.Exclude)" } else { 'none' })"
    Write-Host "    agent pointers   : $(if ($Scan.Agent.Count) { $Scan.Agent -join ', ' } else { 'none' })"
    Write-Host "    test leftovers   : $(@($Scan.Artifacts | Where-Object { $_ -like 'Content\*' -or $_ -like '*Tests' }) -join ', ')"
    Write-Host "  project files that name dwookit plugins (backed up, then cleaned)"
    Write-Host "    .uproject        : $(if ($Scan.Uproject.Count) { $Scan.Uproject -join ', ' } else { 'none' })"
    Write-Host "    Build.cs         : $(if ($Scan.BuildCs.Count) { ($Scan.BuildCs | ForEach-Object { "$($_.Module) in $($_.File)" }) -join '; ' } else { 'none' })"
    Write-Host "    settings files   : $(if ($Scan.Configs.Count) { $Scan.Configs -join ', ' } else { 'none' })"
    Write-Host "    editor settings  : $(if ($Scan.EditorIni.Count) { ($Scan.EditorIni | ForEach-Object { "$($_.Lines) line(s) in $($_.File)" }) -join '; ' } else { 'none' })"
    Write-Host "  your choice"
    Write-Host ("    run history      : {0} runs, {1:N1} MB, reports in {2}, {3} other file(s)" -f $Scan.Runs, ($Scan.DataBytes / 1MB),
                $(if ($Scan.Artifacts | Where-Object { $_ -eq 'Saved\DwoCanvas' -or $_ -eq 'Saved\DesignBridge' }) { 'Saved\DwoCanvas' } else { '-' }), $Scan.Created.Count)
    Write-Host "    generated widgets: $($Scan.Generated.Count) asset(s) made by canvas-import$(if ($Scan.Records.Count) { ", records in $($Scan.Records -join ', ')" })"
    $Scan.Generated | Select-Object -First 10 | ForEach-Object { Write-Host "        $_" }
    if ($Scan.CodeUses.Count) {
        Write-Host "  project code that uses the plugins (cannot be removed automatically; the build fails until you edit it)" -ForegroundColor Yellow
        $Scan.CodeUses | Select-Object -First 15 | ForEach-Object { Write-Host "    $_" -ForegroundColor Yellow }
    }
}

function Read-Choice([string]$Flag1, [string]$Flag2, [string]$Question, [string]$Default) {
    # $Flag1 / $Flag2 answer the question on the command line. Returns 1 or 2.
    if ($Rest -contains $Flag1) { return 1 }
    if ($Rest -contains $Flag2) { return 2 }
    $interactive = [Environment]::UserInteractive -and -not [Console]::IsInputRedirected
    if (-not $interactive) { return [int]$Default }
    while ($true) {
        $answer = Read-Host "[dwookit] $Question"
        if ($answer -match '^1') { return 1 }
        if ($answer -match '^2') { return 2 }
    }
}

function Backup-ProjectFile([string]$Relative, [string]$BackupDir) {
    $target = Join-Path $BackupDir $Relative
    New-Item -ItemType Directory -Force (Split-Path -Parent $target) | Out-Null
    Copy-Item -LiteralPath (Join-Path $Project $Relative) $target -Force
}

function Remove-UprojectEntries([string[]]$Names) {
    # Removes the plugin objects from the Plugins array as text, so the rest of the file keeps its formatting.
    $file = @(Get-ChildItem -Path $Project -Filter *.uproject -File)[0]
    $text = [IO.File]::ReadAllText($file.FullName)
    foreach ($n in $Names) {
        $obj = '\{[^{}]*?"Name"\s*:\s*"' + [regex]::Escape($n) + '"[^{}]*\}'
        $before = $text
        $text = [regex]::Replace($text, ',\s*' + $obj, '')          # not the first entry
        if ($text -eq $before) { $text = [regex]::Replace($text, $obj + '\s*,\s*', '') }   # first of several
        if ($text -eq $before) { $text = [regex]::Replace($text, $obj, '') }               # the only entry
    }
    # An empty Plugins array goes away completely.
    $text = [regex]::Replace($text, ',\s*"Plugins"\s*:\s*\[\s*\]', '')
    $text = [regex]::Replace($text, '"Plugins"\s*:\s*\[\s*\]\s*,?', '')
    $null = $text | ConvertFrom-Json   # stop with an error rather than write a broken .uproject
    Write-ProjectText $file.FullName $text
}

function Write-ProjectText([string]$Path, [string]$Text) {
    # Project files keep their encoding: the engine writes .uproject with a UTF-8 BOM, and dropping it would show
    # up as a change in the user's version control.
    $bytes = [IO.File]::ReadAllBytes($Path)
    $bom = $bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF
    [IO.File]::WriteAllText($Path, $Text, (New-Object System.Text.UTF8Encoding($bom)))
}

function Remove-BuildCsDep([string]$Relative, [string]$Module) {
    # Removes "Module" from dependency lists: as a list item ("A", "Module", "B") or a single Add("Module") call.
    $path = Join-Path $Project $Relative
    $text = [IO.File]::ReadAllText($path)
    $q = [regex]::Escape('"' + $Module + '"')
    $text = [regex]::Replace($text, '(?m)^[ \t]*[A-Za-z]+DependencyModuleNames\.Add\(\s*' + $q + '\s*\);[ \t]*\r?\n', '')
    $text = [regex]::Replace($text, '(?m)^[ \t]*' + $q + '[ \t]*,?[ \t]*(//[^\r\n]*)?\r?\n', '')   # one item per line
    $text = [regex]::Replace($text, '\s*,\s*' + $q, '')                                           # "A", "Module"
    $text = [regex]::Replace($text, $q + '\s*,\s*', '')                                           # "Module", "B"
    $text = [regex]::Replace($text, '\{\s*' + $q + '\s*\}', '{ }')                               # { "Module" }
    Write-ProjectText $path $text
}

function Remove-EditorIniLines($Hits) {
    $regex = Get-EditorIniRegex
    foreach ($h in $Hits) {
        $path = Join-Path $Project $h.File
        $lines = @([IO.File]::ReadAllLines($path) | Where-Object { $_ -notmatch $regex })
        Write-ProjectText $path (($lines -join "`r`n") + "`r`n")
    }
}

function Remove-EmptyParents([string]$Path) {
    $dir = Split-Path -Parent $Path
    while ($dir -and $dir.Length -gt $Project.Length -and (Test-Path $dir) -and -not (Get-ChildItem -Force $dir)) {
        Remove-Item -Force $dir
        $dir = Split-Path -Parent $dir
    }
}

function Move-OrDelete([string]$Relative, [bool]$Keep, [string]$KeepDir) {
    $path = Join-Path $Project $Relative
    if (-not (Test-Path -LiteralPath $path)) { return }
    if ($Keep) {
        $target = Join-Path $KeepDir $Relative
        New-Item -ItemType Directory -Force (Split-Path -Parent $target) | Out-Null
        try { Move-Item -LiteralPath $path $target -Force -ErrorAction Stop }
        catch { Fail "Cannot move $Relative`: $($_.Exception.Message) Close the Unreal Editor and try again." }
    } else {
        if (Test-Path -LiteralPath $path -PathType Container) { Remove-Folder $path $Relative }
        else {
            try { Remove-Item -LiteralPath $path -Force -ErrorAction Stop }
            catch { Fail "Cannot remove $Relative`: $($_.Exception.Message) Close the Unreal Editor and try again." }
        }
    }
    Remove-EmptyParents $path
}

function Remove-LegacyLeftovers {
    # Earlier names that a migration in progress may have left (an empty __dwo-forge or __dwo-lens, a launcher).
    foreach ($g in $LegacyGens) {
        if (Test-Path $g.Dir) {
            Get-ChildItem $g.Dir -Recurse -Force -File | ForEach-Object { if ($_.IsReadOnly) { $_.IsReadOnly = $false } }
            Remove-Item -Recurse -Force $g.Dir
        }
        # The old launcher deletes itself when it is the one running this uninstall.
        if (-not $g.FromBat -and (Test-OurLauncher $g.Launcher)) { Remove-Item -Force $g.Launcher }
    }
}

function Test-AnythingLeft($Scan) {
    return ($Scan.Plugins.Count + $Scan.Launchers.Count + $Scan.Artifacts.Count + $Scan.Records.Count + $Scan.Generated.Count +
            $Scan.Uproject.Count + $Scan.BuildCs.Count + $Scan.Configs.Count + $Scan.EditorIni.Count + $Scan.Runs + $Scan.Created.Count +
            $Scan.Agent.Count) -gt 0 -or
           $Scan.Exclude -or (Test-Path $ForgeDir) -or [bool]@($LegacyGens | Where-Object { Test-Path $_.Dir }).Count
}

function Invoke-Uninstall {
    $state = Read-State
    $scan = Get-UninstallScan
    if ($Rest -contains '--plan-json') {
        # For the GUI dialog: the scan only, nothing is changed.
        [Console]::OutputEncoding = [Text.Encoding]::UTF8
        Write-Output ($scan | ConvertTo-Json -Depth 5 -Compress)
        return
    }
    if (-not (Test-AnythingLeft $scan)) {
        Say 'dwookit is not attached and left nothing in this project.'
        if ($env:DWOOKIT_FROM_BAT -ne '1' -and (Test-Path $LauncherPath)) { Remove-Item -Force $LauncherPath }
        return
    }
    Write-UninstallScan $scan

    # Decide everything before changing anything, so an answer of "no" leaves the project as it was.
    if ($scan.CodeUses.Count -gt 0 -and -not ($Rest -contains '--force')) {
        $go = Read-Choice '--force' '--no-force' 'Project code still uses the plugins. Uninstall anyway? 1 = yes, 2 = no' '2'
        if ($go -ne 1) { Fail 'Uninstall stopped. Edit the code listed above (or run again with --force).' }
    }
    $hasHistory = $scan.Runs -gt 0 -or $scan.Created.Count -gt 0 -or ($scan.Artifacts | Where-Object { $_ -like 'Saved\*' -and $_ -notlike '*Tests' })
    $keepData = $false
    if ($hasHistory) {
        $keepData = (Read-Choice '--keep-data' '--delete-data' 'Run history, logs and reports: 1 = keep (moved out of the project), 2 = delete' '1') -eq 1
    }
    $keepGenerated = $false
    if ($scan.Generated.Count -gt 0 -or $scan.Records.Count -gt 0) {
        $answer = Read-Choice '--keep-generated' '--delete-generated' 'Widgets made by canvas-import (they stop working without DwoCanvas): 1 = keep in the project, 2 = delete' '0'
        if ($answer -eq 0) { Fail 'Uninstall stopped. Choose what happens to the widgets made by canvas-import: --keep-generated or --delete-generated.' }
        $keepGenerated = $answer -eq 1
    }
    $cleanRefs = -not ($Rest -contains '--keep-references')

    $stamp = (Split-Path -Leaf $Project) + '-' + (Get-Date).ToString('yyyyMMdd-HHmmss')
    $keepDir = Join-Path $AppDataDir "uninstalled\$stamp"
    $backupDir = Join-Path $keepDir 'backup'

    # 1. Project files that name the plugins (backed up first).
    if ($cleanRefs) {
        $uprojectRel = (@(Get-ChildItem -Path $Project -Filter *.uproject -File)[0]).Name
        if ($scan.Uproject.Count -gt 0) {
            Backup-ProjectFile $uprojectRel $backupDir
            Remove-UprojectEntries $scan.Uproject
            Say "Removed from $uprojectRel Plugins: $($scan.Uproject -join ', ')"
        }
        foreach ($group in ($scan.BuildCs | Group-Object File)) {
            Backup-ProjectFile $group.Name $backupDir
            foreach ($hit in $group.Group) { Remove-BuildCsDep $group.Name $hit.Module }
            Say "Removed from $($group.Name): $(@($group.Group | ForEach-Object { $_.Module }) -join ', ')"
        }
        foreach ($c in $scan.Configs) { Backup-ProjectFile $c $backupDir; Move-OrDelete $c $false $keepDir; Say "Removed $c" }
        if ($scan.EditorIni.Count -gt 0) {
            foreach ($h in $scan.EditorIni) { Backup-ProjectFile $h.File $backupDir }
            Remove-EditorIniLines $scan.EditorIni
            Say "Removed dwookit module entries from the editor settings ($(@($scan.EditorIni | ForEach-Object { $_.File }) -join ', '))."
        }
    }

    # 1b. DwoBuilder: this project's Jenkins job and the git hooks (Jenkins itself is per PC and stays).
    if (Test-Path (Join-Path $ForgeDir 'ci.json')) {
        & (Join-Path $SystemDir 'tools\ci.ps1') -Command 'ci-job' -Project $Project -Rest @('--remove')
    }

    # 2. Plugins: checks already happened above, so remove unconditionally.
    $null = Remove-StateMonitor $true
    if ((Get-CanvasState) -eq 'managed') { $null = Remove-Canvas $true }
    Remove-Plugin

    # 3. Generated widgets and their import records.
    foreach ($g in $scan.Generated) { if (-not $keepGenerated) { Move-OrDelete $g $false $keepDir } }
    foreach ($r in $scan.Records) { if (-not $keepGenerated) { Move-OrDelete $r $false $keepDir } }
    if ($scan.Generated.Count -gt 0) {
        if ($keepGenerated) { Say "Kept $($scan.Generated.Count) generated widget(s); they need DwoCanvas to open." }
        else { Say "Deleted $($scan.Generated.Count) generated widget(s) and the import records." }
    }

    # 4. Run history, reports, files dwookit runs left, test leftovers.
    if ($keepData -and (Test-Path $DataDir)) {
        New-Item -ItemType Directory -Force $keepDir | Out-Null
        Move-Item $DataDir (Join-Path $keepDir 'data')
    }
    foreach ($f in $scan.Created) { Move-OrDelete $f $keepData $keepDir }
    foreach ($a in $scan.Artifacts) {
        $isTest = $a -like 'Content\*' -or $a -like '*Tests'
        Move-OrDelete $a ($keepData -and -not $isTest) $keepDir
    }
    if ($keepData) { Say "Run history and reports kept at $keepDir" } elseif ($hasHistory) { Say 'Run history and reports deleted.' }
    if ($cleanRefs -and (Test-Path $backupDir)) { Say "Backup of edited project files: $backupDir" }

    # 5. Agent pointer files, attach state, launcher, exclude block.
    if ($scan.Agent.Count) { & python (Join-Path $SystemDir 'tools\lens_agent.py') remove --project $Project --backup (Join-Path $keepDir 'instructions') }
    $cleared = Clear-ExcludeBlock
    Set-Location $Project
    if (Test-Path $ForgeDir) {
        Get-ChildItem $ForgeDir -Recurse -Force -File | ForEach-Object { if ($_.IsReadOnly) { $_.IsReadOnly = $false } }
        Remove-Folder $ForgeDir '__dwookit'
    }
    Remove-LegacyLeftovers
    # A launcher run by itself deletes its own file after this script returns.
    if ($env:DWOOKIT_FROM_BAT -ne '1' -and (Test-Path $LauncherPath)) { Remove-Item -Force $LauncherPath }
    $plugins = Join-Path $Project 'Plugins'
    $existed = $state -and $state.PSObject.Properties['pluginsFolderExisted'] -and [bool]$state.pluginsFolderExisted
    if (-not $existed -and (Test-Path $plugins) -and -not (Get-ChildItem -Force $plugins)) { Remove-Item -Force $plugins }
    if ($cleared) { Say 'Exclude entries removed.' }

    # 6. Verify with a fresh scan.
    $after = Get-UninstallScan
    $left = @()
    $left += $after.Plugins
    $running = @($LegacyGens | Where-Object { $_.FromBat } | ForEach-Object { Split-Path -Leaf $_.Launcher })
    $left += @($after.Launchers | Where-Object { -not ($_ -eq '__dwookit.bat' -and $env:DWOOKIT_FROM_BAT -eq '1') -and $_ -notin $running })
    if ($after.Exclude) { $left += "exclude block in $($after.Exclude)" }
    $left += @($after.Agent)
    if (Test-Path $ForgeDir) { $left += '__dwookit' }
    $left += @($after.Artifacts)
    if ($cleanRefs) { $left += @($after.Uproject | ForEach-Object { ".uproject entry $_" }) + @($after.BuildCs | ForEach-Object { "$($_.Module) in $($_.File)" }) + @($after.Configs) }
    if (-not $keepGenerated) { $left += @($after.Generated) + @($after.Records) }
    if ($left.Count -gt 0) { Say "Uninstalled, but these remain: $($left -join ', ')"; exit 3 }
    if ($scan.CodeUses.Count -gt 0) {
        Say "Uninstalled. Project code still names the plugins in $($scan.CodeUses.Count) place(s) listed above; edit them before building."
        exit 0
    }
    $vcs = Get-Vcs
    if ($vcs.Kind -ne 'none' -and $state -and $state.gitStatusBeforeAttach -ne $null -and -not $keepGenerated -and $scan.Uproject.Count -eq 0 -and $scan.BuildCs.Count -eq 0) {
        $now = (@((Get-ProjectStatus) -split "`n") | Where-Object { $_ -and $_ -notmatch '__dwo-(forge|lens)\.bat"?$' -and $_ -notmatch 'Plugins/DwoCanvas/' }) -join "`n"
        $before = (@("$($state.gitStatusBeforeAttach)" -split "`n") | Where-Object { $_ -and $_ -notmatch 'Plugins/DwoCanvas/' }) -join "`n"
        if ($now -ne $before) { Say 'Uninstalled. No dwookit files remain; git status differs from before attach (your own changes since then). Review: git status'; exit 0 }
        Say 'Uninstalled. No dwookit files remain and git status matches the state before attach.'
    } else {
        Say 'Uninstalled. No dwookit files remain in the project.'
    }
}

# Every command first migrates what dwo-Lens 1.x left in the project ('tools' does not touch a project).
if ($Command -notin @('tools', 'tool', 'library') -and $Command -notlike 'ci-*') { Invoke-Migration }
if ($Command -notin @('tools', 'tool', 'library', 'uninstall', 'detach', 'connect', 'attach', 'update') -and $Command -notlike 'ci-*') { Sync-ConnectionFiles }

switch ($Command) {
    'connect' { Invoke-Attach 'connect' }
    'attach'  { Invoke-Attach 'attach' }
    'tool'    { Invoke-Tool }
    'update'  { Invoke-Attach 'update' }
    'status'  { Invoke-Status }
    'doctor'  { if (-not (Invoke-Doctor)) { exit 1 } }
    'analyze' { Invoke-Analyze }
    'bp'      { Invoke-Bp }
    'native'  { Invoke-Native }
    'agent'   { Invoke-Agent }
    'library' { Invoke-Library }
    'live'    { Invoke-Live }
    'live-ingest' { Invoke-LiveIngest }
    'compile' { Invoke-Compile }
    'package' { Invoke-Package }
    { $_ -like 'ci-*' } {
        # DwoBuilder lives in ci.ps1 (machine-wide Jenkins + this project's job and git hooks).
        & (Join-Path $SystemDir 'tools\ci.ps1') -Command $Command -Project $Project -Rest $Rest
        exit $LASTEXITCODE
    }
    'verify'  { Invoke-Verify }
    'logs'    { Invoke-Logs }
    'smoke'   { Invoke-Smoke }
    'perf'    { Invoke-Perf }
    'sizes'   { Invoke-Sizes }
    'trace'   { Invoke-Trace }
    'play'    { Invoke-Play }
    'tools'   { Invoke-ToolList }
    'sm-install' { Invoke-SmInstall }
    'sm-remove'  { Invoke-SmRemove }
    'canvas-install'  { Invoke-CanvasInstall }
    'canvas-remove'   { Invoke-CanvasRemove }
    'canvas-import'   { Invoke-CanvasImport }
    'canvas-validate' { Invoke-CanvasValidate }
    'gui'     { & (Join-Path $SystemDir 'tools\gui.ps1') -SystemDir $SystemDir -Project $Project -ForgeHome $SystemDir }
    'uninstall' { Invoke-Uninstall }
    'detach'  { Invoke-Uninstall }
}
