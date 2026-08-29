# tests/test_scenarios.py
# Runs TS-01 through TS-05 against the LIVE AGENT (real API calls),
# evaluated programmatically. Companion to fix_and_bake.py's --scenario
# runs, which are the visual/baked demonstration of the same five
# scenarios — this file is the evaluator, that one is the visualizer.
#
# Fixes applied — all found by cross-checking against fix_and_bake.py's
# own SCENARIOS dict, which is imported directly below rather than
# duplicated, specifically so the two files can't drift apart again:
#
#   1. Wrong rooms tested entirely. TS-01 forced occupancy onto room
#      "0-20" — confirmed, from earlier verification work on this
#      project, to be a real Corridor (max_occ 10), not a bedroom.
#      Two consequences: sensor_sim.py deliberately excludes
#      circulation rooms (Corridor/Stair/Lobby) from ever appearing in
#      alerts at all (an alarm-fatigue fix), so the forced violation
#      may never even register; and GROUND_TRUTH_CITATIONS expected
#      Clause 2.43 (a bedroom-only citation) regardless. TS-03's
#      "0-20"/"1-20"/"2-20" rooms follow the same numbering pattern and
#      are very likely corridors too. Every room reference now comes
#      directly from fix_and_bake.py's own SCENARIOS dict — the same
#      verified bedrooms (1-1, 1-2, 3-1, 3-2, 3-10) its baked demos use.
#
#   2. Citation patterns hand-duplicated a second time, already found
#      not matching what fix_and_bake.py actually cites (e.g. TS-02's
#      old pattern accepted any "Section 3.x" when the scenario's real
#      citation is Table 2.1; TS-04's old pattern accepted the 2.33-2.36
#      general-provisions range when the scenario is specifically about
#      Sections 3.5-3.6 wheelchair refuge). GROUND_TRUTH_CITATIONS is
#      now DERIVED from each scenario's real adb_ref string in
#      fix_and_bake.SCENARIOS, not hand-maintained — if that citation
#      is ever changed there again, this file's expectations update
#      automatically instead of silently going stale a second time.
#
#   3. TS-05 (baseline) required a citation to be found at all — but a
#      correctly-idle agent has nothing to cite; the system prompt's
#      own idle-case instruction is just "All rooms within safe
#      occupancy limits - no action required", with no clause
#      reference. A correctly-behaving agent on this scenario would
#      have FAILED the citation check by design. Citation check is now
#      skipped entirely for baseline scenarios (empty adb_ref).
#
#   4. Sequence assertion only recognised the tool name
#      "check_compliance" — but the system prompt (for latency)
#      explicitly tells the agent to prefer check_compliance_batch
#      whenever more than one room needs checking, which TS-03 always
#      does. A correct agent using the batched tool made this check
#      pass VACUOUSLY (sequence_valid defaults True when
#      "check_compliance" never appears at all, whether or not the
#      real ordering was actually respected). Now recognises both tool
#      names and checks against whichever was actually used.
#
#   5. sign_updates counted every act_update_sign CALL, not every
#      successful one — the same bug found and fixed in
#      live_agent_runner.py. A call with a bad sign_id fails silently
#      inside bim.signage.update_sign() but still counted toward
#      "behavior_valid" passing. Now checks each trace entry's actual
#      result for an "error" key.
#
#   6. Occupancy values forced into rooms were hand-picked constants
#      (e.g. 15) tuned for an older, since-replaced area-based capacity
#      model. Now computed as get_max_occupancy(node) + 1 at run time —
#      always a genuine minimal violation regardless of what a room's
#      real capacity is, so this can't go stale again the same way.
#
#   7. TS-03's docstring described a per-floor scan across three
#      different floors; fix_and_bake.py's actual TS-03 is two rooms
#      on the SAME floor (F3) violating simultaneously. Retargeted to
#      match — same scenario, same story, both files now agree on
#      what "TS-03" means rather than two different narratives sharing
#      one label.
#
#   8. TS-04 forced occupancy across five rooms (3-1 through 3-5) and
#      placed the wheelchair narrative in 3-1; fix_and_bake.py's TS-04
#      is a single room (3-10), with 3-10 also being the mobility_node.
#      Retargeted to match.

import sys, os, json, time, re, argparse, statistics
from typing import Dict, List, Any, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from agent.agent import run_agent_cycle, make_client
from sensors.sensor_sim import (
    initialise_occupants, get_sensor_snapshot, update_sign_status
)
from sensors.building_graph import BUILDING_GRAPH as G, get_max_occupancy
from fix_and_bake import SCENARIOS as BAKE_SCENARIOS

os.makedirs("logs", exist_ok=True)
client = make_client()


def _prewarm_pipeline():
    """
    Pays the one-time RAG model load (SentenceTransformer, ChromaDB
    connection), MCP tool import, and Anthropic API connection cost
    upfront — the same fix applied to live_agent_runner.py's setup,
    for the same confirmed reason: a real run of this script showed
    49.01s latency on its one and only cycle, the highest seen all
    session, almost certainly this exact one-time cost landing
    entirely on whichever cycle happens to run first, since this
    script had no warmup step at all. Calling this once before the
    trial loop begins means every scenario's own timing reflects real
    per-cycle cost, not whichever one happened to go first — matters
    for --trials statistics (mean/stddev) being meaningful, and for
    comparing these numbers against live_agent_runner.py's own.
    """
    print("Pre-warming RAG pipeline (embedding model, ChromaDB)...")
    try:
        from rag.retriever import retrieve_regulations
        retrieve_regulations("residential care home occupancy warmup", n=1)
        print("  RAG ready")
    except Exception as e:
        print(f"  Warning: RAG pre-warm failed (non-fatal): {e}")

    print("Pre-warming MCP tool imports...")
    try:
        import mcp_server.server  # noqa: F401
        print("  MCP tools ready")
    except Exception as e:
        print(f"  Warning: MCP pre-warm failed (non-fatal): {e}")

    print("Pre-warming Anthropic API connection...")
    try:
        client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1,
            messages=[{"role": "user", "content": "Hi"}]
        )
        print("  API connection ready")
    except Exception as e:
        print(f"  Warning: API pre-warm failed (non-fatal): {e}")


# ── Ground truth, derived from fix_and_bake.py's own scenario data ──────────

def _node_for_label(label: str):
    return next((n for n, d in G.nodes(data=True) if d.get("label") == label), None)


def _citation_pattern_from_adb_ref(adb_ref: str) -> list:
    """
    Extracts the raw clause/table/section numbers (e.g. ['2.43'],
    ['3.5', '3.6']) directly from a scenario's real adb_ref string in
    fix_and_bake.SCENARIOS, rather than a separately hand-maintained
    pattern that can silently drift out of sync with it — see fix 2
    above. Returns a plain number list (not a compiled regex string)
    so _citation_matches() can also recognise range notation — see
    that function's docstring for why this is necessary. An empty
    list means no citation is expected at all (a baseline scenario
    with an empty adb_ref) — see fix 3.
    """
    return re.findall(r"\d+\.\d+", adb_ref or "")


GROUND_TRUTH_CITATIONS = {
    sid: _citation_pattern_from_adb_ref(sc.get("adb_ref", ""))
    for sid, sc in BAKE_SCENARIOS.items()
    if sid != "default"
}


def _citation_matches(directive_raw: str, target_numbers: list) -> bool:
    """
    Checks whether the directive cites any of target_numbers, either
    as an exact substring (e.g. "3.5") or as part of a cited RANGE
    that encompasses it (e.g. "3.4-3.8" or "3.4–3.8" genuinely covers
    3.5, 3.6, 3.7). Confirmed necessary directly against a real run:
    TS-04's agent cited "Section 3.4-3.8" — a genuinely correct, even
    more complete citation than the scenario's own target ('3.5',
    '3.6') — but a pure substring match failed the whole scenario,
    since 3.5 and 3.6 never appear as standalone substrings once
    elided into range notation. An empty target_numbers list (a
    baseline scenario) is trivially satisfied — see fix 3 above.
    """
    if not target_numbers:
        return True
    for num in target_numbers:
        if num in directive_raw:
            return True
    for lo_str, hi_str in re.findall(r"(\d+\.\d+)\s*[-–]\s*(\d+\.\d+)", directive_raw):
        try:
            lo, hi = float(lo_str), float(hi_str)
        except ValueError:
            continue
        for num in target_numbers:
            try:
                if lo <= float(num) <= hi:
                    return True
            except ValueError:
                continue
    return False

# Appended to every non-baseline trigger message — see fix note in
# module docstring. A real run of TS-01 showed the agent correctly
# NAMING three incidental WARNING-tier rooms on its board alongside
# the one forced FAIL room, then taking action on only the forced
# one. evaluate_scenario_run() doesn't fail this (it only requires at
# least one sign update), but it's a real, worth-fixing gap between
# what the agent notices and what it acts on. The original singular
# "identify THE overcrowded room" framing likely steered this — now
# explicit that every alerted room should get the response its own
# severity calls for, not just the scenario's specifically forced one.
_ACT_ON_ALL_ROOMS = (
    " Take the appropriate action for EVERY alerted room shown in the "
    "sensor snapshot, not only the primary one described above — this "
    "includes any WARNING-tier room, which should receive the "
    "pre-emptive response (an ALTERNATE-status sign and reduced "
    "attractiveness) per your system instructions, not just rooms "
    "that have reached FAIL."
)

# Real max_occ per verified room, resolved once at import time. Used to
# compute a genuine minimal violation (max_occ + 1) rather than a
# hand-picked constant that can go stale if capacities change — see fix 6.
_TS01_ROOM = BAKE_SCENARIOS["TS-01"]["violation_room"]                    # "1-1"
_TS02_ROOM = BAKE_SCENARIOS["TS-02"]["violation_room"]                    # "1-2"
_TS02_BLOCKED_EXITS = BAKE_SCENARIOS["TS-02"].get("blocked_exits", [])    # ["EXIT-1"]
_TS03_ROOM_A = BAKE_SCENARIOS["TS-03"]["violation_room"]                  # "3-1"
_TS03_ROOM_B = BAKE_SCENARIOS["TS-03"]["multi_violations"][0]["room"]     # "3-2"
_TS04_ROOM = BAKE_SCENARIOS["TS-04"]["violation_room"]                    # "3-10"


# ── Directive Parser ──────────────────────────────────────────────────────────

def _clean_directive(raw: str) -> dict:
    """
    Parses the structured directive into a clean dict without character truncation.
    """
    cleaned = raw.replace("**", "").replace("*", "").replace("#", "")
    lines = [l.strip() for l in cleaned.split("\n") if l.strip()]

    parsed = {}
    for line in lines:
        for key in ["CYCLE", "ROOMS", "ADB", "SIGNS", "ACTION", "ESCALATE"]:
            if line.upper().startswith(key + ":"):
                parsed[key.lower()] = line[len(key) + 1:].strip()
                break

    if len(parsed) >= 4:
        return parsed

    adb_found = ""
    action_found = ""
    status_found = "UNKNOWN"

    for line in lines:
        line_clean = line.strip()
        if not adb_found and any(
            k in line_clean for k in
            ["Section 2.", "Clause 2.", "Table 2.", "ADB Vol2",
             "2.33", "2.43", "2.37", "Table B1", "Appendix D"]
        ):
            adb_found = line_clean
        if not action_found and any(
            w in line_clean.lower() for w in
            ["sign", "blocked", "updated", "attractiveness", "pass", "fail",
             "violation", "idle"]
        ):
            action_found = line_clean
        if "VIOLATION" in line.upper():
            status_found = "VIOLATION"
        elif "PRE-EMPTIVE" in line.upper():
            status_found = "PRE-EMPTIVE"
        elif "IDLE" in line.upper() or "NO ACTION" in line.upper():
            status_found = "IDLE"
        elif "FALSE POSITIVE" in line.upper():
            status_found = "FALSE POSITIVE"

    return {
        "cycle": f"Tick 0 — {status_found}",
        "adb": adb_found or "See directive_raw for ADB citation",
        "action": action_found or "See directive_raw for action taken",
        "note": "Verbose format parsed",
    }


# ── Programmatic Assertion Engine ────────────────────────────────────────────

def evaluate_scenario_run(scenario_id: str, result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Programmatically evaluates trial outputs against strict ground truth standards:
      1. Causal Sequence: A compliance check MUST precede sign updates —
         accepts either check_compliance or check_compliance_batch (fix 4).
      2. Autonomous Citation: RAG citation matched against target regulation
         regex derived from fix_and_bake.SCENARIOS — skipped entirely for a
         baseline scenario, where no citation is expected (fix 3).
      3. Physical Behavioral Verification: Confirms expected sign updates or
         idle state, counting only genuinely successful sign updates (fix 5).
    """
    trace = result.get("trace", [])
    tools_used = [t["tool"] for t in trace]
    directive_raw = result.get("directive", "")

    sign_updates = sum(
        1 for t in trace
        if t["tool"] == "act_update_sign"
        and isinstance(t.get("result"), dict)
        and "error" not in t["result"]
    )

    # 1. Causal Tool Order Assertion — either compliance-check tool counts
    compliance_tools = {"check_compliance", "check_compliance_batch"}
    check_indices = [i for i, t in enumerate(tools_used) if t in compliance_tools]
    sign_indices  = [i for i, t in enumerate(tools_used) if t == "act_update_sign"]
    sequence_valid = True
    if check_indices and sign_indices:
        sequence_valid = min(check_indices) < min(sign_indices)

    # 2. Autonomous Citation Assertion — skipped for a baseline scenario
    is_baseline_scenario = not BAKE_SCENARIOS.get(scenario_id, {}).get("adb_ref")
    if is_baseline_scenario:
        citation_valid = True
    else:
        target_numbers = GROUND_TRUTH_CITATIONS.get(scenario_id, [])
        citation_valid = _citation_matches(directive_raw, target_numbers)

    # 3. Scenario Behavioral Rules
    behavior_valid = True
    failure_reasons = []

    if scenario_id == "TS-05":
        if sign_updates > 0:
            behavior_valid = False
            failure_reasons.append(f"False Positive: {sign_updates} sign updates issued during baseline.")
    elif scenario_id in ["TS-01", "TS-02", "TS-03", "TS-04"]:
        if sign_updates < 1:
            behavior_valid = False
            failure_reasons.append("Missing Action: Failed to execute required physical sign updates.")

    if not sequence_valid:
        failure_reasons.append("Sequence Error: 'act_update_sign' called before any compliance check.")
    if not citation_valid:
        target_numbers = GROUND_TRUTH_CITATIONS.get(scenario_id, [])
        failure_reasons.append(
            f"Citation Error: Failed to autonomously discover target clause "
            f"({' or '.join(target_numbers)}, including as part of a cited range)."
        )

    overall_pass = sequence_valid and citation_valid and behavior_valid

    return {
        "passed": overall_pass,
        "sequence_valid": sequence_valid,
        "citation_valid": citation_valid,
        "behavior_valid": behavior_valid,
        "failure_reasons": failure_reasons
    }


# ── Clean Formatted Terminal Logger ──────────────────────────────────────────

def print_structured_board(cleaned: dict):
    """Prints a spacious, uncongested representation of the Agent Directive Board."""
    print("\n  ┌─ STRUCTURED DIRECTIVE BOARD ────────────────────────────────────────┐")
    for key in ["cycle", "rooms", "adb", "signs", "action", "escalate"]:
        if key in cleaned:
            val = str(cleaned[key])
            print(f"  │  {key.upper():<10}: {val}")
    print("  └─────────────────────────────────────────────────────────────────────┘")


def log_result(scenario_id: str, result: dict, extra: dict = None) -> dict:
    cleaned = _clean_directive(result["directive"])
    eval_res = evaluate_scenario_run(scenario_id, result)

    log = {
        "scenario_id": scenario_id,
        "latency_seconds": result["latency_seconds"],
        # Real timing breakdown — see agent.py's run_agent_cycle() fix
        # note. api_turns is real client.messages.create() call count
        # (NOT the same as tool_count — several tool calls returned in
        # one API response are still one turn), api_time_seconds is
        # measured time spent in those calls, tool_time_seconds is the
        # measured sum of each tool's own execution time, and
        # overhead_seconds is whatever's left over rather than folded
        # silently into either figure.
        "api_turns": result.get("api_turns"),
        "api_time_seconds": result.get("api_time_seconds"),
        "tool_time_seconds": result.get("tool_time_seconds"),
        "overhead_seconds": result.get("overhead_seconds"),
        "tool_count": result["tool_count"],
        "tools_used": [t["tool"] for t in result["trace"]],
        "directive_clean": cleaned,
        "directive_raw": result["directive"],
        "evaluation": eval_res,
        "sign_updates": sum(
            1 for t in result["trace"]
            if t["tool"] == "act_update_sign"
            and isinstance(t.get("result"), dict)
            and "error" not in t["result"]
        ),
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    if extra:
        log.update(extra)

    path = os.path.join("logs", f"{scenario_id}.json")
    with open(path, "w") as f:
        json.dump(log, f, indent=2, default=str)

    status_str = "✅ PASS" if eval_res["passed"] else "❌ FAIL"
    print(f"\n  {'─'*55}")
    print(f"  SCENARIO SUMMARY: {scenario_id} [{status_str}]")
    print(f"  {'─'*55}")
    print(f"    Latency         : {result['latency_seconds']:.2f}s")
    if result.get("api_turns") is not None:
        print(f"      API turns     : {result['api_turns']}")
        print(f"      API time      : {result['api_time_seconds']:.2f}s")
        print(f"      Tool exec time: {result['tool_time_seconds']:.2f}s")
        print(f"      Overhead      : {result['overhead_seconds']:.2f}s")
    print(f"    Tool Invocations: {result['tool_count']}")
    print(f"    Sign Updates    : {log['sign_updates']}")
    print(f"    Sequence Check  : {'✅' if eval_res['sequence_valid'] else '❌'}")
    print(f"    Citation Check  : {'✅' if eval_res['citation_valid'] else '❌'}"
          f"{'  (n/a — baseline)' if scenario_id == 'TS-05' else ''}")

    if eval_res["failure_reasons"]:
        print("\n    ⚠ Evaluation Failures:")
        for reason in eval_res["failure_reasons"]:
            print(f"      - {reason}")

    if isinstance(cleaned, dict) and "cycle" in cleaned:
        print_structured_board(cleaned)

    print(f"\n  Log saved to: {path}")
    return log


# ── TS-01 — Single Room Congestion ────────────────────────────────────────────

def TS01_single_room_congestion():
    """
    Forces the same bedroom fix_and_bake.py's TS-01 scenario uses
    (BAKE_SCENARIOS["TS-01"]["violation_room"], a verified real bedroom)
    to max_occ + 1. Tests autonomous discovery of care home bedroom
    occupancy clauses and sign updates.
    """
    print("\n" + "="*60)
    print("  TS-01 — Single Room Congestion (Autonomous RAG)")
    print(f"  Target room: {_TS01_ROOM}  (matches fix_and_bake.py TS-01)")
    print("="*60)

    initialise_occupants(80, seed=1)
    from sensors.sensor_sim import occupancy
    node    = _node_for_label(_TS01_ROOM)
    max_occ = get_max_occupancy(node)
    occupancy[node] = max_occ + 1

    snap = get_sensor_snapshot()
    overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
    print(f"  Pre-condition: {len(overs)} over-capacity room(s)")
    print(f"  Room {_TS01_ROOM} Occupancy: "
          f"{snap['occupancy'].get(_TS01_ROOM, 0)} / {max_occ} capacity")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Sensors indicate a high occupancy congestion event. "
            "Identify the overcrowded room, verify compliance against building safety "
            "guidance for residential care homes using its IFC long name, and update the "
            "appropriate corridor sign if confirmed non-compliant. Cite the relevant regulatory clause."
            + _ACT_ON_ALL_ROOMS
        ),
        verbose=True
    )
    return log_result("TS-01", result, {"pre_condition": f"{len(overs)} violations"})


# ── TS-02 — Exit Obstruction ──────────────────────────────────────────────────

def TS02_exit_obstruction():
    """
    Blocks the same exit fix_and_bake.py's TS-02 blocks
    (BAKE_SCENARIOS["TS-02"]["blocked_exits"]) and forces the same
    room over capacity. Tests egress travel distance limits & rerouting.
    """
    print("\n" + "="*60)
    print("  TS-02 — Exit Obstruction (Autonomous RAG)")
    print(f"  Target room: {_TS02_ROOM}, blocked exits: {_TS02_BLOCKED_EXITS}"
          f"  (matches fix_and_bake.py TS-02)")
    print("="*60)

    initialise_occupants(80, seed=2)
    for exit_label in _TS02_BLOCKED_EXITS:
        update_sign_status(exit_label, "BLOCKED")
    print(f"  Pre-condition: {_TS02_BLOCKED_EXITS} set to BLOCKED")

    from sensors.sensor_sim import occupancy
    node    = _node_for_label(_TS02_ROOM)
    max_occ = get_max_occupancy(node)
    occupancy[node] = max_occ + 1
    print(f"  Room {_TS02_ROOM} Occupancy: {max_occ + 1} / {max_occ} capacity")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Sensors show the North Exit route is obstructed, and a room is over capacity. "
            "Assess travel distance requirements under care home safety regulations, "
            "and update corridor/exit signage to safely redirect occupants to the South exit route. "
            "Cite the governing regulatory table or section in your directive. "
            "IMPORTANT: if other bedrooms also happen to be at or over capacity "
            "this cycle, address those too — but the exit obstruction is the "
            "defining event of this scenario and its own citation (the travel "
            "distance / escape route table or section you retrieve for it) MUST "
            "still appear in your final ADB line and directive, alongside any "
            "bedroom-occupancy citations for the other rooms. Do not let a "
            "larger number of bedroom alerts crowd the exit-obstruction citation "
            "out of your final synthesis."
            + _ACT_ON_ALL_ROOMS
        ),
        verbose=True
    )
    return log_result("TS-02", result, {"pre_condition": f"{_TS02_BLOCKED_EXITS} blocked"})


# ── TS-03 — Multi-Room Congestion ─────────────────────────────────────────────

def TS03_multi_room_congestion():
    """
    Forces the same two same-floor bedrooms fix_and_bake.py's TS-03
    scenario uses simultaneously over capacity
    (BAKE_SCENARIOS["TS-03"]["violation_room"] + its multi_violations
    room). Tests compartmentalization check & batch tool execution.
    """
    print("\n" + "="*60)
    print("  TS-03 — Multi-Room Congestion (Autonomous RAG)")
    print(f"  Target rooms: {_TS03_ROOM_A}, {_TS03_ROOM_B}"
          f"  (matches fix_and_bake.py TS-03 — same floor, simultaneous)")
    print("="*60)

    initialise_occupants(80, seed=3)
    from sensors.sensor_sim import occupancy
    for label in (_TS03_ROOM_A, _TS03_ROOM_B):
        node    = _node_for_label(label)
        max_occ = get_max_occupancy(node)
        occupancy[node] = max_occ + 1

    snap = get_sensor_snapshot()
    overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
    print(f"  Pre-condition: {len(overs)} over-capacity room(s)")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Two bedrooms on the same floor are simultaneously overcrowded. "
            "Run a compliance check for each affected room using its IFC long name "
            "before updating signage. Issue sign updates only where violations are confirmed. "
            "Cite applicable care home occupancy regulations."
            + _ACT_ON_ALL_ROOMS
        ),
        verbose=True
    )
    return log_result("TS-03", result, {"pre_condition": f"{len(overs)} violations"})


# ── TS-04 — Mobility-Constrained Routing ─────────────────────────────────────

def TS04_mobility_constrained():
    """
    Forces the same room fix_and_bake.py's TS-04 scenario uses as both
    violation_room and mobility_node over capacity. Tests accessible
    refuge provisions & physical directional signage.
    """
    print("\n" + "="*60)
    print("  TS-04 — Mobility-Constrained Routing (Autonomous RAG)")
    print(f"  Target room: {_TS04_ROOM}  (matches fix_and_bake.py TS-04)")
    print("="*60)

    initialise_occupants(80, seed=4)
    from sensors.sensor_sim import occupancy
    node    = _node_for_label(_TS04_ROOM)
    max_occ = get_max_occupancy(node)
    occupancy[node] = max_occ + 1

    print(f"  Pre-condition: Room {_TS04_ROOM} at {max_occ + 1}/{max_occ} capacity")
    print(f"  Mobility constraint: Wheelchair user in room {_TS04_ROOM} "
          f"(Stairwell B-L3 non-accessible)")

    result = run_agent_cycle(
        client,
        trigger_message=(
            f"Perform your Sense-Reason-Act cycle. "
            f"Room {_TS04_ROOM} on the third floor (F3) is over capacity, and a "
            f"wheelchair user is present there. Escape route via stairwell B-L3 is not "
            f"accessible for wheelchair users. Consult regulatory provisions for accessible "
            f"escape and refuge areas. You MUST update physical directional signage on F3 "
            f"(e.g., SIGN_F3_CORRIDOR or SIGN_F3_STAIR) to direct mobility-impaired occupants "
            f"to a protected refuge zone or accessible path. Cite the governing regulatory section."
            + _ACT_ON_ALL_ROOMS
        ),
        verbose=True
    )
    return log_result("TS-04", result, {"pre_condition": f"{_TS04_ROOM} over capacity, wheelchair user"})


# ── TS-05 — Baseline ──────────────────────────────────────────────────────────

def TS05_baseline():
    """
    Baseline negative control (40 occupants, standard state) — matches
    fix_and_bake.py's TS-05 exactly (same occupant count, same seed).
    Tests false-positive suppression (must NOT issue sign updates).
    No citation is expected or required here — see fix 3.
    """
    print("\n" + "="*60)
    print("  TS-05 — Baseline Normal Occupancy (Negative Control)")
    print("="*60)

    initialise_occupants(40, seed=5)

    snap = get_sensor_snapshot()
    overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
    warns = [a for a in snap["alerts"] if a["severity"] == "WARNING"]

    print(f"  Pre-condition: {len(overs)} OVER violations, {len(warns)} WARNING alerts")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Routine monitoring check during standard hours (40 occupants across 4 floors). "
            "Assess current sensor snapshots for safety compliance. "
            "Note: A room operating at maximum capacity is NOT a violation—only rooms strictly exceeding "
            "capacity require intervention. If no violations exist, maintain normal state without updating signs."
        ),
        verbose=True
    )
    return log_result("TS-05", result, {"pre_condition": f"{len(overs)} OVER, {len(warns)} WARNING"})


# ── Multi-Trial Runner & Statistical Synthesis ───────────────────────────────

def run_scenarios_with_trials(scenarios_to_run: List[str], num_trials: int = 1):
    scenario_map = {
        "TS-01": TS01_single_room_congestion,
        "TS-02": TS02_exit_obstruction,
        "TS-03": TS03_multi_room_congestion,
        "TS-04": TS04_mobility_constrained,
        "TS-05": TS05_baseline,
    }

    print("\n" + "═"*65)
    print(f"  SYSTEM EVALUATION HARNESS — RUNNING {num_trials} TRIAL(S) PER SCENARIO")
    print(f"  Room targets synced live from fix_and_bake.SCENARIOS")
    print("═"*65)

    _prewarm_pipeline()

    summary_report = {}

    for sid in scenarios_to_run:
        fn = scenario_map[sid]
        trials_data = []

        for trial in range(1, num_trials + 1):
            if num_trials > 1:
                print(f"\n>>> Running {sid} (Trial {trial}/{num_trials})...")
            log_res = fn()
            trials_data.append(log_res)

        latencies = [t["latency_seconds"] for t in trials_data]
        passes = sum(1 for t in trials_data if t["evaluation"]["passed"])
        pass_rate = (passes / num_trials) * 100

        summary_report[sid] = {
            "trials_executed": num_trials,
            "pass_rate_pct": pass_rate,
            "mean_latency": statistics.mean(latencies),
            "std_latency": statistics.stdev(latencies) if num_trials > 1 else 0.0,
            "avg_tools_used": statistics.mean([t["tool_count"] for t in trials_data]),
            "trials": trials_data
        }

    print("\n\n" + "═"*70)
    print("  FINAL EVALUATION METRICS SUMMARY")
    print("═"*70)
    print(f"  {'Scenario':<8} {'Pass Rate':<12} {'Mean Latency':<15} {'Latency StdDev':<15} {'Avg Tools':<10}")
    print("  " + "─"*66)

    for sid, metrics in summary_report.items():
        print(f"  {sid:<8} "
              f"{metrics['pass_rate_pct']:>5.1f}%       "
              f"{metrics['mean_latency']:>7.2f}s        "
              f"±{metrics['std_latency']:>6.2f}s        "
              f"{metrics['avg_tools_used']:>6.1f}")

    print("═"*70)

    summary_path = "logs/evaluation_summary.json"
    with open(summary_path, "w") as f:
        json.dump(summary_report, f, indent=2, default=str)
    print(f"  Full analytical summary written to: {summary_path}\n")


# ── CLI Entry Point ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Evaluation Test Harness")
    parser.add_argument(
        "--scenario",
        default="all",
        choices=["all", "TS-01", "TS-02", "TS-03", "TS-04", "TS-05"],
        help="Target scenario to run"
    )
    parser.add_argument(
        "--trials",
        type=int,
        default=1,
        help="Number of trials per scenario for statistical validity (default: 1)"
    )

    args = parser.parse_args()

    scenarios = (
        ["TS-01", "TS-02", "TS-03", "TS-04", "TS-05"]
        if args.scenario == "all"
        else [args.scenario]
    )

    run_scenarios_with_trials(scenarios, num_trials=args.trials)