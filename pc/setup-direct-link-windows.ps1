<#
.SYNOPSIS
  Configure a Windows Ethernet adapter for a direct PC <-> PS5 cable.

.DESCRIPTION
  Gives the adapter a static IP (default 192.168.88.1/24) with no gateway, so
  your Wi-Fi keeps providing internet while the cable carries PS5 traffic only.
  Also marks the link as a Private network and opens the ps5cable port.

  Run from an elevated PowerShell:
    powershell -ExecutionPolicy Bypass -File setup-direct-link-windows.ps1
  Undo:
    powershell -ExecutionPolicy Bypass -File setup-direct-link-windows.ps1 -Revert
#>
param(
  [string]$Adapter,
  [string]$Ip = "192.168.88.1",
  [int]$Port = 9113,
  [switch]$Revert
)

$ErrorActionPreference = "Stop"

$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
  Write-Error "Run this script from an elevated (Administrator) PowerShell."
}

if (-not $Adapter) {
  $candidates = @(Get-NetAdapter -Physical | Where-Object {
    $_.MediaType -eq "802.3" -and $_.Status -eq "Up"
  })
  if ($candidates.Count -eq 0) {
    Write-Host "No connected wired adapter found. Plug the cable into the PS5 and turn it on, then retry."
    Write-Host "Wired adapters on this PC:"
    Get-NetAdapter -Physical | Where-Object MediaType -eq "802.3" | Format-Table Name, InterfaceDescription, Status
    exit 1
  }
  if ($candidates.Count -gt 1) {
    Write-Host "Several wired adapters are connected; pick one with -Adapter:"
    $candidates | Format-Table Name, InterfaceDescription, LinkSpeed
    exit 1
  }
  $Adapter = $candidates[0].Name
}

$nic = Get-NetAdapter -Name $Adapter
Write-Host "Using adapter '$($nic.Name)' ($($nic.InterfaceDescription), $($nic.LinkSpeed))"

if ($Revert) {
  Remove-NetIPAddress -InterfaceAlias $Adapter -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
  Set-NetIPInterface -InterfaceAlias $Adapter -Dhcp Enabled
  Set-DnsClientServerAddress -InterfaceAlias $Adapter -ResetServerAddresses
  Remove-NetFirewallRule -DisplayName "ps5cable" -ErrorAction SilentlyContinue
  Write-Host "Adapter '$Adapter' is back on DHCP."
  exit 0
}

Set-NetIPInterface -InterfaceAlias $Adapter -Dhcp Disabled
Remove-NetIPAddress -InterfaceAlias $Adapter -AddressFamily IPv4 -Confirm:$false -ErrorAction SilentlyContinue
Remove-NetRoute -InterfaceAlias $Adapter -AddressFamily IPv4 -DestinationPrefix "0.0.0.0/0" -Confirm:$false -ErrorAction SilentlyContinue
New-NetIPAddress -InterfaceAlias $Adapter -IPAddress $Ip -PrefixLength 24 | Out-Null

# A public profile blocks the UDP discovery replies.
Start-Sleep -Seconds 2
Get-NetConnectionProfile -InterfaceAlias $Adapter -ErrorAction SilentlyContinue |
  Set-NetConnectionProfile -NetworkCategory Private

Remove-NetFirewallRule -DisplayName "ps5cable" -ErrorAction SilentlyContinue
New-NetFirewallRule -DisplayName "ps5cable" -Direction Inbound -Protocol UDP -LocalPort $Port `
  -InterfaceAlias $Adapter -Action Allow -Profile Any | Out-Null

$ps5 = ($Ip -replace '\.\d+$', '') + ".2"
Write-Host ""
Write-Host "PC side done: $Ip/24 on '$Adapter' (no gateway, Wi-Fi keeps your internet)."
Write-Host ""
Write-Host "On the PS5: Settings > Network > Settings > Set Up Internet Connection >"
Write-Host "  Set Up Manually > Use a LAN Cable > Manual, and enter:"
Write-Host "    IP address:      $ps5"
Write-Host "    Subnet mask:     255.255.255.0"
Write-Host "    Default gateway: $Ip"
Write-Host "    Primary DNS:     $Ip"
Write-Host "  It will report no internet; that is expected."
Write-Host ""
Write-Host "Then test the link with:  ping $ps5"
