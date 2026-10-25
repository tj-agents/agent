param(
    [string] $TabId,
    [string] $Title,
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
        $tabs += [pscustomobject]@{ Title = $tab.Current.Name; AutomationId = $tab.Current.AutomationId; Element = $tab; Window = $window }
    }
}
if ($Json) {
    # ConvertTo-Json enumerates a single pipeline object differently from an array. Build the
    # brackets ourselves so Windows PowerShell 5.1 emits [] for zero tabs and an array for one or many.
    $items = @($tabs | ForEach-Object {
        ConvertTo-Json -InputObject ([pscustomobject]@{
            title = $_.Title
            automationId = $_.AutomationId
        }) -Compress
    })
    Write-Output ('[' + ($items -join ',') + ']')
    return
}
if (-not $TabId -and -not $Title) { throw 'Specify -TabId or -Title.' }
$matches = @($tabs | Where-Object {
    ($TabId -and $_.AutomationId -eq $TabId) -or ($Title -and $_.Title -eq $Title)
})
if ($matches.Count -ne 1) { throw 'Windows Terminal target is absent or ambiguous.' }
if ($Focus) {
    # Select the tab inside its window, then raise and focus that window. No keystrokes are sent.
    $matches[0].Element.GetCurrentPattern([System.Windows.Automation.SelectionItemPattern]::Pattern).Select()
    $handle = [IntPtr] $matches[0].Window.Current.NativeWindowHandle
    Add-Type -Namespace OpenCli -Name Native -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool ShowWindow(System.IntPtr h, int c);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool IsIconic(System.IntPtr h);
[System.Runtime.InteropServices.DllImport("user32.dll")] public static extern bool SetForegroundWindow(System.IntPtr h);
'@
    if ([OpenCli.Native]::IsIconic($handle)) { [void] [OpenCli.Native]::ShowWindow($handle, 9) }
    $raised = [OpenCli.Native]::SetForegroundWindow($handle)
    if (-not $raised) { [Console]::Error.WriteLine("Windows Terminal tab '$($matches[0].Title)' was selected, but Windows did not raise its window; look for it in the taskbar.") }
    return
}
$close = New-Object System.Windows.Automation.PropertyCondition(
    [System.Windows.Automation.AutomationElement]::AutomationIdProperty, 'CloseButton')
$button = $matches[0].Element.FindFirst([System.Windows.Automation.TreeScope]::Descendants, $close)
if (-not $button) { throw "Windows Terminal tab '$($matches[0].Title)' has no close button." }
$button.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke()
