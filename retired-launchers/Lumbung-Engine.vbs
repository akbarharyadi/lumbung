' Lumbung trading engine - starts hidden at logon.
' Mode comes from TA_MODE in .env, NOT hardcoded. That matters: if this forced
' paper mode and you were trading live, a reboot would bring back a paper engine
' while real positions sat unmonitored -- and Indodax has no server-side stops,
' so those positions would have no stop-loss at all.
' Check which mode it will use with: lumbung doctor
Set sh = CreateObject("WScript.Shell")
sh.CurrentDirectory = "C:\Users\akbar\AppWork\TradingAgent"
sh.Run """C:\Users\akbar\AppWork\TradingAgent\.venv\Scripts\pythonw.exe"" -m lumbung.cli run", 0, False
