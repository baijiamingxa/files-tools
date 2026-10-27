$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Windows.Forms
$appRoot = [IO.Path]::GetFullPath($PSScriptRoot).TrimEnd('\')
$manifestPath = Join-Path $appRoot '安装清单.json'
try {
    $record = Get-Content -LiteralPath $manifestPath -Raw -Encoding UTF8 | ConvertFrom-Json
    if ($record.id -ne 'TxtEncoder.FileToolbox.Install' -or [IO.Path]::GetFullPath($record.root).TrimEnd('\') -ne $appRoot) { throw '安装清单与当前目录不符，已停止卸载。' }
    $choice = [Windows.Forms.MessageBox]::Show('确定卸载文件工具箱？下载结果、个人配置和未列入安装清单的文件将保留。', '卸载文件工具箱', 'YesNo', 'Question')
    if ($choice -ne 'Yes') { exit 0 }
    $targets = @()
    foreach ($name in $record.files.PSObject.Properties.Name) {
        $target = [IO.Path]::GetFullPath((Join-Path $appRoot $name))
        if (-not $target.StartsWith($appRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { throw '安装清单包含越界路径，已停止卸载。' }
        $inspect = $target
        while ($inspect.StartsWith($appRoot + '\', [StringComparison]::OrdinalIgnoreCase)) {
            if ((Test-Path -LiteralPath $inspect) -and ((Get-Item -LiteralPath $inspect -Force).Attributes -band [IO.FileAttributes]::ReparsePoint)) { throw '安装目录包含链接或重解析点，已停止卸载。' }
            $inspect = [IO.Path]::GetDirectoryName($inspect)
        }
        $targets += $target
    }
    foreach ($target in $targets) {
        if ((Test-Path -LiteralPath $target -PathType Leaf) -and $target -notlike '*.ps1') {
            $probe = [IO.File]::Open($target, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::None)
            $probe.Dispose()
        }
    }
    $associationKey = 'HKCU:\Software\Classes\TxtEncoder.FileToolbox'
    if ((Get-ItemProperty -LiteralPath $associationKey -ErrorAction SilentlyContinue).InstallDir -eq $appRoot) {
        $helper = Start-Process -FilePath (Join-Path $appRoot '文件工具箱.exe') -ArgumentList '--uninstall-file-menu' -WindowStyle Hidden -Wait -PassThru
        if ($helper.ExitCode -ne 0) { throw '移除当前安装的右键菜单失败，请在工具箱中先移除后重试。' }
    }
    foreach ($target in $targets) {
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Force }
    }
    $wsh = New-Object -ComObject WScript.Shell
    foreach ($shortcutPath in $record.shortcuts) {
        $allowedMenu = [IO.Path]::GetFullPath((Join-Path $env:APPDATA 'Microsoft\Windows\Start Menu\Programs\文件工具箱')) + '\'
        $shortcutFull = [IO.Path]::GetFullPath($shortcutPath)
        if ($shortcutFull.StartsWith($allowedMenu, [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $shortcutFull)) {
            $shortcut = $wsh.CreateShortcut($shortcutFull)
            if ($shortcut.TargetPath -eq (Join-Path $appRoot '文件工具箱.exe')) { Remove-Item -LiteralPath $shortcutFull -Force }
        }
    }
    $uninstallKey = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Uninstall\TxtEncoderFileToolbox'
    if ((Get-ItemProperty -LiteralPath $uninstallKey -ErrorAction SilentlyContinue).InstallLocation -eq $appRoot) { Remove-Item -LiteralPath $uninstallKey }
    Remove-Item -LiteralPath $manifestPath -Force
    $directories = @{}
    foreach ($target in $targets) {
        $parent = [IO.Path]::GetDirectoryName($target)
        while ($parent.StartsWith($appRoot + '\', [StringComparison]::OrdinalIgnoreCase)) { $directories[$parent] = $true; $parent = [IO.Path]::GetDirectoryName($parent) }
    }
    foreach ($directory in ($directories.Keys | Sort-Object Length -Descending)) {
        $resolved = [IO.Path]::GetFullPath($directory)
        if ($resolved.StartsWith($appRoot + '\', [StringComparison]::OrdinalIgnoreCase) -and (Test-Path -LiteralPath $resolved -PathType Container) -and -not (Get-ChildItem -LiteralPath $resolved -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $resolved }
    }
    if (-not (Get-ChildItem -LiteralPath $appRoot -Force | Select-Object -First 1)) { Remove-Item -LiteralPath $appRoot }
    [Windows.Forms.MessageBox]::Show('文件工具箱已卸载。个人配置和运行组件仍保留在用户目录。', '卸载完成') | Out-Null
} catch {
    [Windows.Forms.MessageBox]::Show(('卸载未完成，请先关闭工具箱后重试。' + "`n" + $_.Exception.Message), '卸载提示') | Out-Null
    exit 1
}
