---
name: Bug report
about: Something isn't working
labels: bug
---

## What happened

<!-- What you expected, and what you got instead. -->

## Steps to reproduce

1.
2.

## Environment

<!-- Airo runs on macOS, Windows and Linux; please fill in the line for yours. -->

```
OS            :
  macOS       : (sw_vers -productVersion)
  Windows     : (powershell -c "[System.Environment]::OSVersion.Version.ToString()")
  Linux       : (cat /etc/os-release | head -2)  — and your desktop, if the tray is involved
Python        : (python3 --version)   — on Windows, (py --version) or (python --version)
Commit        : (git rev-parse --short HEAD)
Tray          : built and installed? Run --print-menu and paste what it says.
  macOS/Linux : (./tray/target/release/airo-tray --print-menu)
  Windows     : (.\tray\target\release\airo-tray.exe --print-menu)
```

## Health check output

<!-- Paste the full output of python3 poller.py --doctor -->

```
```

## Relevant log lines

<!-- From ~/.airo/data/poller.log (or the data_dir you configured).
     ⚠ CHECK THESE FOR YOUR API KEY BEFORE PASTING. -->

```
```

## Anything else

<!-- Screenshots help for dashboard issues. Note that a location-tagged air quality
     history is personal data — consider trimming a CSV before attaching it. -->
