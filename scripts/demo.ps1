<#
.SYNOPSIS
  Sends the ChimericMesh demo traffic over real Syslog, Telnet and IRC connections.

.EXAMPLE
  .\scripts\demo.ps1                 # all three, paced
  .\scripts\demo.ps1 -Only syslog    # just the database disk-full burst
  .\scripts\demo.ps1 -Count 12       # enough events to trigger an UPDATE line
#>
param(
    [ValidateSet("all", "syslog", "telnet", "irc")] [string]$Only = "all",
    [int]$Count = 3,
    [int]$DelayMs = 600,
    [string]$Server = "127.0.0.1",
    [int]$SyslogPort = 5514,
    [int]$TelnetPort = 2323,
    [int]$IrcPort = 6667
)

$ErrorActionPreference = "Stop"
$ascii = [Text.Encoding]::ASCII

function Send-Syslog {
    $udp = New-Object Net.Sockets.UdpClient
    try {
        for ($i = 0; $i -lt $Count; $i++) {
            $line = "<27>Nov 13 11:10:08 db01 postgres[4121]: FATAL: could not write block $(8801 + $i): No space left on device"
            $bytes = $ascii.GetBytes($line)
            [void]$udp.Send($bytes, $bytes.Length, $Server, $SyslogPort)
            Write-Host "syslog  > db01 block $(8801 + $i): No space left on device"
            Start-Sleep -Milliseconds $DelayMs
        }
    } finally { $udp.Close() }
}

function Send-Telnet {
    $tcp = New-Object Net.Sockets.TcpClient($Server, $TelnetPort)
    try {
        $stream = $tcp.GetStream()
        $stream.ReadTimeout = 2000
        $buf = New-Object byte[] 256
        [void]$stream.Read($buf, 0, $buf.Length)          # banner
        for ($i = 0; $i -lt $Count; $i++) {
            # IAC WILL ECHO first, like a real telnet client; the mesh strips it.
            $iac = [byte[]](255, 251, 1)
            $text = $ascii.GetBytes("ALERT sw-core-1: Failed password for admin from 203.0.113.9 port $(441 + $i)`r`n")
            $payload = [byte[]]($iac + $text)
            $stream.Write($payload, 0, $payload.Length)
            [void]$stream.Read($buf, 0, $buf.Length)       # "ack"
            Write-Host "telnet  > sw-core-1 Failed password for admin (port $(441 + $i))"
            Start-Sleep -Milliseconds $DelayMs
        }
    } finally { $tcp.Close() }
}

function Send-Irc {
    $tcp = New-Object Net.Sockets.TcpClient($Server, $IrcPort)
    try {
        $stream = $tcp.GetStream()
        # Fixed demo lines only; CRLF is IRC line framing for this client.
        $send = {
            param($s)
            $b = $ascii.GetBytes("$s`r`n")
            $stream.Write($b, 0, $b.Length)
        }
        & $send "NICK opsdemo"
        & $send "USER opsdemo 0 * :Ops demo"
        & $send "JOIN #ops"
        Start-Sleep -Milliseconds 400
        for ($i = 0; $i -lt $Count; $i++) {
            & $send "PRIVMSG #ops :web02: upstream timed out connecting to 10.0.4.17:5432"
            Write-Host "irc     > #ops web02: upstream timed out"
            Start-Sleep -Milliseconds $DelayMs
        }
        & $send "QUIT"
    } finally { $tcp.Close() }
}

try {
    if ($Only -in "all", "syslog") { Send-Syslog }
    if ($Only -in "all", "telnet") { Send-Telnet }
    if ($Only -in "all", "irc")    { Send-Irc }
    Write-Host "`nDone. Check the dashboard at http://${Server}:8000"
} catch {
    Write-Error "Could not reach the mesh at ${Server}. Is 'python -m chimericmesh' running? $_"
}
