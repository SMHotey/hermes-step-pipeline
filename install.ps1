param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" })
)

$ErrorActionPreference = "Stop"
$src = Join-Path $PSScriptRoot "step_pipeline"
$dst = Join-Path $HermesHome "plugins\step_pipeline"

if (-not (Test-Path $src)) { throw "не найден пакет плагина: $src" }

Write-Host "1/3 копирую плагин → $dst"
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-Item $src $dst -Recurse -Force

Write-Host "2/3 включаю плагин и чищу кэш байткода"
Get-ChildItem $dst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
hermes plugins enable step_pipeline

Write-Host "3/3 готово"
Write-Host "Дальше в каталоге проекта:  hermes pipeline setup"
Write-Host "UI:                          hermes pipeline ui"
