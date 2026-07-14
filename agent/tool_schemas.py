# agent/tool_schemas.py
# Anthropic API tool definitions for the 9 MCP server functions.
# These tell Claude what tools are available and how to call them.

TOOLS = [
    {
        "name": "sense_building_state",
        "description": (
            "Returns the full current building state: occupancy per room and floor, "
            "compliance alerts (WARNING at 80% capacity, OVER at 100%), active events, "
            "and a summary. Call this FIRST in every Sense-Reason-Act cycle."
        ),
        "input_schema": {
            "type": "object", "properties": {}, "required": []
        }
    },
    {
        "name": "sense_room",
        "description": (
            "Returns detailed status for a specific room including current/max occupancy, "
            "ratio, severity, attractiveness, sign_blocked status, and exit path."
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
            "Always call this for occupancy decisions — never estimate thresholds yourself."
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
            "This is the primary bidirectional write-back tool."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sign_id": {
                    "type": "string",
                    "description": "Sign name e.g. 'SIGN_F0_CORRIDOR_N'"
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
                    "description": "ADB clause justifying this decision"
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