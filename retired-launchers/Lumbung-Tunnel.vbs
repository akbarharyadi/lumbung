' Cloudflare Tunnel for lumbung.example.com - starts hidden at logon.
' Waits for the dashboard to bind 8788 first. cloudflared would retry anyway,
' but starting into a dead origin logs a burst of errors that look like a fault.
' Config lives in %USERPROFILE%\.cloudflared\config.yml (tunnel UUID + ingress).
' To stop publishing the dashboard, delete this file from the Startup folder.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\akbar\AppWork\TradingAgent"
WScript.Sleep 20000
sh.Run """C:\Users\akbar\AppWork\TradingAgent\tools\cloudflared.exe"" tunnel run lumbung", 0, False
