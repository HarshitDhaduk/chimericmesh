# Specification 04: Command Center Frontend (Taste Skill v2.5 Architecture)

## 1. Overview
The ChimericMesh Command Center provides a high-taste, single-screen real-time visualization of the legacy-to-AI agent mesh. It adheres strictly to the anti-slop rules of `design-taste-frontend`.

## 2. Layout & Aesthetic Specifications
- **Layout Model:** `layout-v4` Split Hero layout (`assets/layout/deal.mjs`).
  - Left column: Project thesis, live health metric badges, and active agent statuses.
  - Right column: Interactive telemetry waterfall showing real-time raw legacy log conversion into structured agent interventions.
- **Color Palette & Typography:**
  - Background: Obsidian black `#080a0f` with dark slate card containers `#111622`.
  - Borders: Precise 1px hairline dividers (`rgba(255, 255, 255, 0.08)`).
  - Fonts: Google Fonts pairing — `Geist Mono` for numerical telemetry and timestamps, `Plus Jakarta Sans` for titles and body text.
- **WebGL Shader Ground:**
  - Ambient, subtle cyber/mesh shader running on an HTML5 canvas within a 4-context WebGL budget.
  - Automatic reduced-motion and mobile fallback.
- **Section Rule:** Exactly one screen at 1440x900 viewport. Single-screen closing footer (`footer.closing`).
- **Zero Captions:** No captions under visuals. All license credits bundled in a single expandable footer line.

## 3. Real-Time Telemetry Feed
- Lightweight Server-Sent Events (SSE) or WebSocket endpoint (`/api/stream`).
- Frontend buffers incoming events, rendering up to 50 active items with smooth entry transitions.
