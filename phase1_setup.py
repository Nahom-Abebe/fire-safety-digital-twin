# phase1_setup.py
# Phase 1 — Digital Twin Foundation
# Verifies connection, graph, Psets, markers, board,
# room labels and corridor sign panels.

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
    G     = BUILDING_GRAPH
    rooms = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]
    occ   = {node: 0 for node in G.nodes}
    for _ in range(total):
        occ[random.choice(rooms)] += 1
    by_floor = {}
    for n, c in occ.items():
        fl = G.nodes[n]["floor"]
        by_floor[fl] = by_floor.get(fl, 0) + c
    return {
        "timestamp": datetime.now().strftime("%H:%M:%S"),
        "tick"     : 0,
        "total_occ": total,
        "at_exits" : 0,
        "occupancy": {G.nodes[n]["label"]: c for n, c in occ.items() if c > 0},
        "by_floor" : by_floor,
        "event"    : None,
        "alerts"   : [],
    }


def main():
    print("=" * 55)
    print("  PHASE 1 — DIGITAL TWIN FOUNDATION")
    print("=" * 55)

    # ── Step 1: Connection ────────────────────────────────────────────────
    print("\n[1/7] Blender connection...")
    if not test_connection():
        print("  FAILED — open Blender, load IFC, start MCP server in N-panel")
        return
    print("  OK")

    # ── Step 2: Graph verification ────────────────────────────────────────
    print("\n[2/7] Graph verification...")
    G     = BUILDING_GRAPH
    rooms = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]
    exits = [n for n, d in G.nodes(data=True) if d["node_type"] == "exit"]
    print(f"  Nodes={G.number_of_nodes()} Edges={G.number_of_edges()} "
          f"Rooms={len(rooms)} Exits={len(exits)}")
    path = get_exit_path(67)   # worst case: top floor room
    print(f"  Worst-case exit path (3-1): {path.get('path_labels')}")
    assert len(exits) == 4, "Should have 4 fire exits"
    print("  PASS")

    # ── Step 3: Pset round-trip ───────────────────────────────────────────
    print("\n[3/7] Pset read/write (GlobalId pattern)...")
    lounge = next(
        (s for s in SPACES.values() if s["long_name"] == "Lounge"), None)
    if not lounge:
        print("  SKIP — Lounge not in JSON")
    else:
        gid    = lounge["global_id"]
        before = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        print(f"  Before: CurrentOccupancy="
              f"{before.get('properties', {}).get('CurrentOccupancy')}")
        update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", {
            "CurrentOccupancy": 15,
            "ComplianceStatus": "PASS",
            "LastUpdatedBy"   : "Phase1",
        })
        after = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        val   = after.get("properties", {}).get("CurrentOccupancy")
        print(f"  After : CurrentOccupancy={val}")
        print(f"  {'PASS' if val == 15 else 'WARNING — value did not update'}")

    # ── Step 4: Centroid extraction + markers ─────────────────────────────
    print("\n[4/7] Room centroids + occupant markers...")
    geo = extract_room_centroids()
    print(f"  Centroids: {geo}")
    if "extracted" not in geo:
        print("  FAILED — cannot place markers")
        return
    centroids = load_room_centroids()
    print(f"  {len(centroids)}/{len(GRAPH_TO_IFC)} rooms mapped")
    snapshot = fake_snapshot(80)
    result   = create_markers(snapshot)
    print(f"  Markers: {result}")

    # ── Step 5: Board + auto-frame ────────────────────────────────────────
    print("\n[5/7] Board + viewport framing...")
    create_board()
    count = result.get("created", result.get("requested", "?"))
    update_board(snapshot, f"Phase 1 complete — {count} occupants placed")
    frame = frame_view_on_objects("Occupant_")
    print(f"  Frame: {frame}")

    # ── Step 6: Room labels ───────────────────────────────────────────────
    print("\n[6/7] Room labels (door signs)...")
    try:
        from bim.interior_signage import create_room_labels
        label_result = create_room_labels()
        created = label_result.get("created", "?")
        print(f"  Room labels created: {created}")
        print(f"  Status: {label_result.get('status', '?')}")
    except ImportError:
        print("  SKIP — interior_signage.py not found")
        print("  Create bim/interior_signage.py to enable room labels")
    except Exception as e:
        print(f"  WARNING — room labels failed: {e}")

    # ── Step 7: Corridor sign panels ──────────────────────────────────────
    print("\n[7/7] Digital corridor sign panels...")
    try:
        from bim.interior_signage import create_corridor_signs
        sign_result = create_corridor_signs()
        created = sign_result.get("created", "?")
        print(f"  Sign panels created: {created}")
        print(f"  Status: {sign_result.get('status', '?')}")
        print("  Panels start GREEN — turn RED/AMBER when agent updates signs")
    except ImportError:
        print("  SKIP — interior_signage.py not found")
        print("  Create bim/interior_signage.py to enable corridor panels")
    except Exception as e:
        print(f"  WARNING — corridor signs failed: {e}")

    # ── Step 8: Assembly point ────────────────────────────────────────────
    print("\n[8/8] Assembly point (outside building)...")
    try:
        from bim.assembly_point import create_assembly_point
        ap_result = create_assembly_point()
        print(f"  Status: {ap_result.get('status')}")
        print(f"  Location: {ap_result.get('location')}")
    except Exception as e:
        print(f"  WARNING: {e}")

    # ── Step 9: Manager panel ─────────────────────────────────────────────
    print("\n[9/9] Building manager panel...")
    try:
        from bim.manager_panel import install_manager_panel
        install_manager_panel()
        print("  Panel registered — N-panel -> 'Fire Safety' tab")
        print("  Red ESCALATE button redirects all occupants to assembly")
    except Exception as e:
        print(f"  WARNING: {e}")
        
    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  PHASE 1 COMPLETE")
    print("  Check Blender:")
    print("  - 80 cone markers across F0-F3")
    print("  - FireSafetyBoard text visible (left of building)")
    print("  - Lounge Pset_FireSafetyStatus.CurrentOccupancy = 15")
    print("  - Yellow room labels at each door")
    print("  - Green corridor sign panels (one per floor)")
    print("  - Navigate inside building to see labels and signs")
    print("=" * 55)


if __name__ == "__main__":
    main()