$ErrorActionPreference = 'Stop'
$pythonw = (Get-Command pythonw.exe -ErrorAction Stop).Source
$desktop = [Environment]::GetFolderPath('Desktop')
if (-not (Test-Path -LiteralPath $desktop -PathType Container)) { throw 'Desktop folder not found' }
$shortcutPath = Join-Path $desktop 'dede工作台.lnk'
$iconPath = Join-Path $PSScriptRoot 'agent_workbench\assets\dede.ico'
if (-not (Test-Path -LiteralPath $iconPath -PathType Leaf)) { throw 'Workbench icon not found' }
$shell = New-Object -ComObject WScript.Shell
$shortcut = $shell.CreateShortcut($shortcutPath)
if ((Test-Path -LiteralPath $shortcutPath) -and
    ($shortcut.WorkingDirectory -ne $PSScriptRoot -or $shortcut.Arguments -ne '-m agent_workbench.launcher')) {
    throw 'A different shortcut already uses this name; refusing to overwrite it'
}
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = '-m agent_workbench.launcher'
$shortcut.WorkingDirectory = $PSScriptRoot
$shortcut.IconLocation = "$iconPath,0"
$shortcut.Description = '打开 dede工作台；已运行时直接打开页面'
$shortcut.WindowStyle = 7
$shortcut.Save()
$saved = $shell.CreateShortcut($shortcutPath)
if ($saved.TargetPath -ne $pythonw -or $saved.WorkingDirectory -ne $PSScriptRoot -or $saved.IconLocation -ne "$iconPath,0") {
    throw 'Shortcut verification failed'
}
Write-Output "DESKTOP_SHORTCUT_CREATED: $shortcutPath"
