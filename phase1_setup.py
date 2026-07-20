# phase1_setup.py
# Phase 1 orchestrator — run from project root: python phase1_setup.py
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bim.ifc_bridge import test_connection, get_ifc_pset_properties, update_ifc_pset_properties
from bim.bim_query import SPACES, get_space_by_name
from bim.room_geometry import extract_room_centroids, load_room_centroids, GRAPH_TO_IFC
from bim.occupant_markers import create_markers
from bim.board import create_board, update_board
from bim.viewport_utils import frame_view_on_objects
from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy, get_exit_path

import random
from datetime import datetime


def fake_snapshot(total=80):
    """Minimal snapshot for Phase 1 testing (no sensor_sim yet)."""
    G = BUILDING_GRAPH
    rooms = [n for n,d in G.nodes(data=True) if d["node_type"]=="room"]
    occ = {node: 0 for node in G.nodes}
    for _ in range(total):
        occ[random.choice(rooms)] += 1
    by_floor = {}
    for n,c in occ.items():
        fl = G.nodes[n]["floor"]
        by_floor[fl] = by_floor.get(fl,0) + c
    return {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "tick": 0, "total_occ": total, "at_exits": 0,
        "occupancy": {G.nodes[n]["label"]: c for n,c in occ.items() if c>0},
        "by_floor": by_floor, "event": None, "alerts": [],
    }


def main():
    print("=" * 55)
    print("  PHASE 1 — DIGITAL TWIN FOUNDATION")
    print("=" * 55)

    # ── Step 1: Connection ───────────────────────────────────────────────
    print("\n[1/5] Blender connection...")
    if not test_connection():
        print("  FAILED — open Blender, load IFC, start MCP server in N-panel")
        return
    print("  OK")

    # ── Step 2: Graph verification ───────────────────────────────────────
    print("\n[2/5] Graph verification...")
    G = BUILDING_GRAPH
    rooms = [n for n,d in G.nodes(data=True) if d["node_type"]=="room"]
    exits = [n for n,d in G.nodes(data=True) if d["node_type"]=="exit"]
    print(f"  Nodes={G.number_of_nodes()} Edges={G.number_of_edges()} "
          f"Rooms={len(rooms)} Exits={len(exits)}")
    path = get_exit_path(67)  # worst case: top floor room
    print(f"  Worst-case exit path (3-1): {path.get('path_labels')}")
    assert len(exits) == 4, "Should have 4 fire exits"
    print("  PASS")

    # ── Step 3: Pset round-trip  ────────────────────────
    print("\n[3/5] Pset read/write (GlobalId pattern)...")
    lounge = next((s for s in SPACES.values() if s["long_name"]=="Lounge"), None)
    if not lounge:
        print("  SKIP — Lounge not in JSON")
    else:
        gid = lounge["global_id"]
        before = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        print(f"  Before: CurrentOccupancy={before.get('properties',{}).get('CurrentOccupancy')}")
        update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", {
            "CurrentOccupancy": 12, "ComplianceStatus": "PASS", "LastUpdatedBy": "Phase1"})
        after = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        val = after.get("properties",{}).get("CurrentOccupancy")
        print(f"  After : CurrentOccupancy={val}")
        print(f"  {'PASS' if val==12 else 'WARNING — value did not update'}")

    # ── Step 4: Centroid extraction + markers ────────────────────────────
    print("\n[4/5] Room centroids + occupant markers...")
    geo = extract_room_centroids()
    print(f"  Centroids: {geo}")
    if "extracted" not in geo:
        print("  FAILED — cannot place markers"); return
    centroids = load_room_centroids()
    print(f"  {len(centroids)}/{len(GRAPH_TO_IFC)} rooms mapped")
    snapshot = fake_snapshot(80)
    result = create_markers(snapshot)
    print(f"  Markers: {result}")

    # ── Step 5: Board + auto-frame ───────────────────────────────────────
    print("\n[5/5] Board + viewport framing...")
    create_board()
    count = result.get("created", result.get("requested","?"))
    update_board(snapshot, f"Phase 1 complete — {count} occupants placed")
    frame = frame_view_on_objects("Occupant_")
    print(f"  Frame: {frame}")

    print("\n" + "=" * 55)
    print("  PHASE 1 COMPLETE")
    print("  Check Blender:")
    print("  - 80 blue cones across F0–F3")
    print("  - FireSafetyBoard text visible")
    print("  - Lounge Pset_FireSafetyStatus updated")
    print("=" * 55)


if __name__ == "__main__":
    main()