"""Build a clean local .difypkg using an explicitly supplied official CLI."""
import argparse
import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import yaml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cli", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    manifest = yaml.safe_load((root / "manifest.yaml").read_text(encoding="utf-8"))
    version = str(manifest.get("version", ""))
    if not re.fullmatch(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?", version):
        raise ValueError("manifest.yaml must contain a valid package version")
    output = (args.output or root / "dist" / f"vision_markdown-{version}.difypkg").resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="vision-md-package-") as directory:
        stage = Path(directory) / "vision_markdown"
        stage.mkdir()
        for name in ("main.py", "manifest.yaml", "requirements.txt", "README.md", "PRIVACY.md"):
            shutil.copy2(root / name, stage / name)
        if (root / "README.zh_Hans.md").exists():
            shutil.copy2(root / "README.zh_Hans.md", stage / "README.zh_Hans.md")
        for name in ("document_md", "tools", "provider", "_assets"):
            shutil.copytree(root / name, stage / name, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store"))
        subprocess.run([str(args.cli.resolve()), "plugin", "package", str(stage), "-o", str(output)], check=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    record = {"package": output.name, "bytes": output.stat().st_size, "sha256": digest}
    output.with_suffix(".sha256").write_text(f"{digest}  {output.name}\n", encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
