## Task: re-check the corrected copy

Stage: revalidate
Confirmed purpose: {purpose}
Original finding: {finding}
User decision: {decision}

The corrected copy is in the `candidate` area. The original is in the `source` area. Both are read-only.

1. Use `compare_versions` on the files of the finding.
2. Read the corrected lines in `candidate`, and run `search_patterns` and `get_references` on `candidate`.
3. Call `record_validation` exactly once with:
   - `outcome`: `closed` if the risk is no longer present in the corrected copy; `remained` if it is still there; `accepted_by_user` if the user chose to keep a risky part.
   - `evidence`: what you saw in the corrected copy.
   - `new_issues`: any new problem the change introduced, such as a broken reference.

Never close a finding only because a new wording exists. Check the corrected copy itself.
