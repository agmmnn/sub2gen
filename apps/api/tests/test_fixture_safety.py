from __future__ import annotations

import re
from pathlib import Path


API_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_ROOTS = (
    API_ROOT / "tests" / "fixtures",
    API_ROOT / "tests" / "contracts",
)
TEXT_SUFFIXES = {".csv", ".html", ".json", ".md", ".txt", ".xml", ".yaml", ".yml"}
FORBIDDEN_PATTERNS = {
    "sub2gen managed API key": re.compile(r"s2g_live_[A-Za-z0-9_-]{12,}"),
    "JWT-like token": re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b"),
    "Google session cookie": re.compile(r"__Secure-next-auth\.session-token\s*[=:]\s*[^<\s]{12,}"),
    "signed media URL": re.compile(r"https?://[^\s]+[?&](?:Signature|KeyName)=[A-Za-z0-9_%+./=-]{12,}"),
}


def test_contract_fixtures_do_not_contain_credentials() -> None:
    findings: list[str] = []
    for root in FIXTURE_ROOTS:
        if not root.exists():
            continue
        for path in sorted(candidate for candidate in root.rglob("*") if candidate.is_file()):
            if path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            content = path.read_text(encoding="utf-8", errors="replace")
            for label, pattern in FORBIDDEN_PATTERNS.items():
                if pattern.search(content):
                    findings.append(f"{path.relative_to(API_ROOT)}: {label}")

    assert not findings, "Sensitive fixture content detected:\n" + "\n".join(findings)
