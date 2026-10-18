import subprocess
import sys
import textwrap

import pytest

from skill_validator import PROJECT_ROOT
from skill_validator.db import ReviewItem, ReviewTask
from skill_validator.netguard import Guard, load_allowlist
from skill_validator.tools import ToolDenied, ToolError, call_tool, make_context


def ctx_for(db, storage, review, agent_cfg, stage, target_ref=None):
    task = ReviewTask(session_id=review.id, stage=stage, task_type=stage, target_ref=target_ref or {})
    db.add(task)
    db.flush()
    return make_context(db, storage, review, task, agent_cfg.stage(stage))


def write_candidate(storage, review, rel, text):
    p = storage.resolve("candidate", review.user_id, review.id, rel)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def test_read_tools_in_analyze(db, storage, demo_review, agent_cfg):
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "analyze")
    files = call_tool(ctx, "list_files", {})["files"]
    assert {f["path"] for f in files} == {"SKILL.md", "LICENSE", "scripts/send_text.py"}
    out = call_tool(ctx, "read_file", {"path": "SKILL.md", "start": 1, "end": 3})
    assert out["content"].startswith("1: ---")
    hits = call_tool(ctx, "search_patterns", {"path": "scripts/send_text.py"})["hits"]
    assert any(h["category"] == "network_send" for h in hits)
    refs = call_tool(ctx, "get_references", {"path": "scripts/send_text.py"})
    assert refs["incoming"][0]["from"] == "SKILL.md"


def test_candidate_is_not_readable_outside_revalidate(db, storage, demo_review, agent_cfg):
    write_candidate(storage, demo_review, "SKILL.md", "x\n")
    for stage in ("detect_purpose", "analyze", "propose_fix"):
        ctx = ctx_for(db, storage, demo_review, agent_cfg, stage)
        with pytest.raises(ToolDenied, match="not readable"):
            call_tool(ctx, "read_file", {"path": "SKILL.md", "area": "candidate"})
        assert ctx.calls[-1]["status"] == "denied"


def test_candidate_is_readable_in_revalidate(db, storage, demo_review, agent_cfg):
    write_candidate(storage, demo_review, "SKILL.md", "corrected\n")
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "revalidate", {"item_id": "x"})
    assert call_tool(ctx, "read_file", {"path": "SKILL.md", "area": "candidate"})["content"] == "1: corrected"
    diff = call_tool(ctx, "compare_versions", {"path": "SKILL.md"})
    assert diff["status"] == "changed" and "+corrected" in diff["diff"]
    gone = call_tool(ctx, "compare_versions", {"path": "scripts/send_text.py"})
    assert gone["status"] == "removed"


@pytest.mark.parametrize("path", ["../other/SKILL.md", "/etc/passwd", "C:/Windows/win.ini"])
def test_path_escape_is_denied(db, storage, demo_review, agent_cfg, path):
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "analyze")
    with pytest.raises(ToolDenied):
        call_tool(ctx, "read_file", {"path": path})


def test_unknown_and_out_of_stage_tools_are_denied(db, storage, demo_review, agent_cfg):
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "analyze")
    with pytest.raises(ToolDenied, match="unknown tool"):
        call_tool(ctx, "run_shell", {"cmd": "ls"})
    with pytest.raises(ToolDenied, match="not allowed in stage"):
        call_tool(ctx, "compare_versions", {"path": "SKILL.md"})
    with pytest.raises(ToolDenied, match="not allowed in stage"):
        call_tool(ctx, "propose_patch", {"proposed_text": "x", "explanation": "y"})


def test_narrowed_config_removes_a_tool(db, storage, demo_review, agent_cfg):
    stage = agent_cfg.stage("analyze").model_copy(update={"tools": ["list_files", "read_file", "record_finding"]})
    task = ReviewTask(session_id=demo_review.id, stage="analyze", task_type="analyze")
    db.add(task)
    db.flush()
    ctx = make_context(db, storage, demo_review, task, stage)
    with pytest.raises(ToolDenied):
        call_tool(ctx, "search_patterns", {})


def test_record_finding_validates_and_buffers(db, storage, demo_review, agent_cfg):
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "analyze")
    good = {
        "title": "שליחת טקסט לשרת חיצוני",
        "locations": [{"path": "scripts/send_text.py", "start": 1, "end": 19}, {"path": "SKILL.md", "start": 22, "end": 22}],
        "evidence": "urllib.request.urlopen(req)",
        "relevance": "not_relevant",
        "safety_status": "unsafe",
        "defect_type": "network_send",
        "severity": "high",
        "certainty": "high",
        "reason": "r",
        "recommendation": "remove",
    }
    call_tool(ctx, "record_finding", good)
    assert len(ctx.pending) == 1 and ctx.pending[0].locations[1]["role"] == "reference"
    assert db.query(ReviewItem).count() == 0  # nothing is committed by the tool itself
    with pytest.raises(ToolError, match="severity"):
        call_tool(ctx, "record_finding", {**good, "severity": "extreme"})
    with pytest.raises(ToolError, match="outside"):
        call_tool(ctx, "record_finding", {**good, "locations": [{"path": "SKILL.md", "start": 1, "end": 999}]})


def test_propose_patch_rules(db, storage, demo_review, agent_cfg):
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "propose_fix")
    with pytest.raises(ToolError):
        call_tool(ctx, "propose_patch", {"proposed_text": "", "explanation": "e", "no_safe_alternative": False})
    call_tool(ctx, "propose_patch", {"proposed_text": "", "explanation": "אין חלופה", "no_safe_alternative": True})
    assert ctx.results["proposal"]["no_safe_alternative"] is True


def test_record_validation_only_for_task_item(db, storage, demo_review, agent_cfg):
    ctx = ctx_for(db, storage, demo_review, agent_cfg, "revalidate", {"item_id": "abc"})
    with pytest.raises(ToolError, match="does not belong"):
        call_tool(ctx, "record_validation", {"item_id": "other", "outcome": "closed", "evidence": "e"})
    call_tool(ctx, "record_validation", {"item_id": "abc", "outcome": "closed", "evidence": "e"})


def test_secrets_are_masked_in_read_file(db, storage, user, agent_cfg, system_cfg):
    from skill_validator.db import FileRecord, ReviewSession
    from skill_validator.intake import collect_upload, write_source

    skill = b'---\nname: s\n---\nUse key "sk-ant-FAKE-SECRETSECRETSECRET" here.\n'
    res = collect_upload([("SKILL.md", skill)], system_cfg.intake, system_cfg.supported_formats)
    review = ReviewSession(user_id=user.id)
    db.add(review)
    db.flush()
    write_source(storage, user.id, review.id, res)
    db.add(FileRecord(session_id=review.id, area="source", relative_path="SKILL.md", type=".md", size=1, hash="h"))
    db.flush()
    ctx = ctx_for(db, storage, review, agent_cfg, "analyze")
    content = call_tool(ctx, "read_file", {"path": "SKILL.md"})["content"]
    assert "SECRETSECRET" not in content and "[REDACTED:" in content


# ---------------------------------------------------------------- network guard


def test_guard_decisions():
    g = Guard({("api.anthropic.com", 443)})
    assert g.host_allowed("api.anthropic.com", 443)
    assert g.host_allowed("API.ANTHROPIC.COM", "443")
    assert not g.host_allowed("api.anthropic.com", 80)
    assert not g.host_allowed("collector.example.invalid", 443)
    assert not g.host_allowed("93.184.216.34", 443)
    assert not g.connect_allowed(("203.0.113.10", 443))
    g.resolved_ips.add("203.0.113.10")
    assert g.connect_allowed(("203.0.113.10", 443))


def test_allowlist_file_has_only_the_model_api():
    assert load_allowlist() == {("api.anthropic.com", 443)}


GUARD_SCRIPT = textwrap.dedent(
    """
    import json, socket, subprocess, sys
    sys.path.insert(0, {root!r})
    from skill_validator import netguard
    netguard.install()
    results = {{}}
    def attempt(name, fn):
        try:
            fn()
            results[name] = "allowed"
        except PermissionError as e:
            results[name] = "blocked" if "network guard" in str(e) else "other:" + str(e)
        except Exception as e:
            results[name] = "error:" + type(e).__name__
    attempt("resolve_skill_url", lambda: socket.getaddrinfo("collector.example.invalid", 443))
    attempt("connect_by_ip", lambda: socket.create_connection(("93.184.216.34", 80), timeout=1))
    attempt("raw_connect", lambda: socket.socket().connect(("1.1.1.1", 80)))
    attempt("subprocess", lambda: subprocess.run(["cmd", "/c", "echo", "hi"]))
    print(json.dumps(results))
    """
)


def test_guard_blocks_skill_urls_direct_ips_and_processes(tmp_path):
    script = tmp_path / "guard_check.py"
    script.write_text(GUARD_SCRIPT.format(root=str(PROJECT_ROOT)), encoding="utf-8")
    out = subprocess.run([sys.executable, "-I", str(script)], capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    import json

    results = json.loads(out.stdout.strip().splitlines()[-1])
    assert results == {
        "resolve_skill_url": "blocked",
        "connect_by_ip": "blocked",
        "raw_connect": "blocked",
        "subprocess": "blocked",
    }
