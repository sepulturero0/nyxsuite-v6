# Single source of truth for the user-facing version string.
# Changing these here also flows into window titles, packaging spec
# names, the GitHub-release updater check, and the extension/runner
# version handshake once that is wired up.

NYX_VERSION = "6.6.2"
NYXIFY_VERSION = "6.6.2"

NYX_VERSION_LABEL = f"v{NYX_VERSION}"
NYXIFY_VERSION_LABEL = f"v{NYXIFY_VERSION}"

NYX_DISPLAY_NAME = f"Nyx {NYX_VERSION_LABEL}"
NYXIFY_DISPLAY_NAME = f"Nyxify {NYXIFY_VERSION_LABEL}"
