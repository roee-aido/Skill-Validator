"""Hard limits enforced in code. Configuration may narrow them, never widen them."""

STAGES = ("detect_purpose", "analyze", "propose_fix", "revalidate")

AREAS = ("source", "candidate")

# The closed list of agent tools (Architecture.md section 6).
TOOL_NAMES = (
    "list_files",
    "read_file",
    "search_patterns",
    "get_references",
    "compare_versions",
    "record_purpose",
    "record_finding",
    "propose_patch",
    "record_validation",
)

# Maximum tool set per stage. A stage config must be a subset of this.
STAGE_TOOL_CEILING = {
    "detect_purpose": {"list_files", "read_file", "record_purpose"},
    "analyze": {"list_files", "read_file", "search_patterns", "get_references", "record_finding"},
    "propose_fix": {"read_file", "get_references", "propose_patch"},
    "revalidate": {
        "list_files",
        "read_file",
        "search_patterns",
        "get_references",
        "compare_versions",
        "record_validation",
    },
}

# Maximum readable areas per stage. The candidate area is readable only while re-checking.
STAGE_AREA_CEILING = {
    "detect_purpose": {"source"},
    "analyze": {"source"},
    "propose_fix": {"source"},
    "revalidate": {"source", "candidate"},
}

# D8: fixed resume behaviour.
RESUME_REQUIRED_REMAINING = "limits.per_task"
RESUME_ON_INSUFFICIENT = "require_user_ceiling_approval"
