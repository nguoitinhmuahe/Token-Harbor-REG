
$ErrorActionPreference = "Continue"
$dir = Split-Path -Parent $MyInvocation.MyCommand.Path
$out = Join-Path $dir ".v6_addrs"
$log = Join-Path $dir "logs\v6_up.log"

function Log($m) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Add-Content -Path $log -Value $line -Encoding utf8
}

$route = Get-NetRoute -AddressFamily IPv6 -DestinationPrefix "::/0" -ErrorAction SilentlyContinue |
    Sort-Object RouteMetric | Select-Object -First 1
if (-not $route) { Log "khong co default route IPv6 -> thoat"; exit 0 }
$ifAlias = $route.InterfaceAlias

$addr = Get-NetIPAddress -AddressFamily IPv6 -InterfaceAlias $ifAlias -ErrorAction SilentlyContinue |
    Where-Object { $_.IPAddress -notlike "fe80*" -and $_.IPAddress -ne "::1" } |
    Sort-Object { if ($_.AddressState -eq "Preferred") { 0 } else { 1 } } |
    Select-Object -First 1
if (-not $addr) { Log "khong co dia chi IPv6 global tren $ifAlias -> thoat"; exit 0 }

$prefix = (($addr.IPAddress -split ":")[0..3] -join ":")
Log "interface=$ifAlias prefix=$prefix"

$lines = New-Object System.Collections.Generic.List[string]
$added = 0
for ($i = 1; $i -le 128; $i++) {
    $h = "{0:x}" -f $i
    $a = "$prefix`:6::$h"
    $lines.Add($a)
    $have = Get-NetIPAddress -AddressFamily IPv6 -IPAddress $a -ErrorAction SilentlyContinue
    if (-not $have) {
        netsh interface ipv6 add address "$ifAlias" $a | Out-Null
        $added++
    }
}
$lines | Out-File -FilePath $out -Encoding ascii
Log "xong: $added dia chi moi, tong $($lines.Count) trong $out"
