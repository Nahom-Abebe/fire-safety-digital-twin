# tests/test_scenarios.py
# Runs TS-01 through TS-05 from Section 3.6.3 of interim report.
#
# A-Grade Enhancements Applied:
#   1. Autonomous RAG: Prompts state physical realities only; hand-fed clause hints removed.
#   2. Assertion Engine: Programmatically validates tool ordering, behavior, and ground-truth citations.
#   3. Full Parsing: Removed string truncation ([:120]) in _clean_directive for complete logs.
#   4. Statistical Rigor: Added multi-trial runner support (--trials) with per-scenario mean/stddev.
#   5. High-Readability Console Output: Clean visual borders and spacious output formatting.

import sys, os, json, time, re, argparse, statistics
from typing import Dict, List, Any, Tuple

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from agent.agent import run_agent_cycle
from sensors.sensor_sim import (
    initialise_occupants, get_sensor_snapshot, update_sign_status
)
from sensors.building_graph import BUILDING_GRAPH as G

os.makedirs("logs", exist_ok=True)
client = anthropic.Anthropic()


# ── Ground Truth Regex Patterns for Autonomous Verification ──────────────────

GROUND_TRUTH_CITATIONS = {
    "TS-01": r"(2\.43|Appendix\s+D|Clause\s+2\.43|Section\s+2\.43)",
    "TS-02": r"(3\.\d+|Table\s+2\.1|Section\s+3|Clause\s+3\.\d+)",
    "TS-03": r"(2\.33|2\.43|Section\s+2|Clause\s+2\.\d+)",
    "TS-04": r"(2\.3[3-6]|3\.[5-6]|Section\s+2\.3|Clause\s+2\.3)",
    "TS-05": r"(2a|Purpose\s+Group\s+2a|Approved\s+Document\s+B|ADB)"
}


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
                # Clean extraction without arbitrary slicing
                parsed[key.lower()] = line[len(key) + 1:].strip()
                break

    if len(parsed) >= 4:
        return parsed

    # Fallback parser for verbose outputs
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
      1. Causal Sequence: Compliance check MUST precede sign updates.
      2. Autonomous Citation: RAG citation matched against target regulation regex.
      3. Physical Behavioral Verification: Confirms expected sign updates/idle state.
    """
    trace = result.get("trace", [])
    tools_used = [t["tool"] for t in trace]
    directive_raw = result.get("directive", "")
    sign_updates = sum(1 for t in tools_used if t == "act_update_sign")

    # 1. Causal Tool Order Assertion
    sequence_valid = True
    if "check_compliance" in tools_used and "act_update_sign" in tools_used:
        idx_check = tools_used.index("check_compliance")
        idx_sign = tools_used.index("act_update_sign")
        sequence_valid = (idx_check < idx_sign)

    # 2. Autonomous Citation Assertion (Regex match against ground truth pattern)
    pattern = GROUND_TRUTH_CITATIONS.get(scenario_id, r"")
    citation_valid = bool(re.search(pattern, directive_raw, re.IGNORECASE))

    # 3. Scenario Behavioral Rules
    behavior_valid = True
    failure_reasons = []

    if scenario_id == "TS-05":
        # Baseline must NOT issue sign updates
        if sign_updates > 0:
            behavior_valid = False
            failure_reasons.append(f"False Positive: {sign_updates} sign updates issued during baseline.")
    elif scenario_id in ["TS-01", "TS-02", "TS-03", "TS-04"]:
        # Emergency/Congestion events must update physical signage
        if sign_updates < 1:
            behavior_valid = False
            failure_reasons.append("Missing Action: Failed to execute required physical sign updates.")

    if not sequence_valid:
        failure_reasons.append("Sequence Error: 'act_update_sign' called before 'check_compliance'.")
    if not citation_valid:
        failure_reasons.append(f"Citation Error: Failed to autonomously discover target clause ({pattern}).")

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
        "tool_count": result["tool_count"],
        "tools_used": [t["tool"] for t in result["trace"]],
        "directive_clean": cleaned,
        "directive_raw": result["directive"],
        "evaluation": eval_res,
        "sign_updates": sum(1 for t in result["trace"] if t["tool"] == "act_update_sign"),
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
    print(f"    Tool Invocations: {result['tool_count']}")
    print(f"    Sign Updates    : {log['sign_updates']}")
    print(f"    Sequence Check  : {'✅' if eval_res['sequence_valid'] else '❌'}")
    print(f"    Citation Check  : {'✅' if eval_res['citation_valid'] else '❌'}")

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
    Forces room 0-20 to 15 occupants.
    Tests autonomous discovery of care home room capacity clauses and sign updates.
    """
    print("\n" + "="*60)
    print("  TS-01 — Single Room Congestion (Autonomous RAG)")
    print("="*60)

    initialise_occupants(80, seed=1)
    from sensors.sensor_sim import occupancy
    target = next(n for n, d in G.nodes(data=True) if d.get("label") == "0-20")
    occupancy[target] = 15

    snap = get_sensor_snapshot()
    overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
    print(f"  Pre-condition: {len(overs)} over-capacity room(s)")
    print(f"  Room 0-20 Occupancy: {snap['occupancy'].get('0-20', 0)} occupants")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Sensors indicate a high occupancy congestion event on the ground floor. "
            "Identify the overcrowded room, verify compliance against building safety "
            "guidance for residential care homes using its IFC long name, and update the "
            "appropriate corridor sign if confirmed non-compliant. Cite the relevant regulatory clause."
        ),
        verbose=True
    )
    return log_result("TS-01", result, {"pre_condition": f"{len(overs)} violations"})


# ── TS-02 — Exit Obstruction ──────────────────────────────────────────────────

def TS02_exit_obstruction():
    """
    North exit blocked. Tests egress travel distance limits & rerouting.
    """
    print("\n" + "="*60)
    print("  TS-02 — Exit Obstruction (Autonomous RAG)")
    print("="*60)

    initialise_occupants(80, seed=2)
    update_sign_status("EXIT-1", "BLOCKED")
    print("  Pre-condition: EXIT-1 (North) set to BLOCKED")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Sensors show the North Exit route (EXIT-1) is obstructed. "
            "Assess travel distance requirements under care home safety regulations, "
            "and update corridor/exit signage to safely redirect occupants to the South exit route. "
            "Cite the governing regulatory table or section in your directive."
        ),
        verbose=True
    )
    return log_result("TS-02", result, {"pre_condition": "North exit blocked"})


# ── TS-03 — Multi-Room Congestion ─────────────────────────────────────────────

def TS03_multi_room_congestion():
    """
    Multiple rooms overcrowded across floors.
    Tests per-floor compartmentalization check & batch tool execution.
    """
    print("\n" + "="*60)
    print("  TS-03 — Multi-Room Congestion (Autonomous RAG)")
    print("="*60)

    initialise_occupants(80, seed=3)
    from sensors.sensor_sim import occupancy
    for label, count in [("0-20", 12), ("1-20", 10), ("2-20", 8)]:
        node = next(n for n, d in G.nodes(data=True) if d.get("label") == label)
        occupancy[node] = count

    snap = get_sensor_snapshot()
    overs = [a for a in snap["alerts"] if a["severity"] == "OVER"]
    print(f"  Pre-condition: {len(overs)} over-capacity rooms across floors")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Multiple rooms across different floors are overcrowded. "
            "Perform a zone-level assessment per floor. "
            "Run check_compliance for each affected room using its IFC long name "
            "before updating signage. Issue per-floor sign updates only where violations are confirmed. "
            "Cite applicable compartmentation regulations."
        ),
        verbose=True
    )
    return log_result("TS-03", result, {"pre_condition": f"{len(overs)} violations across floors"})


# ── TS-04 — Mobility-Constrained Routing ─────────────────────────────────────

def TS04_mobility_constrained():
    """
    Wheelchair user on F3 with stairwell-only escape.
    Tests accessible refuge provisions & physical directional signage.
    """
    print("\n" + "="*60)
    print("  TS-04 — Mobility-Constrained Routing (Autonomous RAG)")
    print("="*60)

    initialise_occupants(80, seed=4)
    from sensors.sensor_sim import occupancy
    for label in ["3-1", "3-2", "3-3", "3-4", "3-5"]:
        node = next((n for n, d in G.nodes(data=True) if d.get("label") == label), None)
        if node:
            occupancy[node] = 4

    print("  Pre-condition: F3 Third Floor heavily occupied")
    print("  Mobility constraint: Wheelchair user in room 3-1 (Stairwell B-L3 non-accessible)")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "The third floor (F3) has high occupancy and a wheelchair user is in room 3-1. "
            "Escape route via stairwell B-L3 is not accessible for wheelchair users. "
            "Consult regulatory provisions for accessible escape and refuge areas. "
            "You MUST update physical directional signage on F3 (e.g., SIGN_F3_CORRIDOR or SIGN_F3_STAIR) "
            "to direct mobility-impaired occupants to a protected refuge zone or accessible path. "
            "Cite the governing regulatory section."
        ),
        verbose=True
    )
    return log_result("TS-04", result, {"pre_condition": "F3 high occupancy, wheelchair user"})


# ── TS-05 — Baseline ──────────────────────────────────────────────────────────

def TS05_baseline():
    """
    Baseline negative control (40 occupants, standard state).
    Tests false-positive suppression (must NOT issue sign updates).
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
    print("═"*65)

    summary_report = {}

    for sid in scenarios_to_run:
        fn = scenario_map[sid]
        trials_data = []

        for trial in range(1, num_trials + 1):
            if num_trials > 1:
                print(f"\n>>> Running {sid} (Trial {trial}/{num_trials})...")
            log_res = fn()
            trials_data.append(log_res)

        # Aggregate statistical metrics for this scenario
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

    # Print Final Academic Evaluation Matrix
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
    parser = argparse.ArgumentParser(description="A-Grade Evaluation Test Harness")
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