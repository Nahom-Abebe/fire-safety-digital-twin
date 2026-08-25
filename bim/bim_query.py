# bim/bim_query.py

import json, os

_path = os.path.join(os.path.dirname(__file__), "global_ids_v2.json")

try:
    with open(_path, encoding="utf-8") as f:
        _data = json.load(f)
    SPACES = _data["spaces"]
    SIGNS  = _data["signs"]
    ALARMS = _data["alarms"]
    FLOORS = _data["floors"]
except FileNotFoundError:
    raise FileNotFoundError(
        f"bim/bim_query.py: global_ids_v2.json not found at {_path}. "
        f"This file is required at import time — every module that "
        f"imports bim_query will fail until it exists."
    )
except json.JSONDecodeError as e:
    raise ValueError(
        f"bim/bim_query.py: global_ids_v2.json is not valid JSON: {e}"
    )
except KeyError as e:
    raise KeyError(
        f"bim/bim_query.py: global_ids_v2.json is missing required "
        f"top-level key {e} — expected 'spaces', 'signs', 'alarms', "
        f"and 'floors'."
    )

# Room-type -> ADB reference. Text matches what's already cited
# elsewhere in this project so the live compliance path and the demo
# scenarios agree with each other. Extend the keyword lists once the
# real long_name values in global_ids_v2.json are known — corridors,
# stairs, and any other room types currently fall through to GENERIC.
#
# Communal room citation corrected: was "ADB Vol2 Table B1 — Purpose
# Group 2a (communal space occupancy)" — Table B1 is the purpose-group
# classification table, not an occupant-capacity source at all. The
# actual correct basis, confirmed directly against the official ADB
# Vol2 text (gov.uk): Appendix D gives the occupant-number calculation
# methodology ("if the maximum number of people... is not known, it
# should be calculated using the occupant number guidance in Appendix
# D"), and Appendix C's Table C1 gives the floor-space factor the
# calculation actually uses — 1.0 m2/person for exactly this category:
# "committee room, common room, conference room, dining room... lounge
# or bar... meeting room, reading room, restaurant". Lounge, Dining,
# and Conference all share this one factor, so they share one
# reference here. Bedroom is deliberately left unchanged — bedrooms
# aren't in Table C1 at all (capacity there is governed by bed count
# under Clause 2.43, not a density calculation). Bedroom's citation
# is now worded to reflect that honestly: Clause 2.43 genuinely
# governs bed provision, not a headcount density calculation — this
# no longer implies the clause mathematically produces this room's
# specific max_occ figure, only that it's the relevant ADB provision
# while the numeric threshold itself is a modelled system parameter.
GENERIC_ADB_REF  = "ADB Vol2 Table B1 — Purpose Group 2a"
COMMUNAL_ADB_REF = ("ADB Vol2 Appendix D — occupant number calculation "
                    "(Table C1 floor space factor: 1.0 m2/person)")
BEDROOM_ADB_REF  = ("ADB Clause 2.43 (bed provision) — occupancy "
                    "monitored against system-defined room capacity")
ROOM_TYPE_ADB_REFS = {
    "bedroom"   : BEDROOM_ADB_REF,
    "lounge"    : COMMUNAL_ADB_REF,
    "dining"    : COMMUNAL_ADB_REF,
    "conference": COMMUNAL_ADB_REF,
}
ROOM_TYPE_KEYWORDS = {
    "bedroom"   : ["bedroom", "bed room"],
    "lounge"    : ["lounge", "communal", "day room", "dayroom"],
    "dining"    : ["dining"],
    "conference": ["conference", "meeting room"],
}


def _adb_ref_for_space(space: dict) -> str:
    """
    Classifies a space and returns the matching ADB reference. Checks
    an explicit type field first if one exists on the space dict,
    then falls back to keyword matching on long_name, then GENERIC.
    """
    explicit = (space.get("room_type") or space.get("category")
                or space.get("use") or "").lower()
    if explicit in ROOM_TYPE_ADB_REFS:
        return ROOM_TYPE_ADB_REFS[explicit]

    name = space.get("long_name", "").lower()
    for room_type, keywords in ROOM_TYPE_KEYWORDS.items():
        if any(kw in name for kw in keywords):
            return ROOM_TYPE_ADB_REFS[room_type]

    return GENERIC_ADB_REF


def get_space_by_name(long_name: str) -> dict:
    for gid, s in SPACES.items():
        if s.get("long_name", "").lower() == long_name.lower():
            return s
    return {"error": f"'{long_name}' not found"}


def get_adb_ref_for_room_label(graph_label: str) -> str:
    """
    Given a graph label (e.g. '3-1', '1-2'), returns the correct
    room-type-aware ADB reference — the same classification
    _adb_ref_for_space() already applies, just resolved via
    GRAPH_TO_IFC instead of requiring a caller to already have a
    SPACES dict entry in hand.

    Added for animation_baker.py's _build_board_text(), which
    previously hardcoded "(ADB Cl.2.43)" — the bedroom clause — next
    to every room in the OVERCAPACITY list regardless of what that
    room actually was. That's specifically visible in TS-02, whose
    violation_room is a real bedroom but whose scenario is otherwise
    about exit obstruction/travel distance — citing the bedroom
    clause there isn't wrong for that specific room, but the board
    had no way to cite anything else correctly if a future scenario's
    violation_room were a non-bedroom space. Falls back to
    GENERIC_ADB_REF (via _adb_ref_for_space()'s own fallback) for any
    label not in GRAPH_TO_IFC or with no matching real space.
    """
    from bim.room_geometry import GRAPH_TO_IFC
    ifc_name = GRAPH_TO_IFC.get(graph_label)
    if not ifc_name:
        return GENERIC_ADB_REF
    space = next((s for s in SPACES.values() if s.get("name") == ifc_name), None)
    if space is None:
        return GENERIC_ADB_REF
    return _adb_ref_for_space(space)


def get_all_signs() -> list:
    return list(SIGNS.values())


def get_sign(sign_id: str) -> dict:
    return SIGNS.get(sign_id, {"error": f"Sign '{sign_id}' not found"})


def check_occupancy_compliance(long_name: str, current: int) -> dict:
    """
    Fix applied: this was binary (current <= max -> PASS, else FAIL),
    with no WARNING tier at all — meaning a room genuinely AT its real
    capacity (current == max, e.g. 3/3) always returned PASS. Combined
    with agent.py's system prompt instructing the agent to treat any
    PASS result as a "false positive" against the graph's own alert,
    this meant a real WARNING-severity alert from sensors/sensor_sim.py
    (the 80-100% pre-emptive band Peter Lawrence's criterion 3 is
    built around) could NEVER result in pre-emptive action — confirmed
    directly in a real live_agent_runner.py run: every single at-
    capacity room across the whole session was dismissed as a false
    positive, and the only sign update that occurred was reactive
    (a room that had already gone OVER), not pre-emptive. Now mirrors
    the same 80% threshold sensor_sim.py's own alert logic already
    uses, so the deterministic IFC-backed check and the graph's
    alert severity agree with each other instead of one silently
    overriding the other.
    """
    space = get_space_by_name(long_name)
    if "error" in space:
        return space

    adb_ref = _adb_ref_for_space(space)

    if "max_occ" not in space:
        return {
            "space": long_name, "floor": space.get("floor"),
            "current": current, "max": None,
            "compliant": None,
            "status": "UNKNOWN — no capacity data for this space",
            "adb_ref": adb_ref,
        }

    max_occ = space["max_occ"]
    ratio   = (current / max_occ) if max_occ > 0 else 0

    if current > max_occ:
        status, compliant = "FAIL", False
    elif max_occ > 0 and ratio >= 0.8:
        status, compliant = "WARNING", True
    else:
        status, compliant = "PASS", True

    return {
        "space": long_name, "floor": space.get("floor"),
        "current": current, "max": max_occ,
        "compliant": compliant,
        "status": status,
        "adb_ref": adb_ref,
    }


if __name__ == "__main__":
    print(f"Spaces: {len(SPACES)}, Signs: {len(SIGNS)}, Alarms: {len(ALARMS)}")
    print(get_space_by_name("Lounge"))
    print(check_occupancy_compliance("Lounge", 150))
    print(check_occupancy_compliance("Lounge", 50))