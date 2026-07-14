# sensors/agent_walk.py
# Per-agent random walk for baked animation — occupancy management model.
#
# This implements Peter Lawrence's vision:
#   - NO evacuation, NO fire alarm, NO biased walk to exits
#   - Occupants move probabilistically between rooms at every tick
#   - When a room exceeds its ADB capacity, its attractiveness drops
#     so new occupants are discouraged from entering
#   - Occupants already in the overcrowded room continue their normal
#     random walk and naturally disperse over subsequent ticks
#   - Exit nodes are NEVER absorbing — occupants pass through them
#     like any other node and keep moving
#   - The meaningful metric is "rooms brought back into compliance"
#     not "occupants evacuated"

import random
import networkx as nx
from sensors.building_graph import BUILDING_GRAPH, EXIT_IDS, get_max_occupancy

G          = BUILDING_GRAPH
ROOM_NODES = [n for n, d in G.nodes(data=True) if d["node_type"] == "room"]

# Attractiveness applied to overcrowded rooms during the animation
# (separate from the live simulation's attractiveness in sensor_sim.py)
OVERCROWDED_ATTRACTIVENESS = 0.05   # strongly discourages new occupants
NORMAL_ATTRACTIVENESS      = 1.0


def _edge_weight(from_node: int, to_node: int,
                 attractiveness_map: dict) -> float:
    """
    Movement weight from from_node to to_node.
    Lower attractiveness = lower probability of moving there.
    """
    base_weight  = G.edges[from_node, to_node].get("weight", 1)
    attract      = attractiveness_map.get(to_node, NORMAL_ATTRACTIVENESS)
    return attract / base_weight


def _choose_next(node: int, rng: random.Random,
                 attractiveness_map: dict) -> int:
    """
    Probabilistically chooses next node from current position.
    Stay probability proportional to current room's own attractiveness.
    Move probability proportional to each neighbour's attractiveness / edge weight.
    """
    neighbours = list(G.neighbors(node))
    candidates = [node] + neighbours

    stay_weight = attractiveness_map.get(node, NORMAL_ATTRACTIVENESS) * 0.5
    move_weights = [
        _edge_weight(node, nb, attractiveness_map)
        for nb in neighbours
    ]
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
                             seed: int = 42) -> list:
    """
    Simulates per-agent movement for baked animation.

    Up to violation_tick: pure random walk, all rooms equal attractiveness.
    From violation_tick onward: the violation_room's attractiveness drops
    to OVERCROWDED_ATTRACTIVENESS, discouraging new occupants from entering.
    Existing occupants in that room disperse naturally over subsequent ticks.

    Returns a list of tick records:
    [{
        "tick"       : int,
        "violation"  : dict | None,
        "agent_nodes": [node_id, ...],  one per occupant
        "snapshot"   : dict,            aggregate state
    }]

    No evacuation counter. No exit absorption. No fire alarm.
    """
    rng         = random.Random(seed)
    agent_nodes = [rng.choice(ROOM_NODES) for _ in range(total_occupants)]
    violation   = None
    timeline    = []

    # Attractiveness map: node_id → float
    attractiveness_map = {n: NORMAL_ATTRACTIVENESS for n in G.nodes}

    def build_snapshot(tick, violation):
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

        # Count occupants in the violation room (main metric)
        violation_node  = next(
            (n for n, d in G.nodes(data=True)
             if d["label"] == violation_room), None)
        in_violation_rm = occ_count.get(violation_node, 0) if violation_node else 0

        return {
            "tick"            : tick,
            "total_occ"       : total_occupants,
            "by_floor"        : by_floor,
            "occupancy"       : {
                G.nodes[n]["label"]: c
                for n, c in occ_count.items() if c > 0
            },
            "violation"       : violation,
            "alerts"          : alerts,
            "in_violation_room": in_violation_rm,
            # No 'at_exits' — this is not an evacuation model
        }

    # Tick 0 — initial state
    timeline.append({
        "tick"       : 0,
        "violation"  : None,
        "agent_nodes": list(agent_nodes),
        "snapshot"   : build_snapshot(0, None),
    })

    for tick in range(1, total_ticks + 1):

        # Trigger occupancy violation at violation_tick
        if tick == violation_tick:
            vnode = next(
                (n for n, d in G.nodes(data=True)
                 if d["label"] == violation_room), None)
            if vnode is not None:
                violation = {
                    "type"        : "overcrowding",
                    "node_id"     : vnode,
                    "node_label"  : violation_room,
                    "floor"       : G.nodes[vnode]["floor"],
                    "adb_action"  : "sign_redirect",
                }
                # Drop attractiveness of the violation room
                # so new occupants are discouraged from entering
                attractiveness_map[vnode] = OVERCROWDED_ATTRACTIVENESS
                print(f"  [Tick {tick}] Violation triggered: "
                      f"{violation_room} — attractiveness → "
                      f"{OVERCROWDED_ATTRACTIVENESS}")

        # Move all occupants one step
        new_nodes = []
        for node in agent_nodes:
            next_node = _choose_next(node, rng, attractiveness_map)
            new_nodes.append(next_node)

        agent_nodes = new_nodes

        timeline.append({
            "tick"       : tick,
            "violation"  : violation,
            "agent_nodes": list(agent_nodes),
            "snapshot"   : build_snapshot(tick, violation),
        })

    return timeline


if __name__ == "__main__":
    print("=" * 55)
    print("  AGENT WALK — Occupancy Management Model Test")
    print("=" * 55)

    print("\nTest 1: No violation (violation_tick=999)")
    print("Expected: occupants disperse naturally, no clustering at exits")
    timeline = simulate_agent_timeline(
        total_occupants=80, total_ticks=15,
        violation_tick=999, violation_room="0-4", seed=5)

    for r in timeline:
        snap = r["snapshot"]
        print(f"  Tick {snap['tick']:2d} | "
              f"alerts={len(snap['alerts']):2d} | "
              f"in_violation_room={snap['in_violation_room']} | "
              f"violation={'YES' if r['violation'] else 'no'}")

    print("\nTest 2: Violation at tick 5 in room 0-4")
    print("Expected: occupants in 0-4 disperse after tick 5, room clears naturally")
    timeline = simulate_agent_timeline(
        total_occupants=80, total_ticks=20,
        violation_tick=5, violation_room="0-4", seed=42)

    for r in timeline:
        snap = r["snapshot"]
        occ_in_room = snap["in_violation_room"]
        mx = get_max_occupancy(
            next(n for n, d in G.nodes(data=True) if d["label"] == "0-4"))
        status = "OVER" if occ_in_room > mx else "OK"
        print(f"  Tick {snap['tick']:2d} | "
              f"in 0-4={occ_in_room:2d}/{mx} [{status}] | "
              f"alerts={len(snap['alerts']):2d} | "
              f"violation={'YES' if r['violation'] else 'no':>3}")