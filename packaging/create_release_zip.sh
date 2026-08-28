#!/usr/bin/env bash
# Creates the cross-platform NyxSuite release ZIP for macOS/Linux.
# Usage:  bash create_release_zip.sh [--version X.Y.Z] [--output-dir ./dist]
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

PYTHON_BIN="$ROOT/.venv/bin/python3"
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$ROOT/.venv/bin/python"
fi
if [[ ! -x "$PYTHON_BIN" ]]; then
  PYTHON_BIN="$(command -v python3 || command -v python || true)"
fi

# ---- parse args ----
VERSION=""
OUTPUT_DIR=""
while [[ $# -gt 0 ]]; do
  case "$1" in
    --version) VERSION="${2:-}"; shift 2 ;;
    --output-dir) OUTPUT_DIR="${2:-}"; shift 2 ;;
    *) echo "Unknown arg: $1" >&2; exit 1 ;;
  esac
done

# ---- resolve version ----
if [[ -z "$VERSION" ]]; then
  if [[ -n "$PYTHON_BIN" && -x "$PYTHON_BIN" ]]; then
    VERSION="$("$PYTHON_BIN" -c "from core.version import NYX_VERSION; print(NYX_VERSION)")"
  else
    echo "No Python found. Either create '.venv', install python3, or pass --version." >&2
    exit 1
  fi
fi

LABEL="v${VERSION}"
ARCHIVE_NAME="NyxSuite-${LABEL}"
ZIP_NAME="${ARCHIVE_NAME}.zip"

# ---- resolve output dir ----
if [[ -z "$OUTPUT_DIR" ]]; then
  OUTPUT_DIR="$ROOT/dist"
fi
mkdir -p "$OUTPUT_DIR"
OUTPUT_DIR="$(cd "$OUTPUT_DIR" && pwd)"

# ---- temp staging ----
TMP="$(mktemp -d "/tmp/nyx_release_zip.XXXXXX")"
STAGE="$TMP/$ARCHIVE_NAME"
mkdir -p "$STAGE"

echo "[create_release_zip] Assembling $ARCHIVE_NAME ..."

# ---- Python source directories ----
DIRS=("core" "webui" "agent_host" "utils" "snap_selectors" "scripts" "ui_templates")
for d in "${DIRS[@]}"; do
  SRC="$ROOT/$d"
  if [[ -d "$SRC" ]]; then
    cp -a "$SRC" "$STAGE/$d"
    rm -rf "$STAGE/$d/__pycache__" 2>/dev/null || true
    rm -rf "$STAGE/$d/.git" 2>/dev/null || true
    rm -rf "$STAGE/$d/.pytest_cache" 2>/dev/null || true
    find "$STAGE/$d" -name '*.pyc' -delete 2>/dev/null || true
    echo "  + $d/"
  fi
done

# Native-messaging registration rewrites this path per device. Never ship a
# machine-specific absolute path in the release ZIP.
AGENT_MANIFEST="$STAGE/agent_host/com.nyxsuite.agent.json"
if [[ -f "$AGENT_MANIFEST" ]]; then
  "$PYTHON_BIN" - "$AGENT_MANIFEST" <<'PY'
import json
import sys
from pathlib import Path

path = Path(sys.argv[1])
data = json.loads(path.read_text(encoding="utf-8"))
data["path"] = "agent_host/host_main.py"
path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
PY
  echo "  ~ reset agent_host manifest path to template"
fi

# ---- browser extensions ----
for ext in "nyx_extension" "nyxify_extension"; do
  SRC="$ROOT/$ext"
  if [[ -d "$SRC" ]]; then
    cp -a "$SRC" "$STAGE/$ext"
    rm -rf "$STAGE/$ext/__pycache__" 2>/dev/null || true
    echo "  + $ext/"
  fi
done

# ---- data/ (template defaults, exclude runtime DBs) ----
DATA_SRC="$ROOT/data"
DATA_DEST="$STAGE/data"
if [[ -d "$DATA_SRC" ]]; then
  cp -a "$DATA_SRC" "$DATA_DEST"
  find "$DATA_DEST" -name '*.db' -delete
  echo "  + data/  (removed *.db runtime databases)"
else
  mkdir -p "$DATA_DEST"
fi

# ---- root-level files ----
SETUP_README="$ROOT/packaging/SETUP_README.txt"
if [[ -f "$SETUP_README" ]]; then
  cp "$SETUP_README" "$STAGE/SETUP_README.txt"
  echo "  + SETUP_README.txt"
fi

ROOT_FILES=(
  bridge_app.py main.py nyxify_runner.py requirements.txt
  run_nyx_suite.bat run_nyx_suite.sh run_nyx_suite.command
  portable_launch_nyx.ps1 portable_launch_nyx.sh
  .env.example icons8-origami-50.ico icons8-origami-50.png
  icons8-origami-50-gray.ico icons8-origami-50-gray.png
)
for f in "${ROOT_FILES[@]}"; do
  if [[ -f "$ROOT/$f" ]]; then
    cp "$ROOT/$f" "$STAGE/$f"
    echo "  + $f"
  fi
done

# ---- VERSION ----
echo -n "$VERSION" > "$STAGE/VERSION"
echo "  + VERSION ($VERSION)"

# ---- update_config.json ----
cat > "$STAGE/update_config.json" << 'JSONEOF'
{
  "app": "nyxsuite",
  "repo": "jaymaroldan026/nyxsuite-v6",
  "asset_pattern": "NyxSuite-v*.zip",
  "exe_to_relaunch": "",
  "data_preserve_paths": [
    "data/*.db",
    "data/bridge_config.json",
    "data/nyx_config.json",
    "data/nyxify_config.json",
    "data/bitmoji_models.json",
    "data/bitmoji_outfits.json",
    "data/full_auto_usernames/*",
    "data/signup_names/*",
    "data/logs/*"
  ]
}
JSONEOF
echo "  + update_config.json"

# SECURITY: never publish license-generation/admin artifacts or local secrets.
FORBIDDEN_PATHS=(
  "$STAGE/tools"
  "$STAGE/activator.html"
  "$STAGE/activator_server.py"
  "$STAGE/run_activator_ui.ps1"
  "$STAGE/run_activator_ui.bat"
  "$STAGE/run_activator_ui.sh"
  "$STAGE/run_activator_ui.command"
  "$STAGE/core/license_runtime_secret.py"
  "$STAGE/core/license_signing_key.py"
)
for forbidden in "${FORBIDDEN_PATHS[@]}"; do
  if [[ -e "$forbidden" ]]; then
    rm -rf "$forbidden"
    echo "  - stripped (never publish): $forbidden"
  fi
done

if find "$STAGE" -iname '*activator*' -print -quit | grep -q .; then
  echo "Refusing to build: activator/license-generation files found in release stage." >&2
  exit 1
fi
if find "$STAGE" -type f \( -name '*runtime_secret*' -o -name '*signing_key*' \) -print -quit | grep -q .; then
  echo "Refusing to build: license secret/private key found in release stage." >&2
  exit 1
fi

# ---- create ZIP ----
ZIP_PATH="$OUTPUT_DIR/$ZIP_NAME"
rm -f "$ZIP_PATH"

(cd "$TMP" && zip -rq "$ZIP_PATH" "$ARCHIVE_NAME")

# ---- cleanup ----
rm -rf "$TMP"

SIZE="$(du -k "$ZIP_PATH" | cut -f1)"
echo ""
echo "[create_release_zip] Done: $ZIP_PATH"
echo "[create_release_zip] Size: ${SIZE} KB"
echo ""
echo "Upload to GitHub Releases:"
echo "  gh release upload $LABEL '$ZIP_PATH' --repo jaymaroldan026/nyxsuite-v6"
