Option Explicit

Dim fso, sh, app, pyw, cmd
Set fso = CreateObject("Scripting.FileSystemObject")
Set sh  = CreateObject("WScript.Shell")

app = fso.GetParentFolderName(WScript.ScriptFullName)
If Not fso.FileExists(app & "\guard.py") Then
    app = "D:\Desktop\m"
End If

pyw = sh.ExpandEnvironmentStrings("%LOCALAPPDATA%") & _
      "\Programs\Python\Python311\pythonw.exe"
If Not fso.FileExists(pyw) Then
    pyw = "pythonw.exe"          ' last resort: rely on PATH
End If

sh.CurrentDirectory = app
cmd = Chr(34) & pyw & Chr(34) & " " & Chr(34) & app & "\guard.py" & Chr(34)
sh.Run cmd, 0, False             ' 0 = hidden window, False = do not wait
