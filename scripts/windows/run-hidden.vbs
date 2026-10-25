' TweetPilot sync: run launcher.ps1 without a console window
Set fso = CreateObject("Scripting.FileSystemObject")
here = fso.GetParentFolderName(WScript.ScriptFullName)
cmd = "powershell.exe -NoProfile -ExecutionPolicy Bypass -File """ & here & "\launcher.ps1"""
CreateObject("WScript.Shell").Run cmd, 0, True
