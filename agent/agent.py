# agent/agent.py
# Claude Sense-Reason-Act agent for care home occupancy management.
#
# Design philosophy (aligned with Peter Lawrence's emails):
#   - This is an OCCUPANCY MANAGEMENT system, not an evacuation system
#   - The agent monitors room capacity tick by tick
#   - When a room approaches or exceeds its ADB limit, the agent:
#       1. Retrieves the specific ADB care home clause that applies
#       2. Cites the exact section/clause number in its response
#       3. Updates the relevant sign to redirect occupants
#       4. Lowers room attractiveness so new occupants avoid that area
#       5. Informs the building manager via the board
#   - NO evacuation logic, NO fire alarm framing
#   - The agent responds to what it ACTUALLY sees in the live simulation
#
# Board reasoning updates:
#   - _call_tool() updates the Blender board after each meaningful tool call
#   - Tom Cole can see the agent's thought process in the UI in real time
#   - Not just in the terminal log

import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import anthropic
from agent.tool_schemas import TOOLS

# ── System prompt ─────────────────────────────────────────────────────────────
SYSTEM_PROMPT = """You are an intelligent occupancy management AI agent for a
UK residential care home Digital Twin. The building is hosted in Blender using
an IFC model, and your decisions are written back into the IFC property sets in
real time.

YOUR ROLE:
You manage room and corridor occupancy levels to keep them within safe limits
defined by UK Approved Document B (ADB) Volume 2. This is NOT a fire evacuation
system. You do not trigger building-wide evacuations. You identify specific
rooms or corridors that are approaching or exceeding their safe occupancy limit,
then redirect occupants away from those spaces by updating signage and adjusting
room attractiveness.

BUILDING CONTEXT:
- Building type: Residential Care Home
- ADB classification: Purpose Group 2a (Residential Institutional)
- Key ADB sections that apply to this building:
    Section 2.33-2.37: General provisions for residential care homes
    Clause 2.43: Bedroom occupancy standards
    Table 2.1: Maximum travel distances within escape routes
    Table B1/Appendix D: Occupant load factors (floor space per person)

YOU FOLLOW A STRICT SENSE - REASON - ACT LOOP:

SENSE:
Call sense_building_state to get current occupancy across all rooms and floors.
Look at the alerts list:
  - severity WARNING means a room is at 80-99% of its ADB capacity
  - severity OVER    means a room has exceeded its ADB capacity

If there are no alerts, call act_update_board with message
"All rooms within safe occupancy limits - no action required" and stop.

REASON:
For each alert, in priority order (OVER before WARNING):

Step 1: Call sense_room with the room's graph label to get its full details
        including room type, current count, max count, and the IFC long name.

Step 2: Call get_regulations with a query SPECIFICALLY about care home
        occupancy for that room type. Use queries like:
        - "residential care home bedroom occupancy ADB Section 2.43"
        - "residential care home corridor escape route Section 2.33"
        - "Purpose Group 2a travel distance Table 2.1 care home"
        Do NOT use generic queries. Always reference the care home context.

Step 3: Call check_compliance using the IFC long name (e.g. "Bedroom",
        "Lounge") and current occupancy to get the deterministic ADB result.

Step 4: Identify the SPECIFIC clause that justifies your action, e.g.:
        "ADB Vol2 Section 2.33 - residential care home general provisions"
        "ADB Vol2 Clause 2.43 - bedroom occupancy standard"
        "ADB Vol2 Table 2.1 - maximum travel distance in escape route"
        You MUST cite a specific section or clause, never just "ADB Vol2".

ACT:
For each genuine violation (check_compliance returns FAIL or WARNING):

1. Call act_update_sign for the corridor sign serving that floor.
   Sign messages must be SHORT and PRACTICAL — like real building signage.
   No ADB references on signs. Just clear instructions for occupants.

   Good sign message examples:
   - "Room 1-A is full — please use the lounge on Floor 2"
   - "Do not enter Room 0-4 — use Room 0-6 instead"
   - "This corridor is busy — please use the north stairwell"
   - "Wheelchair users: proceed to refuge area on this floor"
   - "Room 3-1 at capacity — staff please redirect residents"

   Bad sign message examples (DO NOT use these):
   - "Room 1-16 at capacity per ADB Cl.2.43 — use alt route"
   - "BLOCKED per ADB Vol2 Section 2.33 — residential care home"

   Status: "BLOCKED" for OVER capacity, "ALTERNATE" for WARNING
   The ADB citation goes in the board directive and logs — NOT on the sign.

2. Call act_set_room_attractiveness for the affected room:
   - OVER capacity  to value 0.0 (fully discourage new occupants entering)
   - WARNING (80%+) to value 0.3 (strongly discourage but not fully block)

3. ALWAYS end every cycle by calling act_update_board with a directive in
   EXACTLY this format. No markdown. No headers. No bullet points.
   No asterisks. No emoji. Plain text only. Exactly 6 lines:

   CYCLE: Tick {n} - {IDLE / VIOLATION / FALSE POSITIVE}
   ROOMS: {label} ({current}/{max}) {PASS/FAIL}, {label} ({current}/{max}) {PASS/FAIL}
   ADB: {Section X.XX} - {one line description of clause}
   SIGNS: {SIGN_ID} -> {STATUS} | {SIGN_ID} -> {STATUS} | None
   ACTION: {one sentence - what was done}
   ESCALATE: {Yes - reason} or {No}

CRITICAL RULES:
1. NEVER use vague citations like "ADB Vol2 Table B1" - always give the
   specific section/clause/table number from the retrieved passage.
2. NEVER trigger building-wide actions. Only act on the specific room
   or corridor that has exceeded or is approaching its limit.
3. NEVER invent occupancy numbers. Always call sense_room or
   sense_building_state first. Always call check_compliance before acting.
4. If check_compliance returns PASS (the IFC model says the room is fine
   even though the graph flagged it), do NOT update any signs for that room.
   Explain in the board directive that the graph-level alert was a false
   positive against the IFC model's capacity definition.
5. Rooms with PASS status are the most important finding - they show the
   system is correctly filtering raw simulation data against authoritative
   IFC building information.
"""


# ── Tool dispatcher ───────────────────────────────────────────────────────────

def _call_tool(tool_name: str, tool_input: dict,
               snapshot: dict = None):
    """
    Dispatches tool calls directly to MCP server functions.
    Updates the Blender board after each meaningful tool call so
    Tom Cole can see the agent reasoning step by step in the UI.
    snapshot: current building state for board updates (optional)
    """
    from mcp_server.server import (
        sense_building_state, sense_room, advance_tick,
        get_regulations, check_compliance, get_adb_violation_context,
        act_update_sign, act_update_board, act_set_room_attractiveness,
        list_signs,
    )

    dispatch = {
        "sense_building_state"     : lambda i: sense_building_state(),
        "sense_room"               : lambda i: sense_room(
                                         i["room_label"]),
        "advance_tick"             : lambda i: advance_tick(),
        "get_regulations"          : lambda i: get_regulations(
                                         i["query"],
                                         i.get("n_results", 3)),
        "check_compliance"         : lambda i: check_compliance(
                                         i["room_long_name"],
                                         i["current_occupancy"]),
        "get_adb_violation_context": lambda i: get_adb_violation_context(
                                         i["room_label"],
                                         i["current"],
                                         i["max_occ"]),
        "act_update_sign"          : lambda i: act_update_sign(
                                         i["sign_id"],
                                         i["message"],
                                         i["status"],
                                         i.get("adb_ref", "")),
        "act_update_board"         : lambda i: act_update_board(
                                         i.get("agent_message", "")),
        "act_set_room_attractiveness": lambda i: act_set_room_attractiveness(
                                         i["room_label"],
                                         i["value"]),
        "list_signs"               : lambda i: list_signs(),
    }

    handler = dispatch.get(tool_name)
    if not handler:
        return {"error": f"Unknown tool: {tool_name}"}

    result = handler(tool_input)

    # ── Live board reasoning updates ──────────────────────────────────────
    # Update the Blender board after each meaningful tool call so the
    # agent's thought process is visible in the UI, not just the terminal.
    if snapshot is not None:
        try:
            from bim.board import update_board as _board

            if tool_name == "get_regulations":
                query = tool_input.get("query", "")[:55]
                _board(snapshot,
                       f"REASONING: Retrieving ADB regulation\n"
                       f"Query: {query}...\n"
                       f"Searching 1297 ADB Vol2 chunks")

            elif tool_name == "check_compliance":
                room   = tool_input.get("room_long_name", "")
                occ    = tool_input.get("current_occupancy", 0)
                status = "unknown"
                max_v  = "?"
                if isinstance(result, dict):
                    status = result.get("status", "unknown")
                    max_v  = result.get("max_occ", "?")
                _board(snapshot,
                       f"COMPLIANCE CHECK: {room}\n"
                       f"Occupancy: {occ} | IFC Max: {max_v}\n"
                       f"Result: {status.upper()}")

            elif tool_name == "act_update_sign":
                sign = tool_input.get("sign_id", "")
                msg  = tool_input.get("message", "")[:40]
                ref  = tool_input.get("adb_ref", "")[:35]
                _board(snapshot,
                       f"ACTION: Sign updated\n"
                       f"{sign} -> BLOCKED\n"
                       f"{msg}\n"
                       f"Ref: {ref}")

            elif tool_name == "act_set_room_attractiveness":
                room = tool_input.get("room_label", "")
                val  = tool_input.get("value", 0)
                _board(snapshot,
                       f"ACTION: Redirecting occupants\n"
                       f"Room {room} attractiveness -> {val}\n"
                       f"New occupants discouraged from entering")

            elif tool_name == "get_adb_violation_context":
                room    = tool_input.get("room_label", "")
                current = tool_input.get("current", 0)
                max_occ = tool_input.get("max_occ", 0)
                _board(snapshot,
                       f"ADB CONTEXT: Room {room}\n"
                       f"Occupancy {current}/{max_occ}\n"
                       f"Retrieving violation context...")

        except Exception:
            pass  # Board update failure never stops the agent cycle

    return result


# ── Single Sense-Reason-Act cycle ─────────────────────────────────────────────

def run_agent_cycle(client: anthropic.Anthropic,
                    trigger_message: str = None,
                    verbose: bool = True) -> dict:
    """
    Runs one complete Sense-Reason-Act cycle.
    The trigger_message describes the current simulation state.
    Returns directive, trace, latency, signs_updated, adb_cited.
    """
    start_time = time.time()

    user_msg = trigger_message or (
        "Perform your Sense-Reason-Act cycle. "
        "Sense the current building state, retrieve the relevant "
        "ADB care home clauses for any rooms approaching or exceeding "
        "safe occupancy, and act only on genuine violations. "
        "Cite specific ADB section or clause numbers in your board directive."
    )

    messages = [{"role": "user", "content": user_msg}]
    trace    = []

    # Get current snapshot for board reasoning updates
    current_snapshot = None
    try:
        from sensors.sensor_sim import get_sensor_snapshot
        current_snapshot = get_sensor_snapshot()
    except Exception:
        pass

    while True:
        response = client.messages.create(
            model      = "claude-haiku-4-5-20251001",
            max_tokens = 1000,
            system     = SYSTEM_PROMPT,
            tools      = TOOLS,
            messages   = messages,
        )

        if response.stop_reason == "end_turn":
            final_text = " ".join(
                b.text for b in response.content
                if hasattr(b, "text"))
            latency = round(time.time() - start_time, 3)

            signs_updated = sum(
                1 for t in trace if t["tool"] == "act_update_sign")

            adb_cited = any(
                keyword in final_text
                for keyword in [
                    "Section 2.", "Clause 2.", "Table 2.", "Table B1",
                    "Appendix D", "2.33", "2.43", "2.37", "ADB Vol2 p.",
                    "Section 3.", "Clause 3.", "3.5", "3.6"
                ]
            )

            return {
                "directive"      : final_text,
                "trace"          : trace,
                "latency_seconds": latency,
                "tool_count"     : len(trace),
                "signs_updated"  : signs_updated,
                "adb_cited"      : adb_cited,
            }

        if response.stop_reason == "tool_use":
            messages.append({
                "role"   : "assistant",
                "content": response.content
            })
            tool_results = []

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if verbose:
                    print(f"  -> {block.name}"
                          f"({json.dumps(block.input)[:120]})")

                call_start = time.time()
                result     = _call_tool(
                    block.name,
                    block.input,
                    snapshot=current_snapshot
                )
                call_time  = round(time.time() - call_start, 3)

                trace.append({
                    "tool"    : block.name,
                    "input"   : block.input,
                    "duration": call_time,
                })

                tool_results.append({
                    "type"       : "tool_result",
                    "tool_use_id": block.id,
                    "content"    : json.dumps(result, default=str)[:3000],
                })

            messages.append({
                "role"   : "user",
                "content": tool_results
            })


# ── Evaluation wrapper ────────────────────────────────────────────────────────

def run_evaluation_cycle(client: anthropic.Anthropic,
                          scenario_name: str,
                          trigger: str = None,
                          save_log: bool = True) -> dict:
    """
    Runs a labelled evaluation cycle and saves the result to logs/.
    Used by tests/test_scenarios.py for TS-01 through TS-05.
    """
    print(f"\n{'='*55}")
    print(f"  {scenario_name}")
    print(f"{'='*55}")
    print("Tool calls:")

    result = run_agent_cycle(client, trigger_message=trigger, verbose=True)

    print(f"\nLatency      : {result['latency_seconds']}s")
    print(f"Tool calls   : {result['tool_count']}")
    print(f"Signs updated: {result['signs_updated']}")
    print(f"ADB cited    : {result['adb_cited']}")
    print(f"\nDirective:\n{result['directive'][:600]}")

    if save_log:
        os.makedirs("logs", exist_ok=True)
        log_path = os.path.join("logs", f"{scenario_name}.json")
        with open(log_path, "w") as f:
            json.dump({
                "scenario" : scenario_name,
                "result"   : result,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            }, f, indent=2, default=str)
        print(f"\nLog saved: {log_path}")

    return result


# ── Standalone test ───────────────────────────────────────────────────────────

if __name__ == "__main__":
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    if not api_key:
        print("Set ANTHROPIC_API_KEY first:")
        print("  $env:ANTHROPIC_API_KEY = 'sk-ant-...'")
        sys.exit(1)

    client = anthropic.Anthropic()

    print("=" * 55)
    print("  PHASE 5 - OCCUPANCY MANAGEMENT AGENT")
    print("  (Care home ADB clause retrieval test)")
    print("=" * 55)
    print("\nRunning single Sense-Reason-Act cycle...")
    print("Tool calls:\n")

    result = run_agent_cycle(
        client,
        trigger_message=(
            "Perform your Sense-Reason-Act cycle. "
            "Check the current occupancy state of the care home. "
            "For any room approaching or exceeding its safe limit, "
            "retrieve the specific ADB care home clause that applies "
            "(e.g. Section 2.33, Clause 2.43, Table 2.1) and cite it "
            "precisely in your board directive. "
            "Do not trigger any evacuation - only redirect occupants "
            "via sign updates and attractiveness adjustments."
        ),
        verbose=True
    )

    print(f"\n{'='*55}")
    print(f"Latency      : {result['latency_seconds']}s")
    print(f"Tool calls   : {result['tool_count']}")
    print(f"Signs updated: {result['signs_updated']}")
    print(f"ADB cited    : {result['adb_cited']}")
    print(f"\nFinal directive:")
    print(result["directive"])