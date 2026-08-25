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
Note available_sign_ids in the response — every real corridor sign ID that
currently exists. ALWAYS pick a sign_id from this list later. Never guess a
naming pattern (floors other than F0 have no "_N"/"_S" suffix) — a guessed ID
fails silently and wastes a full round trip discovering the real one via
list_signs afterward.

If there are no alerts, call act_update_board with message
"All rooms within safe occupancy limits - no action required" and stop.

REASON:
If there is more than one alert this cycle, batch your tool calls — use
sense_rooms (all room labels in ONE call) instead of several sense_room
calls, and check_compliance_batch (all checks in ONE call) instead of
several check_compliance calls. Each separate call is a full model round
trip; three separate sense_room calls for three alerted rooms triples
that cost for no benefit over one sense_rooms call covering all three.
Still reason about and prioritise each room individually (OVER before
WARNING) — only the TOOL CALLS should batch, not the reasoning.

Step 1: Call sense_room (single room) or sense_rooms (multiple rooms) to
        get full details including room type, current count, max count,
        and the IFC long name for every alerted room.

Step 2: Call get_regulations ONCE per cycle with a query SPECIFICALLY
        about care home occupancy for the room type(s) involved. Use
        queries like:
        - "residential care home bedroom occupancy ADB Section 2.43"
        - "residential care home corridor escape route Section 2.33"
        - "Purpose Group 2a travel distance Table 2.1 care home"
        Do NOT use generic queries. Always reference the care home context.
        Only call it a SECOND time if this cycle genuinely involves two
        clearly different room types needing two different clauses
        (e.g. a bedroom AND a corridor in the same cycle) — each
        get_regulations call is a full round trip, so do not make a
        second, speculative, or near-duplicate query when one
        well-chosen query already covers every room in this cycle.

Step 3: Call check_compliance (single room) or check_compliance_batch
        (multiple rooms) using the IFC long name (e.g. "Bedroom", "Lounge")
        and current occupancy for every alerted room, to get the
        deterministic ADB result — PASS, WARNING, or FAIL.

Step 4: Identify the SPECIFIC clause that justifies your action, e.g.:
        "ADB Vol2 Section 2.33 - residential care home general provisions"
        "ADB Vol2 Clause 2.43 - bedroom occupancy standard"
        "ADB Vol2 Table 2.1 - maximum travel distance in escape route"
        You MUST cite a specific section or clause, never just "ADB Vol2".

ACT:
For each genuine violation (check_compliance / check_compliance_batch
returns FAIL or WARNING):

1. Call act_update_sign for the corridor sign serving that floor:
   - sign_id MUST be one of the exact IDs from available_sign_ids — see SENSE
   - Message must state WHICH room is affected and WHY (citing ADB clause)
   - Example: "Room 1-16 (Bedroom) at capacity per ADB Cl.2.43 - use alt route"
   - Status: "BLOCKED" for OVER capacity, "ALTERNATE" for WARNING

2. Call act_set_room_attractiveness for the affected room:
   - OVER capacity  to value 0.0 (fully discourage new occupants entering)
   - WARNING (80%+) to value 0.3 (strongly discourage but not fully block)

3. ALWAYS end every cycle by calling act_update_board with a directive in
   EXACTLY this format. No markdown. No headers. No bullet points.
   No asterisks. No emoji. Plain text only. Exactly 6 lines:

   CYCLE: Tick {n} - {IDLE / VIOLATION / PRE-EMPTIVE / FALSE POSITIVE}
   ROOMS: {label} ({current}/{max}) {PASS/WARNING/FAIL}, {label} ({current}/{max}) {PASS/WARNING/FAIL}
   ADB: {Section X.XX} - {one line description of clause}
   SIGNS: {SIGN_ID} -> {STATUS} | {SIGN_ID} -> {STATUS} | None
   ACTION: {one sentence - what was done}
   ESCALATE: {Yes - reason} or {No}

   Choosing the CYCLE label — check_compliance/check_compliance_batch's
   actual returned status decides this, not a guess at overall severity:
     IDLE           : sense_building_state returned no alerts at all.
     VIOLATION       : at least one room's check returned FAIL.
     PRE-EMPTIVE     : no room returned FAIL, but at least one returned
                       WARNING — this IS a genuine finding requiring
                       action (Step 2's attractiveness 0.3, an
                       ALTERNATE sign), not a false positive. A room
                       genuinely at 80-100% of its real ADB capacity is
                       real, actionable information, whether or not it
                       has yet crossed into FAIL.
     FALSE POSITIVE  : every alerted room's check returned PASS — the
                       graph's alert did not survive the IFC model's
                       own capacity definition at all, not even to
                       WARNING level.
   Do not default to FALSE POSITIVE just because no room reached FAIL —
   check each room's actual returned status; WARNING is a real result,
   not a softer way of saying PASS.

CRITICAL RULES:
1. NEVER use vague citations like "ADB Vol2 Table B1" - always give the
   specific section/clause/table number from the retrieved passage.
2. NEVER trigger building-wide actions. Only act on the specific room
   or corridor that has exceeded or is approaching its limit.
3. NEVER invent occupancy numbers. Always call sense_room/sense_rooms or
   sense_building_state first. Always call check_compliance/
   check_compliance_batch before acting.
4. If check_compliance returns PASS (the IFC model says the room is fine
   even though the graph flagged it), do NOT update any signs for that room.
   Explain in the board directive that the graph-level alert was a false
   positive against the IFC model's capacity definition. If it returns
   WARNING, this is NOT a false positive — take the Step 2 pre-emptive
   action and use the PRE-EMPTIVE cycle label, even if no other room in
   the same cycle reached FAIL.
5. A room's ACTUAL check_compliance status (PASS, WARNING, or FAIL) is
   the most important finding — report it exactly as returned. Do not
   round WARNING down to PASS, and do not let a cycle containing a mix
   of statuses collapse into a single label that only reflects one of them.
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
        sense_building_state, sense_room, sense_rooms, advance_tick,
        get_regulations, check_compliance, check_compliance_batch,
        get_adb_violation_context,
        act_update_sign, act_update_board, act_set_room_attractiveness,
        list_signs,
    )

    dispatch = {
        "sense_building_state"     : lambda i: sense_building_state(),
        "sense_room"               : lambda i: sense_room(
                                         i["room_label"]),
        "sense_rooms"              : lambda i: sense_rooms(
                                         i["room_labels"]),
        "advance_tick"             : lambda i: advance_tick(),
        "get_regulations"          : lambda i: get_regulations(
                                         i["query"],
                                         i.get("n_results", 3)),
        "check_compliance"         : lambda i: check_compliance(
                                         i["room_long_name"],
                                         i["current_occupancy"]),
        "check_compliance_batch"   : lambda i: check_compliance_batch(
                                         i["checks"]),
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
                       f"Searching ADB Vol2 knowledge base")

            elif tool_name == "check_compliance":
                room   = tool_input.get("room_long_name", "")
                occ    = tool_input.get("current_occupancy", 0)
                status = "unknown"
                max_v  = "?"
                if isinstance(result, dict):
                    status = result.get("status", "unknown")
                    # Fix applied: check_occupancy_compliance() returns
                    # its capacity value under the key "max", not
                    # "max_occ" — this line was looking for a key that
                    # never existed in the dict, so it fell through to
                    # the "?" default on every single call, regardless
                    # of the room's real capacity. Confirmed directly
                    # in a real live_agent_runner.py session: "IFC Max:
                    # ?" appeared on every compliance-check board
                    # update without exception.
                    max_v  = result.get("max", "?")
                _board(snapshot,
                       f"COMPLIANCE CHECK: {room}\n"
                       f"Occupancy: {occ} | IFC Max: {max_v}\n"
                       f"Result: {status.upper()}")

            elif tool_name == "check_compliance_batch":
                checks = tool_input.get("checks", [])
                lines  = [f"COMPLIANCE CHECK ({len(checks)} rooms):"]
                if isinstance(result, list):
                    for c, r in zip(checks, result):
                        room   = c.get("room_long_name", "")
                        status = r.get("status", "unknown") if isinstance(r, dict) else "unknown"
                        lines.append(f"  {room}: {status.upper()}")
                _board(snapshot, "\n".join(lines[:5]))

            elif tool_name == "act_update_sign":
                sign   = tool_input.get("sign_id", "")
                msg    = tool_input.get("message", "")[:40]
                ref    = tool_input.get("adb_ref", "")[:35]
                # Was hardcoded "BLOCKED" regardless of actual status —
                # a WARNING-severity ALTERNATE action showed as a full
                # block on this live reasoning display. Now reflects
                # the real status the tool call was actually given.
                status = tool_input.get("status", "BLOCKED")
                _board(snapshot,
                       f"ACTION: Sign updated\n"
                       f"{sign} -> {status}\n"
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

            # Fix applied: this counted every act_update_sign CALL,
            # not every successful one — a call with a bad sign_id
            # (confirmed in a real run: the agent tried
            # "SIGN_F2_CORRIDOR_N", which doesn't exist; only F0 has
            # an _N suffix) fails silently inside bim.signage.
            # update_sign(), returning {"error": ...} with zero real
            # effect, but still counted toward this total. trace now
            # stores each call's actual result, so a failed call can
            # be told apart from a genuine one — this number, and
            # session_log["total_actions"] which it feeds into, now
            # reflect real sign changes only.
            signs_updated = sum(
                1 for t in trace
                if t["tool"] == "act_update_sign"
                and isinstance(t.get("result"), dict)
                and "error" not in t["result"]
            )

            # The actual structured board directive (CYCLE/ROOMS/ADB/
            # SIGNS/ACTION/ESCALATE) is posted as the agent_message
            # argument to act_update_board, per the system prompt —
            # not necessarily repeated in the model's final end_turn
            # text, which is often just a brief wrap-up after the tool
            # call has already fired. Using final_text alone meant
            # both the returned "directive" and the adb_cited check
            # could reflect that generic wrap-up instead of the real
            # directive — and live_agent_runner.py posting that wrong
            # text to the board immediately after run_agent_cycle()
            # returns would visibly overwrite the correct directive
            # act_update_board's own live dispatch had just written
            # moments earlier. Prefer the last act_update_board call's
            # actual content; fall back to final_text only if the
            # agent never called it.
            board_directive = None
            for t in reversed(trace):
                if t["tool"] == "act_update_board":
                    board_directive = t["input"].get("agent_message", "")
                    break

            directive_text = board_directive or final_text

            adb_cited = any(
                keyword in directive_text
                for keyword in [
                    "Section 2.", "Clause 2.", "Table 2.", "Table B1",
                    "Appendix D", "2.33", "2.43", "2.37", "ADB Vol2 p.",
                    "Section 3.", "Clause 3.", "3.5", "3.6"
                ]
            )

            return {
                "directive"      : directive_text,
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
                    "result"  : result,
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