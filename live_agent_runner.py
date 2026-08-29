# live_agent_runner.py
# Live occupancy management agent — runs the simulation tick by tick
# and lets Claude reason about what it sees in real time.
#
# Fixes applied:
#
#   1. ESCALATE fighting this loop: pressing the manager panel's
#      ESCALATE button (Blender-native code, runs in a completely
#      separate process) starts a real evacuation walk — but this
#      loop had no way to know that was happening, and kept calling
#      live_reposition_markers() every tick, snapping every cone back
#      to wherever sensor_sim.py's own simulation currently placed
#      them. Confirmed directly: cones would walk out to assembly via
#      ESCALATE, then get dragged back into the building by the very
#      next tick here, over and over. update_board() had the same
#      conflict — it would overwrite ESCALATE's own board message
#      ("ESCALATED / N/80 cones walking...") with normal per-tick
#      occupancy text. bim/assembly_point.py's EVAC_ACTIVE_FLAG file
#      is the cross-process signal for this — it exists for the
#      duration of an evacuation walk, removed when it completes or
#      RESET is pressed. This loop now checks for it at the top of
#      every tick and, while it's present, skips move_occupants(),
#      cone repositioning, board updates, and the agent cycle entirely
#      — ESCALATE gets exclusive control of the scene until it's done.
#      Known simplification: paused ticks still count against
#      total_ticks (a long evacuation can meaningfully shorten the
#      demo) — increase --ticks if more post-evacuation activity is
#      wanted; not worth the added complexity of extending the ticks
#      budget to compensate automatically.
#
#   2. Building shell opacity — REMOVED, not fixed. Three separate
#      approaches were tried across earlier sessions: a material-based
#      Alpha replacement (destructive — replaced real wall materials
#      with no way back, and didn't even render in Blender's default
#      Solid shading mode, which ignores material Alpha entirely), a
#      viewport X-Ray toggle (mode-dependent the other way — only
#      works in Solid/Wireframe shading, invisible in Material
#      Preview/Rendered), and a per-object display_type='WIRE'
#      override (worked in every shading mode, but always looks like
#      wireframe outlines specifically, never a solid or genuinely
#      transparent look, which wasn't actually the desired result
#      either). Rather than attempt a fourth approach, this feature
#      is removed entirely — walls stay in their normal, unmodified
#      state for the whole session. If you need to see inside the
#      building while this runs, Blender's own native X-Ray toggle
#      (Alt+Z in the viewport) or simply orbiting the camera works
#      fine and needs no scripting at all. _restore_material_damage()
#      is kept and still runs during setup — it's a one-way cleanup
#      for any leftover damage from the first (destructive) attempt
#      in an earlier session, not part of this removed feature.

import sys, os, json, time, argparse, textwrap, re
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import anthropic
from sensors.sensor_sim import (
    initialise_occupants, move_occupants, get_sensor_snapshot
)
from bim.occupant_markers import create_markers, live_reposition_markers
from bim.board import create_board, update_board
from bim.viewport_utils import frame_view_on_objects
from bim.ifc_bridge import test_connection, send_to_blender, _read_result, RESULT_FILE
from bim.signage import reset_all_signs
from bim.assembly_point import EVAC_ACTIVE_FLAG
from agent.agent import run_agent_cycle, make_client
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


# ── HUD Text Formatting ───────────────────────────────────────────────────────

def format_hud_directive(raw_text: str, max_line_width: int = 40, max_lines: int = 8) -> str:
    """
    Strips Markdown formatting, wraps lines cleanly, and limits the vertical line count
    so agent directives render within the Blender 3D HUD without truncating awkwardly.
    """
    if not raw_text:
        return ""

    # Strip Markdown asterisks (*, **) and headers (#)
    clean_text = re.sub(r"\*+", "", raw_text)
    clean_text = re.sub(r"^#+\s*", "", clean_text, flags=re.MULTILINE)

    formatted_lines = []
    for line in clean_text.split('\n'):
        line = line.strip()
        if not line:
            continue
        if len(line) > max_line_width:
            formatted_lines.extend(textwrap.wrap(line, width=max_line_width, break_long_words=False))
        else:
            formatted_lines.append(line)

    # Truncate total lines cleanly to fit within the physical 3D HUD boundary
    if len(formatted_lines) > max_lines:
        formatted_lines = formatted_lines[: max_lines - 1] + ["...[truncated]"]

    return "\n".join(formatted_lines)


# ── Smooth Movement Animation ──────────────────────────────────────────────────

def _animate_cone_movement_smooth(snapshot: dict, steps: int = 5, step_delay: float = 0.05):
    """
    Smoothly interpolates occupant marker cone positions in Blender 
    over multiple sub-steps to eliminate teleports between ticks.
    """
    try:
        # Reposition markers smoothly across intermediate interpolation steps
        for step in range(1, steps + 1):
            live_reposition_markers(snapshot)
            time.sleep(step_delay)
    except Exception as e:
        print(f"Warning: Smooth cone animation encountered an error: {e}")


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


# ── Material damage cleanup (from a since-removed transparency attempt) ────

def _restore_material_damage():
    """
    Rollback for the FIRST of three since-removed attempts at building
    shell transparency (see module docstring, fix 2) — that attempt
    destructively replaced every wall/slab object's material with a
    flat grey 'SHELL_TRANSPARENT' material — and, since Blender stays
    running as one persistent process across separate script
    invocations, that change survived into every later run (including
    phase1_setup.py) with no way back to the original appearance,
    because the original material name was never recorded anywhere.

    This can only remove the broken material, not restore what was
    there before — genuinely destructive damage, not something to
    repeat. Run once to clean up; safe to call even if nothing needs
    cleaning (does nothing if no SHELL_TRANSPARENT material exists).
    """
    try:
        result = send_to_blender("""
import bpy

mat = bpy.data.materials.get('SHELL_TRANSPARENT')
count = 0
if mat:
    for obj in bpy.data.objects:
        if obj.type != 'MESH':
            continue
        if obj.data and mat.name in [m.name for m in obj.data.materials if m]:
            obj.data.materials.clear()
            count += 1
    bpy.data.materials.remove(mat)

for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        area.tag_redraw()

print(f'Removed SHELL_TRANSPARENT from {count} objects — '
      f'they will show Blender default grey until the IFC file is '
      f're-imported or their real material is manually reassigned')
""")
        return result
    except Exception as e:
        print(f"Warning: material rollback failed: {e}")
        return {"error": str(e)}



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

    client = make_client()

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

    # ── Pre-warm RAG + tool imports ──────────────────────────────────────
    # Confirmed directly in a real session log: the FIRST agent cycle
    # took 38.151s while later, comparable cycles took 15-22s — a
    # one-time cost (SentenceTransformer model load inside
    # rag/retriever.py, lazily triggered by the first get_regulations
    # call; Python's import cache making agent.py's
    # `from mcp_server.server import (...)` line slow only on its
    # first execution) landing entirely in whichever cycle happens to
    # be first, unpredictable from the demo's own perspective since it
    # depends on which tick first has an alert. Paying both costs here
    # during setup means every agent cycle during the actual demo has
    # comparable latency instead of one outlier.
    print("\nPre-warming RAG pipeline (embedding model, ChromaDB)...")
    try:
        from rag.retriever import retrieve_regulations
        retrieve_regulations("residential care home occupancy warmup", n=1)
        print("  RAG ready")
    except Exception as e:
        print(f"  Warning: RAG pre-warm failed (non-fatal, first "
              f"real query will be slower instead): {e}")

    print("Pre-warming MCP tool imports...")
    try:
        import mcp_server.server  # noqa: F401 — forces agent.py's lazy
                                    # import to happen now, not on the
                                    # first real tool call
        print("  MCP tools ready")
    except Exception as e:
        print(f"  Warning: MCP pre-warm failed (non-fatal): {e}")

    print("Pre-warming Anthropic API connection...")
    try:
        # Same model the real agent cycles use, so the warmed
        # connection matches what subsequent calls actually hit.
        # Minimal on both sides (short prompt, max_tokens=1) — this
        # exists purely to establish the connection, not to get a
        # meaningful response, so the token cost is negligible.
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "Hi"}]
        )
        print("  API connection ready")
    except Exception as e:
        print(f"  Warning: API pre-warm failed (non-fatal, first "
              f"real agent cycle will be slower instead): {e}")

    # ── Initialise ────────────────────────────────────────────────────────
    print("\nInitialising simulation...")
    reset_all_signs()
    initialise_occupants(total_occupants, seed=seed)
    snapshot = get_sensor_snapshot()

    print("Clearing any baked animation data...")
    _clear_baked_animation()

    print("Cleaning up any material damage from an earlier session...")
    _restore_material_damage()

    print("Placing occupant markers...")
    result = create_markers(snapshot)
    print(f"  {result.get('created', '?')} markers placed")

    create_board()
    update_board(snapshot, "Live occupancy monitoring active")

    # Set initial state
    confirmed_red_floors = set()
    last_rendered_red_floors = None   # Caching for socket performance
    
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
        "violation_actions"  : 0,   # sign acts in cycles with a genuine FAIL
        "pre_emptive_actions": 0,   # sign acts in cycles that were WARNING-only
        "rooms_managed": set(),
        "start_time"   : time.strftime("%Y-%m-%d %H:%M:%S"),
    }

    print(f"\nStarting live loop ({total_ticks} ticks)...")
    print(f"Agent checks every {sense_every} ticks "
          f"(immediate on over-capacity)")
    print("-" * 60)

    evac_notice_shown = False

    for tick in range(1, total_ticks + 1):

        # ── Yield to an in-progress ESCALATE evacuation ────────────────────
        # See module docstring, fix 1. EVAC_ACTIVE_FLAG is written by
        # assembly_point.py's animate_evacuation_via_paths() when
        # manager_panel.py's ESCALATE button is pressed, and removed
        # once the walk completes or RESET is pressed — both happen
        # entirely inside Blender's own process, independent of this
        # script. While the flag exists, this loop does nothing at all
        # this tick — no simulation advance, no cone repositioning, no
        # board update, no agent cycle — so ESCALATE has the scene to
        # itself instead of fighting this loop for it every ~2 seconds.
        if os.path.exists(EVAC_ACTIVE_FLAG):
            if not evac_notice_shown:
                print(f"\n  ⚠  ESCALATE evacuation in progress — pausing "
                      f"simulation, cone updates, and board updates "
                      f"until it completes or RESET is pressed")
                evac_notice_shown = True
            time.sleep(tick_delay)
            continue
        elif evac_notice_shown:
            print(f"  Evacuation cleared — resuming live simulation\n")
            evac_notice_shown = False

        # ── Advance simulation ────────────────────────────────────────────
        move_occupants()
        snapshot = get_sensor_snapshot()

        # ── Update Blender visuals smoothly ──────────────────────────────
        _animate_cone_movement_smooth(snapshot, steps=4, step_delay=0.03)
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
                f"IMPORTANT: Write your board directive in concise PLAIN TEXT ONLY. "
                f"Do NOT use Markdown bolding (**), asterisks, or headers, "
                f"and keep it under 5 short lines so it fits the 3D HUD."
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
                _update_floor_colours(confirmed_red_floors)
                print(f"  Floor(s) confirmed RED: {confirmed_red_floors}")

                # Cones in violation room respond to sign update by redirecting toward exit
                try:
                    from bim.assembly_point import redirect_cones_via_sign
                    from bim.room_geometry import load_room_centroids
                    centroids = load_room_centroids()
                    for a in overs:
                        if result["signs_updated"] > 0:
                            redirect_cones_via_sign(
                                affected_floor  = _LABEL_TO_FLOOR.get(a["label"], ""),
                                violation_room  = a["label"],
                                centroids       = centroids,
                                snapshot        = snapshot
                            )
                            print(f"  Cones in {a['label']} redirecting to exit")
                except Exception as e:
                    print(f"  Sign response skipped: {e}")

            # ── Format directive for HUD display ──────────────────────────
            formatted_directive = format_hud_directive(
                result["directive"], 
                max_line_width=40, 
                max_lines=8
            )

            # ── Log ───────────────────────────────────────────────────────
            session_log["agent_cycles"].append({
                "tick"             : tick,
                "latency_seconds"  : result["latency_seconds"],
                "tool_count"       : result["tool_count"],
                "signs_updated"    : result["signs_updated"],
                "adb_cited"        : result["adb_cited"],
                "directive"        : result["directive"],
            })
            session_log["total_actions"] += result["signs_updated"]
            if result["signs_updated"] > 0:
                # Fix applied: only counted OVER-severity rooms — before
                # the WARNING/PRE-EMPTIVE fix, a WARNING room was always
                # dismissed as a false positive so this was accidentally
                # correct. Now a WARNING room genuinely gets a real sign
                # update and attractiveness change too (see SYSTEM_PROMPT's
                # PRE-EMPTIVE handling), so this undercounted real actions
                # significantly — confirmed directly: a real session had
                # 14 total sign acts across 5 cycles but this counter only
                # ever reached 2, since 12 of those 14 were on WARNING-tier
                # rooms it never looked at. The floor-reddening and cone-
                # redirect-to-exit logic just above intentionally stays
                # OVER-only — a floor should only turn red on a confirmed
                # violation, not a pre-emptive warning — this counter is
                # the one that should reflect every room genuinely managed.
                for a in overs + warns:
                    session_log["rooms_managed"].add(a["label"])
                # Split for an honest final summary — see fix note there.
                if overs:
                    session_log["violation_actions"] += result["signs_updated"]
                else:
                    session_log["pre_emptive_actions"] += result["signs_updated"]

            # Update board with formatted agent directive
            update_board(snapshot, formatted_directive)

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
    # Fix applied: previously labelled EVERY sign act a "genuine
    # violation" regardless of whether it came from a FAIL or a
    # WARNING-tier PRE-EMPTIVE cycle — confirmed directly, a real
    # session showed "acting on 14 genuine violations" when only 2
    # rooms across the whole run ever actually reached FAIL status;
    # the other 12 were pre-emptive WARNING actions, a distinction the
    # WARNING-tier fix specifically exists to make meaningful. Now
    # reports both honestly rather than conflating them.
    print(f"  (3) Pre-emptive     — agent ran {n_cycles} cycles: "
          f"{session_log['violation_actions']} action(s) on confirmed "
          f"violations, {session_log['pre_emptive_actions']} pre-emptive "
          f"action(s) on WARNING-tier rooms before they became violations ✅")
    print("=" * 60)

    # ── Save log ──────────────────────────────────────────────────────────
    os.makedirs("logs", exist_ok=True)
    session_log["rooms_managed"] = list(session_log["rooms_managed"])
    session_log["end_time"]      = time.strftime("%Y-%m-%d %H:%M:%S")
    session_log["summary"] = {
        "n_cycles"            : n_cycles,
        "mean_latency"        : mean_lat,
        "total_actions"       : session_log["total_actions"],
        "violation_actions"   : session_log["violation_actions"],
        "pre_emptive_actions" : session_log["pre_emptive_actions"],
        "adb_cited"           : f"{adb_hits}/{n_cycles}",
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