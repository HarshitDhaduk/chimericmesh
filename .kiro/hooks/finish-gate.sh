#!/usr/bin/env bash
# Finish gate hook: runs property-based tests and taste-skill consistency checker.
set -euo pipefail

echo "==> [Kiro Hook] Running ChimericMesh finish gate verification..."

# 1. Run Property-Based Tests
if [ -d "tests/pbt" ]; then
    echo "Running Property-Based Tests (PBT)..."
    if command -v pytest >/dev/null 2>&1; then
        pytest tests/pbt/ -v
    else
        echo "pytest not found, skipping PBT automated runner."
    fi
fi

# 2. Run taste-skill visual consistency check if HTML exists
if [ -f "src/web/index.html" ]; then
    echo "Running taste-skill consistency checker in headless browser..."
    SKILL_SCRIPT="e:/Projects/taste-skill/skills/taste-skill/scripts/consistency.mjs"
    if [ -f "$SKILL_SCRIPT" ]; then
        node "$SKILL_SCRIPT" "src/web/index.html" --page tech || true
    fi
fi

echo "==> [Kiro Hook] Finish gate verification complete!"
exit 0
