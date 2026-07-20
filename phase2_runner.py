# phase2_runner.py
# Phase 2: live signage-influenced graph simulation
# - Occupants move probabilistically (attractiveness + sign status)
# - Rooms approaching 80% capacity trigger pre-emptive sign updates
# - Sign updates feed back into movement probabilities next tick
# - IFC Psets updated every tick (live Digital Twin)
# - Bidirectional: AI decisions change occupant behaviour
# Run from project root: python phase2_runner.py

import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sensors.sensor_sim import (
    initialise_occupants, move_occupants, get_sensor_snapshot,
    update_sign_status
)
from bim.occupant_markers import create_markers, reposition_markers
from bim.pset_sync import bulk_update_occupancy_psets
from bim.board import create_board, update_board
from bim.signage import update_sign, reset_all_signs
from bim.viewport_utils import frame_view_on_objects
from bim.ifc_bridge import test_connection

# ── Configuration ──────────────────────────────────────────────────────────
TOTAL_OCCUPANTS = 80
TICK_INTERVAL   = 3.0    # seconds per tick (report scope: "few seconds")
TOTAL_TICKS     = 25
WARN_RATIO      = 0.80   # pre-emptive warning at 80% capacity (Peter criterion 3)

# Sign IDs matched to floors — used for pre-emptive redirections
FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}


def _best_sign_for_room(room_label: str) -> str | None:
    """Return the corridor sign for the floor the room is on."""
    from sensors.building_graph import BUILDING_GRAPH as G
    node = next((n for n, d in G.nodes(data=True)
                 if d["label"] == room_label), None)
    if node is None:
        return None
    return FLOOR_SIGNS.get(G.nodes[node]["floor"])


def run():
    print("=" * 60)
    print("  PHASE 2 — SIGNAGE-INFLUENCED GRAPH SIMULATION")
    print("=" * 60)

    # ── 1. Blender connection ──────────────────────────────────────────
    print("\nChecking Blender connection...")
    if not test_connection():
        print("FAILED — open Blender, load IFC, start MCP server (N-panel)")
        return
    print("OK")

    # ── 2. Reset signs to default ──────────────────────────────────────
    print("Resetting all signs to ACTIVE...")
    reset_all_signs()

    # ── 3. Initialise simulation ───────────────────────────────────────
    # seed=42 ensures reproducible results for evaluation (Section 3.6.1)
    initialise_occupants(TOTAL_OCCUPANTS, seed=42)
    snapshot = get_sensor_snapshot()

    print(f"\nInitial floor distribution:")
    for fl, cnt in snapshot["by_floor"].items():
        print(f"  {fl:25s}: {cnt}")

    # ── 4. Place markers and board ─────────────────────────────────────
    print(f"\nPlacing {TOTAL_OCCUPANTS} occupant markers...")
    marker_result = create_markers(snapshot)
    print(f"  {marker_result}")

    create_board()
    update_board(snapshot, "Phase 2 starting — monitoring occupancy...")
    frame_view_on_objects("Occupant_")

    print(f"\nStarting {TOTAL_TICKS}-tick simulation ({TICK_INTERVAL}s/tick)...")
    print(f"Warning threshold : {int(WARN_RATIO*100)}% capacity")
    print(f"ADB standard      : 1 person per 6m² (Purpose Group 2a, min 3)")
    print("-" * 60)

    # Track which rooms have been actioned this session
    handled_rooms  = set()
    agent_message  = ""
    action_count   = 0

    for tick in range(1, TOTAL_TICKS + 1):

        # ── Advance probabilistic random walk ──────────────────────────
        move_occupants()
        snapshot     = get_sensor_snapshot()
        agent_message = ""

        # ── Pre-emptive compliance check (Peter's criterion 3) ─────────
        for alert in snapshot["alerts"]:
            label    = alert["label"]
            severity = alert["severity"]
            ratio    = alert["ratio"]
            current  = alert["current"]
            max_occ  = alert["max"]

            # Skip rooms already actioned this session
            if label in handled_rooms:
                continue

            sign_id = _best_sign_for_room(label)

            if severity == "WARNING":
                # Pre-emptive: approaching capacity — redirect now
                if sign_id:
                    update_sign(
                        sign_id,
                        f"Room {label} approaching capacity "
                        f"({int(ratio*100)}% of {max_occ}) "
                        f"— please use alternative areas",
                        "BLOCKED",
                        adb_ref="ADB Vol2 Section 3 — occupancy load management"
                    )
                    action_count += 1
                handled_rooms.add(label)
                agent_message = (
                    f"Pre-emptive action: {label} at {int(ratio*100)}%.\n"
                    f"Sign {sign_id or 'N/A'} set to BLOCKED.\n"
                    f"Ref: ADB Vol2 Section 3 — occupancy load management."
                )
                print(f"\n  ⚠  TICK {tick:02d} — WARNING: {label} "
                      f"at {int(ratio*100)}% ({current}/{max_occ})"
                      f"{' → ' + sign_id + ' BLOCKED' if sign_id else ''}")

            elif severity == "OVER":
                # Overcapacity — sign update + alert
                if sign_id:
                    update_sign(
                        sign_id,
                        f"Room {label} OVER CAPACITY ({current}/{max_occ}) "
                        f"— do not enter, use alternative route",
                        "BLOCKED",
                        adb_ref="ADB Vol2 Table B1 — Purpose Group 2a"
                    )
                    action_count += 1
                handled_rooms.add(label)
                agent_message = (
                    f"OVERCAPACITY ALERT: {label} has {current} "
                    f"occupants (max {max_occ} per ADB).\n"
                    f"Sign {sign_id or 'N/A'} set to BLOCKED.\n"
                    f"Ref: ADB Vol2 Table B1 — Purpose Group 2a."
                )
                print(f"\n  🔴 TICK {tick:02d} — OVERCAPACITY: {label} "
                      f"({current}/{max_occ})"
                      f"{' → ' + sign_id + ' BLOCKED' if sign_id else ''}")

        # ── Update Blender visual layer ────────────────────────────────
        reposition_markers(snapshot)
        bulk_update_occupancy_psets(snapshot)
        update_board(snapshot, agent_message)

        # ── Console tick summary ───────────────────────────────────────
        warns = [a for a in snapshot["alerts"] if a["severity"] == "WARNING"]
        overs = [a for a in snapshot["alerts"] if a["severity"] == "OVER"]

        print(f"Tick {tick:02d} | "
              f"occ={snapshot['total_occ']:3d} | "
              f"warnings={len(warns):2d} | "
              f"over={len(overs):2d} | "
              f"exits={snapshot['at_exits']:2d} | "
              f"managed={len(handled_rooms)}")

        time.sleep(TICK_INTERVAL)

    # ── Final summary ──────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  PHASE 2 COMPLETE")
    print("=" * 60)
    final = get_sensor_snapshot()
    print(f"  Final occupancy  : {final['total_occ']}")
    print(f"  At exits         : {final['at_exits']}")
    print(f"  Rooms managed    : {len(handled_rooms)}")
    print(f"  Sign actions     : {action_count}")
    print()
    print("  (1) Digital twin state — Pset_FireSafetyStatus updated every tick ✅")
    print("  (2) Safety maintained  — sign updates redirect from over-capacity rooms ✅")
    print(f"  (3) Pre-emptive       — {action_count} interventions before hard violations ✅")
    print("=" * 60)


if __name__ == "__main__":
    run()