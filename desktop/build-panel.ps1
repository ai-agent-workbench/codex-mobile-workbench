[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$OutputDirectory
)

Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false)

$desktopDirectory = Split-Path -Parent $PSCommandPath
$sourceRoot = Split-Path -Parent $desktopDirectory
$resolvedOutput = [System.IO.Path]::GetFullPath($OutputDirectory)
$resolvedRoot = [System.IO.Path]::GetFullPath($sourceRoot)
if ($resolvedOutput.Equals($resolvedRoot, [System.StringComparison]::OrdinalIgnoreCase) -or
    $resolvedOutput.StartsWith($resolvedRoot + [System.IO.Path]::DirectorySeparatorChar, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw '输出目录必须位于源码仓库之外；请指定任务私有输出目录。'
}

$cscCandidates = @(
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'),
    (Join-Path $env:WINDIR 'Microsoft.NET\Framework\v4.0.30319\csc.exe')
)
$csc = $cscCandidates | Where-Object { Test-Path -LiteralPath $_ } | Select-Object -First 1
if (-not $csc) {
    throw '未找到 Windows 自带 .NET Framework csc.exe。未安装 SDK，也不会自动安装。'
}

New-Item -ItemType Directory -Force -Path $resolvedOutput | Out-Null
$outputFile = Join-Path $resolvedOutput 'CodexWorkbenchPanel.exe'
$sourceFile = Join-Path $desktopDirectory 'WorkbenchPanel.cs'

& $csc /nologo /target:winexe /utf8output ("/out:{0}" -f $outputFile) `
    /reference:System.Windows.Forms.dll /reference:System.Drawing.dll /reference:System.Web.Extensions.dll $sourceFile
if ($LASTEXITCODE -ne 0) {
    throw "编译失败，csc.exe 退出码：$LASTEXITCODE"
}

Write-Output "已在任务输出目录生成：$outputFile"
