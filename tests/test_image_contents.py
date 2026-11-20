"""The Dockerfile copies source files explicitly; a module it misses only fails
at runtime in the cluster (the nightly job once died on `import promotion`)."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _copied() -> set[str]:
    files = set()
    for line in (ROOT / "Dockerfile").read_text().splitlines():
        m = re.match(r"\s*COPY\s+(?!--from)(.+)\s+\S+\s*$", line)
        if m:
            for src in m.group(1).split():
                files.update(str(p.relative_to(ROOT)) for p in ROOT.glob(src))
    return files


def test_every_server_and_script_module_is_in_the_image():
    copied = _copied()
    wanted = {str(p.relative_to(ROOT)) for p in (ROOT / "src").glob("*.py")}
    wanted |= {
        str(p.relative_to(ROOT)) for p in (ROOT / "src" / "scripts").glob("*.py")
    }
    assert wanted - copied == set()
