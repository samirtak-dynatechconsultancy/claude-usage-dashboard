# Claude Code Usage Collector

End-user agent that scans Claude Code's local JSONL transcripts and pushes
parsed metadata + raw conversation content to the team dashboard server.

For end users, this is installed silently via the Windows installer in
[installer/](../installer/) and runs as a Scheduled Task every 15 minutes.
You almost certainly want to start there.

## Running manually (for development)

```powershell
# stdlib only — no pip install needed
cd collector
copy config.example.json config.json
notepad config.json    # fill in server_url + ingest_token

python collector.py status            # show config + state
python collector.py push --dry-run    # list files that would upload
python collector.py push              # actually upload
python collector.py reset-state       # forget upload history
```

## Config file lookup order

1. `--config <path>` CLI flag
2. `$env:COLLECTOR_CONFIG`
3. Directory containing the running script / .exe
4. `%APPDATA%\ClaudeUsageCollector\config.json`
5. Current working directory

## Local state

- **State:** `%LOCALAPPDATA%\ClaudeUsageCollector\state.json`
  Tracks file mtimes and SHA-256 hashes so re-runs only upload what changed.
- **Logs:** `%LOCALAPPDATA%\ClaudeUsageCollector\collector.log`
  Rotated when the file exceeds 5 MB (keeps last 2 MB).

Delete `state.json` to force a full re-upload. The server deduplicates on
`message_id`, so this is safe — just expensive in storage egress.

## What gets sent

- **Metadata** to `POST /api/ingest`: user (OS username), machine (hostname +
  stable hash), sessions (`session_uuid`, project, branch, timestamps), turns
  (model, tokens, timestamps, tool name, cwd — no message content).
- **Raw JSONL files** to Supabase Storage via signed PUT URLs from
  `POST /api/upload-url`. These contain full prompt + response content.

The dashboard surfaces both: token charts come from metadata, the
"view conversation" drill-down fetches raw content on demand.
