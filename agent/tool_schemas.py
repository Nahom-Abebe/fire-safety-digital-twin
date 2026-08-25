# agent/tool_schemas.py
# Anthropic API tool definitions for the MCP server functions.
# These tell Claude what tools are available and how to call them.
#
# Fix applied: added sense_rooms and check_compliance_batch — batched
# versions of sense_room and check_compliance. A real session log
# showed a 3-room tick making 3 separate sense_room round trips and 3
# separate check_compliance round trips back to back, each a full
# model turn, directly inflating cycle latency (confirmed: one tick
# took 28.394s). The system prompt now instructs the agent to prefer
# these when more than one room needs checking in the same cycle.

TOOLS = [
    {
        "name": "sense_building_state",
        "description": (
            "Returns the full current building state: occupancy per room and floor, "
            "compliance alerts (WARNING at 80% capacity, OVER at 100%), active events, "
            "a summary, and available_sign_ids (every real corridor sign ID that "
            "currently exists — always pick from this list, never guess a naming "
            "pattern). Call this FIRST in every Sense-Reason-Act cycle."
        ),
        "input_schema": {
            "type": "object", "properties": {}, "required": []
        }
    },
    {
        "name": "sense_room",
        "description": (
            "Returns detailed status for a specific room including current/max occupancy, "
            "ratio, severity, attractiveness, sign_blocked status, and exit path. "
            "If checking more than one room this cycle, use sense_rooms (plural) instead — "
            "one round trip for every room, not one round trip each."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "room_label": {
                    "type": "string",
                    "description": "Graph label e.g. '0-1', '0-A', '2-15'"
                }
            },
            "required": ["room_label"]
        }
    },
    {
        "name": "sense_rooms",
        "description": (
            "Returns detailed status for MULTIPLE rooms in a single call — the batched "
            "equivalent of calling sense_room once per room. ALWAYS use this instead of "
            "several sense_room calls whenever more than one room needs checking in the "
            "same cycle (e.g. several alerts from the same sense_building_state call)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "room_labels": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Graph labels to check, e.g. ['2-7', '2-9', '3-3']"
                }
            },
            "required": ["room_labels"]
        }
    },
    {
        "name": "advance_tick",
        "description": (
            "Advances the simulation by one probabilistic random-walk tick. "
            "Updates Blender markers, Psets, and board automatically. "
            "Returns updated building snapshot."
        ),
        "input_schema": {
            "type": "object", "properties": {}, "required": []
        }
    },
    {
        "name": "get_regulations",
        "description": (
            "Retrieves relevant UK Approved Document B passages from ChromaDB via RAG. "
            "ALWAYS call this before stating any numeric threshold (distances, capacities). "
            "Never estimate regulatory values — retrieve them."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Natural language regulatory question"
                },
                "n_results": {
                    "type": "integer",
                    "description": "Number of passages to return (default 3)",
                    "default": 3
                }
            },
            "required": ["query"]
        }
    },
    {
        "name": "check_compliance",
        "description": (
            "DETERMINISTIC ADB compliance check using the IFC model's max_occ value. "
            "Always call this for occupancy decisions — never estimate thresholds yourself. "
            "If checking more than one room this cycle, use check_compliance_batch instead — "
            "one round trip for every room, not one round trip each."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "room_long_name": {
                    "type": "string",
                    "description": "IFC long name e.g. 'Lounge', 'Bedroom'"
                },
                "current_occupancy": {
                    "type": "integer",
                    "description": "Current number of occupants"
                }
            },
            "required": ["room_long_name", "current_occupancy"]
        }
    },
    {
        "name": "check_compliance_batch",
        "description": (
            "Runs check_compliance for MULTIPLE rooms in a single call — the batched "
            "equivalent of calling check_compliance once per room. ALWAYS use this instead "
            "of several check_compliance calls whenever more than one room needs a "
            "compliance check in the same cycle."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "checks": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "room_long_name": {
                                "type": "string",
                                "description": "IFC long name e.g. 'Lounge', 'Bedroom'"
                            },
                            "current_occupancy": {
                                "type": "integer",
                                "description": "Current number of occupants"
                            }
                        },
                        "required": ["room_long_name", "current_occupancy"]
                    },
                    "description": (
                        "One entry per room to check, e.g. "
                        "[{'room_long_name': 'Bedroom', 'current_occupancy': 3}, "
                        "{'room_long_name': 'Bedroom', 'current_occupancy': 4}]"
                    )
                }
            },
            "required": ["checks"]
        }
    },
    {
        "name": "get_adb_violation_context",
        "description": (
            "Returns ADB passages most relevant to a specific occupancy violation. "
            "Use when you need a specific ADB citation to justify a sign update."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "room_label": {
                    "type": "string",
                    "description": "Graph label of the affected room"
                },
                "current": {
                    "type": "integer",
                    "description": "Current occupant count"
                },
                "max_occ": {
                    "type": "integer",
                    "description": "Maximum permitted occupancy"
                }
            },
            "required": ["room_label", "current", "max_occ"]
        }
    },
    {
        "name": "act_update_sign",
        "description": (
            "Updates an evacuation sign in the IFC model AND feeds the new status "
            "back into the simulation (sign_blocked changes movement probabilities). "
            "This is the primary bidirectional write-back tool. sign_id MUST be one of "
            "the exact IDs from sense_building_state's available_sign_ids list — do not "
            "guess a naming pattern, it will fail silently."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sign_id": {
                    "type": "string",
                    "description": "Sign name — must be one of available_sign_ids, e.g. 'SIGN_F0_CORRIDOR_N'"
                },
                "message": {
                    "type": "string",
                    "description": "Message to display on the sign"
                },
                "status": {
                    "type": "string",
                    "enum": ["ACTIVE", "BLOCKED", "ALTERNATE"],
                    "description": "BLOCKED reduces movement toward that zone"
                },
                "adb_ref": {
                    "type": "string",
                    "description": "ADB clause justifying this decision (for traceability)"
                }
            },
            "required": ["sign_id", "message", "status"]
        }
    },
    {
        "name": "act_update_board",
        "description": (
            "Updates the building manager's dashboard board in Blender. "
            "ALWAYS call this at the end of every cycle — even if no action needed. "
            "Use 'System idle — no violations detected' when all is clear."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "agent_message": {
                    "type": "string",
                    "description": "Directive summary citing ADB section, or 'System idle'"
                }
            },
            "required": []
        }
    },
    {
        "name": "act_set_room_attractiveness",
        "description": (
            "Sets room attractiveness (0.0–2.0, default 1.0). "
            "Lower values discourage movement toward this room. "
            "Use to redistribute occupants away from high-density areas "
            "without blocking physical routes."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "room_label": {
                    "type": "string",
                    "description": "Graph label e.g. '0-20'"
                },
                "value": {
                    "type": "number",
                    "description": "0.0=avoid, 1.0=neutral, 2.0=attract"
                }
            },
            "required": ["room_label", "value"]
        }
    },
    {
        "name": "list_signs",
        "description": "Returns all evacuation signs and their current status.",
        "input_schema": {
            "type": "object", "properties": {}, "required": []
        }
    },
]