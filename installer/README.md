# Installer — Claude Code Usage Collector

Builds a single Windows `.exe` installer that:

- Bundles [collector.py](../collector/collector.py) into a single-file `.exe`
- Shows a consent screen explaining what gets collected
- Prompts for the server URL + ingest token (or accepts them as
  command-line flags for silent rollouts)
- Installs to `%ProgramFiles%\ClaudeUsageCollector\`
- Writes `config.json` from the values entered
- Registers a Windows Scheduled Task that runs `ClaudeUsageCollector.exe push`
  every 15 minutes
- Adds an "Add or remove programs" entry that cleanly removes the task and
  files on uninstall

## Build prerequisites

1. **Python 3.8+** on PATH
2. **PyInstaller** (auto-installed by `build_exe.ps1` if missing)
3. **Inno Setup 6** — install from <https://jrsoftware.org/isinfo.php>.
   The compiler `ISCC.exe` ends up in `C:\Program Files (x86)\Inno Setup 6\`.

## Build steps

From an elevated PowerShell in `installer/`:

```powershell
# 1. Build the standalone .exe
powershell -ExecutionPolicy Bypass -File .\build_exe.ps1

# 2. Compile the installer (path may vary by Inno Setup version)
& "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" .\setup.iss
```

Output: `installer\Output\ClaudeUsageCollector-Setup-1.0.0.exe`.

Ship that single file to teammates.

## End-user install modes

### Interactive (default)

Double-click the setup `.exe`. The wizard:

1. Welcome screen
2. **Consent screen** (loaded from [CONSENT.txt](CONSENT.txt))
3. Install location
4. **Server connection page** — paste the dashboard URL and ingest token
5. Install → first push runs immediately → Scheduled Task registered

### Silent (for IT-managed fleet rollouts)

```powershell
ClaudeUsageCollector-Setup-1.0.0.exe `
    /VERYSILENT /SUPPRESSMSGBOXES `
    /SERVERURL=https://your-app.vercel.app `
    /TOKEN=YOUR_SHARED_INGEST_TOKEN
```

Suitable for GPO, PDQ Deploy, SCCM, Intune.

## What gets installed where

| Path | Purpose |
|---|---|
| `%ProgramFiles%\ClaudeUsageCollector\ClaudeUsageCollector.exe` | The collector binary |
| `%ProgramFiles%\ClaudeUsageCollector\config.json` | Server URL + ingest token (written from wizard values) |
| `%ProgramFiles%\ClaudeUsageCollector\CONSENT.txt` | Audit copy of what the user agreed to |
| Scheduled Task: `ClaudeCodeUsageCollector` | Runs `… push` every 15 min |
| `%LOCALAPPDATA%\ClaudeUsageCollector\state.json` | Per-machine upload history |
| `%LOCALAPPDATA%\ClaudeUsageCollector\collector.log` | Rolling log (capped ~5 MB) |

## Uninstall

`Add or remove programs` → **Claude Code Usage Collector** → Uninstall.

Removes: the Scheduled Task, the install directory, and `config.json`.

Leaves in place (intentional — diagnostic value if a teammate reports an
issue after uninstalling): `%LOCALAPPDATA%\ClaudeUsageCollector\`. The user
can delete that folder manually if they want a clean wipe.

Server-side data is never touched by uninstall — an admin must remove the
machine row in the dashboard if they want to purge it.
