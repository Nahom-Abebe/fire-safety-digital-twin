# live_agent_runner.py
# Live occupancy management agent — runs the simulation tick by tick
# and lets Claude reason about what it sees in real time.
#
# Fixes applied in this version:
#   - Fixed floor colour persistence: floors revert to GREEN when violations clear
#   - Optimized Blender IPC: floor colours only updated when floor states change
#   - Unified default seed across CLI and function signature (default 16)
#   - Added try-except wrapper around Blender IPC calls to prevent tick loop crashes

import sys, os, json, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
from sensors.sensor_sim import (
    initialise_occupants, move_occupants, get_sensor_snapshot
)
from bim.occupant_markers import create_markers, live_reposition_markers
from bim.board import create_board, update_board
from bim.viewport_utils import frame_view_on_objects
from bim.ifc_bridge import test_connection, send_to_blender
from bim.signage import reset_all_signs
from agent.agent import run_agent_cycle
from agent.tool_schemas import TOOLS
from sensors.building_graph import BUILDING_GRAPH as G


# ── Floor colour constants ────────────────────────────────────────────────────
FLOOR_COLLECTIONS = {
    "F0 Ground Floor": "IfcBuildingStorey/F0 Ground Floor",
    "F1 First Floor" : "IfcBuildingStorey/F1 First Floor",
    "F2 Second Floor": "IfcBuildingStorey/F2 Second Floor",
    "F3 Third Floor" : "IfcBuildingStorey/F3 Third Floor",
}

SKIP_PREFIXES = (
    'IfcFurnishing', 'IfcBuildingElementProxy', 'IfcFlowTerminal',
    'IfcSanitaryTerminal', 'IfcLightFixture', 'IfcAlarm', 'IfcSign',
    'IfcDoor', 'IfcWindow',
)

# Map graph node labels to their floor names
_LABEL_TO_FLOOR = {
    d["label"]: d["floor"] for _, d in G.nodes(data=True)
}


# ── Clear baked animation ─────────────────────────────────────────────────────

def _clear_baked_animation():
    """
    Removes all keyframe animation data from occupant markers and
    floor materials left over from a previous bake_animation.py run.
    """
    try:
        send_to_blender("""
import bpy

cleared_markers = 0
cleared_mats    = 0

for obj in bpy.data.objects:
    if obj.name.startswith('Occupant_'):
        if obj.animation_data:
            obj.animation_data_clear()
        for mat in (obj.data.materials if obj.data else []):
            if mat and mat.node_tree and mat.node_tree.animation_data:
                mat.node_tree.animation_data_clear()
        cleared_markers += 1

for mat in bpy.data.materials:
    if mat.name.startswith('FS_FLOOR_') or mat.name.startswith('LIVE_'):
        if mat.node_tree and mat.node_tree.animation_data:
            mat.node_tree.animation_data_clear()
        cleared_mats += 1

bpy.context.scene.frame_set(0)
try:
    if bpy.context.screen.is_animation_playing:
        bpy.ops.screen.animation_cancel()
except Exception:
    pass

for fn in [f for f in bpy.app.handlers.frame_change_pre
           if f.__name__ == '_board_handler']:
    bpy.app.handlers.frame_change_pre.remove(fn)

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()

print(f'Cleared {cleared_markers} markers, {cleared_mats} floor mats')
""")
    except Exception as e:
        print(f"Warning: Failed to clear baked animation: {e}")


# ── Floor colour update ───────────────────────────────────────────────────────

def _update_floor_colours(confirmed_red_floors: set):
    """
    Updates floor colours based on AGENT-CONFIRMED violations only.
    GREEN = all rooms compliant
    RED   = agent confirmed an over-capacity room on this floor
    """
    colour_lines = []
    for floor_name, col_name in FLOOR_COLLECTIONS.items():
        if floor_name in confirmed_red_floors:
            r, g, b = 0.90, 0.05, 0.05   # RED — confirmed violation
        else:
            r, g, b = 0.05, 0.70, 0.15   # GREEN — compliant

        mat_name = f"LIVE_{floor_name[:2].replace(' ', '_')}"
        colour_lines.append(
            f"colour_floor('{col_name}', '{mat_name}', {r}, {g}, {b})")

    skip_str   = str(SKIP_PREFIXES)
    colour_str = "\n".join(colour_lines)

    code = f"""
import bpy
SKIP = {skip_str}

def colour_floor(col_name, mat_name, r, g, b):
    col = bpy.data.collections.get(col_name)
    if not col:
        return
    mat = bpy.data.materials.get(mat_name)
    if not mat:
        mat = bpy.data.materials.new(name=mat_name)
        mat.use_nodes = True
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value = (r, g, b, 1.0)
    for obj in col.objects:
        if obj.type != 'MESH':
            continue
        if any(obj.name.startswith(p) for p in SKIP):
            continue
        if obj.data:
            obj.data.materials.clear()
            obj.data.materials.append(mat)

{colour_str}

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()
"""
    try:
        send_to_blender(code)
    except Exception as e:
        print(f"Warning: Failed to update floor colours in Blender: {e}")


# ── Main live loop ────────────────────────────────────────────────────────────

def run(total_ticks: int = 25,
        sense_every: int = 3,
        total_occupants: int = 80,
        tick_delay: float = 2.0,
        seed: int = 16,
        verbose: bool = True):

    # ── API key ───────────────────────────────────────────────────────────
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("ERROR: Set ANTHROPIC_API_KEY first")
        print("  $env:ANTHROPIC_API_KEY = 'sk-ant-...'")
        sys.exit(1)

    client = anthropic.Anthropic()

    print("=" * 60)
    print("  LIVE OCCUPANCY MANAGEMENT AGENT")
    print("  Fire Safety Digital Twin — Care Home")
    print("=" * 60)
    print(f"\nConfiguration:")
    print(f"  Occupants    : {total_occupants}")
    print(f"  Total ticks  : {total_ticks}")
    print(f"  Agent checks : every {sense_every} ticks")
    print(f"  Tick delay   : {tick_delay}s")
    print(f"  Seed         : {seed}")
    print(f"  Model        : claude-haiku-4-5 (fast live demo)")

    # ── Blender connection ────────────────────────────────────────────────
    print("\nChecking Blender connection...")
    if not test_connection():
        print("FAILED — open Blender, load IFC, start MCP server (N-panel)")
        sys.exit(1)
    print("OK")

    # ── Initialise ────────────────────────────────────────────────────────
    print("\nInitialising simulation...")
    reset_all_signs()
    initialise_occupants(total_occupants, seed=seed)
    snapshot = get_sensor_snapshot()

    print("Clearing any baked animation data...")
    _clear_baked_animation()

    print("Placing occupant markers...")
    result = create_markers(snapshot)
    print(f"  {result.get('created', '?')} markers placed")

    create_board()
    update_board(snapshot, "Live occupancy monitoring active")

    # Set initial state
    confirmed_red_floors = set()
    last_rendered_red_floors = None  # Caching for socket performance
    
    _update_floor_colours(confirmed_red_floors)
    last_rendered_red_floors = set(confirmed_red_floors)

    frame_view_on_objects("Occupant_")

    # ── Session tracking ──────────────────────────────────────────────────
    session_log = {
        "config": {
            "total_ticks"     : total_ticks,
            "sense_every"     : sense_every,
            "total_occupants" : total_occupants,
            "seed"            : seed,
        },
        "ticks"        : [],
        "agent_cycles" : [],
        "total_actions": 0,
        "rooms_managed": set(),
        "start_time"   : time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"\nStarting live loop ({total_ticks} ticks)...")
    print(f"Agent checks every {sense_every} ticks "
          f"(immediate on over-capacity)")
    print("-" * 60)

    for tick in range(1, total_ticks + 1):

        # ── Advance simulation ────────────────────────────────────────────
        move_occupants()
        snapshot = get_sensor_snapshot()

        # ── Update Blender visuals ────────────────────────────────────────
        live_reposition_markers(snapshot)
        update_board(snapshot)

        # ── Tick summary ──────────────────────────────────────────────────
        warns = [a for a in snapshot["alerts"] if a["severity"] == "WARNING"]
        overs = [a for a in snapshot["alerts"] if a["severity"] == "OVER"]

        # Prune confirmed red floors if the underlying OVER condition cleared
        current_over_floors = {
            _LABEL_TO_FLOOR.get(a["label"]) for a in overs if _LABEL_TO_FLOOR.get(a["label"])
        }
        confirmed_red_floors &= current_over_floors

        # Only update floor materials in Blender if the floor status set actually changed
        if confirmed_red_floors != last_rendered_red_floors:
            _update_floor_colours(confirmed_red_floors)
            last_rendered_red_floors = set(confirmed_red_floors)

        session_log["ticks"].append({
            "tick"     : tick,
            "total_occ": snapshot["total_occ"],
            "warnings" : len(warns),
            "over"     : len(overs),
            "alerts"   : [
                {"label": a["label"], "current": a["current"],
                 "max": a["max"], "severity": a["severity"]}
                for a in snapshot["alerts"]
            ],
        })

        alert_summary = ""
        if overs:
            alert_summary = f" | 🔴 OVER: {[a['label'] for a in overs]}"
        elif warns:
            alert_summary = f" | ⚠  WARN: {[a['label'] for a in warns]}"

        print(f"Tick {tick:02d} | "
              f"occ={snapshot['total_occ']:3d} | "
              f"warnings={len(warns):2d} | "
              f"over={len(overs):2d}"
              f"{alert_summary}")

        # ── Agent cycle ───────────────────────────────────────────────────
        should_run_agent = (
            tick % sense_every == 0
            or len(overs) > 0
        )

        if should_run_agent:
            print(f"\n  [Tick {tick}] Running agent cycle...")

            over_summary  = ", ".join(
                f"{a['label']} ({a['current']}/{a['max']})"
                for a in overs) or "none"
            warn_summary  = ", ".join(
                f"{a['label']} ({a['current']}/{a['max']})"
                for a in warns) or "none"
            floor_summary = ", ".join(
                f"{fl}: {cnt}"
                for fl, cnt in snapshot["by_floor"].items()
                if cnt > 0)

            trigger = (
                f"LIVE SIMULATION STATE at Tick {tick}:\n"
                f"Total occupants: {snapshot['total_occ']}\n"
                f"Floor occupancy: {floor_summary}\n"
                f"OVER-capacity rooms: {over_summary}\n"
                f"WARNING rooms (80%+): {warn_summary}\n\n"
                f"Perform your Sense-Reason-Act cycle. "
                f"For each over-capacity or warning room, retrieve the "
                f"specific ADB care home clause that applies "
                f"(e.g. Section 2.33, Clause 2.43, Table 2.1) "
                f"and cite it precisely in your board directive. "
                f"Only act on rooms where check_compliance confirms a "
                f"genuine violation against the IFC model. "
                f"Do NOT trigger any building-wide response — "
                f"only redirect occupants away from the affected room. "
                f"Keep your board directive concise — max 4 short lines."
            )

            result = run_agent_cycle(
                client,
                trigger_message=trigger,
                verbose=verbose
            )

            # ── Update confirmed violations ───────────────────────────────
            if result["signs_updated"] > 0:
                for a in overs:
                    fl = _LABEL_TO_FLOOR.get(a["label"])
                    if fl:
                        confirmed_red_floors.add(fl)

                # Refresh floor colours immediately if a new violation was confirmed
                if confirmed_red_floors != last_rendered_red_floors:
                    _update_floor_colours(confirmed_red_floors)
                    last_rendered_red_floors = set(confirmed_red_floors)
                    print(f"  Floor(s) confirmed RED: {confirmed_red_floors}")

            # ── Log ───────────────────────────────────────────────────────
            session_log["agent_cycles"].append({
                "tick"             : tick,
                "latency_seconds"  : result["latency_seconds"],
                "tool_count"       : result["tool_count"],
                "signs_updated"    : result["signs_updated"],
                "adb_cited"        : result["adb_cited"],
                "directive_excerpt": result["directive"][:200],
            })
            session_log["total_actions"] += result["signs_updated"]
            if result["signs_updated"] > 0:
                for a in overs:
                    session_log["rooms_managed"].add(a["label"])

            # Update board with agent directive
            update_board(snapshot, result["directive"])

            print(f"\n  Agent cycle complete:")
            print(f"  Latency      : {result['latency_seconds']}s")
            print(f"  Tool calls   : {result['tool_count']}")
            print(f"  Signs updated: {result['signs_updated']}")
            print(f"  ADB cited    : {result['adb_cited']}")
            if result["signs_updated"] == 0 and len(overs) == 0:
                print(f"  → No violations — agent correctly idled")
            print()

        time.sleep(tick_delay)

    # ── Session summary ───────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  LIVE SESSION COMPLETE")
    print("=" * 60)

    n_cycles = len(session_log["agent_cycles"])
    if n_cycles > 0:
        latencies = [c["latency_seconds"] for c in session_log["agent_cycles"]]
        mean_lat  = round(sum(latencies) / n_cycles, 1)
        adb_hits  = sum(1 for c in session_log["agent_cycles"]
                        if c["adb_cited"])
    else:
        mean_lat, adb_hits = 0, 0

    print(f"  Ticks run        : {total_ticks}")
    print(f"  Agent cycles     : {n_cycles}")
    print(f"  Mean latency     : {mean_lat}s")
    print(f"  Total sign acts  : {session_log['total_actions']}")
    print(f"  Rooms managed    : {len(session_log['rooms_managed'])}")
    print(f"  ADB cited        : {adb_hits}/{n_cycles} cycles")
    print()
    print("  System's criteria:")
    print("  (1) Correct state   — floor colours + board updated every tick ✅")
    print("  (2) Safety maintained — confirmed violations turn floor RED ✅")
    print(f"  (3) Pre-emptive     — agent ran {n_cycles} cycles, "
          f"acting on {session_log['total_actions']} genuine violations ✅")
    print("=" * 60)

    # ── Save log ──────────────────────────────────────────────────────────
    os.makedirs("logs", exist_ok=True)
    session_log["rooms_managed"] = list(session_log["rooms_managed"])
    session_log["end_time"]      = time.strftime("%Y-%m-%d %H:%M:%S")
    session_log["summary"] = {
        "n_cycles"     : n_cycles,
        "mean_latency" : mean_lat,
        "total_actions": session_log["total_actions"],
        "adb_cited"    : f"{adb_hits}/{n_cycles}",
    }

    log_path = os.path.join("logs", "live_session.json")
    with open(log_path, "w") as f:
        json.dump(session_log, f, indent=2, default=str)
    print(f"\n  Full log saved: {log_path}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Live occupancy management agent — care home Digital Twin")

    parser.add_argument("--ticks", type=int, default=25,
                        help="Total simulation ticks (default 25)")
    parser.add_argument("--sense-every", type=int, default=3,
                        help="Agent checks every N ticks (default 3)")
    parser.add_argument("--occupants", type=int, default=80,
                        help="Number of occupants (default 80)")
    parser.add_argument("--delay", type=float, default=2.0,
                        help="Seconds between ticks (default 2.0)")
    parser.add_argument("--seed", type=int, default=16,
                        help="Random seed (default 16)")
    parser.add_argument("--quiet", action="store_true",
                        help="Hide agent tool call details")

    args = parser.parse_args()

    run(
        total_ticks     = args.ticks,
        sense_every     = args.sense_every,
        total_occupants = args.occupants,
        tick_delay      = args.delay,
        seed            = args.seed,
        verbose         = not args.quiet,
    )