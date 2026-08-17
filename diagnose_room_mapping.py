# diagnose_room_mapping.py
# Cross-checks room_geometry.py's GRAPH_TO_IFC against the real IFC
# space data in bim_query.SPACES. Prints, for every graph room node,
# what its GRAPH_TO_IFC target ACTUALLY is in the real IFC model —
# real long_name, real max_occ, real floor — next to what the graph
# itself assumes about that node (its own floor label, and what
# get_max_occupancy() currently computes from area_m2).
#
# Run this once, then look specifically at the "BEDROOM CANDIDATES"
# section at the bottom — those are the only graph nodes currently
# safe to use as a scenario's violation_room if the scenario wants
# to depict a genuine bedroom overcrowding, given the CURRENT
# GRAPH_TO_IFC mapping. Everything else either isn't a bedroom at
# all, or has a cross-floor mismatch and shouldn't be trusted for
# geometry placement either.

import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bim.room_geometry import GRAPH_TO_IFC
from bim.bim_query import SPACES
from sensors.building_graph import BUILDING_GRAPH, get_max_occupancy

GRAPH_FLOOR = {
    "0": "F0 Ground Floor", "1": "F1 First Floor",
    "2": "F2 Second Floor", "3": "F3 Third Floor",
}

by_ifc_name = {}
for gid, s in SPACES.items():
    by_ifc_name[s.get("name")] = s

label_to_node = {d["label"]: n for n, d in BUILDING_GRAPH.nodes(data=True)}

print("=" * 100)
print(f"{'Graph':8s} {'IFC':7s} {'Real long_name':16s} {'Real max_occ':13s} "
      f"{'Sim max_occ':12s} {'Floor OK?':10s}")
print("=" * 100)

mismatches      = []
bedroom_matches = []

for graph_label, ifc_name in sorted(GRAPH_TO_IFC.items()):
    if graph_label.endswith("-A") or graph_label.endswith("-B"):
        continue  # corridor nodes, not numbered rooms — skip

    entry = by_ifc_name.get(ifc_name)
    node  = label_to_node.get(graph_label)
    sim_max = get_max_occupancy(node) if node is not None else "?"

    if entry is None:
        print(f"{graph_label:8s} {ifc_name:7s} <not found in bim_query.SPACES>")
        continue

    real_name  = entry.get("long_name", "?")
    real_max   = entry.get("max_occ", "?")
    real_floor = entry.get("floor", "?")

    expected_floor = GRAPH_FLOOR.get(graph_label.split("-")[0])
    floor_ok = "YES" if real_floor == expected_floor else "NO <-- MISMATCH"

    print(f"{graph_label:8s} {ifc_name:7s} {real_name:16s} {str(real_max):13s} "
          f"{str(sim_max):12s} {floor_ok:10s}")

    if floor_ok.startswith("NO"):
        mismatches.append((graph_label, ifc_name, real_name, expected_floor, real_floor))
    if real_name.lower() == "bedroom" and floor_ok == "YES":
        bedroom_matches.append((graph_label, ifc_name, real_max))

print("\n" + "=" * 100)
print("CROSS-FLOOR MISMATCHES — do not trust these nodes for geometry OR occupancy")
print("=" * 100)
for m in mismatches:
    print(f"  {m[0]} -> IFC {m[1]} ('{m[2]}') expected {m[3]}, actually on {m[4]}")
if not mismatches:
    print("  none found")

print("\n" + "=" * 100)
print("BEDROOM CANDIDATES — graph nodes whose real IFC identity IS 'Bedroom',")
print("with a matching floor. Safe to use as a scenario's violation_room if")
print("you want a genuine bedroom-overcrowding narrative.")
print("=" * 100)
for b in bedroom_matches:
    print(f"  {b[0]:8s} -> IFC {b[1]:6s} (real max_occ={b[2]})")
if not bedroom_matches:
    print("  none found — GRAPH_TO_IFC may need rebuilding, not just reordering")
print(f"\nTotal verified bedroom candidates: {len(bedroom_matches)}")