param(
    [string]$HermesHome = $(if ($env:HERMES_HOME) { $env:HERMES_HOME } else { Join-Path $env:LOCALAPPDATA "hermes" })
)

# ─────────────────────────────────────────────────────────────────────────────
# Файл сохранён в UTF-8 С BOM — это не случайность. Windows PowerShell 5.1
# (powershell.exe) читает .ps1 без BOM как ANSI: кириллица в строках превращается в
# мусор, а кавычки-ёлочки рвут разбор — скрипт падал с «Отсутствует закрывающий
# знак "}"» ещё до первой команды. Не пересохранять без BOM: pwsh 7 BOM не требует,
# но и не мешает, а 5.1 без него не работает.
# ─────────────────────────────────────────────────────────────────────────────

$ErrorActionPreference = "Stop"
$name = "step_pipeline"
$src = Join-Path $PSScriptRoot "step_pipeline"

if (-not (Test-Path $src)) { throw "не найден пакет плагина: $src" }
if (-not (Get-Command hermes -ErrorAction SilentlyContinue)) {
    throw "команда hermes не найдена в PATH — без неё плагин не включить (см. установку Hermes Agent)"
}

# HERMES_HOME внутри сессии Hermes указывает на каталог ПРОФИЛЯ
# (%LOCALAPPDATA%\hermes\profiles\<имя>), а плагины Hermes читает из общего каталога
# (%LOCALAPPDATA%\hermes\plugins): установка в профиль оставляет панель на старом коде —
# ровно на это наступили 12.11.2026. Профильный сегмент снимаем.
$home_ = $HermesHome.TrimEnd('\', '/')
if ((Split-Path (Split-Path $home_ -Parent) -Leaf) -eq "profiles") {
    $home_ = Split-Path (Split-Path $home_ -Parent) -Parent
    Write-Host "HERMES_HOME указывает на профиль — ставлю в общий каталог Hermes: $home_"
}
$dst = Join-Path $home_ "plugins\$name"

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

Write-Host "1/3 копирую плагин -> $dst"
New-Item -ItemType Directory -Force -Path (Split-Path $dst) | Out-Null
if (Test-Path $dst) { Remove-Item $dst -Recurse -Force }
Copy-Item $src $dst -Recurse -Force

Write-Host "2/3 включаю плагин и чищу кэш байткода"
Get-ChildItem $dst -Recurse -Directory -Filter "__pycache__" | Remove-Item -Recurse -Force
# Команды Hermes ищут плагины в $HERMES_HOME\plugins: без выравнивания переменной они
# смотрят в профиль, не находят там только что скопированный плагин и падают с
# «No plugin named». Выравниваем на тот каталог, куда копировали.
$env:HERMES_HOME = $home_
hermes plugins enable $name
if ($LASTEXITCODE -ne 0) { throw "hermes plugins enable $name завершился с кодом $LASTEXITCODE" }

$shown = hermes plugins show $name 2>&1 | Out-String
if ($shown -notmatch "Status:\s*enabled") {
    throw "плагин скопирован в $dst, но Hermes не считает его включённым: hermes plugins show $name не сообщает «Status: enabled»"
}

# Копия в профиле не читается Hermes, но путает: рядом лежит другой код.
# Ссылка (junction/symlink) — не копия: через неё Hermes как раз и видит плагин в профиле,
# поэтому совет «удалите» здесь означал бы «удалите плагин из профиля».
$staleCopy = Join-Path $HermesHome "plugins\$name"
if ($home_ -ne $HermesHome.TrimEnd('\', '/') -and (Test-Path $staleCopy)) {
    $stale = Get-Item -LiteralPath $staleCopy -Force
    $isLink = [bool]($stale.Attributes -band [IO.FileAttributes]::ReparsePoint)
    if ($isLink) {
        Write-Host "каталог профиля — ссылка на пакет ($staleCopy): Hermes читает плагин через неё, удалять не нужно"
    } else {
        Write-Host "внимание: в каталоге профиля осталась копия плагина ($staleCopy) — Hermes её не читает, удалите:" -ForegroundColor Yellow
        Write-Host "  Remove-Item -Recurse -Force `"$staleCopy`"" -ForegroundColor Yellow
    }
}

Write-Host "3/3 готово: $dst"
Write-Host "Если панель была запущена, она держит прежний код в памяти — перезапустите её:"
Write-Host "  hermes pipeline ui --stop     (затем снова: hermes pipeline ui)"
Write-Host "Дальше в каталоге проекта:  hermes pipeline setup"
Write-Host "UI:                          hermes pipeline ui"
Write-Host "Удаление:                    hermes plugins disable $name; hermes plugins remove $name"
