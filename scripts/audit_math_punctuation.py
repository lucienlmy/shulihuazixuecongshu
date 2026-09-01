#!/usr/bin/env python3
"""Reject terminal sentence punctuation left inside math delimiters in math books."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BOOK_ROOT = ROOT / "books"
REPORT = ROOT / "reports" / "math-punctuation-audit.json"
BOOK_GLOB = "* - 数理化自学丛书编委会.md"
MATH_PREFIXES = ("代数", "平面三角", "平面几何", "平面解析几何", "立体几何")
MATH_SPAN = re.compile(
    r"\$\$(.*?)\$\$|\\\[(.*?)\\\]|\\\((.*?)\\\)|(?<!\\)\$(?!\$)(.*?)(?<!\\)\$",
    re.S,
)
TYPES = ("display-dollar", "display-bracket", "inline-paren", "inline-dollar")


def main() -> int:
    books = sorted(BOOK_ROOT.glob(BOOK_GLOB))
    math_books = [book for book in books if book.name.startswith(MATH_PREFIXES)]
    findings: list[dict[str, object]] = []
    out_of_scope: list[dict[str, object]] = []
    formula_spans = Counter()
    external_punctuation = Counter()

    for book in books:
        text = book.read_text(encoding="utf-8")
        is_math_book = book in math_books
        for match in MATH_SPAN.finditer(text):
            index = next((i for i, value in enumerate(match.groups()) if value is not None), None)
            if index is None:
                continue
            expression_type = TYPES[index]
            formula_spans[expression_type] += 1
            body = match.group(index + 1)
            stripped = body.rstrip()
            if stripped.endswith((".", "。")):
                punctuation = stripped[-1]
                structural = punctuation == "." and re.search(r"\\right\s*\.$", stripped)
                if not structural:
                    item = {
                        "book": book.name,
                        "line": text.count("\n", 0, match.start()) + 1,
                        "expression_type": expression_type,
                        "punctuation": punctuation,
                        "formula_tail": stripped[-240:],
                    }
                    (findings if is_math_book else out_of_scope).append(item)
            tail = text[match.end() :]
            external = re.match(r"[ \t]*([。.])", tail)
            if external:
                external_punctuation[(expression_type, external.group(1), "math" if is_math_book else "other")] += 1

    math_external = Counter()
    other_external = Counter()
    for (expression_type, punctuation, scope), count in external_punctuation.items():
        (math_external if scope == "math" else other_external)[(expression_type, punctuation)] += count

    report = {
        "schema_version": "1.0",
        "operation": "canonical-math-terminal-punctuation-audit",
        "status": "PASS" if len(books) == 17 and len(math_books) == 9 and not findings else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "books": len(books),
        "math_books": len(math_books),
        "formula_spans": dict(formula_spans),
        "math_external_punctuation": {f"{kind}:{punct}": count for (kind, punct), count in math_external.items()},
        "other_book_external_punctuation": {f"{kind}:{punct}": count for (kind, punct), count in other_external.items()},
        "math_findings": findings,
        "other_book_out_of_scope_findings": out_of_scope,
        "policy": {
            "math_scope": "9 mathematics books",
            "forbidden": "sentence-terminal . or 。 immediately before a math closing delimiter",
            "allowed": r"TeX structural \right. delimiters; punctuation outside math delimiters; original ./。 choice",
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        json.dumps(
            {
                "books": len(books),
                "math_books": len(math_books),
                "math_formula_spans": sum(formula_spans.values()),
                "math_findings": len(findings),
                "other_book_out_of_scope_findings": len(out_of_scope),
                "status": report["status"],
            },
            ensure_ascii=False,
        )
    )
    if findings:
        for item in findings[:20]:
            print(
                f"ERROR: {item['book']}:{item['line']}: "
                f"发现数学定界符内句末{item['punctuation']!r}: {item['formula_tail']}",
                file=sys.stderr,
            )
    return 0 if report["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
