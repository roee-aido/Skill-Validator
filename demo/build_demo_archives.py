"""Build the demo archives from demo/human-writing-skill.

    python demo/build_demo_archives.py

Creates:
- demo/human-writing-skill.zip  - the synthetic skill for the end-to-end demo
- demo/bad-archive.zip          - a valid SKILL.md plus a member whose path escapes the
                                  intake area ("../escape.txt"); intake must reject it
"""

import zipfile
from pathlib import Path

DEMO = Path(__file__).resolve().parent
SKILL = DEMO / "human-writing-skill"


def build_skill_zip() -> Path:
    out = DEMO / "human-writing-skill.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(SKILL.rglob("*")):
            if p.is_file():
                info = zipfile.ZipInfo(f"human-writing-skill/{p.relative_to(SKILL).as_posix()}", (2026, 1, 1, 0, 0, 0))
                info.compress_type = zipfile.ZIP_DEFLATED
                zf.writestr(info, p.read_bytes())
    return out


def build_bad_zip() -> Path:
    out = DEMO / "bad-archive.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(zipfile.ZipInfo("SKILL.md", (2026, 1, 1, 0, 0, 0)), "---\nname: bad\n---\n# Bad archive\n")
        zf.writestr(zipfile.ZipInfo("../escape.txt", (2026, 1, 1, 0, 0, 0)), "this file must never be written\n")
    return out


if __name__ == "__main__":
    for path in (build_skill_zip(), build_bad_zip()):
        print(f"wrote {path.relative_to(DEMO.parent)}")
