# UI & Visual Design Steering (Taste Skill v2.5 Integration)

This document steers Kiro to enforce the anti-slop visual standards of `design-taste-frontend`.

## House Rules
1. **One Section Per Screen:** At desktop (1440x900), the hero and subsequent primary sections must occupy exactly one screen height. No peeking headers or accidental double-section stacking.
2. **Equal Edges:** Left and right content margins must be mathematically balanced.
3. **Restrained, Intentional Aesthetic:**
   - Dark tech/terminal palette: deep slate `#0a0d12`, muted card borders `#1f2937`, sharp neon accents (terminal green `#10b981`, alert crimson `#ef4444`, cyber amber `#f59e0b`).
   - Clean Google Font pairings (e.g. `Geist Mono` or `JetBrains Mono` for telemetry; `Inter` or `Plus Jakarta Sans` for headers).
4. **No Captions Under Visuals:** Image/card captions are forbidden. Attribution or license credits belong in a single expandable footer line.
5. **No Empty Visual States:** Every card, metric tile, or canvas MUST render live content or simulated live activity.
6. **Automated Consistency Verification:**
   - All frontend iterations must run through `node scripts/consistency.mjs <target>` in headless Edge/Chrome across breakpoints 1440, 1024, 768, and 390.
