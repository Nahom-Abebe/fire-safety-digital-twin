# sensors/agent_walk.py
# Per-agent random walk for baked animation — occupancy management model.
#
# Scenario support:
#   - Single violation (TS-01, TS-04)          — violation_room + violation_tick
#   - Multiple violations (TS-03)              — multi_violations list
#   - Exit obstruction (TS-02)                 — blocked_exits list
#   - Baseline no-violation (TS-05)            — violation_tick >= 999
#   - Mobility-tracked occupant (all non-baseline scenarios) — mobility_node
#
# Mobility marker behaviour has two independent layers:
#   1. Standing accessibility bias (always on for the tracked marker,
#      every non-baseline scenario): stairs are avoided at all times.
#   2. Refuge-seeking (only when mobility_refuge=True, i.e. TS-04): once
#      the scenario's violation is active, the corridor node on the
#      marker's current floor gets a large attractiveness boost — the
#      same mechanism that redirects every other occupant away from an
#      overcrowded room. Once the marker's position actually equals that
#      refuge node, it settles there permanently for the rest of the
#      simulation (a real decision to wait for staff rather than keep
#      wandering) rather than continuing to be probabilistically nudged
#      tick after tick, which previously let it drift away again.
#
# Fix applied — build_snapshot()'s alerts list previously checked EVERY
# room-type node against get_max_occupancy() with no exclusions at all,
# including Bath/W-C/Store rooms floored at max_occ=1. With 80 occupants
# wandering ~80 rooms over 20-25 ticks, an incidental second visitor to
# a 1-person Bath room is common by chance, not a genuine occupancy
# problem — this was flagging many such rooms every run and turning
# multiple floors red simultaneously, unrelated to the scenario's actual
# scripted violation. Now uses is_occupancy_alert_relevant() (already
# built and proven in sensors/building_graph.py for exactly this
# purpose in sensor_sim.py) to scope alerts to genuine bedrooms and
# communal rooms only. Safe with respect to every scripted scenario:
# the violation_room/multi_violations mechanism below is completely
# independent of this alerts list — it only ever sets attractiveness_map
# directly on a tick match — and every scripted violation_room in every
# current scenario (1-1, 3-1, 3-2, 3-10) is a genuine bedroom, so none
# of them are affected by this exclusion.

import random
from sensors.building_graph import (
    BUILDING_GRAPH, EXIT_IDS, get_max_occupancy, is_occupancy_alert_relevant
)

G          = BUILDING_GRAPH
ROOM_NODES = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]

OVERCROWDED_ATTRACTIVENESS  = 0.05   # discourages new occupants from a full room
NORMAL_ATTRACTIVENESS       = 1.0
BLOCKED_EXIT_ATTRACTIVENESS = 0.01   # occupants avoid a blocked exit / stairs
REFUGE_ATTRACTIVENESS       = 6.0    # draws the mobility marker to its floor's corridor


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


def _floor_corridor_nodes() -> dict:
    """Maps floor name -> its corridor node id. Used as the refuge target
    for the mobility-constrained occupant. Built from the graph itself —
    no hardcoded node ids or coordinates."""
    mapping = {}
    for n, d in G.nodes(data=True):
        if d.get("node_type") == "corridor" and d["floor"] not in mapping:
            mapping[d["floor"]] = n
    return mapping


def simulate_agent_timeline(total_occupants: int = 80,
                             total_ticks: int = 25,
                             violation_tick: int = 5,
                             violation_room: str = "0-4",
                             seed: int = 42,
                             multi_violations: list = None,
                             blocked_exits: list = None,
                             mobility_node: str = None,
                             mobility_refuge: bool = False) -> list:
    """
    Simulates per-agent movement for baked animation.

    Parameters
    ----------
    mobility_node : str | None
        Graph label of the room containing the mobility-constrained
        occupant. Pass None only for a baseline run with no violation.
    mobility_refuge : bool
        When True, the marker seeks its floor's corridor node once the
        violation is active and, on arrival, stays there for the rest
        of the run. When False (default), the marker only carries the
        standing stair-avoidance bias — no seeking, no settling.

    Returns
    -------
    list of tick records:
    [{
        "tick"              : int,
        "violation"         : dict | None,
        "agent_nodes"       : [node_id, ...],
        "snapshot"          : dict,   # includes "mobility_status" when
                                       # a mobility marker is tracked
        "mobility_marker_id": int | None,
    }]
    """
    is_baseline = (violation_tick >= 999 or violation_room is None)

    rng         = random.Random(seed)
    agent_nodes = [rng.choice(ROOM_NODES) for _ in range(total_occupants)]
    violation   = None
    timeline    = []

    attractiveness_map = {n: NORMAL_ATTRACTIVENESS for n in G.nodes}
    floor_corridor      = _floor_corridor_nodes()

    # ── Block specified exits immediately (TS-02) ──────────────────────────
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

    # ── Pre-parse multi-violation schedule (TS-03) ─────────────────────────
    multi_sched = {}
    if multi_violations:
        for mv in multi_violations:
            mv_label = mv.get("room")
            mv_tick  = mv.get("tick", violation_tick)
            mv_node  = next((n for n, d in G.nodes(data=True)
                             if d["label"] == mv_label), None)
            if mv_node is not None:
                multi_sched.setdefault(mv_tick, []).append(mv_node)

    # ── Track mobility-constrained occupant ────────────────────────────────
    mobility_marker_id = None
    if mobility_node and not is_baseline:
        mob_node = next((n for n, d in G.nodes(data=True)
                         if d["label"] == mobility_node), None)
        if mob_node is not None:
            for i, n in enumerate(agent_nodes):
                if n == mob_node:
                    mobility_marker_id = i
                    break
            if mobility_marker_id is None:
                agent_nodes[0] = mob_node
                mobility_marker_id = 0
            print(f"  MOBILITY CONSTRAINT: marker {mobility_marker_id} "
                  f"starting in room {mobility_node}"
                  f"{' (refuge-seeking enabled)' if mobility_refuge else ''}")

    # Sticky settle state — once True, the marker no longer moves for the
    # remainder of the simulation. Only ever set when mobility_refuge=True.
    mobility_settled = False

    # ── Snapshot builder ────────────────────────────────────────────────────

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
            # Scoped to genuine bedrooms/communal rooms only — see
            # module docstring fix note. Excludes non-occupiable rooms
            # (Store/W-C/Storage) and single-occupant personal-care
            # rooms (Bath/Women) from ever generating an alert here,
            # same scoping already proven in sensor_sim.py.
            if not is_occupancy_alert_relevant(n):
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

        violation_node = (
            next((n for n, d in G.nodes(data=True)
                  if d["label"] == violation_room), None)
            if violation_room else None
        )
        in_violation_rm = occ_count.get(violation_node, 0) if violation_node else 0

        near_blocked_exit = sum(occ_count.get(bn, 0) for bn in blocked_exit_nodes)

        # Mobility status — real, computed from the marker's current node.
        # at_refuge mirrors the sticky settle flag exactly, so it is only
        # ever True once genuine, permanent arrival has happened (and only
        # possible at all when mobility_refuge=True was passed in).
        mobility_status = None
        if mobility_marker_id is not None:
            mnode  = agent_nodes[mobility_marker_id]
            mlabel = G.nodes[mnode]["label"]
            mfloor = G.nodes[mnode]["floor"]
            mobility_status = {
                "room"     : mlabel,
                "floor"    : mfloor,
                "at_refuge": mobility_settled,
            }

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
            "mobility_status"   : mobility_status,
        }

    # ── Tick 0 ───────────────────────────────────────────────────────────────
    timeline.append({
        "tick"              : 0,
        "violation"         : None,
        "agent_nodes"       : list(agent_nodes),
        "snapshot"          : build_snapshot(0, None),
        "mobility_marker_id": mobility_marker_id,
    })

    # ── Main loop ────────────────────────────────────────────────────────────
    for tick in range(1, total_ticks + 1):

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

        if tick in multi_sched:
            for mv_node in multi_sched[tick]:
                mv_label = G.nodes[mv_node]["label"]
                attractiveness_map[mv_node] = OVERCROWDED_ATTRACTIVENESS
                print(f"  [Tick {tick}] Additional violation: "
                      f"{mv_label} — attractiveness → "
                      f"{OVERCROWDED_ATTRACTIVENESS}")

        violation_is_active = (not is_baseline) and (tick >= violation_tick)

        new_nodes = []
        for i, node in enumerate(agent_nodes):
            if i == mobility_marker_id and not is_baseline:

                # Already settled at refuge — stay put for good. No rng
                # draw, no further movement, regardless of what any other
                # occupant does for the rest of the simulation.
                if mobility_settled:
                    new_nodes.append(node)
                    continue

                local_attract = dict(attractiveness_map)

                # Standing accessibility bias — always avoid stairs
                stair_nodes = [n for n, d in G.nodes(data=True)
                               if d["node_type"] == "stair"]
                for sn in stair_nodes:
                    local_attract[sn] = BLOCKED_EXIT_ATTRACTIVENESS

                refuge_node = None
                if mobility_refuge and violation_is_active:
                    current_floor = G.nodes[node]["floor"]
                    refuge_node   = floor_corridor.get(current_floor)
                    if refuge_node is not None:
                        local_attract[refuge_node] = REFUGE_ATTRACTIVENESS

                next_node = _choose_next(node, rng, local_attract)

                # Arrived at refuge this step — settle from here on
                if (mobility_refuge and violation_is_active and
                        refuge_node is not None and next_node == refuge_node):
                    mobility_settled = True

                new_nodes.append(next_node)
                continue

            new_nodes.append(_choose_next(node, rng, attractiveness_map))

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

    print("\nTS-04-style: mobility marker in 3-10, violation at tick 2, refuge ON")
    tl = simulate_agent_timeline(
        total_occupants=80, total_ticks=15,
        violation_tick=2, violation_room="3-10", seed=7,
        mobility_node="3-10", mobility_refuge=True)
    for r in tl:
        ms = r["snapshot"]["mobility_status"]
        print(f"  Tick {r['tick']:2d} | room={ms['room']:6s} | "
              f"at_refuge={ms['at_refuge']}")

    print("\nTS-02-style: mobility marker present but refuge OFF (should just walk)")
    tl2 = simulate_agent_timeline(
        total_occupants=80, total_ticks=15,
        violation_tick=5, violation_room="0-9", seed=2,
        mobility_node="0-9", mobility_refuge=False,
        blocked_exits=["EXIT-1"])
    for r in tl2:
        ms = r["snapshot"]["mobility_status"]
        print(f"  Tick {r['tick']:2d} | room={ms['room']:6s} | "
              f"at_refuge={ms['at_refuge']}")