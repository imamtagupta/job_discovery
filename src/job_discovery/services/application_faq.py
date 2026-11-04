import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml


def normalize_question(value: str) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", value.casefold()))


def load_faq(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        return yaml.safe_load(stream) or {"entries": []}


def resolve_question(path: Path, question: str) -> dict[str, Any]:
    normalized = normalize_question(question)
    for entry in load_faq(path).get("entries", []):
        variants = [entry["question"], *entry.get("aliases", [])]
        if normalized in {normalize_question(value) for value in variants}:
            return {
                "status": entry.get("status", "needs_review"),
                "key": entry["key"],
                "answer": entry.get("answer"),
                "question": question,
            }
    return {"status": "unseen", "key": None, "answer": None, "question": question}


def record_unseen_question(path: Path, question: str, source: str | None = None) -> bool:
    normalized = normalize_question(question)
    if path.exists():
        with path.open(encoding="utf-8") as stream:
            document = yaml.safe_load(stream) or {}
    else:
        document = {}
    questions = document.setdefault("questions", [])
    if any(item.get("normalized") == normalized for item in questions):
        return False
    questions.append(
        {
            "question": question,
            "normalized": normalized,
            "source": source,
            "status": "unresolved",
            "first_seen_at": datetime.now(UTC).isoformat(),
        }
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(document, stream, sort_keys=False, allow_unicode=True)
    return True


def add_faq_entry(
    path: Path,
    *,
    key: str,
    question: str,
    answer: str,
    aliases: list[str] | None = None,
) -> None:
    document = load_faq(path)
    entries = document.setdefault("entries", [])
    if any(entry.get("key") == key for entry in entries):
        raise ValueError(f"FAQ key already exists: {key}")
    entries.append(
        {
            "key": key,
            "question": question,
            "aliases": aliases or [],
            "answer": answer,
            "status": "approved",
        }
    )
    with path.open("w", encoding="utf-8") as stream:
        yaml.safe_dump(document, stream, sort_keys=False, allow_unicode=True)
