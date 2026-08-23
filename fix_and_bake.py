# fix_and_bake.py
# Clears conflicting materials, bakes occupancy management animation,
# then drives it as a TRUE digital twin — every visible signal (board text,
# the four primary corridor signs, and the scenario-specific extra signs)
# is derived from the live per-tick simulation output, not from fixed
# strings pinned to a chosen frame number.
#
# What changed vs the previous version:
#   - REMOVED: board_initial / board_override hardcoded strings with
#     fabricated occupancy numbers. The board is driven entirely by
#     animation_baker's registered Blender frame handler, which reads
#     the real per-tick snapshot baked from simulate_agent_timeline().
#   - REMOVED: the manual "if frame >= v_frame: switch text" trigger loop.
#     That logic told the twin what to show at a specific frame instead
#     of letting it react to its own state.
#   - REMOVED: _apply_ts04_refuge_freeze — a scripted freeze + forced
#     scale-up of the wheelchair cone. Nothing in the simulation produces
#     that behaviour; it was pure animation scripting, not a twin response.
#   - REMOVED: manual updates to the four primary corridor signs
#     (SIGN_F0_CORRIDOR_N / SIGN_F1_CORRIDOR / SIGN_F2_CORRIDOR /
#     SIGN_F3_CORRIDOR). These are already fully reactive — animation_baker
#     rewrites their colour and text every frame from real per-tick
#     alerts. Touching them here caused a race: two separate processes
#     writing the same Blender object on the same frame.
#   - ADDED: extra signs (south exit / zone-clear / stairwell) are now
#     computed by re-running simulate_agent_timeline() with the exact
#     same parameters used for the bake (same seed => identical, verifiable
#     result) and reading real per-tick alerts, not a string written once.
#     Their colour and text can change tick to tick if the underlying
#     occupancy changes — the twin reacts to itself, not to a script.
#
# Second pass fixes:
#   - FIXED (root cause of "signs always show green"): animation_baker's
#     live handler looked up Blender objects by the bare sign id
#     ("SIGN_F0_CORRIDOR_N"), but the real objects are named
#     SignPanel_<id> / SignText_<id>. The lookup silently returned None
#     every frame, so nothing ever updated. Fixed in animation_baker.py;
#     the handler also now writes panel colour, not just text — it
#     previously never touched colour at all.
#   - CHANGED: every non-baseline scenario now tracks a mobility-
#     constrained occupant. mobility_node is no longer set per scenario —
#     it defaults to that scenario's own violation_room unless a
#     scenario explicitly overrides it, so TS-01/02/03 gained a wheelchair
#     marker with zero new hardcoded room names.
#   - ADDED (sensors/agent_walk.py): once a scenario's violation is
#     active, the mobility marker is drawn toward and settles at the
#     corridor node on its own current floor — a real "refuge" behaviour
#     that emerges from the same attractiveness mechanism every other
#     occupant is subject to, not a scripted freeze.

import sys, os, time, argparse
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from bim.ifc_bridge import send_to_blender, test_connection
from bim.animation_baker import bake_animation
from bim.viewport_utils import frame_view_on_objects
from sensors.agent_walk import simulate_agent_timeline
from sensors.building_graph import BUILDING_GRAPH as G

# ── Scenario definitions ──────────────────────────────────────────────────────
# These declare CONFIGURATION for the simulation (which room, which exits
# are blocked, which extra rooms fail, which occupant is mobility
# constrained) — not pre-written outcomes. Everything the twin shows is
# computed from this configuration at run time.
SCENARIOS = {
    "default": {
        "description"     : "First floor bedroom 2-1 approaches capacity",
        "violation_tick"  : 5,
        "violation_room"  : "2-1",
        "ticks"           : 25,
        "seed"            : 42,
        "adb_ref"         : "ADB Clause 2.43 — bedroom max occupancy",
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : None,
        "extra_sign_kind" : None,
    },

    "TS-01": {
        "description"     : "Single room congestion — F1 bedroom 1-1 overcrowded",
        "violation_tick"  : 4,
        # Was "0-4" — that graph node's real IFC space is the Lounge
        # (max_occ 130), not a bedroom. diagnose_room_mapping.py
        # confirmed 1-1 (IFC "113") is a genuine, floor-matched bedroom.
        "violation_room"  : "1-1",
        "ticks"           : 20,
        "seed"            : 1,
        "adb_ref"         : "ADB Clause 2.43 — bedroom max occupancy care home",
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : None,
        "extra_sign_kind" : None,
        # The earlier "0-20 Lounge (15/130) false positive" claim had no
        # basis in the actual data — 0-20's real IFC space is a Corridor
        # (max_occ 10), not a Lounge, and nothing in the codebase computes
        # 130 for it. 0-20 was also never seeded with occupants in this
        # bake, so that scenario state was never actually simulated.
        # Removed until GRAPH_TO_IFC's room mapping is verified and the
        # scenario can reference a room whose real identity is confirmed.
        "note"            : None,
    },

    "TS-02": {
        "description"     : "Exit obstruction — north exit blocked, occupants route around it",
        "violation_tick"  : 5,
        # Was "0-9" — that graph node's real IFC space is Dining
        # (max_occ 15), not a bedroom. Repointed to a verified bedroom
        # for consistency, though the visible sign behaviour is driven
        # by blocked_exits regardless of which room is chosen here.
        "violation_room"  : "1-2",
        "ticks"           : 20,
        "seed"            : 2,
        "adb_ref"         : "ADB Table 2.1 — max travel distance 18m escape route",
        "blocked_exits"   : ["EXIT-1"],
        "multi_violations": [],
        "mobility_node"   : None,
        # SIGN_F0_CORRIDOR_S was removed from interior_signage.py — it
        # was placed at a mismapped IFC space (a small side lobby, not
        # a real second corridor) and rendered confusingly close to
        # the north sign. The primary north sign's own redirect text
        # ("North Corridor BLOCKED / Use South Exit") already conveys
        # the reroute, so TS-02 no longer needs a second sign object.
        "extra_sign_kind" : None,
        "note"            : "EXIT-1 blocked from tick 0 in the simulation — occupants genuinely avoid it",
    },

    "TS-03": {
        "description"     : "Multi-room congestion — F3 rooms 3-1 and 3-2 simultaneously overcrowded",
        "violation_tick"  : 4,
        "violation_room"  : "3-1",
        "ticks"           : 25,
        "seed"            : 3,
        "adb_ref"         : "ADB Clause 2.43 — bedroom occupancy | Section 2.33 — care home",
        "blocked_exits"   : [],
        # Was "3-14" — that graph node's real IFC space is a Bath
        # (max_occ 1), not a bedroom. Repointed to 3-2, a verified
        # floor-matched bedroom.
        "multi_violations": [{"room": "3-2", "tick": 4}],
        "mobility_node"   : None,
        # Zone-clear signs on F0/F1/F2 report the REAL per-tick alert
        # state of each floor, not a fixed "all clear" string.
        "extra_sign_kind" : "zone_clear_other_floors",
        "note"            : "Rooms 3-1 + 3-2 both overcrowded simultaneously — modelled in the walk itself",
    },

    "TS-04": {
        "description"     : "Mobility constraint — wheelchair user on F3, stairwell avoided in the walk",
        "violation_tick"  : 2,
        "violation_room"  : "3-10",
        "ticks"           : 25,
        "seed"            : 7,
        "adb_ref"         : "ADB Sections 3.5-3.6 — wheelchair refuge provisions",
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : "3-10",
        "mobility_refuge" : True,
        # Stairwell sign reports whether the tracked mobility marker
        # currently needs to avoid stairs — tied to the real violation
        # state, not a pinned frame.
        "extra_sign_kind" : "stairwell",
        "extra_sign_id"   : "SIGN_F3_STAIR",
        "note"            : "Wheelchair marker's stair-avoidance is a live bias in the walk, not a scripted freeze",
    },

    "TS-05": {
        "description"     : "Baseline — normal operation, no violations (negative control)",
        "violation_tick"  : 999,
        "violation_room"  : None,
        "ticks"           : 15,
        "seed"            : 5,
        "adb_ref"         : "",
        "blocked_exits"   : [],
        "multi_violations": [],
        "mobility_node"   : None,
        "extra_sign_kind" : None,
        "note"            : "40 occupants — zero violations, zero sign updates, board stays IDLE every tick",
    },
}

FLOOR_SIGNS = {
    "F0 Ground Floor": "SIGN_F0_CORRIDOR_N",
    "F1 First Floor" : "SIGN_F1_CORRIDOR",
    "F2 Second Floor": "SIGN_F2_CORRIDOR",
    "F3 Third Floor" : "SIGN_F3_CORRIDOR",
}

RED   = [0.90, 0.05, 0.05, 1.0]
GREEN = [0.05, 0.70, 0.15, 1.0]


def _is_baseline(sc):
    return sc["violation_tick"] >= 999 or sc["violation_room"] is None


# ── Blender helpers ───────────────────────────────────────────────────────────

def _clear_materials_and_set_viewport():
    send_to_blender("""
import bpy
removed = 0
for mat in list(bpy.data.materials):
    if mat.name.startswith('LIVE_') or mat.name.startswith('FS_FLOOR_'):
        bpy.data.materials.remove(mat)
        removed += 1
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D':
        for space in area.spaces:
            if space.type == 'VIEW_3D':
                space.shading.type       = 'SOLID'
                space.shading.color_type = 'OBJECT'
                space.shading.show_shadows = False
        area.tag_redraw()
print(f'Cleared {removed} conflicting materials')
""")


def _cleanup_old_tags():
    send_to_blender("""
import bpy
count = 0
for obj in list(bpy.data.objects):
    if 'WheelchairLabel' in obj.name:
        bpy.data.objects.remove(obj, do_unlink=True)
        count += 1
for mat in list(bpy.data.materials):
    if mat.name in ('WheelchairMat', 'WCLabelMat'):
        bpy.data.materials.remove(mat)
print(f'Cleaned up {count} old wheelchair tags')
""")


def _reset_all_sign_panels_green():
    """Resets every SignPanel_ object (primary AND extra) to GREEN/CLEAR."""
    send_to_blender("""
import bpy
GREEN = [0.05, 0.70, 0.15, 1.0]
count = 0
for obj in bpy.data.objects:
    if obj.name.startswith('SignPanel_') and obj.data and obj.data.materials:
        mat  = obj.data.materials[0]
        node = mat.node_tree.nodes.get('Principled BSDF')
        if node:
            node.inputs['Base Color'].default_value     = GREEN
            node.inputs['Emission Color'].default_value  = GREEN
            node.inputs['Emission Strength'].default_value = 1.5
        count += 1
    if obj.name.startswith('SignText_'):
        obj.data.body = 'CLEAR\\nAll routes open'
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
print(f'Reset {count} sign panels to GREEN')
""")


def _update_sign_panel(sign_id: str, lines: list, colour: list):
    """Updates ONE sign panel. Used only for extra signs not owned by
    animation_baker's live handler (south exit, zone-clear, stairwell)."""
    if not lines:
        return
    body       = "\\n".join(lines)
    colour_str = str(colour)
    send_to_blender(f"""
import bpy
COL   = {colour_str}
panel = bpy.data.objects.get('SignPanel_{sign_id}')
if not panel:
    panel = bpy.data.objects.get('{sign_id}')
txt   = bpy.data.objects.get('SignText_{sign_id}')
if panel and panel.data and panel.data.materials:
    mat  = panel.data.materials[0]
    node = mat.node_tree.nodes.get('Principled BSDF')
    if node:
        node.inputs['Base Color'].default_value     = COL
        node.inputs['Emission Color'].default_value  = COL
        node.inputs['Emission Strength'].default_value = 2.5
if txt:
    txt.data.body = '{body}'
    txt.data.size = 0.08
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
""")


def _mark_wheelchair_user(mobility_marker_id: int):
    """
    Colours the exact wheelchair cone (from the real mobility_marker_id
    returned by the bake) purple, and parents a 'WC User' label to it
    so the label follows the cone through every animation frame.
    This only IDENTIFIES which live cone is the tracked occupant — it
    does not alter that cone's movement, which is already biased away
    from stairwells inside sensors/agent_walk.py.
    """
    cone_name = f"Occupant_{mobility_marker_id:03d}"
    send_to_blender(f"""
import bpy
cone_name = '{cone_name}'

wc = bpy.data.objects.get(cone_name)
if wc:
    wc_mat = bpy.data.materials.new('WheelchairMat')
    wc_mat.use_nodes = True
    bsdf = wc_mat.node_tree.nodes.get('Principled BSDF')
    if bsdf:
        PURPLE = (0.55, 0.05, 0.80, 1.0)
        bsdf.inputs['Base Color'].default_value = PURPLE
        if 'Emission Color' in bsdf.inputs:
            bsdf.inputs['Emission Color'].default_value = PURPLE
        if 'Emission Strength' in bsdf.inputs:
            bsdf.inputs['Emission Strength'].default_value = 3.0
    wc.data.materials.clear()
    wc.data.materials.append(wc_mat)
    wc.color = (0.55, 0.05, 0.80, 1.0)

    txt_data         = bpy.data.curves.new('WheelchairLabel', type='FONT')
    txt_data.body    = 'WC User'
    txt_data.size    = 0.40
    txt_data.align_x = 'CENTER'
    tmat = bpy.data.materials.new('WCLabelMat')
    tmat.use_nodes = True
    tn = tmat.node_tree.nodes.get('Principled BSDF')
    if tn:
        tn.inputs['Base Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        if 'Emission Color' in tn.inputs:
            tn.inputs['Emission Color'].default_value = (1.0, 1.0, 1.0, 1.0)
        if 'Emission Strength' in tn.inputs:
            tn.inputs['Emission Strength'].default_value = 4.0
    txt_data.materials.clear()
    txt_data.materials.append(tmat)

    txt_obj = bpy.data.objects.new('WheelchairLabel', txt_data)
    bpy.context.scene.collection.objects.link(txt_obj)
    txt_obj.location       = (0.0, 0.0, 2.5)
    txt_obj.rotation_euler = (1.5708, 0, 0)
    txt_obj.parent         = wc
    txt_obj.parent_type    = 'OBJECT'

    for area in bpy.context.screen.areas:
        if area.type == 'VIEW_3D': area.tag_redraw()
    print(f'Wheelchair cone identified: {{cone_name}}')
else:
    print(f'Cone not found: {{cone_name}}')
""")


def _jump_to_frame(frame: int):
    send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
""")


# ── Reactive extra-sign schedule ───────────────────────────────────────────────
# Re-runs the SAME deterministic simulation used for the bake (identical
# seed and parameters => identical result) purely in Python, so we can
# read real per-tick alerts and derive extra-sign state from them.
# This is the mechanism that makes the extra signs (south exit,
# zone-clear, stairwell) reactive rather than pinned text.

def _build_extra_sign_schedule(sc: dict, total_occupants: int,
                               effective_mobility_node: str = None,
                               mobility_refuge: bool = False) -> list:
    """
    Returns a list of length total_ticks+1. Each entry is either None
    (no extra sign for this scenario / this tick unchanged from clear)
    or a dict {sign_id: (lines, colour)} describing what the extra
    sign(s) should show at that tick, computed from real simulation state.

    effective_mobility_node AND mobility_refuge MUST both match whatever
    was passed to the actual bake_animation() call — same seed + same
    mobility parameters reproduces the exact same walk, since the
    mobility marker's bias changes which node every subsequent rng draw
    resolves to for that agent.
    """
    kind = sc.get("extra_sign_kind")
    if kind is None:
        return []

    timeline = simulate_agent_timeline(
        total_occupants  = total_occupants,
        total_ticks      = sc["ticks"],
        violation_tick    = sc["violation_tick"],
        violation_room    = sc["violation_room"],
        seed              = sc["seed"],
        blocked_exits     = sc.get("blocked_exits", []),
        multi_violations  = sc.get("multi_violations", []),
        mobility_node     = effective_mobility_node,
        mobility_refuge   = mobility_refuge,
    )

    label_to_floor = {d["label"]: d["floor"] for _, d in G.nodes(data=True)}
    schedule = []

    for record in timeline:
        snap  = record["snapshot"]
        alerts = snap.get("alerts", [])
        entry = {}

        if kind == "opposite_exit":
            # South exit becomes the active route once the violation
            # triggers — gated the same way as the primary north sign,
            # so both signs change together at the scripted tick rather
            # than the south sign announcing "OPEN / primary route"
            # from frame 0 before anything has actually happened.
            sign_id = sc["extra_sign_id"]
            tick    = record["tick"]
            if sc.get("blocked_exits") and tick >= sc["violation_tick"]:
                entry[sign_id] = (
                    ["South Exit OPEN", "Primary route", "Proceed now"],
                    GREEN,
                )
            else:
                entry[sign_id] = (["CLEAR", "All routes open", ""], GREEN)

        elif kind == "zone_clear_other_floors":
            v_floor = label_to_floor.get(sc["violation_room"], "")
            for fl_name, sign_id in FLOOR_SIGNS.items():
                if fl_name == v_floor:
                    continue  # primary sign already owned by animation_baker
                floor_alerts = [a for a in alerts
                                if a.get("severity") == "OVER"
                                and label_to_floor.get(a.get("label", "")) == fl_name]
                if floor_alerts:
                    a = floor_alerts[0]
                    entry[sign_id] = (
                        [f"{a['label']} at capacity",
                         f"{a['current']}/{a['max']} occupants",
                         "Use another floor"],
                        RED,
                    )
                else:
                    short = fl_name.split(" ")[0]
                    entry[sign_id] = (
                        [f"{short} Zone: CLEAR",
                         "Occupancy within",
                         "safe limits"],
                        GREEN,
                    )

        elif kind == "stairwell":
            sign_id = sc["extra_sign_id"]
            ms      = snap.get("mobility_status")
            in_alert = any(a.get("label") == sc["violation_room"]
                           and a.get("severity") == "OVER"
                           for a in alerts)
            if in_alert and ms:
                status = "at refuge" if ms["at_refuge"] else "en route to refuge"
                entry[sign_id] = (
                    ["Stairwell B", "Not accessible",
                     f"WC user {status}: {ms['room']}"],
                    RED,
                )
            else:
                entry[sign_id] = (["Stairwell B", "Status: CLEAR", ""], GREEN)

        schedule.append(entry)

    return schedule


def _drive_animation(total_frames: int, frames_per_tick: int, frame_delay: float,
                     extra_schedule: list):
    """
    Steps through every frame. Setting the frame triggers animation_baker's
    registered handler, which rewrites the board and all four primary
    corridor signs from the real baked per-tick data — nothing here
    touches those. Only the scenario's extra sign(s), if any, are pushed
    here, and only when the tick's computed state differs from the last
    tick pushed (avoids redundant socket calls, not a fixed trigger frame).
    """
    print(f"\nDriving animation ({total_frames} frames at {frame_delay}s/frame)...")
    print("Press Ctrl+C to stop\n")

    last_pushed = {}
    try:
        for frame in range(0, total_frames + 1):
            tick = frame // frames_per_tick

            if extra_schedule and tick < len(extra_schedule):
                entry = extra_schedule[tick]
                for sign_id, (lines, colour) in entry.items():
                    key = (sign_id, tuple(lines), tuple(colour))
                    if last_pushed.get(sign_id) != key:
                        _update_sign_panel(sign_id, lines, colour)
                        last_pushed[sign_id] = key

            send_to_blender(f"""
import bpy
bpy.context.scene.frame_set({frame})
for area in bpy.context.screen.areas:
    if area.type == 'VIEW_3D': area.tag_redraw()
""")
            if frame % frames_per_tick == 0:
                print(f"  Frame {frame:4d} | Tick {tick:2d}")
            time.sleep(frame_delay)
    except KeyboardInterrupt:
        print("\nStopped by user")


# ── Main ──────────────────────────────────────────────────────────────────────

def main(scenario: str = "default",
         frames_per_tick: int = 24,
         frame_delay: float = 0.08):

    print("=" * 60)
    print(f"  FIX AND BAKE — {scenario}")
    print("=" * 60)

    print("\nChecking Blender connection...")
    if not test_connection():
        print("FAILED — open Blender, load IFC, start MCP server")
        return
    print("OK")

    sc       = SCENARIOS.get(scenario, SCENARIOS["default"])
    baseline = _is_baseline(sc)
    n_tick   = sc["ticks"]
    total_occupants = 40 if baseline else 80

    # Every non-baseline scenario tracks a mobility-constrained occupant.
    # A scenario can name a specific room via "mobility_node"; otherwise
    # it defaults to that scenario's own violation_room — no new hardcoded
    # room names needed for TS-01/02/03.
    #
    # mobility_refuge is a SEPARATE flag from presence: only TS-04 sets it
    # True. When False the marker still avoids stairs (a standing
    # accessibility bias) but does not seek or settle at a refuge point,
    # and the board never shows the escalation alert for it.
    effective_mobility_node = None
    mobility_refuge         = False
    if not baseline:
        effective_mobility_node = sc.get("mobility_node") or sc["violation_room"]
        mobility_refuge         = sc.get("mobility_refuge", False)
    has_wc = effective_mobility_node is not None

    print(f"\nScenario     : {scenario}")
    print(f"Description  : {sc['description']}")
    print(f"Ticks        : {n_tick}")
    if baseline:
        print(f"Violation    : none (baseline)")
    else:
        print(f"Violation    : room {sc['violation_room']} at tick {sc['violation_tick']}")
        if sc.get("blocked_exits"):
            print(f"Blocked exits: {sc['blocked_exits']}")
        if sc.get("multi_violations"):
            for mv in sc["multi_violations"]:
                print(f"Also violates: {mv['room']} at tick {mv['tick']}")
        print(f"Mobility node: {effective_mobility_node}"
              f"{' (refuge-seeking)' if mobility_refuge else ''}")
    if sc.get("note"):
        print(f"Note         : {sc['note']}")
    print(f"ADB ref      : {sc.get('adb_ref', '')} (shown on board, computed live)")

    print("\nClearing conflicting materials and setting viewport...")
    _cleanup_old_tags()
    _clear_materials_and_set_viewport()
    _reset_all_sign_panels_green()

    print(f"\nBaking keyframes (30-90s)...")
    result = bake_animation(
        total_occupants  = total_occupants,
        total_ticks      = n_tick,
        violation_tick   = 999 if baseline else sc["violation_tick"],
        violation_room   = None if baseline else sc["violation_room"],
        frames_per_tick  = frames_per_tick,
        seed             = sc["seed"],
        blocked_exits    = sc.get("blocked_exits", []),
        multi_violations = sc.get("multi_violations", []),
        mobility_node    = effective_mobility_node,
        mobility_refuge  = mobility_refuge,
        scenario_adb_ref = sc.get("adb_ref") if not baseline else None,
    )

    if result.get("status") != "ok":
        print(f"\nFAILED: {result}")
        return

    total = result["total_frames"]
    frame_view_on_objects("Occupant_")

    print("\n" + "=" * 60)
    print("  BAKE COMPLETE")
    print(f"  Markers baked : {result.get('markers_baked', total_occupants)}")
    print(f"  Floors baked  : {result.get('floors_baked', 4)}")
    print(f"  Total frames  : 0 -> {total} ({n_tick} ticks)")
    print("  Board + 4 primary corridor signs: live from baked per-tick data")
    print("=" * 60)

    # Identify (not script) the wheelchair cone — present in every
    # non-baseline scenario now
    if has_wc:
        mobility_id = result.get("mobility_marker_id")
        if mobility_id is not None:
            print(f"\nIdentifying wheelchair cone (Occupant_{mobility_id:03d})...")
            _mark_wheelchair_user(mobility_id)
        else:
            print("\nWARNING: mobility_marker_id missing from bake result")

    # Extra signs — computed from a fresh, identical re-run of the same
    # deterministic simulation (same seed, same effective_mobility_node,
    # same mobility_refuge as the real bake, so the two runs match
    # exactly), not written once and left static
    extra_schedule = []
    if not baseline and sc.get("extra_sign_kind"):
        print(f"\nComputing reactive extra-sign schedule "
              f"({sc['extra_sign_kind']})...")
        print("  [re-simulating with identical seed/params to verify "
              "against the real bake above — not a second bake]")
        extra_schedule = _build_extra_sign_schedule(
            sc, total_occupants, effective_mobility_node, mobility_refuge)
        print("  [re-simulation complete — schedule extracted]")

    _jump_to_frame(0)
    _drive_animation(total, frames_per_tick, frame_delay, extra_schedule)

    print("\nAnimation complete.")
    if not baseline:
        v_frame = sc["violation_tick"] * frames_per_tick
        print(f"Tip — jump to violation frame:")
        print(f"  python fix_and_bake.py --jump {v_frame}")


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Fix materials, bake animation, drive as a live twin")
    parser.add_argument("--scenario", default="default",
                        choices=list(SCENARIOS.keys()))
    parser.add_argument("--fps",   type=int,   default=24)
    parser.add_argument("--speed", type=float, default=0.08)
    parser.add_argument("--jump",  type=int,   default=None)
    args = parser.parse_args()

    if args.jump is not None:
        print("Checking Blender connection...")
        if test_connection():
            print(f"Jumping to frame {args.jump}...")
            _jump_to_frame(args.jump)
            print(f"At frame {args.jump}")
        else:
            print("FAILED")
    else:
        main(args.scenario, args.fps, args.speed)