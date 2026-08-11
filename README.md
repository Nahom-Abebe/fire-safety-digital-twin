<<<<<<< HEAD
# Fire Safety Digital Twin — Occupancy Management System

A Blender + IFC digital twin for care home occupancy management, built on top of [Bonsai-mcp](https://github.com/JotaDeRodriguez/Bonsai_mcp). The system reads a live IFC/BIM model, simulates occupant movement through the building graph, and drives an autonomous agent that checks occupancy against Approved Document B (ADB) fire safety guidance and updates corridor signage in real time.

This is not an evacuation model. Following the project brief, the system does not simulate fire, alarms, or biased movement toward exits. Instead it identifies when a room's occupancy exceeds a safe threshold and redirects occupants toward alternative spaces, it is an occupancy management twin.

## Features

- **Live occupancy simulation**: An 80-occupant random walk over the building's room/corridor/exit graph, with per-room attractiveness that drops when a room becomes overcrowded — occupants naturally disperse rather than being scripted out
- **ADB-grounded compliance agent**: An LLM agent retrieves the relevant Approved Document B clause, checks live IFC occupancy data against it, and only acts once compliance is confirmed
- **Reactive corridor signage**: Physical sign panels in the 3D model update in real time from the simulation's own per-tick state — not from pre-written text pinned to a chosen frame
- **Baked demo scenarios**: Five test scenarios (single-room congestion, exit obstruction, multi-room congestion, mobility-constrained routing, and a negative-control baseline) bake to keyframed Blender animations for repeatable, narratable demos
- **Manager override panel**: A clickable in-viewport overlay lets a building manager escalate to full evacuation, sending all occupants toward the marked assembly point via computed exit paths
- **IFC Pset read/write**: Compliance status and sign state are written back into the IFC model itself via `Pset_FireSafetyStatus`, so the twin's decisions persist in the BIM data
- **Wheelchair/accessibility routing**: A tracked mobility-constrained occupant is biased away from stairwells in the walk itself, with a distinct marker and dedicated refuge signage

## Components

The system is built in four layers on top of the Bonsai-mcp Blender/IFC bridge:

1. **Building graph (`sensors/building_graph.py`)**: A NetworkX graph of rooms, corridors, stairs, and exits derived from the IFC spatial structure, carrying ADB maximum-occupancy values per room

2. **Occupant walk (`sensors/agent_walk.py`)**: A per-agent probabilistic random walk across the graph. Room attractiveness governs movement; overcrowded rooms, blocked exits, multi-room violations, and mobility-constrained routing are all first-class simulation parameters, not scripted animation

3. **Compliance agent (`agent/agent.py`)**: Retrieves ADB guidance, checks live occupancy via IFC queries, and only updates signage after compliance is confirmed — every reasoning step is written to an in-scene status board as it happens

4. **Animation & signage bridge (`bim/animation_baker.py`, `bim/signage.py`, `bim/manager_panel.py`)**: Bakes the simulated timeline into Blender keyframes and registers a live frame-change handler so the board, corridor signs, and occupant markers all read from the same computed per-tick state during playback

## Installation

### Prerequisites

- Blender 4.0 or newer
- Python 3.12 or newer
- [Bonsai-mcp](https://github.com/JotaDeRodriguez/Bonsai_mcp) installed and connected (see that repo for the Blender addon + MCP server setup)
- Bonsai BIM addon for Blender, with an IFC model of the building loaded
- `networkx` for the building graph

### Clone the repository

```bash
git clone https://github.com/Nahom-Abebe/fire-safety-digital-twin.git
cd fire-safety-digital-twin
```

### Install dependencies

```bash
pip install networkx
```

### Load the building model

1. Open Blender with the Bonsai BIM addon enabled
2. Load the project IFC file
3. In the 3D View sidebar, connect the Bonsai-mcp Blender addon (see Bonsai-mcp installation above)
4. Confirm the MCP server is running and reachable

## Quick Start

```bash
# Pre-warm the ADB regulation retriever (no API cost)
python -m rag.retriever

# Build the scene: transparency, occupant markers, signage, assembly point, manager panel
python phase1_setup.py

# Run a baked demo scenario — repeatable, no API cost
python fix_and_bake.py --scenario TS-01 --fps 12 --speed 0.08

# Run the live compliance agent against the model (uses the Anthropic API)
python live_agent_runner.py --ticks 15 --delay 2.0 --sense-every 3 --seed 3

# Run the full evaluation suite across all scenarios
python tests/test_scenarios.py --scenario all
```

## Usage

### Baked scenarios

`fix_and_bake.py` bakes a full occupancy-management timeline into Blender keyframes and drives it. Every visible signal — the status board, the four primary corridor signs, and any scenario-specific extra signage — is computed from the simulation's real per-tick state, not written in advance.

| Scenario | Description |
|---|---|
| `TS-01` | Single-room congestion on the ground floor, with a nearby lounge correctly filtered as a false positive |
| `TS-02` | North exit obstruction — occupants route around it toward the south exit |
| `TS-03` | Multi-room congestion — two bedrooms on the top floor overcrowded simultaneously |
| `TS-04` | Mobility-constrained routing — a tracked wheelchair occupant avoids stairwells in the walk itself |
| `TS-05` | Baseline negative control — zero violations, zero sign updates, for verifying the twin stays silent when nothing is wrong |

```bash
python fix_and_bake.py --scenario TS-04 --fps 12 --speed 0.08
python fix_and_bake.py --jump 48        # jump straight to a specific frame
```

### Manager override

An in-viewport overlay (installed by `phase1_setup.py`) provides two buttons:

- **ESCALATE** — computes an exit path (room → corridor → exit → assembly point) for every occupant and walks them there at a fixed pace, turning all corridor signs red
- **RESET** — clears all signage back to green

Keyboard shortcuts `Shift+E` / `Shift+R` are also bound.

### Live agent

`live_agent_runner.py` runs the actual LLM compliance agent against the live IFC model tick by tick, retrieving ADB guidance, checking compliance, and updating signage only once a genuine violation is confirmed. Each reasoning step is written to the in-scene board as it happens.

## Evaluation

`tests/test_scenarios.py` runs the agent against all five scenarios and checks:

- Causal ordering (compliance is checked *before* any sign is updated)
- ADB citation accuracy against a ground-truth regex
- Behavioural rules — TS-05 must produce zero sign updates, TS-01–04 must produce at least one

```bash
python tests/test_scenarios.py --scenario all
python tests/test_scenarios.py --scenario TS-01 --trials 3   # repeat for consistency
```

## Project Structure

```
phase1_setup.py          Scene setup: transparency, markers, signage, assembly point, manager panel
fix_and_bake.py           Bakes and drives the five demo scenarios
live_agent_runner.py      Runs the live compliance agent against the model
bim/
  animation_baker.py      Bakes the simulated timeline into Blender keyframes
  signage.py               Reads/writes corridor sign IFC Psets and panel state
  manager_panel.py         In-viewport manager override overlay
  assembly_point.py        Assembly point marker and exit-path evacuation
sensors/
  building_graph.py        NetworkX graph of the building, ADB occupancy limits
  agent_walk.py             Per-agent occupancy management random walk
agent/
  agent.py                  LLM compliance agent — ADB retrieval, checks, actions
tests/
  test_scenarios.py         Evaluation harness for all five scenarios
```

## Troubleshooting

- **Connection issues**: Confirm the Bonsai-mcp Blender addon server is running before starting any script
- **Duplicate viewport overlays**: The manager panel handler is stored in `bpy.app.driver_namespace`, which persists across script re-runs — re-running `phase1_setup.py` should always remove the old handler before installing a new one
- **Sign updates taking too long**: Sign and board updates should be batched into a single `send_to_blender()` call rather than one call per sign; sequential round-trips are the usual cause of multi-second delays
- **Baseline scenario shows a violation**: Confirm `violation_room` is `None` for the baseline scenario — this suppresses the IFC Pset write and all sign updates

## Technical Details

The simulation is a probabilistic random walk over the building graph: each occupant's next-node choice is weighted by neighbouring rooms' "attractiveness," which drops sharply for any room found to be over its ADB maximum occupancy. Exit nodes are not absorbing — occupants pass through them like any other node, which keeps the model an occupancy-management twin rather than an evacuation simulator. Blocked exits, multiple simultaneous violations, and mobility-constrained routing are all implemented as parameters to this same walk, so scenario behaviour comes from the simulation itself rather than from scripted animation.

## Limitations

- Corridor sign wording for the four primary floor signs is generated by a shared template inside `animation_baker.py`; scenario-specific styling (for example, distinct wording for accessibility-related signage) requires extending that template directly rather than overriding it from the driving script, to avoid two processes writing the same Blender object on the same frame
- The building graph and ADB occupancy limits are specific to the modelled care home and would need remapping for a different building
- Large IFC models may slow keyframe baking; scenario ticks and occupant counts can be reduced for faster iteration during development

## Acknowledgements

- Built on [Bonsai-mcp](https://github.com/JotaDeRodriguez/Bonsai_mcp) by JotaDeRodriguez, itself a fork of [BlenderMCP](https://github.com/ahujasid/blender-mcp) by Siddharth Ahuja
- IFC integration via the Bonsai BIM addon for Blender
- MSc dissertation project, University of Greenwich — supervised by Dr Tom Cole and Dr Peter Lawrence (FSEG)
=======
# Fire Safety Digital Twin — Care Home Occupancy Management

An AI-driven Digital Twin and real-time occupancy management system for care homes. Built using **Blender (3D Visualization)**, **NetworkX (Probabilistic Graph-based Occupancy Simulation)**, **IFC/BIM Data**, and **Claude (LLM Autonomous Agent)**.

The system monitors occupant movement, enforces building safety compliance against **Approved Document B (ADB)** regulations, dynamically updates 3D HUD/signage, and manages emergency evacuation routing.

---

## 🌟 Key Features

* **Probabilistic Occupancy Simulation:** Graph-based movement model incorporating room attractiveness, capacity limits, active signage guidance, and shortest-path evacuation routing.
* **Autonomous AI Agent:** Driven by Claude to execute Sense-Reason-Act cycles, audit room compliance against IFC specifications and ADB regulations, and issue 3D HUD directives.
* **Live Blender 3D Visualization:** Real-time IPC bridge connecting Python to Blender for:
  * Dynamic HUD/Dashboard text updating (`FireSafetyBoard`).
  * Floor compliance highlighting (Green = Compliant, Red = Violations/Evacuation).
  * Smooth occupant cone marker interpolation and real-time rerouting.
  * Adaptive evacuation corridor signage Pset updates.
* **Emergency Escalation Mode:** Triggerable global evacuation mode that routes occupants to nearest assembly exits, shifts floor states to critical red, and activates emergency egress signage.

---

## 🏗 System Architecture

                              ┌────────────────────────┐
                              │   Claude AI Agent      │
                              │ (Sense-Reason-Act Loop)│
                              └───────────┬────────────┘
                                          │ Directives & Sign Updates
                                          ▼
┌────────────────────────┐         ┌────────────────────────┐         ┌────────────────────────┐
│  Sensors & Graph Model ├────────►│    live_agent_runner   ├────────►│  Blender 3D Twin (IPC) │
│ (Occupancy, Paths, Evac)│ Snapshot│  (Tick Loop & Visuals) │ Commands│ (Cones, Board, Floors) │
└────────────────────────┘         └────────────────────────┘         └────────────────────────┘

---

## 🚀 Getting Started

### Prerequisites

* **Python 3.10+**
* **Blender 3.x / 4.x** with the IFC model loaded and the MCP / IPC server running via the N-panel.
* **Anthropic API Key** (for live agent reasoning):
  ```bash
  # PowerShell
  $env:ANTHROPIC_API_KEY = "sk-ant-..."

  # Bash
  export ANTHROPIC_API_KEY="sk-ant-..."

  Installation
Clone the repository:

Bash
git clone [https://github.com/your-username/your-repo-name.git](https://github.com/your-username/your-repo-name.git)
cd your-repo-name
Install Python dependencies:

Bash
pip install networkx anthropic
🎮 Running the Simulation
Launch Blender, open your care home IFC project file, and start the IPC socket server in the N-panel interface.

Run the live agent simulation runner:

Bash
python live_agent_runner.py --ticks 25 --occupants 80 --delay 2.0

Repository Structure
├── agent/                # AI Agent tools, schemas, and cycle logic
├── bim/                  # Blender IPC, HUD board, signage, and marker modules
│   ├── board.py          # Dashboard text panel and floor material controller
│   ├── ifc_bridge.py     # IPC socket communication bridge with Blender
│   └── occupant_markers.py # 3D marker placement and smooth repositioning
├── sensors/              # Graph & sensor simulation
│   ├── building_graph.py # Building topology, exits, and path calculation
│   └── sensor_sim.py    # Occupant movement rules and evacuation state machine
├── logs/                 # Output execution logs and session histories
└── live_agent_runner.py  # Main tick loop and live runner script
>>>>>>> 1cd95e277fe59456f07e7e0127b3ae4ed5a8335a
