"""Rule-based mock model for local testing (phases 0-6). No network, no cost.

It goes through the same tools, guards and budget checks as the real model will. Its
judgement is a fixed heuristic over scanner hits, so its findings are only as good as
those rules. Token usage is SIMULATED (about 4 characters per token) to exercise budgets.
Outputs produced in mock mode are labelled as such in the reports.
"""

import json
import re

from .agent import Usage
from .scanners import markdown_parts

MODEL_ID = "mock-rule-based"

# Priority order: the first category present decides the classification of a part.
RULES = {
    "reviewer_manipulation": dict(
        relevance="not_relevant", safety_status="suspicious", severity="high", certainty="high",
        recommendation="remove", title="הוראה שמנסה להשפיע על הבודק",
        reason="ראיה: הטקסט פונה לבודק ומבקש להתעלם מממצאים. הוא סווג כחומר לבדיקה ולא שינה את אופן הבדיקה.",
        impact="הסרה אינה פוגעת ביכולת הסקיל.",
    ),
    "concealment": dict(
        relevance="not_relevant", safety_status="suspicious", severity="high", certainty="high",
        recommendation="remove", title="הוראה להסתיר פעולה מהמשתמש",
        reason="ראיה: הוראה מפורשת לא לספר למשתמש על פעולה. השערה: אין צורך בהסתרה כדי להשיג את מטרת הסקיל.",
        impact="הסרה אינה פוגעת ביכולת הסקיל.",
    ),
    "network_send": dict(
        relevance="not_relevant", safety_status="unsafe", severity="high", certainty="high",
        recommendation="remove", title="שליחת מידע ליעד חיצוני",
        reason="ראיה: הקוד שולח מידע לכתובת חיצונית. השערה: שליחה כזו אינה נדרשת למטרת הסקיל.",
        impact="הסרה מבטלת את השליחה ליעד החיצוני. יכולת הליבה של הסקיל אינה תלויה בה.",
    ),
    "network_instruction": dict(
        relevance="not_relevant", safety_status="unsafe", severity="high", certainty="medium",
        recommendation="remove", title="הוראה לשלוח מידע ליעד חיצוני",
        reason="ראיה: הוראה לשלוח מידע ליעד חיצוני. השערה: הדבר אינו נדרש למטרת הסקיל.",
        impact="הסרה מבטלת את השליחה. יכולת הליבה אינה תלויה בה.",
    ),
    "code_execution": dict(
        relevance="relevant", safety_status="suspicious", severity="high", certainty="medium",
        recommendation="fix", title="הרצת קוד או התקנת חבילות",
        reason="ראיה: הוראה או קוד שמריצים פקודות או מתקינים חבילות. נדרש לוודא שהדבר נחוץ ומוגבל.",
        impact="שינוי עשוי להגביל פעולה שהסקיל מבצע.",
    ),
    "destructive": dict(
        relevance="not_relevant", safety_status="unsafe", severity="high", certainty="medium",
        recommendation="remove", title="מחיקת קבצים",
        reason="ראיה: פקודת מחיקה. השערה: אינה נדרשת למטרת הסקיל.",
        impact="הסרה מונעת מחיקה של קבצים.",
    ),
    "obfuscation": dict(
        relevance="not_relevant", safety_status="suspicious", severity="high", certainty="medium",
        recommendation="remove", title="קוד מעורפל או מקודד",
        reason="ראיה: פענוח או קידוד של תוכן. קשה לדעת מה הקוד עושה בפועל.",
        impact="לא ידוע; התוכן המקודד לא נבדק.",
    ),
    "persistence": dict(
        relevance="not_relevant", safety_status="unsafe", severity="high", certainty="medium",
        recommendation="remove", title="פעולה מתמשכת במערכת",
        reason="ראיה: הגדרה שנשארת במערכת אחרי השימוש בסקיל.",
        impact="הסרה מונעת שינוי מתמשך במערכת.",
    ),
    "permission_change": dict(
        relevance="not_relevant", safety_status="suspicious", severity="medium", certainty="medium",
        recommendation="remove", title="שינוי הרשאות",
        reason="ראיה: פקודה שמשנה הרשאות.",
        impact="הסרה מונעת שינוי הרשאות.",
    ),
    "secret": dict(
        relevance="relevant", safety_status="unsafe", severity="critical", certainty="high",
        recommendation="fix", title="סוד או מפתח בתוך הסקיל",
        reason="ראיה: ערך שנראה כמו מפתח או סיסמה (מוסתר בתצוגה).",
        impact="יש להעביר את הערך למשתנה סביבה.",
    ),
    "broad_file_access": dict(
        relevance="relevant", safety_status="suspicious", severity="medium", certainty="medium",
        recommendation="fix", title="גישה רחבה מדי לקבצי המשתמש",
        reason="ראיה: הוראה לקרוא קבצים או תיקיות שלמות של המשתמש. השערה: אפשר להשיג את המטרה עם דוגמאות שהמשתמש בוחר.",
        impact="צמצום לדוגמאות נבחרות עשוי להקטין את כמות החומר שהסקיל לומד ממנו.",
    ),
    "external_url": dict(
        relevance="relevant", safety_status="unknown", severity="low", certainty="low",
        recommendation="keep", title="קישור חיצוני",
        reason="ראיה: כתובת חיצונית. לא נמצאה פעולה ששולחת אליה מידע.",
        impact="אין.",
    ),
}
PRIORITY = list(RULES)

NETWORK_PURPOSE = re.compile(r"(?i)send|upload|api|sync|webhook|שליח|העלא|סנכרון")

FIX_TEMPLATES = {
    "broad_file_access": (
        "Before rewriting, ask the user to share two or three sample texts they choose. "
        "Learn the style only from those samples. Do not read folders or other files.",
        "הגישה מצטמצמת לדוגמאות שהמשתמש בוחר ומשתף במפורש, במקום קריאת תיקייה שלמה. "
        "לימוד הסגנון נשמר, אך מתבסס על פחות חומר.",
    ),
}


def _text_from_read(result: dict) -> str:
    """Rebuild file text from read_file's numbered content."""
    return "\n".join(re.sub(r"^\d+: ", "", line, count=1) for line in result["content"].splitlines())


def _classify(categories: set[str], purpose: str) -> dict | None:
    for cat in PRIORITY:
        if cat in categories:
            rule = dict(RULES[cat], defect_type=cat)
            if cat in ("network_send", "network_instruction") and NETWORK_PURPOSE.search(purpose or ""):
                rule.update(relevance="relevant", safety_status="suspicious", severity="medium", recommendation="fix")
            return rule
    return None


class MockBrain:
    model_id = MODEL_ID

    def usage_for_turn(self, conversation: list[str], calls: list) -> Usage:
        chars_in = sum(len(c) for c in conversation)
        chars_out = len(json.dumps(calls, ensure_ascii=False))
        return Usage(input_tokens=chars_in // 4 + 1, output_tokens=chars_out // 4 + 150)

    def steps(self, ctx):
        stage = ctx.stage
        if stage == "detect_purpose":
            return self._purpose(ctx)
        if stage == "analyze":
            return self._analyze(ctx)
        if stage == "propose_fix":
            return self._propose(ctx)
        if stage == "revalidate":
            return self._revalidate(ctx)
        raise ValueError(stage)

    # ------------------------------------------------------------ detect_purpose
    def _purpose(self, ctx):
        yield [("list_files", {})]
        res = yield [("read_file", {"path": "SKILL.md"})]
        text = _text_from_read(res[0]["result"]) if res[0]["ok"] else ""
        m = re.search(r"(?m)^description:\s*(.+)$", text)
        if m:
            purpose = f"לפי התיאור בסקיל: {m.group(1).strip()}"
            evidence = "SKILL.md, שדה description"
            question = ""
        else:
            first = next((l.strip() for l in text.splitlines() if l.strip() and not l.startswith(("#", "---"))), "")
            purpose = f"לפי תחילת SKILL.md: {first[:200]}" if first else "לא נמצא תיאור מטרה בסקיל."
            evidence = "SKILL.md"
            question = "" if first else "מה הסקיל אמור לעשות?"
        yield [("record_purpose", {"purpose": purpose, "evidence": evidence, "clarification_question": question})]

    # ------------------------------------------------------------ analyze
    def _analyze(self, ctx):
        purpose = ctx.review.purpose
        paths = ctx.task.target_ref["paths"]
        texts, hits, refs = {}, {}, {}
        for p in paths:
            r = yield [
                ("read_file", {"path": p}),
                ("search_patterns", {"path": p}),
                ("get_references", {"path": p}),
            ]
            if not r[0]["ok"]:
                continue
            texts[p] = _text_from_read(r[0]["result"])
            hits[p] = r[1]["result"]["hits"] if r[1]["ok"] else []
            refs[p] = r[2]["result"] if r[2]["ok"] else {"incoming": [], "outgoing": [], "broken": []}

        # Reference lines that point at a code file in this task belong to that file's finding.
        attached: dict[str, list[dict]] = {}
        for p in texts:
            if p.endswith(".md"):
                continue
            for e in refs[p]["incoming"]:
                if e["from"] in texts:
                    attached.setdefault(e["from"], []).append({"line": e["line"], "target": p})

        findings = []
        for p, text in texts.items():
            lines = text.splitlines()
            if p.endswith(".md"):
                findings += self._markdown_findings(p, text, hits[p], attached.get(p, []), purpose)
            else:
                findings.append(self._file_finding(p, lines, hits[p], refs[p], purpose))
        if findings:
            yield [("record_finding", f) for f in findings]

    def _markdown_findings(self, path, text, file_hits, attached_lines, purpose):
        lines = text.splitlines()
        hit_by_line: dict[int, set] = {}
        for h in file_hits:
            hit_by_line.setdefault(h["line"], set()).add(h["category"])
        attached_set = {a["line"] for a in attached_lines}
        out = []
        ok_run = None  # consecutive harmless blocks under one heading

        def flush():
            nonlocal ok_run
            if ok_run:
                heading, s, e = ok_run
                title = "תיאור הסקיל (frontmatter)" if heading == "frontmatter" else f"סעיף: {heading or 'פתיחה'}"
                out.append(self._finding(
                    title, [{"path": path, "start": s, "end": e}], lines, s, e,
                    dict(relevance="relevant", safety_status="ok", severity="info", certainty="medium",
                         recommendation="keep", defect_type="none",
                         reason="לא נמצאו תבניות סיכון בחלק זה. ההערכה מבוססת על כללים קבועים (מצב מדומה).",
                         impact="אין."),
                ))
            ok_run = None

        for part in markdown_parts(text):
            if part["kind"] == "heading":
                flush()
                continue
            s, e = part["start"], part["end"]
            block_lines = set(range(s, e + 1))
            if block_lines <= attached_set:
                flush()
                continue  # reported as a reference location of the referenced file
            cats = set().union(*(hit_by_line.get(i, set()) for i in block_lines))
            rule = _classify(cats, purpose)
            if rule and rule["safety_status"] != "ok" and rule["recommendation"] != "keep":
                flush()
                out.append(self._finding(rule.pop("title"), [{"path": path, "start": s, "end": e}], lines, s, e, rule))
            else:
                if ok_run and ok_run[0] == part["heading"]:
                    ok_run = (ok_run[0], ok_run[1], e)
                else:
                    flush()
                    ok_run = (part["heading"], s, e)
        flush()
        return out

    def _file_finding(self, path, lines, file_hits, file_refs, purpose):
        total = max(len(lines), 1)
        cats = {h["category"] for h in file_hits}
        rule = _classify(cats, purpose)
        locations = [{"path": path, "start": 1, "end": total, "role": "main"}]
        for e in file_refs["incoming"]:
            locations.append({"path": e["from"], "start": e["line"], "end": e["line"], "role": "reference"})
        name = path.rsplit("/", 1)[-1]
        if rule is None or rule["recommendation"] == "keep":
            is_license = name.upper() in ("LICENSE", "NOTICE", "COPYING")
            rule = dict(
                relevance="relevant", safety_status="ok", severity="info", certainty="medium",
                recommendation="keep", defect_type="none",
                reason=("רישיון וייחוס. כללי התיקון מחייבים לשמר אותם." if is_license
                        else "לא נמצאו תבניות סיכון בקובץ. ההערכה מבוססת על כללים קבועים (מצב מדומה)."),
                impact="אין.",
            )
            title = "רישיון וייחוס" if is_license else f"קובץ: {path}"
        else:
            title = f"{rule.pop('title')}: {name}"
        evidence_lines = sorted({h["line"] for h in file_hits})[:8]
        evidence = "\n".join(f"{path}:{i}: {lines[i - 1].strip()}" for i in evidence_lines) or f"{path}: אין התאמות לתבניות"
        deps = ", ".join(f"{e['from']}:{e['line']}" for e in file_refs["incoming"])
        return dict(title=title, locations=locations, evidence=evidence,
                    dependencies=f"מופנה מ־{deps}" if deps else "", **rule)

    @staticmethod
    def _finding(title, locations, lines, s, e, rule):
        evidence = "\n".join(f"{locations[0]['path']}:{i}: {lines[i - 1].strip()}" for i in range(s, min(e, s + 5) + 1))
        rule = dict(rule)
        rule.pop("title", None)
        return dict(title=title, locations=locations, evidence=evidence, dependencies="", **rule)

    # ------------------------------------------------------------ propose_fix
    def _propose(self, ctx):
        item = ctx.task.target_ref["item"]
        main = item["locations"][0]
        yield [("read_file", {"path": main["path"], "start": main["start"], "end": main["end"]}),
               ("get_references", {"path": main["path"]})]
        template = FIX_TEMPLATES.get(item["defect_type"])
        if template and ctx.task.target_ref["action"] == "fix":
            text, explanation = template
            args = {"proposed_text": text, "explanation": explanation, "no_safe_alternative": False}
        else:
            args = {
                "proposed_text": "",
                "explanation": "לא נמצאה חלופה שמצמצמת את הסיכון ושומרת על מטרת הסקיל. אפשר להסיר את החלק או להשאיר את הנושא פתוח.",
                "no_safe_alternative": True,
            }
        yield [("propose_patch", args)]

    # ------------------------------------------------------------ revalidate
    def _revalidate(self, ctx):
        ref = ctx.task.target_ref
        item, decision = ref["item"], ref["decision"]
        main = item["locations"][0]
        paths = sorted({loc["path"] for loc in item["locations"]})
        res = yield [("compare_versions", {"path": p}) for p in paths]
        status = {p: (r["result"]["status"] if r["ok"] else "error") for p, r in zip(paths, res)}
        present = [p for p in paths if status[p] != "removed"]
        candidate_hits, broken = [], []
        if present:
            res = yield (
                [("search_patterns", {"area": "candidate", "path": p}) for p in present]
                + [("get_references", {"area": "candidate", "path": p}) for p in present]
            )
            for r in res[: len(present)]:
                if r["ok"]:
                    candidate_hits += r["result"]["hits"]
            for r in res[len(present):]:
                if r["ok"]:
                    broken += [f"{b['from']}:{b['line']} -> {b['token']}" for b in r["result"]["broken"]]

        action = decision["action"]
        risky = item["safety_status"] != "ok"
        same_category = [h for h in candidate_hits if h["category"] == item["defect_type"]]
        if action in ("keep", "keep_open"):
            outcome = "accepted_by_user" if risky else "closed"
            evidence = "החלק נשמר בעותק לפי בחירת המשתמש." + (" הסיכון נותר." if risky else "")
        elif status.get(main["path"]) == "removed" or not same_category:
            outcome = "closed"
            evidence = (f"{main['path']}: הקובץ הוסר מהעותק המתוקן." if status.get(main["path"]) == "removed"
                        else f"בעותק המתוקן לא נמצאה עוד תבנית מסוג {item['defect_type']} בקבצי הממצא.")
        else:
            outcome = "remained"
            evidence = "בעותק המתוקן עדיין נמצאה תבנית מאותו סוג: " + "; ".join(
                f"{h['path']}:{h['line']}" for h in same_category[:5])
        yield [("record_validation", {"item_id": ref["item_id"], "outcome": outcome,
                                       "evidence": evidence, "new_issues": broken})]
