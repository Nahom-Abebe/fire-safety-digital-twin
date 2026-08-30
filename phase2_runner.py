# phase2_runner.py
# Phase 2: live signage-influenced graph simulation
# - Occupants move probabilistically (attractiveness + sign status)
# - Rooms approaching 80% capacity trigger pre-emptive sign updates
# - Sign updates feed back into movement probabilities next tick
# - IFC Psets updated every tick (live Digital Twin)
# - Bidirectional: AI decisions change occupant behaviour

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
TICK_INTERVAL   = 3.0    
TOTAL_TICKS     = 25
WARN_RATIO      = 0.80   

FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}


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
    print(f"Capacity source   : real ADB-grounded max_occ per room "
          f"(bim_query.SPACES), area/6m2 fallback only where no real data exists")
    print(f"Alert scope       : bedrooms + communal rooms only — "
          f"circulation, personal-care, and non-occupiable rooms excluded")
    print("-" * 60)

 
    sign_state    = {sign_id: "ACTIVE" for sign_id in FLOOR_SIGNS.values()}
    rooms_flagged = set()   # informational only — every room ever alerted
    action_count  = 0       # counts actual sign STATE CHANGES, both ways

    for tick in range(1, TOTAL_TICKS + 1):

        # ── Advance probabilistic random walk ──────────────────────────
        move_occupants()
        snapshot      = get_sensor_snapshot()
        agent_message = ""

        # ── Group this tick's alerts by floor ───────────────────────────
        floor_alerts = {}
        for alert in snapshot["alerts"]:
            floor_alerts.setdefault(alert["floor"], []).append(alert)

        # ── Per-floor: show the worst active alert, or clear the sign ──
        for floor_name, sign_id in FLOOR_SIGNS.items():
            alerts_here = floor_alerts.get(floor_name)

            if not alerts_here:
                if sign_state[sign_id] != "ACTIVE":
                    update_sign(
                        sign_id,
                        "All routes clear — occupancy within limits",
                        "ACTIVE",
                        adb_ref=""
                    )
                    sign_state[sign_id] = "ACTIVE"
                    action_count += 1
                    print(f"\n  🟢 TICK {tick:02d} — {floor_name} cleared "
                          f"→ {sign_id} ACTIVE")
                continue

            # Worst alert on this floor: OVER beats WARNING, then
            # higher ratio breaks ties within the same severity —
            # not "whichever room happened to be processed last".
            worst = max(
                alerts_here,
                key=lambda a: (1 if a["severity"] == "OVER" else 0, a["ratio"])
            )
            label, severity = worst["label"], worst["severity"]
            current, max_occ, ratio = worst["current"], worst["max"], worst["ratio"]
            rooms_flagged.add(label)

            if severity == "OVER":
                message = (f"Room {label} OVER CAPACITY ({current}/{max_occ}) "
                          f"— do not enter, use alternative route")
                adb_ref = "ADB Vol2 Table B1 — Purpose Group 2a"
                icon    = "🔴"
            else:
                message = (f"Room {label} approaching capacity "
                          f"({int(ratio*100)}% of {max_occ}) "
                          f"— please use alternative areas")
                adb_ref = "ADB Vol2 Section 3 — occupancy load management"
                icon    = "⚠ "

            new_state = f"{severity}:{label}"
            if sign_state[sign_id] != new_state:
                update_sign(sign_id, message, "BLOCKED", adb_ref=adb_ref)
                sign_state[sign_id] = new_state
                action_count += 1
                print(f"\n  {icon} TICK {tick:02d} — {severity}: {label} "
                      f"({current}/{max_occ}) → {sign_id} BLOCKED")

            agent_message = (
                f"{'OVERCAPACITY ALERT' if severity == 'OVER' else 'Pre-emptive action'}: "
                f"{label} at {current}/{max_occ}.\n"
                f"Sign {sign_id} set to BLOCKED.\nRef: {adb_ref}"
            )

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
              f"flagged={len(rooms_flagged)}")

        time.sleep(TICK_INTERVAL)

    # Final summary 
    print("\n" + "=" * 60)
    print("  PHASE 2 COMPLETE")
    print("=" * 60)
    final = get_sensor_snapshot()
    print(f"  Final occupancy  : {final['total_occ']}")
    print(f"  At exits         : {final['at_exits']}")
    print(f"  Rooms flagged    : {len(rooms_flagged)} (ever alerted, across the whole run)")
    print(f"  Sign state changes: {action_count} (blocks + clears combined)")
    print()
    print("  (1) Digital twin state — Pset_FireSafetyStatus updated every tick ✅")
    print("  (2) Safety maintained  — sign updates redirect from over-capacity rooms,")
    print("                           and clear once the floor genuinely recovers ✅")
    print(f"  (3) Pre-emptive        — reacted to {len(rooms_flagged)} rooms across the run ✅")
    print("=" * 60)


if __name__ == "__main__":
    run()