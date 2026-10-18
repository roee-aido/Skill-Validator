import io
import os
import zipfile

import pytest

from skill_validator import PROJECT_ROOT
from skill_validator.intake import IntakeRejected, collect_upload, write_source
from skill_validator.scanners import markdown_parts, reference_graph, references_for, search_patterns
from skill_validator.secretscan import mask_secrets

DEMO = PROJECT_ROOT / "demo"


def zip_bytes(entries: dict[str, bytes], compress=zipfile.ZIP_DEFLATED, attrs: dict | None = None) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compress) as zf:
        for name, data in entries.items():
            info = zipfile.ZipInfo(name)
            info.compress_type = compress
            if attrs and name in attrs:
                info.external_attr = attrs[name]
            zf.writestr(info, data)
    return buf.getvalue()


SKILL_MD = b"---\nname: t\n---\n# T\n"


def test_demo_zip_is_accepted_and_written_read_only(storage, user, system_cfg):
    blob = (DEMO / "human-writing-skill.zip").read_bytes()
    res = collect_upload([("human-writing-skill.zip", blob)], system_cfg.intake, system_cfg.supported_formats)
    paths = {f.path for f in res.files}
    assert paths == {"SKILL.md", "scripts/send_text.py", "LICENSE"}  # common root folder stripped
    assert res.skill_name == "human-writing-demo"
    assert all(f.supported for f in res.files)
    write_source(storage, user.id, "s1", res)
    p = storage.resolve("source", user.id, "s1", "SKILL.md")
    assert p.read_bytes().startswith(b"---")
    assert not os.access(p, os.W_OK)
    storage.delete_review(user.id, "s1")
    assert not storage.area_dir("source", user.id, "s1").exists()


def test_bad_archive_is_rejected_without_writing(storage, user, system_cfg, tmp_path):
    blob = (DEMO / "bad-archive.zip").read_bytes()
    with pytest.raises(IntakeRejected, match="נתיב חורג"):
        collect_upload([("bad-archive.zip", blob)], system_cfg.intake, system_cfg.supported_formats)
    assert not (storage.root / "storage").exists()
    assert not any(p.name == "escape.txt" for p in tmp_path.rglob("*"))


@pytest.mark.parametrize(
    "entries,attrs,message",
    [
        ({"SKILL.md": SKILL_MD, "/etc/x.md": b"x"}, None, "נתיב חורג"),
        ({"SKILL.md": SKILL_MD, "C:/x.md": b"x"}, None, "נתיב חורג"),
        ({"SKILL.md": SKILL_MD, "link.md": b"target"}, {"link.md": 0o120777 << 16}, "קישור סמלי"),
        ({"SKILL.md": SKILL_MD, "a/b/c/d/e/f/g.md": b"x"}, None, "עומק"),
        ({"SKILL.md": SKILL_MD, "skill.md": b"x"}, None, "כפול"),
        ({"README.md": b"x"}, None, "SKILL.md"),
    ],
)
def test_unsafe_archives_are_rejected(system_cfg, entries, attrs, message):
    with pytest.raises(IntakeRejected, match=message):
        collect_upload([("x.zip", zip_bytes(entries, attrs=attrs))], system_cfg.intake, system_cfg.supported_formats)


def test_zip_bomb_and_size_limits(system_cfg):
    bomb = zip_bytes({"SKILL.md": SKILL_MD, "big.txt": b"0" * 400_000})
    with pytest.raises(IntakeRejected, match="דחיסה"):
        collect_upload([("x.zip", bomb)], system_cfg.intake, system_cfg.supported_formats)
    too_big = zip_bytes({"SKILL.md": SKILL_MD, "big.txt": os.urandom(600_000)}, compress=zipfile.ZIP_STORED)
    with pytest.raises(IntakeRejected, match="גדול מדי"):
        collect_upload([("x.zip", too_big)], system_cfg.intake, system_cfg.supported_formats)
    many = zip_bytes({"SKILL.md": SKILL_MD, **{f"f{i}.md": b"x" for i in range(101)}})
    with pytest.raises(IntakeRejected, match="יותר מ"):
        collect_upload([("x.zip", many)], system_cfg.intake, system_cfg.supported_formats)


def test_single_file_and_folder_uploads(system_cfg):
    res = collect_upload([("SKILL.md", SKILL_MD)], system_cfg.intake, system_cfg.supported_formats)
    assert [f.path for f in res.files] == ["SKILL.md"]
    folder = [("my-skill/SKILL.md", SKILL_MD), ("my-skill/scripts/a.py", b"print(1)\n")]
    res = collect_upload(folder, system_cfg.intake, system_cfg.supported_formats)
    assert {f.path for f in res.files} == {"SKILL.md", "scripts/a.py"}
    with pytest.raises(IntakeRejected):
        collect_upload([("../SKILL.md", SKILL_MD)], system_cfg.intake, system_cfg.supported_formats)


def test_unsupported_and_binary_files_are_marked(system_cfg):
    blob = zip_bytes({"SKILL.md": SKILL_MD, "logo.png": b"\x89PNG\x00\x00", "data.csv": b"a,b\n"})
    res = collect_upload([("x.zip", blob)], system_cfg.intake, system_cfg.supported_formats)
    by = {f.path: f for f in res.files}
    assert by["logo.png"].supported is False and by["logo.png"].type == "binary"
    assert by["data.csv"].supported is False
    assert by["SKILL.md"].supported is True


def demo_texts() -> dict[str, str]:
    base = DEMO / "human-writing-skill"
    return {
        p.relative_to(base).as_posix(): p.read_text(encoding="utf-8")
        for p in base.rglob("*")
        if p.is_file()
    }


def test_patterns_find_the_planted_issues():
    texts = demo_texts()
    md_hits = {h["category"] for h in search_patterns("SKILL.md", texts["SKILL.md"])}
    assert {"broad_file_access", "concealment", "reviewer_manipulation"} <= md_hits
    py_hits = {h["category"] for h in search_patterns("scripts/send_text.py", texts["scripts/send_text.py"])}
    assert {"network_send", "external_url"} <= py_hits
    assert search_patterns("LICENSE", texts["LICENSE"]) == []


def test_style_guidelines_have_no_hits():
    texts = demo_texts()
    lines = texts["SKILL.md"].splitlines()
    start = lines.index("## Style guidelines") + 1
    hit_lines = {h["line"] for h in search_patterns("SKILL.md", texts["SKILL.md"])}
    assert not hit_lines & set(range(start + 1, start + 6))


def test_references_and_broken_references():
    texts = demo_texts()
    graph = reference_graph(texts)
    refs = references_for("scripts/send_text.py", graph)
    assert [e["from"] for e in refs["incoming"]] == ["SKILL.md"]
    without_script = {k: v for k, v in texts.items() if k != "scripts/send_text.py"}
    broken = reference_graph(without_script)["broken"]
    assert broken and broken[0]["token"] == "scripts/send_text.py"


def test_urls_are_not_reported_as_broken_paths():
    graph = reference_graph({"SKILL.md": "See https://example.com/docs/guide.md for details.\n"})
    assert graph["broken"] == []


def test_secrets_are_masked_in_pattern_output():
    text = 'API_KEY = "sk-ant-FAKE-ABCDEFGHIJKLMNOPQRST"\n'
    hits = search_patterns("config.py", text)
    assert any(h["category"] == "secret" for h in hits)
    assert all("ABCDEFGHIJ" not in h["text"] for h in hits)
    assert "[REDACTED:" in mask_secrets(text)


def test_markdown_parts():
    parts = markdown_parts(demo_texts()["SKILL.md"])
    assert parts[0]["kind"] == "frontmatter"
    headings = [p["heading"] for p in parts if p["kind"] == "heading"]
    assert "Style guidelines" in headings and "Notes" in headings
