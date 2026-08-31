' Lumbung dashboard - starts hidden at logon.
'
' Three guards, each answering a different threat:
'   --host 127.0.0.1  nothing on the LAN reaches it; the only way in is the
'                     tunnel, which sits behind Cloudflare Access.
'   --readonly        pause / flat / kill refused, in the chat as well as on
'                     the buttons. Those controls need a dashboard started
'                     without this flag, on the home network.
'   --use-access      trust Cloudflare Access's SIGNED identity instead of
'                     asking for a bearer token. Access has already logged you
'                     in by the time the request arrives, so asking again is
'                     friction with nothing gained. The signature and the
'                     audience are both verified; the plain
'                     Cf-Access-Authenticated-User-Email header is ignored
'                     because anything reaching the origin could forge it.
'
' There used to be a fourth: --require-code, a Telegram login code on top of
' Access. Telegram is gone, and with it the channel that delivered the code.
' Google sign-in and the one-time PIN, both handled by Access at the edge, are
' what now stand between someone holding the link and someone getting in.
'
' The bearer token still works when there is no Access header, so reaching
' 127.0.0.1:8788 directly on this PC is not locked out.
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\akbar\AppWork\TradingAgent"
sh.Run """C:\Users\akbar\AppWork\TradingAgent\.venv\Scripts\pythonw.exe"" -m lumbung.cli dashboard --readonly --use-access --host 127.0.0.1 --port 8788", 0, False
