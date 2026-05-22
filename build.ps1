param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$CommandArgs
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Get-EnvOrDefault {
    param(
        [string]$Name,
        [string]$DefaultValue
    )

    $value = [Environment]::GetEnvironmentVariable($Name)

    if ([string]::IsNullOrWhiteSpace($value)) {
        return $DefaultValue
    }

    return $value
}

function Get-DefaultJobs {
    $jobsFromEnv = [Environment]::GetEnvironmentVariable("JOBS")

    if (-not [string]::IsNullOrWhiteSpace($jobsFromEnv)) {
        return $jobsFromEnv
    }

    $processorCount = [Environment]::ProcessorCount

    if ($processorCount -gt 0) {
        return "$processorCount"
    }

    return "4"
}

function Has-Command {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-NormalizedArchitectureName {
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture.ToString()

    switch ($arch) {
        "X64" {
            return "x86_64"
        }

        "X86" {
            return "i686"
        }

        "Arm64" {
            return "aarch64"
        }

        "Arm" {
            return "arm"
        }

        default {
            return $arch.ToLowerInvariant()
        }
    }
}

function Get-DefaultTargetTriple {
    $tripleFromEnv = [Environment]::GetEnvironmentVariable("BUILD_TARGET_TRIPLE")

    if (-not [string]::IsNullOrWhiteSpace($tripleFromEnv)) {
        return $tripleFromEnv
    }

    foreach ($compiler in @("clang", "clang.exe", "cc", "cc.exe")) {
        if (-not (Has-Command $compiler)) {
            continue
        }

        $output = & $compiler -dumpmachine 2>$null

        if ($LASTEXITCODE -eq 0 -and $output.Count -gt 0) {
            $triple = "$($output[0])".Trim()

            if (-not [string]::IsNullOrWhiteSpace($triple)) {
                return $triple
            }
        }
    }

    $arch = Get-NormalizedArchitectureName

    if ([System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Windows)) {
        return "$arch-pc-windows-msvc"
    }

    if ([System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::OSX)) {
        if ($arch -eq "aarch64") {
            return "arm64-apple-darwin"
        }

        return "$arch-apple-darwin"
    }

    if ([System.Runtime.InteropServices.RuntimeInformation]::IsOSPlatform([System.Runtime.InteropServices.OSPlatform]::Linux)) {
        switch ($arch) {
            "x86_64" {
                return "x86_64-pc-linux-gnu"
            }

            "aarch64" {
                return "aarch64-unknown-linux-gnu"
            }

            "arm" {
                return "armv7-unknown-linux-gnueabihf"
            }

            default {
                return "$arch-unknown-linux-gnu"
            }
        }
    }

    return "$arch-unknown-platform"
}

function Test-Truthy {
    param(
        [string]$Value
    )

    return $Value -match '^(1|true|TRUE|yes|YES|on|ON|enabled|ENABLED)$'
}

function Invoke-External {
    param(
        [string]$Command,

        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & $Command @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw "$Command $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Invoke-Git {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & git @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw "git $($Arguments -join ' ') failed with exit code $LASTEXITCODE"
    }
}

function Test-GitCommand {
    param(
        [Parameter(ValueFromRemainingArguments = $true)]
        [string[]]$Arguments
    )

    & git @Arguments *> $null
    return $LASTEXITCODE -eq 0
}

function Test-LlvmRepo {
    if (-not (Test-Path $script:LlvmDir)) {
        return $false
    }

    & git -C $script:LlvmDir rev-parse --is-inside-work-tree *> $null
    return $LASTEXITCODE -eq 0
}

function Require-LlvmRepo {
    if (-not (Test-LlvmRepo)) {
        Write-Host "ERROR: LLVM is not cloned yet."
        Write-Host
        Write-Host "Run:"
        Write-Host "  pwsh -File ./build.ps1 clone"
        Write-Host
        Write-Host "or:"
        Write-Host "  pwsh -File ./build.ps1 bootstrap"
        exit 1
    }
}

function Print-Header {
    Write-Host "=== clang-mg ==="
    Write-Host "Command:       $script:Command"
    Write-Host "LLVM ref:      $script:LlvmRef"
    Write-Host "LLVM dir:      $script:LlvmDir"
    Write-Host "Target triple: $script:BuildTargetTriple"
    Write-Host "Build dir:     $script:BuildDir"
    Write-Host "Build type:    $script:BuildType"
    Write-Host "Jobs:          $script:Jobs"
    Write-Host
}

function Write-Usage {
    Write-Host @"
Usage:
  pwsh -File ./build.ps1 [command]

Commands:
  bootstrap                  Clone/update LLVM if needed, apply all enabled patches, then build
  install                    Clone/update LLVM, reset clean, apply all enabled patches, build, then add clang-mg to PATH
  clone                      Clone LLVM only
  update                     Update LLVM only if the checkout is clean
  reset                      Reset LLVM checkout to LLVM_REF / origin ref
  apply                      Apply all enabled clang-mg patches
  apply <feature-name...>    Apply one or more specific feature patch stacks
  enable <feature-name...>   Enable one or more feature patch stacks
  disable <feature-name...>  Disable one or more feature patch stacks
  build                      Build current LLVM tree only
  fresh                      Reset LLVM, apply all enabled patches, then build
  rebuild                    Same as fresh
  save <feature-name>        Save current LLVM changes as patches for a feature
  help                       Show this help menu

Examples:
  pwsh -File ./build.ps1
  pwsh -File ./build.ps1 bootstrap
  pwsh -File ./build.ps1 install
  pwsh -File ./build.ps1 apply
  pwsh -File ./build.ps1 apply change-bin-name
  pwsh -File ./build.ps1 apply change-bin-name curlinclude
  pwsh -File ./build.ps1 enable if-constexpr-members
  pwsh -File ./build.ps1 disable curlinclude
  pwsh -File ./build.ps1 enable core if-constexpr-members
  pwsh -File ./build.ps1 build
  pwsh -File ./build.ps1 fresh
  pwsh -File ./build.ps1 save curlinclude

Environment variables:
  LLVM_REF=main
  LLVM_URL=https://github.com/llvm/llvm-project.git
  WORK_DIR=$($script:RootDir)/work
  LLVM_DIR=$($script:RootDir)/work/llvm-project
  BUILD_TARGET_TRIPLE=x86_64-pc-windows-msvc
  BUILD_DIR=$($script:RootDir)/work/build-<target-triple>
  BUILD_TYPE=Debug
  JOBS=4
  FEATURE_CONFIG_NAME=feature.conf
  INTERACTIVE=1
"@
}

function List-PatchFeatures {
    if (-not (Test-Path $script:PatchRoot)) {
        Write-Host "No patches directory found:"
        Write-Host "  $script:PatchRoot"
        return
    }

    Get-ChildItem -Path $script:PatchRoot -Directory |
        Sort-Object Name |
        ForEach-Object { Write-Host $_.Name }
}

function Write-DefaultFeatureConfig {
    param(
        [string]$ConfigFile,
        [string]$FeatureName
    )

    $parent = Split-Path -Parent $ConfigFile

    if (-not [string]::IsNullOrWhiteSpace($parent)) {
        New-Item -ItemType Directory -Force -Path $parent | Out-Null
    }

    $config = @"
# Auto-generated config for clang-mg feature: $FeatureName

# Whether this feature should be applied.
# Valid values: 1, 0, true, false, yes, no, on, off
ENABLED=1

# Features that must be applied before this feature.
# Example:
#   DEPENDS=(core)
DEPENDS=()

# Features that this feature must be applied before.
# Usually DEPENDS is enough, but this is useful for ordering from the other side.
# Example:
#   BEFORE=(if-constexpr-members)
BEFORE=()
"@

    Set-Content -Path $ConfigFile -Value $config -Encoding UTF8
}

function Ensure-FeatureConfig {
    param(
        [string]$FeatureName
    )

    if ([string]::IsNullOrWhiteSpace($FeatureName)) {
        Write-Host "ERROR: Missing feature name."
        Write-Host
        Write-Host "Usage:"
        Write-Host "  pwsh -File ./build.ps1 enable <feature-name...>"
        Write-Host "  pwsh -File ./build.ps1 disable <feature-name...>"
        exit 1
    }

    $featureDir = Join-Path $script:PatchRoot $FeatureName
    $configFile = Join-Path $featureDir $script:FeatureConfigName

    if (-not (Test-Path $featureDir)) {
        Write-Host "ERROR: Feature patch directory does not exist:"
        Write-Host "  $featureDir"
        Write-Host
        Write-Host "Available features:"
        if (Test-Path $script:PatchRoot) {
            Get-ChildItem -Path $script:PatchRoot -Directory |
                Sort-Object Name |
                ForEach-Object { Write-Host "  $($_.Name)" }
        }
        exit 1
    }

    if (-not (Test-Path $configFile)) {
        Write-Host "Generating missing config:"
        Write-Host "  $configFile"
        Write-DefaultFeatureConfig $configFile $FeatureName
    }

    return $configFile
}

function Set-FeatureEnabled {
    param(
        [string]$FeatureName,
        [string]$EnabledValue
    )

    $configFile = Ensure-FeatureConfig $FeatureName
    $lines = @(Get-Content -Path $configFile -ErrorAction Stop)
    $replaced = $false
    $newLines = New-Object System.Collections.Generic.List[string]

    foreach ($line in $lines) {
        if (-not $replaced -and $line -match '^\s*ENABLED\s*=') {
            $newLines.Add("ENABLED=$EnabledValue")
            $replaced = $true
        }
        else {
            $newLines.Add($line)
        }
    }

    if (-not $replaced) {
        $newLines.Add("")
        $newLines.Add("# Whether this feature should be applied.")
        $newLines.Add("ENABLED=$EnabledValue")
    }

    Set-Content -Path $configFile -Value $newLines -Encoding UTF8

    if ($EnabledValue -eq "1") {
        Write-Host "Enabled feature:  $FeatureName"
    }
    else {
        Write-Host "Disabled feature: $FeatureName"
    }

    Write-Host "Config:"
    Write-Host "  $configFile"
}

function Run-SetFeatureEnabled {
    param(
        [string]$EnabledValue,
        [string[]]$FeatureNames
    )

    if ($FeatureNames.Count -eq 0) {
        Write-Host "ERROR: Missing feature name."

        if ($EnabledValue -eq "1") {
            Write-Host
            Write-Host "Usage:"
            Write-Host "  pwsh -File ./build.ps1 enable <feature-name...>"
        }
        else {
            Write-Host
            Write-Host "Usage:"
            Write-Host "  pwsh -File ./build.ps1 disable <feature-name...>"
        }

        Write-Host
        Write-Host "Available features:"
        if (Test-Path $script:PatchRoot) {
            Get-ChildItem -Path $script:PatchRoot -Directory |
                Sort-Object Name |
                ForEach-Object { Write-Host "  $($_.Name)" }
        }
        exit 1
    }

    foreach ($featureName in $FeatureNames) {
        Set-FeatureEnabled $featureName $EnabledValue
        Write-Host
    }
}

function Run-Clone {
    Invoke-External $script:CloneScript $script:LlvmUrl $script:LlvmRef $script:LlvmDir
}

function Run-Update {
    $oldLlvmUrl = $env:LLVM_URL
    $oldLlvmRef = $env:LLVM_REF
    $oldWorkDir = $env:WORK_DIR
    $oldLlvmDir = $env:LLVM_DIR

    try {
        $env:LLVM_URL = $script:LlvmUrl
        $env:LLVM_REF = $script:LlvmRef
        $env:WORK_DIR = $script:WorkDir
        $env:LLVM_DIR = $script:LlvmDir

        Invoke-External $script:UpdateScript
    }
    finally {
        $env:LLVM_URL = $oldLlvmUrl
        $env:LLVM_REF = $oldLlvmRef
        $env:WORK_DIR = $oldWorkDir
        $env:LLVM_DIR = $oldLlvmDir
    }
}

function Resolve-ResetRef {
    Require-LlvmRepo

    Push-Location $script:LlvmDir

    try {
        Invoke-Git fetch origin --tags

        if (Test-GitCommand show-ref --verify --quiet "refs/remotes/origin/$script:LlvmRef") {
            return "origin/$script:LlvmRef"
        }

        return $script:LlvmRef
    }
    finally {
        Pop-Location
    }
}

function Run-Reset {
    Require-LlvmRepo

    $resetRef = Resolve-ResetRef

    Push-Location $script:LlvmDir

    try {
        Invoke-External $script:ResetScript $resetRef
    }
    finally {
        Pop-Location
    }
}

function Run-ApplyPatches {
    Require-LlvmRepo

    Invoke-External $script:ApplyPatchesScript $script:RootDir $script:LlvmDir
}

function Run-ApplyFeatures {
    param(
        [string[]]$FeatureNames
    )

    Require-LlvmRepo

    if ($FeatureNames.Count -eq 0) {
        Run-ApplyPatches
        return
    }

    foreach ($featureName in $FeatureNames) {
        $oldLlvmDir = $env:LLVM_DIR

        try {
            $env:LLVM_DIR = $script:LlvmDir
            Invoke-External $script:ApplyFeatureScript $featureName
        }
        finally {
            $env:LLVM_DIR = $oldLlvmDir
        }
    }
}

function Run-Build {
    Require-LlvmRepo

    $oldBuildTargetTriple = $env:BUILD_TARGET_TRIPLE

    try {
        $env:BUILD_TARGET_TRIPLE = $script:BuildTargetTriple

        if ($script:Interactive -eq 1) {
            Invoke-External $script:BuildLlvmScript $script:LlvmDir $script:BuildDir $script:BuildType $script:Jobs "--interactive"
        }
        else {
            Invoke-External $script:BuildLlvmScript $script:LlvmDir $script:BuildDir $script:BuildType $script:Jobs
        }
    }
    finally {
        $env:BUILD_TARGET_TRIPLE = $oldBuildTargetTriple
    }
}

function Run-InstallPath {
    Require-LlvmRepo

    $oldBuildDir = $env:BUILD_DIR

    try {
        $env:BUILD_DIR = $script:BuildDir
        Invoke-External $script:InstallScript $script:BuildDir
    }
    finally {
        $env:BUILD_DIR = $oldBuildDir
    }
}

function Run-SaveFeature {
    param(
        [string]$FeatureName
    )

    Require-LlvmRepo

    if ([string]::IsNullOrWhiteSpace($FeatureName)) {
        Write-Host "ERROR: Missing feature name."
        Write-Host
        Write-Host "Usage:"
        Write-Host "  pwsh -File ./build.ps1 save <feature-name>"
        Write-Host
        Write-Host "Example:"
        Write-Host "  pwsh -File ./build.ps1 save curlinclude"
        exit 1
    }

    Push-Location $script:LlvmDir

    try {
        if (Test-GitCommand -C $script:LlvmDir rev-parse --verify "origin/$script:LlvmRef") {
            Invoke-External $script:SaveFeatureScript $FeatureName "origin/$script:LlvmRef"
        }
        else {
            Invoke-External $script:SaveFeatureScript $FeatureName $script:LlvmRef
        }
    }
    finally {
        Pop-Location
    }
}

$script:RootDir = $PSScriptRoot

if ([string]::IsNullOrWhiteSpace($script:RootDir)) {
    $script:RootDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

$script:RootDir = (Resolve-Path $script:RootDir).Path

$script:LlvmUrl = Get-EnvOrDefault "LLVM_URL" "https://github.com/llvm/llvm-project.git"
$script:LlvmRef = Get-EnvOrDefault "LLVM_REF" "main"

$script:WorkDir = Get-EnvOrDefault "WORK_DIR" (Join-Path $script:RootDir "work")
$script:LlvmDir = Get-EnvOrDefault "LLVM_DIR" (Join-Path $script:WorkDir "llvm-project")

$script:BuildTargetTriple = Get-DefaultTargetTriple
$script:BuildDir = Get-EnvOrDefault "BUILD_DIR" (Join-Path $script:WorkDir "build-$($script:BuildTargetTriple)")

$script:BuildType = Get-EnvOrDefault "BUILD_TYPE" "Release"
$script:Jobs = Get-DefaultJobs

$script:CloneScript = Join-Path $script:RootDir "scripts/clone-llvm.ps1"
$script:UpdateScript = Join-Path $script:RootDir "scripts/update-llvm.ps1"
$script:ResetScript = Join-Path $script:RootDir "scripts/reset-llvm.ps1"
$script:ApplyPatchesScript = Join-Path $script:RootDir "scripts/apply-patches.ps1"
$script:ApplyFeatureScript = Join-Path $script:RootDir "scripts/apply-feature.ps1"
$script:BuildLlvmScript = Join-Path $script:RootDir "scripts/build-llvm.ps1"
$script:SaveFeatureScript = Join-Path $script:RootDir "scripts/save-feature.ps1"
$script:InstallScript = Join-Path $script:RootDir "scripts/install-clang-mg.ps1"

$script:PatchRoot = Join-Path $script:RootDir "patches"
$script:FeatureConfigName = Get-EnvOrDefault "FEATURE_CONFIG_NAME" "feature.conf"

$interactiveEnv = Get-EnvOrDefault "INTERACTIVE" "0"
$script:Interactive = if (Test-Truthy $interactiveEnv) { 1 } else { 0 }

$filteredArgs = New-Object System.Collections.Generic.List[string]

foreach ($arg in @($CommandArgs)) {
    if ($arg -eq "--interactive") {
        $script:Interactive = 1
    }
    else {
        $filteredArgs.Add($arg)
    }
}

if ($filteredArgs.Count -gt 0) {
    $script:Command = $filteredArgs[0]
    $script:CommandRest = @($filteredArgs | Select-Object -Skip 1)
}
else {
    $script:Command = "bootstrap"
    $script:CommandRest = @()
}

Print-Header

switch ($script:Command) {
    { $_ -in @("help", "-h", "--help") } {
        Write-Usage
    }

    "clone" {
        Run-Clone
    }

    "update" {
        Run-Update
    }

    "reset" {
        Run-Reset
    }

    "apply" {
        Run-ApplyFeatures $script:CommandRest
    }

    "enable" {
        Run-SetFeatureEnabled "1" $script:CommandRest
    }

    "disable" {
        Run-SetFeatureEnabled "0" $script:CommandRest
    }

    "build" {
        Run-Build
    }

    "bootstrap" {
        Run-Update
        Run-ApplyPatches
        Run-Build
    }

    "install" {
        Run-Update

        # Make sure we are applying patches onto a clean LLVM base.
        # This prevents accidentally applying the same patch stack twice.
        Run-Reset

        Run-ApplyPatches
        Run-Build
        Run-InstallPath
    }

    { $_ -in @("fresh", "rebuild") } {
        if (-not (Test-LlvmRepo)) {
            Run-Clone
        }
        else {
            Run-Reset
        }

        Run-ApplyPatches
        Run-Build
    }

    "save" {
        $featureName = if ($script:CommandRest.Count -gt 0) { $script:CommandRest[0] } else { "" }
        Run-SaveFeature $featureName
    }

    default {
        Write-Host "ERROR: Unknown command: $script:Command"
        Write-Host
        Write-Usage
        exit 1
    }
}

Write-Host
Write-Host "Done."
