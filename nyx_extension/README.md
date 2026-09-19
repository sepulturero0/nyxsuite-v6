# Nyx Chrome Extension v6

Watches `https://snapboard-production.up.railway.app/`, detects `AdsPower ID` and `Model`, and syncs those rows directly into the local Nyx queue.

## What it does

- Runs on `snapboard-production.up.railway.app` and Snapchat profile pages used by the workflow.
- Detects SnapBoard rows and pushes them into the local Nyx app over `http://127.0.0.1:8865`.
- Reads the live local queue back so the popup shows current runner status and queue rows.
- "Open Web App" button opens the Nyx Suite dashboard (`:8870`).
- "Connect to Agent" button launches/attaches the headless bridge agent via native messaging.

## Load the extension

1. Open `chrome://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select the `nyx_extension/` folder.

## Required setup

1. Start the Nyx Suite bridge agent (`python bridge_app.py` or the frozen build).
2. The extension defaults to `http://127.0.0.1:8865` (v6 Nyx API).
3. Connect to Agent gets the per-install token automatically.
