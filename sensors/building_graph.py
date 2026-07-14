# sensors/building_graph.py
# Building topology derived from BuildingGraphHosp.html (Peter Lawrence)
# Nodes: rooms, corridors, stairs, exits
# Edges: doors (weight=1), stairs (weight=20)

import networkx as nx

# ── Node definitions ──────────────────────────────────────────────────────────
# (id, label, floor, type, area_m2)
NODES = [
    # Floor 0 — Ground Floor
    (1,"0-1","F0 Ground Floor","room",28.60),
    (2,"0-2","F0 Ground Floor","room",16.38),
    (3,"0-3","F0 Ground Floor","room",31.00),
    (4,"0-4","F0 Ground Floor","room",18.84),
    (5,"0-5","F0 Ground Floor","room",30.87),
    (6,"0-6","F0 Ground Floor","room",15.62),
    (7,"0-7","F0 Ground Floor","room",30.79),
    (8,"0-8","F0 Ground Floor","room",10.34),
    (9,"0-9","F0 Ground Floor","room",19.03),
    (10,"0-10","F0 Ground Floor","room",10.15),
    (11,"0-11","F0 Ground Floor","room",14.11),
    (12,"0-12","F0 Ground Floor","room",6.01),
    (13,"0-13","F0 Ground Floor","room",5.70),
    (14,"0-14","F0 Ground Floor","room",8.77),
    (15,"0-15","F0 Ground Floor","room",13.74),
    (16,"0-16","F0 Ground Floor","room",6.37),
    (17,"0-17","F0 Ground Floor","room",26.20),
    (18,"0-18","F0 Ground Floor","room",21.45),
    (19,"0-19","F0 Ground Floor","room",4.55),
    (20,"0-20","F0 Ground Floor","room",47.92),
    (21,"0-A","F0 Ground Floor","corridor",155.01),
    (22,"0-B","F0 Ground Floor","corridor",292.15),
    # Floor 1 — First Floor
    (23,"1-1","F1 First Floor","room",28.60),
    (24,"1-2","F1 First Floor","room",16.38),
    (25,"1-3","F1 First Floor","room",31.00),
    (26,"1-4","F1 First Floor","room",18.84),
    (27,"1-5","F1 First Floor","room",30.87),
    (28,"1-6","F1 First Floor","room",15.62),
    (29,"1-7","F1 First Floor","room",30.79),
    (30,"1-8","F1 First Floor","room",10.34),
    (31,"1-9","F1 First Floor","room",19.03),
    (32,"1-10","F1 First Floor","room",10.15),
    (33,"1-11","F1 First Floor","room",14.36),
    (34,"1-12","F1 First Floor","room",6.01),
    (35,"1-13","F1 First Floor","room",5.70),
    (36,"1-14","F1 First Floor","room",8.77),
    (37,"1-15","F1 First Floor","room",13.74),
    (38,"1-16","F1 First Floor","room",6.40),
    (39,"1-17","F1 First Floor","room",26.20),
    (40,"1-18","F1 First Floor","room",21.45),
    (41,"1-19","F1 First Floor","room",4.55),
    (42,"1-20","F1 First Floor","room",47.92),
    (43,"1-A","F1 First Floor","corridor",154.86),
    (44,"1-B","F1 First Floor","corridor",292.15),
    # Floor 2 — Second Floor
    (45,"2-1","F2 Second Floor","room",28.60),
    (46,"2-2","F2 Second Floor","room",16.38),
    (47,"2-3","F2 Second Floor","room",31.00),
    (48,"2-4","F2 Second Floor","room",18.84),
    (49,"2-5","F2 Second Floor","room",30.87),
    (50,"2-6","F2 Second Floor","room",15.62),
    (51,"2-7","F2 Second Floor","room",30.79),
    (52,"2-8","F2 Second Floor","room",10.34),
    (53,"2-9","F2 Second Floor","room",19.03),
    (54,"2-10","F2 Second Floor","room",10.15),
    (55,"2-11","F2 Second Floor","room",14.42),
    (56,"2-12","F2 Second Floor","room",6.01),
    (57,"2-13","F2 Second Floor","room",5.70),
    (58,"2-14","F2 Second Floor","room",8.77),
    (59,"2-15","F2 Second Floor","room",13.74),
    (60,"2-16","F2 Second Floor","room",6.40),
    (61,"2-17","F2 Second Floor","room",26.20),
    (62,"2-18","F2 Second Floor","room",21.45),
    (63,"2-19","F2 Second Floor","room",4.55),
    (64,"2-20","F2 Second Floor","room",47.92),
    (65,"2-A","F2 Second Floor","corridor",154.95),
    (66,"2-B","F2 Second Floor","corridor",292.15),
    # Floor 3 — Third Floor
    (67,"3-1","F3 Third Floor","room",28.60),
    (68,"3-2","F3 Third Floor","room",16.38),
    (69,"3-3","F3 Third Floor","room",31.00),
    (70,"3-4","F3 Third Floor","room",18.84),
    (71,"3-5","F3 Third Floor","room",30.87),
    (72,"3-6","F3 Third Floor","room",15.62),
    (73,"3-7","F3 Third Floor","room",30.79),
    (74,"3-8","F3 Third Floor","room",10.34),
    (75,"3-9","F3 Third Floor","room",19.03),
    (76,"3-10","F3 Third Floor","room",10.15),
    (77,"3-11","F3 Third Floor","room",14.55),
    (78,"3-12","F3 Third Floor","room",6.01),
    (79,"3-13","F3 Third Floor","room",5.70),
    (80,"3-14","F3 Third Floor","room",8.77),
    (81,"3-15","F3 Third Floor","room",13.74),
    (82,"3-16","F3 Third Floor","room",6.40),
    (83,"3-17","F3 Third Floor","room",26.20),
    (84,"3-18","F3 Third Floor","room",21.45),
    (85,"3-19","F3 Third Floor","room",4.55),
    (86,"3-20","F3 Third Floor","room",47.92),
    (87,"3-A","F3 Third Floor","corridor",155.56),
    (88,"3-B","F3 Third Floor","corridor",292.15),
    # Stairwell B — connects all four floors
    (91,"B-L0","F0 Ground Floor","stair",18.60),
    (92,"B-L1","F1 First Floor","stair",18.60),
    (93,"B-L2","F2 Second Floor","stair",18.60),
    (94,"B-L3","F3 Third Floor","stair",18.54),
    # Secondary stairwell lobbies (Space-4 to Space-7 in Peter's graph)
    (97,"SP4","F0 Ground Floor","lobby",13.50),
    (98,"SP5","F1 First Floor","lobby",13.51),
    (99,"SP6","F2 Second Floor","lobby",13.47),
    (100,"SP7","F3 Third Floor","lobby",12.63),
    # Entrance and utility
    (95,"Main","F0 Ground Floor","lobby",2.99),
    (101,"StY","F0 Ground Floor","stair",2.99),
    # Fire exits — all ground floor
    (102,"EXIT-1","F0 Ground Floor","exit",0.0),
    (103,"EXIT-2","F0 Ground Floor","exit",0.0),
    (104,"EXIT-3","F0 Ground Floor","exit",0.0),
    (105,"EXIT-4","F0 Ground Floor","exit",0.0),
]

EXIT_IDS  = {102, 103, 104, 105}
STAIR_IDS = {91, 92, 93, 94, 101}

# ── Edges ─────────────────────────────────────────────────────────────────────
DOOR_EDGES = [
    # Ground floor rooms → main corridor 0-A
    (1,21),(2,21),(3,21),(4,21),(5,21),(6,21),(7,21),(8,21),
    (9,21),(10,21),(11,21),(12,21),(13,21),(14,21),(15,21),
    (16,21),(17,21),(18,21),(19,21),(20,21),
    # Ground floor corridor connections
    (21,22),(21,95),(21,91),(21,97),
    # Ground floor exits
    (21,102),(21,103),(22,104),(91,105),
    # First floor rooms → corridor 1-A
    (23,43),(24,43),(25,43),(26,43),(27,43),(28,43),(29,43),(30,43),
    (31,43),(32,43),(33,43),(34,43),(35,43),(36,43),(37,43),
    (38,43),(39,43),(40,43),(41,43),(42,43),
    # First floor corridor connections
    (43,44),(43,92),(43,98),(43,101),
    # Second floor rooms → corridor 2-A
    (45,65),(46,65),(47,65),(48,65),(49,65),(50,65),(51,65),(52,65),
    (53,65),(54,65),(55,65),(56,65),(57,65),(58,65),(59,65),
    (60,65),(61,65),(62,65),(63,65),(64,65),
    # Second floor corridor connections
    (65,66),(65,93),(65,99),
    # Third floor rooms → corridor 3-A
    (67,87),(68,87),(69,87),(70,87),(71,87),(72,87),(73,87),(74,87),
    (75,87),(76,87),(77,87),(78,87),(79,87),(80,87),(81,87),
    (82,87),(83,87),(84,87),(85,87),(86,87),
    # Third floor corridor connections
    (87,88),(87,94),(87,100),
]

STAIR_EDGES = [
    (91,92),(92,93),(93,94),   # Stairwell B
    (97,98),(98,99),(99,100),  # Secondary stairwell lobbies
]

ADB_M2_PER_PERSON = 6.0


def build_graph() -> nx.Graph:
    G = nx.Graph()
    for nid, label, floor, ntype, area in NODES:
        G.add_node(nid, label=label, floor=floor,
                   node_type=ntype, area_m2=area,
                   attractiveness=1.0,  # default — modified by agent
                   sign_blocked=False)  # set True when sign says BLOCKED
    for u, v in DOOR_EDGES:
        G.add_edge(u, v, weight=1, edge_type="door")
    for u, v in STAIR_EDGES:
        G.add_edge(u, v, weight=20, edge_type="stair")
    return G


BUILDING_GRAPH = build_graph()


def get_max_occupancy(node_id: int) -> int:
    area = BUILDING_GRAPH.nodes[node_id].get("area_m2", 0)
    # ADB formula: 1 person per 6m² (Purpose Group 2a)
    # Minimum of 3 to prevent trivial overcrowding in small rooms
    return max(3, int(area / ADB_M2_PER_PERSON))

def get_exit_path(node_id: int) -> dict:
    best_path, best_len = None, float("inf")
    for eid in EXIT_IDS:
        try:
            path = nx.shortest_path(BUILDING_GRAPH, node_id, eid, weight="weight")
            length = nx.shortest_path_length(BUILDING_GRAPH, node_id, eid, weight="weight")
            if length < best_len:
                best_len, best_path = length, path
        except nx.NetworkXNoPath:
            continue
    if not best_path:
        return {"error": f"No exit path from {node_id}"}
    return {
        "from_label" : BUILDING_GRAPH.nodes[node_id]["label"],
        "exit_node"  : best_path[-1],
        "path_labels": [BUILDING_GRAPH.nodes[n]["label"] for n in best_path],
        "path_weight": best_len,
    }


if __name__ == "__main__":
    G = BUILDING_GRAPH
    rooms = [n for n,d in G.nodes(data=True) if d["node_type"]=="room"]
    exits = [n for n,d in G.nodes(data=True) if d["node_type"]=="exit"]
    print(f"✅ Graph built")
    print(f"   Nodes : {G.number_of_nodes()}")
    print(f"   Edges : {G.number_of_edges()}")
    print(f"   Rooms : {len(rooms)}")
    print(f"   Exits : {len(exits)}")
    print(f"\nGround floor path: {get_exit_path(1)}")
    print(f"Top floor path:    {get_exit_path(67)}")