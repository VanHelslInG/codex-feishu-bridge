' Launch the bridge's keep-alive script with no console window at all.
'
' Why this exists: Task Scheduler starts a console application (pwsh.exe) with a
' real console attached. -WindowStyle Hidden only hides that console after
' Windows has already created it, which is the terminal flash seen on the
' desktop every time the task fires.
'
' wscript.exe is a GUI-subsystem host, so it never allocates a console, and
' WScript.Shell.Run with a window style of 0 hides the child it starts. That
' combination produces no visible window at any point.
'
' Usage: wscript.exe //nologo run-hidden.vbs <pwsh.exe> <script.ps1>
Option Explicit

Dim shell, command

If WScript.Arguments.Count < 2 Then
    WScript.Echo "usage: run-hidden.vbs <pwsh.exe> <script.ps1>"
    WScript.Quit 1
End If

command = """" & WScript.Arguments(0) & """" _
        & " -NoProfile -NonInteractive -WindowStyle Hidden -File """ _
        & WScript.Arguments(1) & """"

Set shell = CreateObject("WScript.Shell")
' 0 = hidden window, False = do not wait for it to finish.
shell.Run command, 0, False
