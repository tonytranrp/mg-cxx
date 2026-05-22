$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

function Test-IsWindows {
    return $env:OS -eq "Windows_NT" -or [System.IO.Path]::DirectorySeparatorChar -eq "\"
}

function Show-Usage {
    $scriptName = if ($PSCommandPath) { $PSCommandPath } else { "scripts/build-llvm.ps1" }

    Write-Host "Usage: $scriptName <llvm-dir> [build-dir] [build-type] [jobs] [--interactive]"
    Write-Host ""
    Write-Host "Examples:"
    Write-Host "  $scriptName work/llvm-project"
    Write-Host "  $scriptName work/llvm-project work/build-x86_64-pc-windows-msvc"
    Write-Host "  $scriptName work/llvm-project work/build-x86_64-pc-windows-msvc Release 8"
    Write-Host "  $scriptName work/llvm-project work/build-x86_64-pc-windows-msvc Release 8 --interactive"
    Write-Host ""
    Write-Host "Environment variables:"
    Write-Host "  BUILD_TARGET_TRIPLE=x86_64-pc-windows-msvc"
    Write-Host "  CC=<path-or-name>                 Override C compiler"
    Write-Host "  CXX=<path-or-name>                Override C++ compiler"
    Write-Host "  ASM=<path-or-name>                Override ASM compiler"
    Write-Host "  CLANG_MG_GENERATOR=Ninja          Override CMake generator"
    Write-Host "  CLANG_MG_DEEP_COMPILER_SEARCH=1   Also search Program Files recursively"
}

function Has-Command {
    param([Parameter(Mandatory = $true)][string]$Name)

    return $null -ne (Get-Command $Name -ErrorAction SilentlyContinue)
}

function Get-CommandPath {
    param([Parameter(Mandatory = $true)][string]$Name)

    $command = Get-Command $Name -ErrorAction SilentlyContinue

    if ($null -eq $command) {
        return $null
    }

    return $command.Source
}

function Get-DefaultJobs {
    $count = [Environment]::ProcessorCount

    if ($count -le 0) {
        return 4
    }

    return $count
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

function Prompt-DebugBuild {
    if (-not $script:INTERACTIVE) {
        return
    }

    if ([Console]::IsInputRedirected) {
        Write-Host "Interactive mode requested, but stdin is not a terminal. Using Release build."
        $script:BUILD_TYPE = "Release"
        return
    }

    Write-Host ""
    $answer = Read-Host "Build an unoptimized Debug build instead of optimized Release? [y/N]"

    switch -Regex ($answer) {
        '^(y|Y|yes|YES|Yes)$' {
            $script:BUILD_TYPE = "Debug"
            break
        }
        default {
            $script:BUILD_TYPE = "Release"
            break
        }
    }
}

function Invoke-CommandChecked {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [Parameter(Mandatory = $true)]
        [string[]]$Arguments
    )

    & $FilePath @Arguments

    if ($LASTEXITCODE -ne 0) {
        throw "Command failed with exit code ${LASTEXITCODE}: $FilePath $($Arguments -join ' ')"
    }
}

function Add-DirectoryToPath {
    param([Parameter(Mandatory = $true)][string]$Directory)

    if (-not (Test-Path $Directory -PathType Container)) {
        return
    }

    $pathParts = @($env:PATH -split [System.IO.Path]::PathSeparator) |
        Where-Object { -not [string]::IsNullOrWhiteSpace($_) }

    foreach ($pathPart in $pathParts) {
        if ($pathPart.TrimEnd('\', '/') -ieq $Directory.TrimEnd('\', '/')) {
            return
        }
    }

    $env:PATH = "$Directory$([System.IO.Path]::PathSeparator)$env:PATH"
}

function Import-EnvironmentFromBatch {
    param(
        [Parameter(Mandatory = $true)]
        [string]$BatchFile,

        [string[]]$BatchArguments = @()
    )

    if (-not (Test-Path $BatchFile -PathType Leaf)) {
        return $false
    }

    $quotedBatch = '"' + $BatchFile + '"'
    $argumentText = ""

    if ($BatchArguments.Count -gt 0) {
        $argumentText = " " + ($BatchArguments -join " ")
    }

    $cmdLine = "$quotedBatch$argumentText >nul && set"
    $output = & cmd.exe /s /c $cmdLine

    if ($LASTEXITCODE -ne 0) {
        return $false
    }

    foreach ($line in $output) {
        $splitIndex = $line.IndexOf("=")

        if ($splitIndex -le 0) {
            continue
        }

        $name = $line.Substring(0, $splitIndex)
        $value = $line.Substring($splitIndex + 1)

        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }

    return $true
}

function Find-VsWhere {
    $fromPath = Get-CommandPath "vswhere.exe"

    if ($fromPath) {
        return $fromPath
    }

    $programFilesX86 = ${env:ProgramFiles(x86)}

    if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
        $candidate = Join-Path $programFilesX86 "Microsoft Visual Studio\Installer\vswhere.exe"

        if (Test-Path $candidate -PathType Leaf) {
            return $candidate
        }
    }

    return $null
}

function Get-VisualStudioInstallations {
    $vswhere = Find-VsWhere

    if (-not $vswhere) {
        return @()
    }

    $installations = & $vswhere `
        -products * `
        -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
        -property installationPath

    if ($LASTEXITCODE -ne 0) {
        return @()
    }

    return @($installations | Where-Object { -not [string]::IsNullOrWhiteSpace($_) })
}

function Import-VisualStudioEnvironment {
    if (-not (Test-IsWindows)) {
        return $false
    }

    if (-not [string]::IsNullOrWhiteSpace($env:VSCMD_VER)) {
        Write-Host "Visual Studio developer environment already active: $env:VSCMD_VER"
        return $true
    }

    $installations = Get-VisualStudioInstallations

    foreach ($installation in $installations) {
        $vsDevCmd = Join-Path $installation "Common7\Tools\VsDevCmd.bat"

        if (Test-Path $vsDevCmd -PathType Leaf) {
            Write-Host "Loading Visual Studio developer environment:"
            Write-Host "  $vsDevCmd"

            if (Import-EnvironmentFromBatch $vsDevCmd @("-arch=x64", "-host_arch=x64")) {
                return $true
            }
        }

        $vcVars64 = Join-Path $installation "VC\Auxiliary\Build\vcvars64.bat"

        if (Test-Path $vcVars64 -PathType Leaf) {
            Write-Host "Loading Visual Studio VC environment:"
            Write-Host "  $vcVars64"

            if (Import-EnvironmentFromBatch $vcVars64) {
                return $true
            }
        }
    }

    return $false
}

function Add-DirectoryIfPresent {
    param(
        [System.Collections.Generic.List[string]]$Directories,
        [string]$Directory
    )

    if (-not [string]::IsNullOrWhiteSpace($Directory) -and (Test-Path $Directory -PathType Container)) {
        $Directories.Add($Directory)
    }
}

function Get-KnownCompilerSearchDirectories {
    $directories = New-Object System.Collections.Generic.List[string]

    foreach ($pathPart in @($env:PATH -split [System.IO.Path]::PathSeparator)) {
        Add-DirectoryIfPresent $directories $pathPart
    }

    if (Test-IsWindows) {
        $programFiles = $env:ProgramFiles
        $programFilesX86 = ${env:ProgramFiles(x86)}

        if (-not [string]::IsNullOrWhiteSpace($programFiles)) {
            Add-DirectoryIfPresent $directories (Join-Path $programFiles "LLVM\bin")
        }

        if (-not [string]::IsNullOrWhiteSpace($programFilesX86)) {
            Add-DirectoryIfPresent $directories (Join-Path $programFilesX86 "LLVM\bin")
        }

        foreach ($installation in Get-VisualStudioInstallations) {
            Add-DirectoryIfPresent $directories (Join-Path $installation "VC\Tools\Llvm\x64\bin")
            Add-DirectoryIfPresent $directories (Join-Path $installation "VC\Tools\Llvm\bin")

            $msvcRoot = Join-Path $installation "VC\Tools\MSVC"

            if (Test-Path $msvcRoot -PathType Container) {
                $msvcVersions = @(Get-ChildItem -Path $msvcRoot -Directory | Sort-Object Name -Descending)

                foreach ($versionDir in $msvcVersions) {
                    Add-DirectoryIfPresent $directories (Join-Path $versionDir.FullName "bin\Hostx64\x64")
                    Add-DirectoryIfPresent $directories (Join-Path $versionDir.FullName "bin\Hostx64\x86")
                    Add-DirectoryIfPresent $directories (Join-Path $versionDir.FullName "bin\Hostx86\x64")
                    Add-DirectoryIfPresent $directories (Join-Path $versionDir.FullName "bin\Hostx86\x86")
                }
            }
        }

        if ($env:CLANG_MG_DEEP_COMPILER_SEARCH -match '^(1|true|yes|on)$') {
            Write-Host "Deep compiler search enabled. Searching Program Files; this may take a bit..."

            foreach ($root in @($programFiles, $programFilesX86)) {
                if ([string]::IsNullOrWhiteSpace($root) -or -not (Test-Path $root -PathType Container)) {
                    continue
                }

                foreach ($name in @("clang-cl.exe", "clang.exe", "clang++.exe", "cl.exe")) {
                    Get-ChildItem -Path $root -Recurse -File -Filter $name -ErrorAction SilentlyContinue |
                        ForEach-Object {
                            Add-DirectoryIfPresent $directories $_.DirectoryName
                        }
                }
            }
        }
    }

    return @($directories | Select-Object -Unique)
}

function Find-ExecutableInKnownLocations {
    param([Parameter(Mandatory = $true)][string[]]$Names)

    foreach ($name in $Names) {
        $fromPath = Get-CommandPath $name

        if ($fromPath) {
            return $fromPath
        }
    }

    foreach ($directory in Get-KnownCompilerSearchDirectories) {
        foreach ($name in $Names) {
            $candidate = Join-Path $directory $name

            if (Test-Path $candidate -PathType Leaf) {
                Add-DirectoryToPath $directory
                return $candidate
            }
        }
    }

    return $null
}

function Resolve-CompilerPath {
    param([string]$Value)

    if ([string]::IsNullOrWhiteSpace($Value)) {
        return $null
    }

    if (Test-Path $Value -PathType Leaf) {
        return (Resolve-Path $Value).Path
    }

    $fromPath = Get-CommandPath $Value

    if ($fromPath) {
        return $fromPath
    }

    return $Value
}

function Find-CMakeToolchain {
    $envCC = [Environment]::GetEnvironmentVariable("CC")
    $envCXX = [Environment]::GetEnvironmentVariable("CXX")
    $envASM = [Environment]::GetEnvironmentVariable("ASM")

    if (-not [string]::IsNullOrWhiteSpace($envCC) -or -not [string]::IsNullOrWhiteSpace($envCXX)) {
        $cc = Resolve-CompilerPath $envCC
        $cxx = Resolve-CompilerPath $envCXX

        if ([string]::IsNullOrWhiteSpace($cc)) {
            $cc = $cxx
        }

        if ([string]::IsNullOrWhiteSpace($cxx)) {
            $cxx = $cc
        }

        return @{
            C = $cc
            CXX = $cxx
            ASM = Resolve-CompilerPath $envASM
            Name = "environment override"
        }
    }

    if (Test-IsWindows) {
        Import-VisualStudioEnvironment | Out-Null

        $clangCl = Find-ExecutableInKnownLocations @("clang-cl.exe")

        if ($clangCl) {
            return @{
                C = $clangCl
                CXX = $clangCl
                ASM = $clangCl
                Name = "Visual Studio clang-cl"
            }
        }

        $cl = Find-ExecutableInKnownLocations @("cl.exe")

        if ($cl) {
            return @{
                C = $cl
                CXX = $cl
                ASM = $cl
                Name = "Visual Studio MSVC cl"
            }
        }
    }

    $clang = Find-ExecutableInKnownLocations @("clang.exe", "clang")
    $clangxx = Find-ExecutableInKnownLocations @("clang++.exe", "clang++")

    if ($clang -and $clangxx) {
        return @{
            C = $clang
            CXX = $clangxx
            ASM = $clang
            Name = "LLVM clang"
        }
    }

    $gcc = Find-ExecutableInKnownLocations @("gcc.exe", "gcc", "cc")
    $gxx = Find-ExecutableInKnownLocations @("g++.exe", "g++", "c++")

    if ($gcc -and $gxx) {
        return @{
            C = $gcc
            CXX = $gxx
            ASM = $gcc
            Name = "GNU compiler"
        }
    }

    return $null
}

function Remove-BadCMakeCompilerCache {
    param([Parameter(Mandatory = $true)][string]$BuildDirectory)

    $cacheFile = Join-Path $BuildDirectory "CMakeCache.txt"
    $cmakeFiles = Join-Path $BuildDirectory "CMakeFiles"

    if (-not (Test-Path $cacheFile -PathType Leaf)) {
        return
    }

    $cacheText = Get-Content -Path $cacheFile -Raw

    if ($cacheText -match 'CMAKE_(C|CXX|ASM)_COMPILER[^=]*=.*-NOTFOUND') {
        Write-Host "Removing failed CMake compiler cache:"
        Write-Host "  $cacheFile"

        Remove-Item -Force $cacheFile

        if (Test-Path $cmakeFiles -PathType Container) {
            Remove-Item -Recurse -Force $cmakeFiles
        }
    }
}

$LLVM_DIR = ""
$BUILD_TARGET_TRIPLE = Get-DefaultTargetTriple
$BUILD_DIR = ""
$BUILD_TYPE = "Release"
$JOBS = Get-DefaultJobs
$INTERACTIVE = $false

$positional = New-Object System.Collections.Generic.List[string]

foreach ($arg in $args) {
    switch ($arg) {
        "--interactive" {
            $INTERACTIVE = $true
            continue
        }
        "-h" {
            Show-Usage
            exit 0
        }
        "--help" {
            Show-Usage
            exit 0
        }
        default {
            if ($arg.StartsWith("-")) {
                Write-Host "ERROR: Unknown option: $arg"
                exit 1
            }

            $positional.Add($arg)
        }
    }
}

if ($positional.Count -gt 4) {
    Write-Host "ERROR: Too many arguments."
    Write-Host ""
    Show-Usage
    exit 1
}

if ($positional.Count -ge 1) {
    $LLVM_DIR = $positional[0]
}

if ($positional.Count -ge 2) {
    $BUILD_DIR = $positional[1]
}

if ($positional.Count -ge 3) {
    $BUILD_TYPE = $positional[2]
}

if ($positional.Count -ge 4) {
    $parsedJobs = 0

    if (-not [int]::TryParse($positional[3], [ref]$parsedJobs) -or $parsedJobs -le 0) {
        Write-Host "ERROR: Jobs must be a positive integer: $($positional[3])"
        exit 1
    }

    $JOBS = $parsedJobs
}

if ([string]::IsNullOrWhiteSpace($LLVM_DIR)) {
    Write-Host "ERROR: Missing LLVM source directory."
    Write-Host ""
    Write-Host "Usage:"
    Write-Host "  scripts/build-llvm.ps1 <llvm-dir> [build-dir] [build-type] [jobs] [--interactive]"
    exit 1
}

$llvmSourceDir = Join-Path $LLVM_DIR "llvm"

if (-not (Test-Path $llvmSourceDir -PathType Container)) {
    Write-Host "ERROR: Could not find LLVM source directory:"
    Write-Host "  $llvmSourceDir"
    exit 1
}

if ([string]::IsNullOrWhiteSpace($BUILD_DIR)) {
    $resolvedLlvmDir = (Resolve-Path $LLVM_DIR).Path
    $llvmParentDir = Split-Path -Parent $resolvedLlvmDir
    $BUILD_DIR = Join-Path $llvmParentDir "build-$BUILD_TARGET_TRIPLE"
}

Prompt-DebugBuild

if (-not (Has-Command "cmake")) {
    Write-Host "ERROR: CMake was not found."
    Write-Host "Please install CMake and make sure it is available in PATH."
    exit 1
}

if (Has-Command "ninja") {
    $GENERATOR = "Ninja"
} elseif (Has-Command "ninja-build") {
    $GENERATOR = "Ninja"
} else {
    Write-Host "ERROR: Ninja was not found."
    Write-Host "Please install ninja or ninja-build."
    exit 1
}

$generatorOverride = [Environment]::GetEnvironmentVariable("CLANG_MG_GENERATOR")

if (-not [string]::IsNullOrWhiteSpace($generatorOverride)) {
    $GENERATOR = $generatorOverride
}

$toolchain = Find-CMakeToolchain

if ($null -eq $toolchain) {
    Write-Host "ERROR: No usable C/C++ compiler was found."
    Write-Host ""
    Write-Host "On Windows, install one of these:"
    Write-Host "  - Visual Studio Build Tools with C++ tools and Clang tools"
    Write-Host "  - Visual Studio with Desktop development with C++"
    Write-Host "  - LLVM for Windows"
    Write-Host ""
    Write-Host "Then either run from Developer PowerShell, or let this script load VsDevCmd automatically."
    Write-Host ""
    Write-Host "You can also override manually:"
    Write-Host '  $env:CC="C:\path\to\clang-cl.exe"'
    Write-Host '  $env:CXX="C:\path\to\clang-cl.exe"'
    exit 1
}

Remove-BadCMakeCompilerCache $BUILD_DIR

Write-Host ""
Write-Host "Configuring LLVM build..."
Write-Host "LLVM dir:      $LLVM_DIR"
Write-Host "Target triple: $BUILD_TARGET_TRIPLE"
Write-Host "Build dir:     $BUILD_DIR"
Write-Host "Build type:    $BUILD_TYPE"
Write-Host "Jobs:          $JOBS"
Write-Host "Generator:     $GENERATOR"
Write-Host "Toolchain:     $($toolchain.Name)"
Write-Host "C compiler:    $($toolchain.C)"
Write-Host "CXX compiler:  $($toolchain.CXX)"
if (-not [string]::IsNullOrWhiteSpace($toolchain.ASM)) {
    Write-Host "ASM compiler:  $($toolchain.ASM)"
}
Write-Host ""

$cmakeConfigureArgs = @(
    "-S", $llvmSourceDir,
    "-B", $BUILD_DIR,
    "-G", $GENERATOR,
    "-DLLVM_ENABLE_PROJECTS=clang",
    "-DCMAKE_BUILD_TYPE=$BUILD_TYPE",
    "-DLLVM_ENABLE_ASSERTIONS=ON",
    "-DCMAKE_C_COMPILER=$($toolchain.C)",
    "-DCMAKE_CXX_COMPILER=$($toolchain.CXX)"
)

if (-not [string]::IsNullOrWhiteSpace($toolchain.ASM)) {
    $cmakeConfigureArgs += "-DCMAKE_ASM_COMPILER=$($toolchain.ASM)"
}

Invoke-CommandChecked "cmake" $cmakeConfigureArgs

Write-Host ""
Write-Host "Building clang..."

Invoke-CommandChecked "cmake" @(
    "--build", $BUILD_DIR,
    "--target", "clang",
    "--",
    "-j", "$JOBS"
)

Write-Host ""
Write-Host "Build complete."

$clangMgExe = Join-Path $BUILD_DIR "bin/clang-mg.exe"
$clangMg = Join-Path $BUILD_DIR "bin/clang-mg"
$clangExe = Join-Path $BUILD_DIR "bin/clang.exe"
$clang = Join-Path $BUILD_DIR "bin/clang"

if (Test-Path $clangMgExe -PathType Leaf) {
    Write-Host "Built: $clangMgExe"
    try { & $clangMgExe --version } catch { }
} elseif (Test-Path $clangMg -PathType Leaf) {
    Write-Host "Built: $clangMg"
    try { & $clangMg --version } catch { }
} elseif (Test-Path $clangExe -PathType Leaf) {
    Write-Host "Built: $clangExe"
    try { & $clangExe --version } catch { }
} elseif (Test-Path $clang -PathType Leaf) {
    Write-Host "Built: $clang"
    try { & $clang --version } catch { }
} else {
    Write-Host "WARNING: Build finished, but no clang or clang-mg binary was found in:"
    Write-Host "  $(Join-Path $BUILD_DIR 'bin')"
}
