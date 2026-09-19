# Nyxify Chrome Extension v6

Watches `https://snapboard-production.up.railway.app/`, detects new top rows, and creates AdsPower profiles through the Nyxify Runner.

## What it does

- Runs on `snapboard-production.up.railway.app` and Snapchat profile pages.
- Detects SnapBoard rows and pushes them into the local Nyxify app over `http://127.0.0.1:8866`.
- Reads the live local queue back so the popup shows current runner status.
- "Open Web App" button opens the Nyx Suite dashboard (`:8870`).
- "Connect to Agent" button launches/attaches the headless bridge agent via native messaging.

## Load the extension

1. Open `chrome://extensions`.
2. Enable `Developer mode`.
3. Click `Load unpacked`.
4. Select the `nyxify_extension/` folder.

## Required setup

1. Start the Nyx Suite bridge agent (`python bridge_app.py` or the frozen build).
2. The extension defaults to `http://127.0.0.1:8866` (v6 Nyxify API).
3. Connect to Agent gets the per-install token automatically.
