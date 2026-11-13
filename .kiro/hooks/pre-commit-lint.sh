#!/usr/bin/env bash
# Pre-commit hook: enforces linting and syntax integrity across Python and JavaScript files.
set -euo pipefail

echo "==> [Kiro Hook] Running pre-commit lint and syntax check..."

# Check Python syntax if python is available
if command -v python >/dev/null 2>&1; then
    echo "Checking Python files..."
    python -m compileall -q src/ tests/
fi

# Check JavaScript / Node syntax if node is available
if command -v node >/dev/null 2>&1; then
    echo "Checking Node/JS files..."
    for f in $(find src/ -name "*.js" -o -name "*.mjs" 2>/dev/null); do
        node --check "$f"
    done
fi

echo "==> [Kiro Hook] Pre-commit lint passed successfully!"
exit 0
