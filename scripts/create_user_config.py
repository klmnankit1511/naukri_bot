#!/usr/bin/env python3
"""Create the next Naukri user configuration from a resume."""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import Any

import yaml


SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx"}
SKILL_TERMS = (
    "generative ai",
    "agentic ai",
    "machine learning",
    "data science",
    "python",
    "java",
    "javascript",
    "typescript",
    "react",
    "node.js",
    "fastapi",
    "django",
    "spring boot",
    "devops",
    "aws",
    "azure",
    "kubernetes",
    "docker",
    "sql",
)


def next_config_path(directory: Path) -> Path:
    primary = directory / "config.yaml"
    if not primary.exists():
        return primary
    person_number = 2
    while (directory / f"config.person{person_number}.yaml").exists():
        person_number += 1
    return directory / f"config.person{person_number}.yaml"


def extract_resume_text(path: Path) -> str:
    suffix = path.suffix.casefold()
    if suffix not in SUPPORTED_SUFFIXES:
        supported = ", ".join(sorted(SUPPORTED_SUFFIXES))
        raise SystemExit(f"Unsupported resume type '{suffix}'. Supported types: {supported}")
    if suffix in {".txt", ".md"}:
        text = path.read_text(encoding="utf-8")
    elif suffix == ".pdf":
        from pypdf import PdfReader

        text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
    else:
        from docx import Document

        document = Document(path)
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise SystemExit(
            "No text could be extracted from the resume. Use a text-based PDF, DOCX, TXT, or MD file."
        )
    return text


def inferred_keywords(resume_text: str) -> list[str]:
    lowered = resume_text.casefold()
    matches = [term for term in SKILL_TERMS if term in lowered]
    return matches[:12] or ["python"]


def build_config(
    name: str, resume_path: Path, resume_text: str, output: Path
) -> dict[str, Any]:
    keywords = inferred_keywords(resume_text)
    primary_search = " ".join(keywords[:3])
    summary = resume_text[:6_000]
    person_match = re.fullmatch(r"config\.person(\d+)\.ya?ml", output.name)
    data_suffix = f"person{person_match.group(1)}" if person_match else "profile"
    history_name = (
        f"applied_jobs.{data_suffix}.json"
        if person_match
        else "applied_jobs.json"
    )
    answers_name = f"answers.{data_suffix}.json" if person_match else "answers.json"
    return {
        "profile_name": name,
        "resume_path": str(resume_path.resolve()),
        "searches": [{"keywords": primary_search, "location": ""}],
        "include_keywords": keywords,
        "exclude_keywords": ["internship", "unpaid", "data entry", "sales"],
        "max_jobs_per_run": 2,
        "search_load_timeout_seconds": 60,
        "search_load_retries": 2,
        "post_apply_timeout_seconds": 30,
        "headless": False,
        "slow_mo_ms": 250,
        "browser_channel": "chrome",
        "profile_dir": f"data/browser/{data_suffix}",
        "history_file": f"data/history/{history_name}",
        "answers_file": f"data/answers/{answers_name}",
        "openai_model": "gpt-4o-mini",
        "resume_summary": summary,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Create config.yaml or the next config.personN.yaml from a resume."
    )
    parser.add_argument("name", help="name of the user represented by this configuration")
    parser.add_argument("resume", type=Path, help="path to a TXT, MD, PDF, or DOCX resume")
    parser.add_argument(
        "--output",
        type=Path,
        help="optional YAML output path; existing files are never overwritten",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if not args.resume.is_file():
        raise SystemExit(f"Resume not found: {args.resume}")
    output = args.output or next_config_path(Path.cwd())
    if output.suffix.casefold() not in {".yaml", ".yml"}:
        raise SystemExit("Output file must use a .yaml or .yml extension.")
    if output.exists():
        raise SystemExit(f"Refusing to overwrite existing config: {output}")
    resume_text = extract_resume_text(args.resume)
    config = build_config(args.name.strip(), args.resume, resume_text, output)
    if not config["profile_name"]:
        raise SystemExit("User name cannot be empty.")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        "# Generated from a resume. Review every field before using --submit.\n"
        + yaml.safe_dump(config, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    print(f"Created {output}")
    print("Review searches, location, keywords, resume_summary, and data paths before running.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
