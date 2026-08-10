# sensors/agent_walk.py
# Per-agent random walk for baked animation — occupancy management model.
#
# Peter Lawrence's vision:
#   - NO evacuation, NO fire alarm, NO biased walk to exits
#   - Occupants move probabilistically between rooms at every tick
#   - When a room exceeds ADB capacity its attractiveness drops so new
#     occupants are discouraged from entering
#   - Exit nodes are NEVER absorbing — occupants pass through like any
#     other node and keep moving
#   - The meaningful metric is "rooms brought back into compliance"
#     not "occupants evacuated"
#
# Scenario support:
#   - Single violation (TS-01, TS-04)          — violation_room + violation_tick
#   - Multiple violations (TS-03)              — multi_violations list
#   - Exit obstruction (TS-02)                 — blocked_exits list
#   - Baseline no-violation (TS-05)            — violation_tick >= 999
#   - Mobility constraint (TS-04)              — mobility_node label

import random
from sensors.building_graph import BUILDING_GRAPH, EXIT_IDS, get_max_occupancy

G          = BUILDING_GRAPH
ROOM_NODES = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]

OVERCROWDED_ATTRACTIVENESS = 0.05   # strongly discourages new occupants
NORMAL_ATTRACTIVENESS      = 1.0
BLOCKED_EXIT_ATTRACTIVENESS = 0.01  # nearly zero — occupants avoid blocked exit


def _edge_weight(from_node: int, to_node: int,
                 attractiveness_map: dict) -> float:
    base_weight = G.edges[from_node, to_node].get("weight", 1)
    attract     = attractiveness_map.get(to_node, NORMAL_ATTRACTIVENESS)
    return attract / base_weight


def _choose_next(node: int, rng: random.Random,
                 attractiveness_map: dict) -> int:
    neighbours = list(G.neighbors(node))
    candidates = [node] + neighbours

    stay_weight  = attractiveness_map.get(node, NORMAL_ATTRACTIVENESS) * 0.5
    move_weights = [_edge_weight(node, nb, attractiveness_map)
                    for nb in neighbours]
    weights = [stay_weight] + move_weights

    total = sum(weights)
    if total <= 0:
        return node

    norm = [w / total for w in weights]
    return rng.choices(candidates, weights=norm)[0]


def simulate_agent_timeline(total_occupants: int = 80,
                             total_ticks: int = 25,
                             violation_tick: int = 5,
                             violation_room: str = "0-4",
                             seed: int = 42,
                             multi_violations: list = None,
                             blocked_exits: list = None,
                             mobility_node: str = None) -> list:
    """
    Simulates per-agent movement for baked animation.

    Parameters
    ----------
    total_occupants : int
        Number of occupants to simulate.
    total_ticks : int
        Duration of simulation.
    violation_tick : int
        Tick at which the primary violation triggers.
        Pass 999 for baseline (no violation).
    violation_room : str | None
        Graph label of primary violation room.
        Pass None for baseline.
    seed : int
        Random seed for reproducible simulation.
    multi_violations : list of dict | None
        Additional violations beyond the primary one.
        Format: [{"room": "3-14", "tick": 4}, ...]
        Used by TS-03 to model multiple overcrowded rooms.
    blocked_exits : list of str | None
        Graph labels of exit nodes to block.
        Format: ["EXIT-1"]
        Used by TS-02 to model a physically blocked exit.
        Blocked exits get near-zero attractiveness so occupants
        naturally route toward alternative exits.
    mobility_node : str | None
        Graph label of the room containing the mobility-constrained
        occupant (wheelchair user). Used by TS-04.
        The first occupant placed in this room is tracked separately
        so the baker can colour them distinctly.

    Returns
    -------
    list of tick records:
    [{
        "tick"              : int,
        "violation"         : dict | None,
        "agent_nodes"       : [node_id, ...],
        "snapshot"          : dict,
        "mobility_marker_id": int | None,
    }]
    """
    is_baseline = (violation_tick >= 999 or violation_room is None)

    rng         = random.Random(seed)
    agent_nodes = [rng.choice(ROOM_NODES) for _ in range(total_occupants)]
    violation   = None
    timeline    = []

    # Attractiveness map — all rooms start at 1.0
    attractiveness_map = {n: NORMAL_ATTRACTIVENESS for n in G.nodes}

    # ── TS-02: Block specified exits immediately ───────────────────────────
    # Blocked exits get near-zero attractiveness so the random walk
    # naturally routes occupants toward alternative exits.
    blocked_exit_nodes = []
    if blocked_exits:
        for exit_label in blocked_exits:
            exit_node = next((n for n, d in G.nodes(data=True)
                              if d["label"] == exit_label), None)
            if exit_node is not None:
                attractiveness_map[exit_node] = BLOCKED_EXIT_ATTRACTIVENESS
                blocked_exit_nodes.append(exit_node)
                print(f"  EXIT BLOCKED: {exit_label} — "
                      f"attractiveness → {BLOCKED_EXIT_ATTRACTIVENESS}")

    # ── TS-03: Pre-parse multi-violation schedule ─────────────────────────
    multi_sched = {}   # tick → [node_id, ...]
    if multi_violations:
        for mv in multi_violations:
            mv_label = mv.get("room")
            mv_tick  = mv.get("tick", violation_tick)
            mv_node  = next((n for n, d in G.nodes(data=True)
                             if d["label"] == mv_label), None)
            if mv_node is not None:
                multi_sched.setdefault(mv_tick, []).append(mv_node)

    # ── TS-04: Track mobility-constrained occupant ────────────────────────
    # Find the first occupant already in the mobility_node room.
    # If none starts there, assign one explicitly.
    mobility_marker_id = None
    if mobility_node and not is_baseline:
        mob_node = next((n for n, d in G.nodes(data=True)
                         if d["label"] == mobility_node), None)
        if mob_node is not None:
            # Find marker already in that room
            for i, n in enumerate(agent_nodes):
                if n == mob_node:
                    mobility_marker_id = i
                    break
            # If no one starts there, move the first occupant there
            if mobility_marker_id is None:
                agent_nodes[0] = mob_node
                mobility_marker_id = 0
            print(f"  MOBILITY CONSTRAINT: marker {mobility_marker_id} "
                  f"in room {mobility_node}")

    # ── Snapshot builder ──────────────────────────────────────────────────

    def build_snapshot(tick, violation_record):
        occ_count = {}
        for n in agent_nodes:
            occ_count[n] = occ_count.get(n, 0) + 1

        by_floor = {}
        for n, c in occ_count.items():
            fl = G.nodes[n]["floor"]
            by_floor[fl] = by_floor.get(fl, 0) + c

        alerts = []
        for n, c in occ_count.items():
            if G.nodes[n]["node_type"] != "room":
                continue
            mx = get_max_occupancy(n)
            if c > mx:
                alerts.append({
                    "node_id" : n,
                    "label"   : G.nodes[n]["label"],
                    "floor"   : G.nodes[n]["floor"],
                    "current" : c,
                    "max"     : mx,
                    "severity": "OVER",
                })

        # Track primary violation room occupancy
        violation_node = (
            next((n for n, d in G.nodes(data=True)
                  if d["label"] == violation_room), None)
            if violation_room else None
        )
        in_violation_rm = occ_count.get(violation_node, 0) if violation_node else 0

        # Track blocked exit occupancy for TS-02
        near_blocked_exit = 0
        for bn in blocked_exit_nodes:
            near_blocked_exit += occ_count.get(bn, 0)

        return {
            "tick"              : tick,
            "total_occ"         : total_occupants,
            "by_floor"          : by_floor,
            "occupancy"         : {G.nodes[n]["label"]: c
                                   for n, c in occ_count.items() if c > 0},
            "violation"         : violation_record,
            "alerts"            : alerts,
            "in_violation_room" : in_violation_rm,
            "near_blocked_exit" : near_blocked_exit,
            "blocked_exits"     : [G.nodes[bn]["label"]
                                   for bn in blocked_exit_nodes],
        }

    # ── Tick 0 — initial state ────────────────────────────────────────────
    timeline.append({
        "tick"              : 0,
        "violation"         : None,
        "agent_nodes"       : list(agent_nodes),
        "snapshot"          : build_snapshot(0, None),
        "mobility_marker_id": mobility_marker_id,
    })

    # ── Main simulation loop ──────────────────────────────────────────────
    for tick in range(1, total_ticks + 1):

        # ── Primary violation trigger ──────────────────────────────────
        if not is_baseline and tick == violation_tick:
            vnode = next((n for n, d in G.nodes(data=True)
                          if d["label"] == violation_room), None)
            if vnode is not None:
                violation = {
                    "type"      : "overcrowding",
                    "node_id"   : vnode,
                    "node_label": violation_room,
                    "floor"     : G.nodes[vnode]["floor"],
                    "adb_action": "sign_redirect",
                }
                attractiveness_map[vnode] = OVERCROWDED_ATTRACTIVENESS
                print(f"  [Tick {tick}] Violation triggered: "
                      f"{violation_room} — attractiveness → "
                      f"{OVERCROWDED_ATTRACTIVENESS}")

        # ── Multi-violation triggers (TS-03) ───────────────────────────
        if tick in multi_sched:
            for mv_node in multi_sched[tick]:
                mv_label = G.nodes[mv_node]["label"]
                attractiveness_map[mv_node] = OVERCROWDED_ATTRACTIVENESS
                print(f"  [Tick {tick}] Additional violation: "
                      f"{mv_label} — attractiveness → "
                      f"{OVERCROWDED_ATTRACTIVENESS}")

        # ── Move all occupants ─────────────────────────────────────────
        new_nodes = []
        for i, node in enumerate(agent_nodes):
            # Mobility-constrained occupant (TS-04): moves toward corridor
            # not stairwell — simulated by reducing stairwell attractiveness
            if i == mobility_marker_id and not is_baseline:
                stair_nodes = [n for n, d in G.nodes(data=True)
                               if d["node_type"] == "stair"]
                local_attract = dict(attractiveness_map)
                for sn in stair_nodes:
                    local_attract[sn] = BLOCKED_EXIT_ATTRACTIVENESS
                next_node = _choose_next(node, rng, local_attract)
            else:
                next_node = _choose_next(node, rng, attractiveness_map)
            new_nodes.append(next_node)

        agent_nodes = new_nodes

        timeline.append({
            "tick"              : tick,
            "violation"         : violation,
            "agent_nodes"       : list(agent_nodes),
            "snapshot"          : build_snapshot(tick, violation),
            "mobility_marker_id": mobility_marker_id,
        })

    return timeline


# ── Self-test ─────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("  AGENT WALK — Scenario Tests")
    print("=" * 60)

    # TS-05: Baseline
    print("\nTS-05: Baseline (violation_tick=999)")
    tl = simulate_agent_timeline(
        total_occupants=40, total_ticks=15,
        violation_tick=999, violation_room=None, seed=5)
    for r in tl:
        snap = r["snapshot"]
        print(f"  Tick {snap['tick']:2d} | alerts={len(snap['alerts']):2d} | "
              f"violation={'YES' if r['violation'] else 'no'}")

    # TS-01: Single violation
    print("\nTS-01: Single violation room 0-4 at tick 4")
    tl = simulate_agent_timeline(
        total_occupants=80, total_ticks=20,
        violation_tick=4, violation_room="0-4", seed=1)
    vnode = next(n for n, d in G.nodes(data=True) if d["label"] == "0-4")
    mx    = get_max_occupancy(vnode)
    for r in tl:
        snap = r["snapshot"]
        cnt  = snap["in_violation_room"]
        print(f"  Tick {snap['tick']:2d} | "
              f"in 0-4={cnt:2d}/{mx} "
              f"{'OVER' if cnt > mx else 'OK  '} | "
              f"alerts={len(snap['alerts']):2d}")

    # TS-02: Exit obstruction
    print("\nTS-02: North exit blocked, violation room 0-9 at tick 5")
    tl = simulate_agent_timeline(
        total_occupants=80, total_ticks=20,
        violation_tick=5, violation_room="0-9", seed=2,
        blocked_exits=["EXIT-1"])
    for r in tl:
        snap = r["snapshot"]
        print(f"  Tick {snap['tick']:2d} | "
              f"near_blocked={snap['near_blocked_exit']} | "
              f"alerts={len(snap['alerts']):2d}")

    # TS-03: Multi-room violation
    print("\nTS-03: Multi-room — 3-1 at tick 4, 3-14 at tick 4")
    tl = simulate_agent_timeline(
        total_occupants=80, total_ticks=25,
        violation_tick=4, violation_room="3-1", seed=3,
        multi_violations=[{"room": "3-14", "tick": 4}])
    for r in tl:
        snap = r["snapshot"]
        over = [a["label"] for a in snap["alerts"] if a["severity"] == "OVER"]
        print(f"  Tick {snap['tick']:2d} | over={over}")

    # TS-04: Mobility constraint
    print("\nTS-04: Wheelchair user in 3-10, violation at tick 2")
    tl = simulate_agent_timeline(
        total_occupants=80, total_ticks=25,
        violation_tick=2, violation_room="3-10", seed=7,
        mobility_node="3-10")
    for r in tl:
        snap = r["snapshot"]
        mid  = r["mobility_marker_id"]
        if mid is not None and tick < 5:
            node = r["agent_nodes"][mid]
            loc  = G.nodes[node]["label"]
            print(f"  Tick {snap['tick']:2d} | "
                  f"WC marker in {loc} | "
                  f"alerts={len(snap['alerts'])}")