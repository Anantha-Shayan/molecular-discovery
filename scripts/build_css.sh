#!/usr/bin/env bash
# Rebuild frontend/assets/tailwind.css (the app serves it itself, so no CDN/internet is needed at runtime).
# One-off tooling lives outside the repo: npm install tailwindcss@3.4.17 in $TWBUILD (default ~/tools/twbuild).
set -euo pipefail
cd "$(dirname "$0")/.."
TW="${TWBUILD:-$HOME/tools/twbuild}"
"$TW/node_modules/.bin/tailwindcss" -c frontend/build/tailwind.config.js -i frontend/build/input.css -o frontend/assets/tailwind.css --minify
