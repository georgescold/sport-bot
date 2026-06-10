#!/bin/bash
set -euo pipefail

# Only run in remote (Claude Code on the web) environments
if [ "${CLAUDE_CODE_REMOTE:-}" != "true" ]; then
  exit 0
fi

# Install pip dependencies
pip install -q google-auth google-auth-httplib2 google-api-python-client requests

# Recreate the Google service account credentials file from env var
CREDS_FILE="$CLAUDE_PROJECT_DIR/gen-lang-client-0218641615-b114179ddeb2.json"
if [ -n "${GOOGLE_SERVICE_ACCOUNT_JSON:-}" ]; then
  echo "$GOOGLE_SERVICE_ACCOUNT_JSON" > "$CREDS_FILE"
  echo "✅ Credentials file created."
else
  echo "⚠️  GOOGLE_SERVICE_ACCOUNT_JSON not set — credentials file not created."
fi
