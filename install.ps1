param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" })
)

$ErrorActionPreference = "Stop"
$name = "step_pipeline"
$src = Join-Path $PSScriptRoot "step_pipeline"
$dst = Join-Path $HermesHome "plugins\$name"

if (-not (Test-Path $src)) { throw "не найден пакет плагина: $src" }

# Сторож «одного имени»: name в манифесте обязан совпадать с каталогом и командами ниже,
# иначе `plugins update`/`remove` ищут один каталог, а `plugins list` показывает другое имя.
$manifestLine = Select-String -Path (Join-Path $src "plugin.yaml") -Pattern '^name:\s*(.+)$' |
    Select-Object -First 1
$manifestName = if ($manifestLine) { $manifestLine.Matches[0].Groups[1].Value.Trim() } else { "" }
if ($manifestName -ne $name) {
    Write-Host "манифест $(Join-Path $src 'plugin.yaml') называет плагин «$manifestName», а каталог и команды ждут «$name»" -ForegroundColor Red
    Write-Host "поправьте name: в plugin.yaml — расхождение ломает plugins update/remove" -ForegroundColor Red
    exit 1
}

Write-Host "1/3 копирую плагин → $dst"
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-Item $src $dst -Recurse -Force

Write-Host "2/3 включаю плагин и чищу кэш байткода"
Get-ChildItem $dst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
hermes plugins enable $name

Write-Host "3/3 готово"
Write-Host "Дальше в каталоге проекта:  hermes pipeline setup"
Write-Host "UI:                          hermes pipeline ui"
Write-Host "Удаление:                    hermes plugins disable $name; hermes plugins remove $name"
