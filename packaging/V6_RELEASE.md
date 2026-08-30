# Nyx Suite v6 Build and Release

> **Current version:** see `core/version.py` and `VERSION`.

Nyx Suite v6 is published from one public GitHub repository:
`sepulturero0/nyxsuite-v6`.

The same repo hosts:

- source code
- GitHub Releases
- update ZIP assets consumed by the dashboard updater

## One-Time Setup

1. Create or reuse the repo:

   ```bash
   gh repo create sepulturero0/nyxsuite-v6 --public --source . --remote origin
   ```

2. Install dependencies:

   ```bash
   python -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   .venv/bin/python -m playwright install chromium
   ```

   On Windows, use `.venv\Scripts\python.exe`.

## Build Source Update ZIP

macOS/Linux:

```bash
bash packaging/create_release_zip.sh --version <version>
```

Windows:

```powershell
powershell -ExecutionPolicy Bypass -File .\packaging\create_release_zip.ps1 -Version <version>
```

Both scripts generate `NyxSuite-v<version>.zip` with `update_config.json`
pointing at `sepulturero0/nyxsuite-v6`.

## Publish

```bash
gh release create v<version> dist/NyxSuite-v<version>.zip `
  --repo sepulturero0/nyxsuite-v6 `
  --title "NyxSuite v<version>" `
  --notes "Describe the user-facing changes."
```

Use normal shell line continuations (`\`) on macOS/Linux instead of PowerShell
backticks.

## Verify

- Install or run an older v6 build.
- Open Dashboard -> Settings.
- Click Check for Update.
- Apply update.
- Confirm the app restarts and reports the new version.
- Repeat on Windows and macOS before announcing a release.
