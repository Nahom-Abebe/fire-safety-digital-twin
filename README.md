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
