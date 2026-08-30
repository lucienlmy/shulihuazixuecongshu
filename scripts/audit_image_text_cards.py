#!/usr/bin/env python3
"""Read-only audit for images suspected of containing headings or running text."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import struct
import sys
from datetime import datetime, timezone
from pathlib import Path

BOOK_GLOB = "* - 数理化自学丛书编委会.md"
HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*$")
ALLOWED_ACTIONS = {
    "undecided",
    "replace_with_heading",
    "remove_duplicate",
    "relocate_heading",
    "remove_layout_artifact",
    "keep_image",
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def png_size(path: Path) -> tuple[int, int]:
    with path.open("rb") as handle:
        header = handle.read(24)
    if len(header) != 24 or header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError("不是带标准 IHDR 的 PNG")
    return struct.unpack(">II", header[16:24])


def normalize_heading(value: str) -> str:
    value = re.sub(r"^#{1,6}\s*", "", value.strip())
    value = value.replace("（", "(").replace("）", ")").replace(".", "·").replace("～", "~")
    return re.sub(r"\s+", "", value)


def nearest_heading(lines: list[str], start: int, step: int) -> dict[str, object] | None:
    indexes = range(start, -1, -1) if step < 0 else range(start, len(lines))
    for index in indexes:
        match = HEADING_RE.match(lines[index])
        if match:
            return {"line": index + 1, "level": len(match.group(1)), "text": match.group(2)}
    return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--context-lines", type=int, default=12)
    args = parser.parse_args()
    repo = args.repo.resolve()
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    items = manifest.get("items")
    if not isinstance(items, list) or not items:
        raise SystemExit("manifest.items 必须是非空数组")
    books: dict[Path, list[str]] = {
        path: path.read_text(encoding="utf-8").splitlines()
        for path in sorted((repo / "books").glob(BOOK_GLOB))
    }
    rows: list[dict[str, object]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for number, item in enumerate(items, 1):
        rel = str(item.get("path", "")).replace("\\", "/").lstrip("/")
        if rel in seen:
            errors.append(f"重复路径：{rel}")
        seen.add(rel)
        action = str(item.get("action", "undecided"))
        if action not in ALLOWED_ACTIONS:
            errors.append(f"{rel}: 未知 action={action}")
        asset = (repo / rel).resolve()
        try:
            asset.relative_to(repo)
        except ValueError:
            errors.append(f"{rel}: 路径越出仓库")
            continue
        if not asset.is_file():
            errors.append(f"{rel}: 文件不存在")
            continue
        try:
            width, height = png_size(asset)
        except ValueError as exc:
            errors.append(f"{rel}: {exc}")
            continue
        markdown_ref = rel.removeprefix("books/")
        hits: list[tuple[Path, int]] = []
        for book, lines in books.items():
            hits.extend((book, index) for index, line in enumerate(lines) if markdown_ref in line)
        if len(hits) != 1:
            errors.append(f"{rel}: Markdown引用应恰好一次，实际{len(hits)}次")
            rows.append({"path": rel, "references": len(hits)})
            continue
        book, index = hits[0]
        lines = books[book]
        radius = max(1, args.context_lines)
        context_start = max(0, index - radius)
        context_end = min(len(lines), index + radius + 1)
        observed = str(item.get("observed_text", "")).strip()
        nearby = []
        if observed:
            for candidate in range(max(0, index - 120), min(len(lines), index + 121)):
                match = HEADING_RE.match(lines[candidate])
                if match and normalize_heading(match.group(2)) == normalize_heading(observed):
                    nearby.append({"line": candidate + 1, "level": len(match.group(1)), "text": match.group(2)})
        if action == "remove_duplicate" and not nearby:
            errors.append(f"{rel}: remove_duplicate 未找到邻近同名标题")
        if action == "replace_with_heading" and (not observed or item.get("heading_level") not in {1, 2, 3, 4, 5, 6}):
            errors.append(f"{rel}: replace_with_heading 缺 observed_text 或合法 heading_level")
        if action == "relocate_heading":
            anchor = str(item.get("target_anchor", ""))
            if not observed or not anchor or sum(anchor in line for line in lines) != 1:
                errors.append(f"{rel}: relocate_heading 需要唯一 target_anchor 和 observed_text")
        if action == "remove_layout_artifact" and not str(item.get("evidence", "")).strip():
            errors.append(f"{rel}: remove_layout_artifact 必须记录 evidence")
        rows.append(
            {
                "path": rel,
                "sha256": sha256(asset),
                "bytes": asset.stat().st_size,
                "width": width,
                "height": height,
                "markdown": book.relative_to(repo).as_posix(),
                "line": index + 1,
                "reference_line": lines[index],
                "observed_text": observed,
                "action": action,
                "previous_heading": nearest_heading(lines, index - 1, -1),
                "next_heading": nearest_heading(lines, index + 1, 1),
                "nearby_matching_headings": nearby,
                "context": [
                    {"line": line_number + 1, "text": lines[line_number]}
                    for line_number in range(context_start, context_end)
                ],
                "target_anchor": item.get("target_anchor"),
                "evidence": item.get("evidence"),
            }
        )
    payload = {
        "schema_version": "1.0",
        "operation": "read-only-image-text-card-audit",
        "status": "PASS" if not errors else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "manifest": str(args.manifest.resolve()),
        "repo": str(repo),
        "summary": {"items": len(items), "resolved": len(rows), "errors": len(errors)},
        "items": rows,
        "errors": errors,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], **payload["summary"]}, ensure_ascii=False))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
