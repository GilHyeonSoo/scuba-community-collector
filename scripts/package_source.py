"""Create a clean source release without local data, credentials or environments."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

project = Path(__file__).resolve().parents[1]
destination = project.parent / "SCUBA_Collector_Windows.zip"
excluded = {".git", ".venv", "__pycache__", ".ruff_cache", "build", "dist", "raw", "demo"}
files = []
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for path in sorted(project.rglob("*")):
        relative = path.relative_to(project)
        if not path.is_file() or any(part in excluded for part in relative.parts):
            continue
        if path.name in {".DS_Store", "credentials.dpapi", "settings.json", "collector.lock"}:
            continue
        if path.suffix in {".pyc", ".sqlite3", ".db"} or ".sqlite3-" in path.name:
            continue
        data = path.read_bytes()
        if path.suffix.lower() in {".bat", ".ps1"}:
            data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
        archive.writestr("scuba_collector/" + relative.as_posix(), data)
        files.append(relative.as_posix())
with ZipFile(destination) as archive:
    assert archive.testzip() is None
    required = {
        "START_WINDOWS.bat",
        "BUILD_WINDOWS.bat",
        "README.md",
        "collector/ui.py",
        "collector/adapters/reddit.py",
        ".github/workflows/windows.yml",
        "docs/VALIDATION.md",
    }
    assert required.issubset(set(files))
print(f"Packaged {len(files)} files: {destination} ({destination.stat().st_size:,} bytes)")
