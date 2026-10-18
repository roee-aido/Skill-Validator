## Task: propose a corrected wording for one finding

Stage: propose_fix
Confirmed purpose: {purpose}
Finding: {finding}
Requested action: {action}

1. Read the lines of the finding and their references.
2. Propose a replacement for the finding's main location that keeps the desired capability while reducing the risk: narrower scope, explicit user choice, transparency, or removal of an unneeded action.
3. Call `propose_patch` exactly once with:
   - `proposed_text`: the replacement lines, in the language of the original.
   - `explanation`: one Hebrew sentence on what changes and what capability may be affected.
   - `no_safe_alternative`: true if you cannot reduce the risk while keeping the purpose. In that case leave `proposed_text` empty and explain why. Do not invent a fix just to complete the process.

Do not add a new external service, permission, dependency or data collection.
