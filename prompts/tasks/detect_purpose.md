## Task: detect the purpose of the skill

Stage: detect_purpose
User's optional description of the intended use: {usage_description}

1. Use `list_files` to see the package structure.
2. Read SKILL.md first, then any file it relies on to explain what the skill does.
3. Call `record_purpose` exactly once with:
   - `purpose`: one or two sentences in Hebrew describing what the skill is meant to do, based on the package itself.
   - `evidence`: the paths and lines you relied on.
   - `clarification_question`: a short question in Hebrew if the purpose is unclear, or if it conflicts with the user's description. Otherwise empty.

Describe the declared purpose. Do not judge safety in this task.
