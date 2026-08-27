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
from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy, get_exit_path
from rag.retriever import retrieve_regulations, get_adb_context
from bim.bim_query import (
    get_all_signs, get_sign, check_occupancy_compliance
)
from bim.signage import update_sign, reset_all_signs
from bim.interior_signage import VISUAL_SIGN_IDS
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
    - available_sign_ids: every sign ID with a real, visible Blender
      panel. ALWAYS pick a sign_id from this list for act_update_sign
      — do not guess a naming pattern (e.g. floors other than F0 have
      no "_N"/"_S" suffix), and never invent one that isn't listed
      even if it fits the scenario's narrative (e.g. an exit-specific
      sign) — see CRITICAL RULES.

      Fix applied: this previously listed EVERY sign in
      bim_query.SIGNS, loaded straight from global_ids_v2.json — the
      full IFC-derived sign registry, a separate and LARGER set than
      the 5 signs interior_signage.py actually renders a Blender
      panel for. Confirmed directly: the agent picked
      "SIGN_F0_EXIT_N"/"SIGN_F0_EXIT_S" from that full list — both
      wrote successfully to their IFC Pset (a real, correctly-counted
      success) but had no matching panel object to update, so nothing
      changed on screen. Now scoped to
      interior_signage.VISUAL_SIGN_IDS, the authoritative list of
      signs that actually exist visually, so a "successful" call can
      no longer be invisible.
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
    snap["available_sign_ids"] = [
        s["name"] for s in get_all_signs() if s["name"] in VISUAL_SIGN_IDS
    ]
    return snap


@mcp.tool()
def sense_room(room_label: str) -> dict:
    """
    Returns detailed status for a specific room.
    room_label: graph label e.g. '0-1', '0-A', '1-16'
    Includes: current/max occupancy, ratio, severity, exit path,
    attractiveness, sign_blocked, IFC long name.

    If checking MORE THAN ONE room this cycle, use sense_rooms (plural)
    instead — one round trip for every room, not one round trip each.
    """
    return get_room_status(room_label)


@mcp.tool()
def sense_rooms(room_labels: list) -> dict:
    """
    Returns detailed status for MULTIPLE rooms in a single call —
    the batched equivalent of calling sense_room once per room.

    Use this whenever more than one room needs checking in the same
    cycle (e.g. several WARNING alerts from the same
    sense_building_state call). Three sequential sense_room calls for
    three alerted rooms means three full model round trips before any
    reasoning can happen; one sense_rooms call covering all three
    means one. Confirmed directly in a real session log: a 3-room
    tick's cycle latency was dominated by exactly this kind of
    avoidable serial tool dispatch.

    room_labels: list of graph labels, e.g. ["2-7", "2-9", "3-3"]
    Returns: {room_label: room_status_dict, ...} — same per-room shape
    sense_room returns, keyed by the label you passed in.
    """
    return {label: get_room_status(label) for label in room_labels}


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
    Returns: compliant (bool), status (PASS/WARNING/FAIL), adb_ref

    If checking MORE THAN ONE room this cycle, use
    check_compliance_batch instead — one round trip for every room,
    not one round trip each.
    """
    return check_occupancy_compliance(room_long_name, current_occupancy)


@mcp.tool()
def check_compliance_batch(checks: list) -> list:
    """
    Runs check_compliance for MULTIPLE rooms in a single call — the
    batched equivalent of calling check_compliance once per room.

    Use this whenever more than one room needs a compliance check in
    the same cycle. Confirmed directly in a real session log: a
    3-room tick made three separate check_compliance round trips back
    to back, each one a full model turn, when a single batched call
    would have covered all three.

    checks: list of {"room_long_name": str, "current_occupancy": int}
        e.g. [{"room_long_name": "Bedroom", "current_occupancy": 3},
              {"room_long_name": "Bedroom", "current_occupancy": 4}]
    Returns: list of compliance result dicts, same order as checks,
        same shape check_compliance returns for a single room.
    """
    return [
        check_occupancy_compliance(c["room_long_name"], c["current_occupancy"])
        for c in checks
    ]


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
    # Was hardcoded to "room" regardless of the violation's actual
    # location — meaning a corridor or stairwell violation still
    # retrieved bedroom-occupancy-oriented passages (Clause 2.43)
    # instead of the correct horizontal-escape/travel-distance
    # passages (Table 2.1), directly undermining retriever.py's
    # room-type-aware query selection. The agent has no reliable way
    # to supply the correct type itself — get_room_status() (what
    # sense_room returns) doesn't expose node_type at all — so it's
    # derived here directly from the graph, which already tracks it
    # correctly for every node.
    node = next((n for n, d in BUILDING_GRAPH.nodes(data=True)
                if d["label"] == room_label), None)
    room_type = BUILDING_GRAPH.nodes[node]["node_type"] if node is not None else "room"

    return get_adb_context(room_label, room_type, current, max_occ)


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

    If setting attractiveness for MORE THAN ONE room this cycle, use
    act_set_room_attractiveness_batch instead — one round trip for
    every room, not one round trip each.
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
def act_set_room_attractiveness_batch(rooms: list) -> list:
    """
    Runs act_set_room_attractiveness for MULTIPLE rooms in a single
    call — the batched equivalent of calling it once per room.

    Use this whenever more than one room needs its attractiveness
    adjusted in the same cycle (e.g. one FAIL room needing 0.0 and
    several WARNING rooms needing 0.3 in the same ACT step).
    Confirmed directly in a real session log: a single cycle made
    four separate act_set_room_attractiveness calls back to back for
    exactly this reason — one for the FAIL room, three for incidental
    WARNING rooms named on the same board.

    rooms: list of {"room_label": str, "value": float}
        e.g. [{"room_label": "1-1", "value": 0.0},
              {"room_label": "1-10", "value": 0.3}]
    Returns: list of result dicts, same order as rooms, same shape
        act_set_room_attractiveness returns for a single room.
    """
    results = []
    for r in rooms:
        room_label = r["room_label"]
        value      = r["value"]
        set_room_attractiveness(room_label, value)
        results.append({
            "room_label"    : room_label,
            "attractiveness": value,
            "effect"        : ("avoid"   if value < 0.5
                               else "neutral" if value <= 1.2
                               else "attract"),
        })
    return results


@mcp.tool()
def list_signs() -> list:
    """
    Returns all evacuation signs with a real, visible Blender panel
    and their current status and message. Use this to understand
    which signs are already BLOCKED before deciding which one to
    update.

    Scoped to interior_signage.VISUAL_SIGN_IDS for the same reason
    sense_building_state()'s available_sign_ids is — see that
    docstring. Previously returned the full IFC-derived sign
    registry, which could re-introduce a sign with no visual panel
    even after the agent specifically called this to self-correct
    from a bad guess.
    """
    return [s for s in get_all_signs() if s["name"] in VISUAL_SIGN_IDS]


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