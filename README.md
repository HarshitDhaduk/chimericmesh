# 🎃 ChimericMesh: Self-Healing Legacy-to-Cloud Agent Mesh

[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)
[![Hackathon: Kiroween 2026](https://img.shields.io/badge/Kiroween-2026-orange.svg)](https://kiroween.devpost.com/)
[![Category: Frankenstein](https://img.shields.io/badge/Category-Frankenstein-purple.svg)](https://kiroween.devpost.com/)
[![Built with Kiro](https://img.shields.io/badge/Built%20with-Kiro%20Crew%20%7C%20MCP%20%7C%20Specs-00D26A.svg)](https://kiro.dev/)

> **Kiroween 2026 Submission** — Category: **Frankenstein**  
> *Stitching together the monsters of the past (1980s BSD Syslog, IRC bots, raw TCP telemetry) with autonomous **Kiro Crew** agent swarms, **Model Context Protocol (MCP)** execution tools, and a high-taste, anti-slop visual command center.*

---

## ⚡ The Chimera: Why "Frankenstein"?

Modern cloud infrastructure is clean, JSON-based, and REST/GraphQL-driven. Yet mission-critical enterprise systems still run on **decades-old, archaic protocols**:
- Unstructured **RFC 3164 BSD Syslog** datagrams.
- Legacy **IRC chat bot** alert channels and serial TTY dumps.
- Brittle, unformatted standard error streams that crash standard parsers.

**ChimericMesh fuses these incompatible worlds into a single, self-healing organism:**
1. **The Legacy Ingestion Bridge:** Listens to raw BSD Syslog UDP packets and IRC streams, converting erratic text streams into validated Pydantic events.
2. **The Kiro Crew Multi-Agent Swarm:** A 3-agent autonomous loop (**Triage Sentinel**, **Spec Auditor**, and **Remediation Specialist**) that diagnoses anomalies against formal requirements in `/.kiro/specs/`.
3. **Model Context Protocol (MCP) Bridge:** Directly invokes sandboxed operational tools to restart crashed daemons, patch invalid configurations, and verify system recovery.
4. **Anti-Slop Visual Dashboard (`taste-skill`):** A high-performance command center built using the `design-taste-frontend` specification with a WebGL shader ground, dark tech palette, and real-time incident streaming.

---

## 🏗️ System Architecture

```text
[ Vintage UDP Syslog ] ──┐
[ Legacy IRC Gateway ] ──┼──> [ Legacy Stream Parser ] ──> [ Pydantic Event Normalizer ]
[ Raw TCP Socket     ] ──┘                                        │
                                                                  ▼
┌─────────────────────────────────────────────────── [ Kiro Crew Swarm ] ─────────────────────────┐
│                                                                                                  │
│   ┌──────────────────────┐       ┌──────────────────────┐       ┌────────────────────────────┐   │
│   │   Triage Sentinel    │ ────> │     Spec Auditor     │ ────> │   Remediation Specialist   │   │
│   │ (Anomaly & Priority) │       │ (Evaluates .kiro/spec│       │ (Formulates Recovery Plan) │   │
│   └──────────────────────┘       └──────────────────────┘       └──────────────┬─────────────┘   │
└────────────────────────────────────────────────────────────────────────────────┼─────────────────┘
                                                                                 │
                                          ┌──────────────────────────────────────┴────────────────┐
                                          ▼                                                       ▼
                            ┌───────────────────────────┐                           ┌───────────────────────────┐
                            │   Model Context Protocol  │                           │   High-Taste Dashboard    │
                            │      (MCP Tool Server)    │                           │ (taste-skill layout-v4)   │
                            │  - restart_daemon         │                           │  - Real-time telemetry    │
                            │  - diagnose_process       │                           │  - WebGL shader ground    │
                            │  - rollback_config        │                           │  - Headless browser gate  │
                            └───────────────────────────┘                           └───────────────────────────┘
```

---

## 🤖 How Kiro Was Used (The 5 Write-Up Pillars)

### 1. Spec-Driven Development vs. "Vibe Coding"
Instead of unconstrained "vibe coding" prompts, ChimericMesh was architected strictly through **Spec-Driven Development**. 
All data schemas, invariant bounds, and tool interfaces were authored in `/.kiro/specs/` *before* code generation began:
- [`01-legacy-stream-parser.spec.md`](.kiro/specs/01-legacy-stream-parser.spec.md)
- [`02-kiro-crew-orchestration.spec.md`](.kiro/specs/02-kiro-crew-orchestration.spec.md)
- [`03-mcp-remediation-tools.spec.md`](.kiro/specs/03-mcp-remediation-tools.spec.md)
- [`04-taste-frontend-dashboard.spec.md`](.kiro/specs/04-taste-frontend-dashboard.spec.md)

### 2. Kiro Crew & Multi-Agent Collaboration
ChimericMesh leverages a dedicated 3-agent **Kiro Crew**:
- **Agent 1 (Triage Sentinel):** High-throughput anomaly filter that discards noise and detects incident clusters.
- **Agent 2 (Spec Auditor):** Validates real-time incident states against defined system invariants.
- **Agent 3 (Remediation Specialist):** Safely triggers corrective MCP interventions without manual operator intervention.

### 3. Agent Steering & Hooks
- **Steering Documents (`/.kiro/steering/`):** Enforce strict coding conventions ([`coding-standards.md`](.kiro/steering/coding-standards.md)), mathematical property invariants ([`pbt-guidelines.md`](.kiro/steering/pbt-guidelines.md)), and layout-v4 UI rules ([`ui-conventions.md`](.kiro/steering/ui-conventions.md)).
- **Lifecycle Hooks (`/.kiro/hooks/`):** Automated pre-commit linting ([`pre-commit-lint.sh`](.kiro/hooks/pre-commit-lint.sh)) and the finish-gate verification suite ([`finish-gate.sh`](.kiro/hooks/finish-gate.sh)).

### 4. Model Context Protocol (MCP) Integration
Extends Kiro's capabilities into active infrastructure operations through standardized MCP tool servers. The agents do not execute blind shell strings; they call strongly typed MCP tools (`diagnose_process`, `restart_legacy_daemon`, `rollback_configuration`) with structured auditing.

### 5. Property-Based Testing (PBT)
Standard unit tests only verify edge cases the author anticipated. To handle unpredictable 1980s socket data, we implemented **Property-Based Testing** with `hypothesis`. PBT fuzzes our parser with over 1,000 generated permutations of corrupted bytes, broken UTF-8 fragments, and out-of-order timestamps to mathematically prove crash-invariance.

---

## 🚀 Quickstart & Setup

### Prerequisites
- Python 3.11+
- Node.js 22+ (for taste-skill consistency verification)
- Git

### Installation
```bash
# Clone the repository
git clone https://github.com/<your-username>/Kiroween-2026.git
cd Kiroween-2026

# Set up Python virtual environment
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
npm install
```

### Running the Application
```bash
# Launch the API server and mock legacy telemetry streams
npm start
```
Visit `http://localhost:8080` to view the **ChimericMesh Command Center**.

---

## 🧪 Running the Verification Suites

```bash
# 1. Run Unit Tests
pytest tests/unit/

# 2. Run Property-Based Tests (PBT)
pytest tests/pbt/ -v

# 3. Run Taste-Skill Frontend Consistency Checker in Headless Browser
npm run check:ui
```

---

## 📜 Repository Compliance
- [x] Includes root `/.kiro` directory (with `specs/`, `steering/`, and `hooks/`).
- [x] Includes root OSI-approved [LICENSE](LICENSE) (MIT).
- [x] Public GitHub repository with clean history.
- [x] Built during the official Kiroween 2026 submission period.
