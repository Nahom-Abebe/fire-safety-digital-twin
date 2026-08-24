# phase1_setup.py
# Phase 1 — Digital Twin Foundation
# Verifies connection, graph, Psets, markers, board,
# room labels and corridor sign panels.
#
# Fixes applied:
#   1. Pset round-trip test now writes a value guaranteed to differ
#      from whatever is currently stored, verifies the change actually
#      took effect, then restores a sensible baseline. The previous
#      version wrote 15 and read back 15 regardless of whether the
#      write did anything, because a prior run had often already left
#      the value at 15 — proving nothing.
#   2. Room label / corridor sign steps now surface a WARNING with the
#      actual failure message whenever status != "ok", instead of
#      only printing whatever status came back with no follow-up.
#   3. The centroid count line now breaks 88 down into "80 rooms + 8
#      corridors" against the graph's own counts, instead of an
#      unqualified "88/88 rooms mapped" that implied 88 rooms when
#      the graph itself reports 80.
#   4. Fixed step counter: TOTAL_STEPS is a single constant referenced
#      by every step header, so the denominator can't drift ([1/7]
#      through [7/7] then jumping to [8/8], [9/9] as it did before).
#   5. The exit-path check and the exit-count assert were bundled
#      under one ambiguous "PASS" that actually only validated exit
#      count. They're now two separate, explicitly labelled checks.
#      The printed path_weight is explicitly labelled as graph
#      movement-cost units (door=1, stair=20, outdoor=10 — relative
#      traversal-difficulty weights used by the occupancy simulation),
#      not metres, so it's not misread as an ADB Table 2.1 18m
#      travel-distance compliance result.
#   6. The manager-panel description here was describing an old blind
#      "redirect to assembly" behaviour that manager_panel.py itself
#      no longer does — it now computes a real per-occupant exit path
#      (room -> corridor -> exit -> assembly). Updated to match.
#   7. Summary block only claims room labels / corridor signs were
#      created if they actually were, based on the real step outcomes,
#      rather than always listing them as delivered.

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

TOTAL_STEPS = 9


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

    # Track real outcomes for an honest summary block at the end
    labels_ok = False
    signs_ok  = False

    # ── Step 1: Connection ────────────────────────────────────────────────
    print(f"\n[1/{TOTAL_STEPS}] Blender connection...")
    if not test_connection():
        print("  FAILED — open Blender, load IFC, start MCP server in N-panel")
        return
    print("  OK")

    # ── Step 2: Graph verification ────────────────────────────────────────
    print(f"\n[2/{TOTAL_STEPS}] Graph verification...")
    G     = BUILDING_GRAPH
    rooms = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]
    exits = [n for n, d in G.nodes(data=True) if d["node_type"] == "exit"]
    print(f"  Nodes={G.number_of_nodes()} Edges={G.number_of_edges()} "
          f"Rooms={len(rooms)} Exits={len(exits)}")

    assert len(exits) == 4, "Should have 4 fire exits"
    print("  Exit count check: PASS (4 fire exits present)")

    # Worst-case room by graph movement-cost weight. These weights
    # (door=1, stair=20, outdoor=10) are relative traversal-difficulty
    # values used to bias the occupancy simulation's random walk — NOT
    # metre distances — so this validates graph connectivity and
    # identifies the hardest-to-reach room, not ADB Table 2.1 travel
    # distance compliance (that lives in the live agent's ADB checks).
    path = get_exit_path(67)
    connectivity_ok = "error" not in path
    print(f"  Worst-case room (3-1) reaches assembly via: "
          f"{path.get('path_labels')}")
    print(f"  Path movement-cost weight: {path.get('path_weight')} ")
    print(f"  Connectivity check: {'PASS' if connectivity_ok else 'FAIL'}")

    # ── Step 3: Pset round-trip ───────────────────────────────────────────
    print(f"\n[3/{TOTAL_STEPS}] Pset read/write (GlobalId pattern)...")
    lounge = next(
        (s for s in SPACES.values() if s["long_name"] == "Lounge"), None)
    if not lounge:
        print("  SKIP — Lounge not in JSON")
    else:
        gid    = lounge["global_id"]
        before = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        before_val = before.get("properties", {}).get("CurrentOccupancy")
        print(f"  Before: CurrentOccupancy={before_val}")

        # Write a value guaranteed to differ from whatever is currently
        # stored — writing the same value the field already holds
        # (the previous version always wrote 15, which was often
        # already there from an earlier run) cannot distinguish a
        # working write from a silently failed one.
        test_val = 999 if before_val != 999 else 888
        update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", {
            "CurrentOccupancy": test_val,
            "ComplianceStatus": "TEST",
            "LastUpdatedBy"   : "Phase1-VerificationTest",
        })
        mid = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        mid_val = mid.get("properties", {}).get("CurrentOccupancy")
        print(f"  After : CurrentOccupancy={mid_val} (wrote {test_val})")

        write_verified = (mid_val == test_val)
        print(f"  {'PASS — write verified' if write_verified else 'FAIL — value did not change to test value'}")

        # Restore a sensible baseline regardless of pass/fail, so later
        # scenarios aren't left with a stray test value.
        update_ifc_pset_properties(gid, "Pset_FireSafetyStatus", {
            "CurrentOccupancy": 15,
            "ComplianceStatus": "PASS",
            "LastUpdatedBy"   : "Phase1",
        })
        restored = get_ifc_pset_properties(gid, "Pset_FireSafetyStatus")
        restored_val = restored.get("properties", {}).get("CurrentOccupancy")
        print(f"  Restored: CurrentOccupancy={restored_val}")

    # ── Step 4: Centroid extraction + markers ─────────────────────────────
    print(f"\n[4/{TOTAL_STEPS}] Room centroids + occupant markers...")
    geo = extract_room_centroids()
    print(f"  Centroids: {geo}")
    if "extracted" not in geo:
        print("  FAILED — cannot place markers")
        return
    centroids = load_room_centroids()

    # Break the count down against the graph's own room/corridor
    # counts instead of one unqualified "N/N rooms mapped" line —
    # GRAPH_TO_IFC covers both room AND corridor labels (80 + 8 = 88),
    # while the graph's own room count is 80. Both numbers are
    # correct; they were just never shown to mean different things.
    graph_room_labels     = {d["label"] for _, d in G.nodes(data=True)
                             if d["node_type"] == "room"}
    graph_corridor_labels = {d["label"] for _, d in G.nodes(data=True)
                             if d["node_type"] == "corridor"}
    mapped_rooms     = sum(1 for lbl in graph_room_labels if lbl in centroids)
    mapped_corridors = sum(1 for lbl in graph_corridor_labels if lbl in centroids)
    print(f"  {len(centroids)}/{len(GRAPH_TO_IFC)} spatial entities mapped "
          f"({mapped_rooms}/{len(graph_room_labels)} rooms, "
          f"{mapped_corridors}/{len(graph_corridor_labels)} corridors)")

    snapshot = fake_snapshot(80)
    result   = create_markers(snapshot)
    print(f"  Markers: {result}")

    # ── Step 5: Board + auto-frame ────────────────────────────────────────
    print(f"\n[5/{TOTAL_STEPS}] Board + viewport framing...")
    create_board()
    count = result.get("created", result.get("requested", "?"))
    update_board(snapshot, f"Phase 1 complete — {count} occupants placed")
    frame = frame_view_on_objects("Occupant_")
    print(f"  Frame: {frame}")

    # ── Step 6: Room labels ───────────────────────────────────────────────
    print(f"\n[6/{TOTAL_STEPS}] Room labels (door signs)...")
    try:
        from bim.interior_signage import create_room_labels
        label_result = create_room_labels()
        created = label_result.get("created", "?")
        status  = label_result.get("status", "?")
        print(f"  Room labels created: {created}")
        print(f"  Status: {status}")
        if status != "ok":
            print(f"  WARNING: {label_result.get('message', 'unspecified failure')}")
        if label_result.get("warning"):
            print(f"  NOTE: {label_result['warning']}")
        labels_ok = (status == "ok" and isinstance(created, int) and created > 0)
    except ImportError:
        print("  SKIP — interior_signage.py not found")
        print("  Create bim/interior_signage.py to enable room labels")
    except Exception as e:
        print(f"  WARNING — room labels failed: {e}")

    # ── Step 7: Corridor sign panels ──────────────────────────────────────
    print(f"\n[7/{TOTAL_STEPS}] Digital corridor sign panels...")
    try:
        from bim.interior_signage import create_corridor_signs
        sign_result = create_corridor_signs()
        created = sign_result.get("created", "?")
        status  = sign_result.get("status", "?")
        expected = sign_result.get("expected")
        print(f"  Sign panels created: {created}"
              + (f" / {expected} expected" if expected is not None else ""))
        print(f"  Status: {status}")
        if status != "ok":
            print(f"  WARNING: {sign_result.get('message', 'unspecified failure')}")
        else:
            print("  Panels start GREEN — turn RED/AMBER when agent updates signs")
        signs_ok = (status == "ok" and isinstance(created, int) and created > 0)
    except ImportError:
        print("  SKIP — interior_signage.py not found")
        print("  Create bim/interior_signage.py to enable corridor panels")
    except Exception as e:
        print(f"  WARNING — corridor signs failed: {e}")

    # ── Step 8: Assembly point ────────────────────────────────────────────
    print(f"\n[8/{TOTAL_STEPS}] Assembly point (outside building)...")
    try:
        from bim.assembly_point import create_assembly_point
        ap_result = create_assembly_point()
        print(f"  Status: {ap_result.get('status')}")
        print(f"  Location: {ap_result.get('location')}")
    except Exception as e:
        print(f"  WARNING: {e}")

    # ── Step 9: Manager panel ─────────────────────────────────────────────
    print(f"\n[9/{TOTAL_STEPS}] Building manager panel...")
    try:
        from bim.manager_panel import install_manager_panel
        install_manager_panel()
        print("  Panel registered — N-panel -> 'Fire Safety' tab")
        print("  ESCALATE button computes a real exit path per occupant")
        print("  (room -> corridor -> exit -> assembly)")
    except Exception as e:
        print(f"  WARNING: {e}")

    # ── Summary ───────────────────────────────────────────────────────────
    print("\n" + "=" * 55)
    print("  PHASE 1 COMPLETE")
    print("  Check Blender:")
    print("  - 80 cone markers across F0-F3")
    print("  - FireSafetyBoard text visible")
    if labels_ok:
        print("  - Yellow room labels at each door")
    else:
        print("  - Room labels: NOT confirmed created — see step 6 warning above")
    if signs_ok:
        print("  - Green corridor sign panels")
    else:
        print("  - Corridor signs: NOT confirmed created — see step 7 warning above")
    print("  - Navigate inside building to see labels and signs")
    print("=" * 55)


if __name__ == "__main__":
    main()