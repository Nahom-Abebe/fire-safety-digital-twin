# mcp_server/server.py
# FastMCP server — the interoperability layer (Section 3.4 of report).
# Exposes 9 typed tools covering Sense, Reason and Act operations.
#
# Fix applied: removed module-level initialise_occupants() and create_board()
# calls. These were firing on every import, resetting the simulation state
# to 80 occupants/seed=42 even when test_scenarios.py had already
# initialised with different parameters (e.g. TS-05 with 40 occupants).
# Each calling script (live_agent_runner, test_scenarios, phase2_runner)
# now owns its own initialisation.
#
# Run standalone: python -m mcp_server.server

import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastmcp import FastMCP

from sensors.sensor_sim import (
    initialise_occupants, move_occupants, get_sensor_snapshot,
    get_room_status, update_sign_status, set_room_attractiveness,
    trigger_event, clear_event
)
from sensors.building_graph import get_max_occupancy, get_exit_path
from rag.retriever import retrieve_regulations, get_adb_context
from bim.bim_query import (
    get_all_signs, get_sign, check_occupancy_compliance
)
from bim.signage import update_sign, reset_all_signs
from bim.pset_sync import bulk_update_occupancy_psets
from bim.board import create_board, update_board
from bim.occupant_markers import create_markers, live_reposition_markers
from bim.viewport_utils import frame_view_on_objects

# ── MCP server ────────────────────────────────────────────────────────────────
# NOTE: No initialise_occupants() or create_board() here.
# The calling script owns initialisation so test scenarios with
# different occupant counts (e.g. TS-05 with 40) are not overridden.

mcp = FastMCP("FireSafetyDigitalTwin")


# ════════════════════════════════════════════════════════════════════════════
# SENSE TOOLS — perceive the building state
# ════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def sense_building_state() -> dict:
    """
    Returns the current full building state:
    - Occupancy per room and per floor
    - Compliance alerts (WARNING at 80-100%, OVER strictly above 100%)
    - Active events
    - Summary counts
    Call this FIRST in every Sense-Reason-Act cycle.
    """
    snap = get_sensor_snapshot()
    snap["summary"] = {
        "total"          : snap["total_occ"],
        "warning_count"  : sum(1 for a in snap["alerts"]
                               if a["severity"] == "WARNING"),
        "over_count"     : sum(1 for a in snap["alerts"]
                               if a["severity"] == "OVER"),
        "floors_affected": list({a["floor"] for a in snap["alerts"]}),
    }
    return snap


@mcp.tool()
def sense_room(room_label: str) -> dict:
    """
    Returns detailed status for a specific room.
    room_label: graph label e.g. '0-1', '0-A', '1-16'
    Includes: current/max occupancy, ratio, severity, exit path,
    attractiveness, sign_blocked, IFC long name.
    """
    return get_room_status(room_label)


@mcp.tool()
def advance_tick() -> dict:
    """
    Advances the simulation by one probabilistic random-walk tick.
    Occupants move based on room attractiveness, sign status, and
    capacity headroom. Updates Blender markers and board.
    Returns the updated building snapshot.
    """
    move_occupants()
    snap = get_sensor_snapshot()
    live_reposition_markers(snap)
    update_board(snap)
    return snap


# ════════════════════════════════════════════════════════════════════════════
# REASON TOOLS — regulatory grounding
# ════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def get_regulations(query: str, n_results: int = 3) -> list:
    """
    Retrieves relevant UK Approved Document B passages via RAG.
    Use targeted care-home queries for best results:
    e.g. 'residential care home bedroom occupancy ADB Section 2.43'
    ALWAYS call this before stating any numeric threshold.
    Never estimate regulatory values — retrieve them.
    """
    return retrieve_regulations(query, n=n_results)


@mcp.tool()
def check_compliance(room_long_name: str,
                      current_occupancy: int) -> dict:
    """
    DETERMINISTIC ADB occupancy compliance check using the IFC model.
    Uses the actual max_occ value from the IFC file — no LLM estimation.
    Always call this before stating whether a room is compliant.
    room_long_name: IFC long name e.g. 'Lounge', 'Bedroom'
    Returns: compliant (bool), status (PASS/FAIL), adb_ref
    """
    return check_occupancy_compliance(room_long_name, current_occupancy)


@mcp.tool()
def get_adb_violation_context(room_label: str,
                               current: int,
                               max_occ: int) -> str:
    """
    Returns formatted ADB care home passages most relevant to a
    specific occupancy violation. Combines targeted RAG queries
    for Section 2.33, Clause 2.43, and Table 2.1.
    Use when you need a specific ADB citation for a sign update.
    """
    return get_adb_context(room_label, "room", current, max_occ)


# ════════════════════════════════════════════════════════════════════════════
# ACT TOOLS — interventions that change the building state
# ════════════════════════════════════════════════════════════════════════════

@mcp.tool()
def act_update_sign(sign_id: str,
                    message: str,
                    status: str,
                    adb_ref: str = "") -> dict:
    """
    Updates an evacuation sign's message and status in the IFC model.
    This is a WRITE-BACK operation — updates Pset_EvacuationSign via
    GlobalId, and feeds the status back into the simulation so
    occupant movement probabilities change on the next tick.
    sign_id: sign name e.g. 'SIGN_F0_CORRIDOR_N'
    status: 'ACTIVE' | 'BLOCKED' | 'ALTERNATE'
    adb_ref: ADB clause justifying this decision (for traceability)
    """
    return update_sign(sign_id, message, status, adb_ref)


@mcp.tool()
def act_update_board(agent_message: str = "") -> dict:
    """
    Updates the building manager's dashboard board in Blender.
    ALWAYS call this at the end of every cycle — even if no action needed.
    Use 'System idle — no violations detected' when all is clear.
    agent_message: short directive summary citing the ADB section used.
    """
    snap = get_sensor_snapshot()
    live_reposition_markers(snap)
    update_board(snap, agent_message)
    return {
        "status"       : "updated",
        "tick"         : snap["tick"],
        "total_occ"    : snap["total_occ"],
        "alerts"       : len(snap["alerts"]),
        "agent_message": agent_message,
    }


@mcp.tool()
def act_set_room_attractiveness(room_label: str,
                                  value: float) -> dict:
    """
    Sets the attractiveness of a room (0.0–2.0, default 1.0).
    Lower values discourage occupants from moving toward this room.
    This implements Peter Lawrence's 'relative attractiveness' concept.
    room_label: graph label e.g. '0-20', '1-A'
    value: 0.0 = avoid, 1.0 = neutral, 2.0 = attract
    """
    set_room_attractiveness(room_label, value)
    return {
        "room_label"    : room_label,
        "attractiveness": value,
        "effect"        : ("avoid"   if value < 0.5
                           else "neutral" if value <= 1.2
                           else "attract"),
    }


@mcp.tool()
def list_signs() -> list:
    """
    Returns all evacuation signs with their current status and message.
    Use this to understand which signs are already BLOCKED before
    deciding which one to update.
    """
    return get_all_signs()


# ── Standalone server run ─────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("  FIRE SAFETY DIGITAL TWIN — MCP SERVER")
    print("=" * 55)
    print("Tools:")
    print("  SENSE  : sense_building_state, sense_room, advance_tick")
    print("  REASON : get_regulations, check_compliance,")
    print("           get_adb_violation_context")
    print("  ACT    : act_update_sign, act_update_board,")
    print("           act_set_room_attractiveness, list_signs")
    print()
    print("NOTE: Simulation not initialised here.")
    print("      Call initialise_occupants() from your runner script.")
    print()
    mcp.run()