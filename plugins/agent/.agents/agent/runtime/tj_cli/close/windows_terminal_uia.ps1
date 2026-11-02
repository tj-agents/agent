param(
    [string] $TabId,
    [switch] $Json,
    [switch] $Focus
)
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName UIAutomationClient, UIAutomationTypes
$root = [System.Windows.Automation.AutomationElement]::RootElement
$windowType = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
    [System.Windows.Automation.ControlType]::Window)
$tabs = @()
foreach ($window in @($root.FindAll([System.Windows.Automation.TreeScope]::Children, $windowType))) {
    $process = Get-Process -Id $window.Current.ProcessId -ErrorAction SilentlyContinue
    if (-not $process -or $process.ProcessName -notin @('WindowsTerminal', 'WindowsTerminalPreview')) { continue }
    $tabType = New-Object System.Windows.Automation.PropertyCondition(
        [System.Windows.Automation.AutomationElement]::ControlTypeProperty,
        [System.Windows.Automation.ControlType]::TabItem)
    foreach ($tab in @($window.FindAll([System.Windows.Automation.TreeScope]::Descendants, $tabType))) {
        $tabs += [pscustomobject]@{ RuntimeId = ($tab.GetRuntimeId() -join ':'); Element = $tab; Window = $window; ProcessId = $window.Current.ProcessId; Title = $tab.Current.Name }
    }
}
if ($Json) {
    $items = @($tabs | ForEach-Object {
        ConvertTo-Json -InputObject ([pscustomobject]@{ runtimeId=$_.RuntimeId; processId=$_.ProcessId; title=$_.Title }) -Compress
    })
    Write-Output ('[' + ($items -join ',') + ']')
    return
}
$matches = @($tabs | Where-Object { $TabId -and $_.RuntimeId -eq $TabId })
if ($matches.Count -ne 1) { throw 'Windows Terminal target is absent or ambiguous.' }
if ($Focus) {
    $matches[0].Element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    $handle = [IntPtr] $matches[0].Window.Current.NativeWindowHandle
    Add-Type -Namespace TjCli -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int c);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
'@
    if ([TjCli.Native]::IsIconic($handle)) { [void] [TjCli.Native]::ShowWindow($handle, 9) }
    [void] [TjCli.Native]::SetForegroundWindow($handle)
    return
}
$close = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'CloseButton')
$button = $matches[0].Element.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $close)
if (-not $button) { throw 'Windows Terminal tab has no close button.' }
$button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
