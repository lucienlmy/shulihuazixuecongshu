#!/usr/bin/env python3
"""Audit the 17 raw source PDFs for integrity, active content, metadata, and privacy leaks."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

try:
    from PyPDF2 import PdfReader
    from PyPDF2.generic import ArrayObject, ByteStringObject, DictionaryObject, IndirectObject, StreamObject, TextStringObject
except ImportError as exc:  # pragma: no cover - dependency failure is reported to the operator.
    raise SystemExit("audit_pdfs.py 需要 PyPDF2 3.x：python3 -m pip install PyPDF2") from exc

ROOT = Path(__file__).resolve().parents[1]
RAW = ROOT / "raw"
REPORT = ROOT / "reports" / "pdf-audit.json"
EXPECTED_PDFS = 17
EXPECTED_AUTHOR = "数理化自学丛书编委会"
GITHUB_MAX_FILE = 100 * 1024 * 1024

DANGEROUS_KEYS = {
    "/JavaScript",
    "/JS",
    "/Launch",
    "/AA",
    "/EmbeddedFiles",
    "/Filespec",
    "/XFA",
    "/RichMedia",
    "/SubmitForm",
    "/ImportData",
}
PXC_COMMENT_RE = re.compile(
    rb"%PXC-Ver:[0-9.]+-Date:[0-9]{14}-SHA:[0-9A-F]+:[0-9A-F]{64}(?:\r?\n|\r)"
)
TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "windows_user_path",
        re.compile(r"(?<![A-Za-z0-9])[A-Za-z]:[\\/](?:Users|Documents and Settings)[\\/][^\\/\s<>\"']+(?:[\\/][^\s<>\"']*)?", re.I),
    ),
    ("posix_user_home_path", re.compile(r"(?<![\w/])/(?:home|Users)/[A-Za-z0-9._-]+(?:/[^\s<>\"']*)?", re.I)),
    ("mounted_windows_user_path", re.compile(r"(?<![\w/])/mnt/[a-z]/Users/[A-Za-z0-9._-]+(?:/[^\s<>\"']*)?", re.I)),
    ("wsl_unc_path", re.compile(r"\\\\wsl(?:\.localhost)?\\[^\s<>\"']+", re.I)),
    ("email_address", re.compile(r"(?<![\w.+-])[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}(?![\w.-])", re.I)),
    ("credential_in_url", re.compile(r"https?://[^\s/@:]+:[^\s/@]+@", re.I)),
    ("aws_access_key", re.compile(r"(?<![A-Z0-9])(?:AKIA|ASIA)[0-9A-Z]{16}(?![A-Z0-9])")),
    ("github_token", re.compile(r"(?<![A-Za-z0-9_])(?:ghp_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,})(?![A-Za-z0-9_])")),
    ("openai_api_key", re.compile(r"(?<![A-Za-z0-9_-])sk-[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")),
    ("private_key_block", re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----")),
    ("possible_cn_phone", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    ("possible_cn_id", re.compile(r"(?<!\d)\d{17}[0-9Xx](?!\d)")),
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def run_tool(command: list[str], *, timeout: int = 300) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def local_identifiers() -> set[str]:
    values = {
        os.environ.get("USER", ""),
        os.environ.get("USERNAME", ""),
        Path.home().name,
        socket.gethostname(),
    }
    windows_users = Path("/mnt/c/Users")
    if windows_users.is_dir():
        ignored = {"all users", "default", "default user", "public", "desktop.ini"}
        try:
            values.update(path.name for path in windows_users.iterdir() if path.name.casefold() not in ignored)
        except OSError:
            pass
    return {value for value in values if len(value) >= 4}


def scan_text(text: str, source: str, findings: list[dict[str, str]], identifiers: set[str]) -> None:
    for kind, pattern in TEXT_PATTERNS:
        for match in pattern.finditer(text):
            findings.append({"type": kind, "source": source, "sample": match.group(0)[:160]})
    lowered = text.casefold()
    for value in identifiers:
        pattern = re.compile(rf"(?<![\w.-]){re.escape(value.casefold())}(?![\w.-])")
        for match in pattern.finditer(lowered):
            findings.append(
                {"type": "local_user_or_host_identifier", "source": source, "sample": text[match.start() : match.end()]}
            )


def walk_pdf_objects(
    value: object,
    source: str,
    findings: list[dict[str, str]],
    features: Counter[str],
    identifiers: set[str],
    seen: set[tuple[int, int]],
    depth: int = 0,
) -> None:
    if depth > 80:
        return
    if isinstance(value, IndirectObject):
        key = (value.idnum, value.generation)
        if key in seen:
            return
        seen.add(key)
        value = value.get_object()
    if isinstance(value, DictionaryObject):
        for key, child in value.items():
            key_text = str(key)
            if key_text in DANGEROUS_KEYS:
                features[key_text] += 1
            # Page/image streams are large and not metadata. Their dictionaries are still visited elsewhere.
            if key_text in {"/Contents", "/XObject"}:
                continue
            walk_pdf_objects(
                child,
                f"{source}{key_text}",
                findings,
                features,
                identifiers,
                seen,
                depth + 1,
            )
    elif isinstance(value, ArrayObject):
        for index, child in enumerate(value):
            walk_pdf_objects(
                child,
                f"{source}[{index}]",
                findings,
                features,
                identifiers,
                seen,
                depth + 1,
            )
    elif isinstance(value, TextStringObject):
        scan_text(str(value), source, findings, identifiers)
    elif isinstance(value, ByteStringObject) and len(value) <= 2_000_000:
        scan_text(bytes(value).decode("utf-8", errors="ignore"), source, findings, identifiers)
    elif isinstance(value, StreamObject) and (
        value.get("/Type") == "/Metadata" or value.get("/Subtype") == "/XML"
    ):
        scan_text(value.get_data().decode("utf-8", errors="ignore"), source, findings, identifiers)


def expected_checksums() -> dict[str, str]:
    path = RAW / "SHA256SUMS.txt"
    if not path.is_file():
        raise ValueError("raw/SHA256SUMS.txt 不存在")
    rows: dict[str, str] = {}
    for line_number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        match = re.fullmatch(r"([0-9a-f]{64})  (.+\.pdf)", line)
        if not match:
            raise ValueError(f"SHA256SUMS.txt 第 {line_number} 行格式错误")
        digest, name = match.groups()
        if name in rows:
            raise ValueError(f"SHA256SUMS.txt 文件名重复：{name}")
        rows[name] = digest
    return rows


def audit_pdf(path: Path, expected_hash: str, identifiers: set[str]) -> dict[str, object]:
    relative = path.relative_to(ROOT).as_posix()
    findings: list[dict[str, str]] = []
    errors: list[str] = []
    warnings: list[str] = []
    features: Counter[str] = Counter()
    digest = sha256_file(path)
    if digest != expected_hash:
        errors.append("SHA-256 与 raw/SHA256SUMS.txt 不一致")
    data = path.read_bytes()
    if not data.startswith(b"%PDF-") or b"%%EOF" not in data[-4096:]:
        errors.append("PDF 文件头或 EOF 标记无效")
    if path.stat().st_size > GITHUB_MAX_FILE:
        errors.append("文件超过 GitHub 100 MiB 限制")

    qpdf = run_tool(["qpdf", "--check", str(path)])
    if qpdf.returncode != 0:
        errors.append(f"qpdf --check 失败（rc={qpdf.returncode}）")
    detach = run_tool(["pdfdetach", "-list", str(path)])
    attachments = [line.strip() for line in detach.stdout.splitlines() if re.match(r"^\d+:\s", line.strip())]
    if detach.returncode not in {0, 1}:
        errors.append(f"pdfdetach 失败（rc={detach.returncode}）")
    if attachments:
        errors.append(f"存在嵌入附件：{attachments}")
    signature = run_tool(["pdfsig", str(path)])
    signature_count = sum(line.startswith("Digital Signature Info") for line in signature.stdout.splitlines())
    if signature_count:
        warnings.append(f"存在 {signature_count} 个数字签名")

    expected_title = path.name.removesuffix(f" - {EXPECTED_AUTHOR}.pdf")
    try:
        reader = PdfReader(str(path), strict=True)
        if reader.is_encrypted:
            errors.append("PDF 已加密")
        metadata = {str(key): str(value) for key, value in (reader.metadata or {}).items()}
        if metadata.get("/Title") != expected_title:
            errors.append(f"Title 元数据不匹配：{metadata.get('/Title')!r}")
        if metadata.get("/Author") != EXPECTED_AUTHOR:
            errors.append(f"Author 元数据不匹配：{metadata.get('/Author')!r}")
        for key, value in metadata.items():
            scan_text(value, f"{relative}:metadata:{key}", findings, identifiers)
        root = reader.trailer["/Root"]
        open_action = root.get("/OpenAction")
        if open_action is not None:
            open_action = open_action.get_object() if isinstance(open_action, IndirectObject) else open_action
            if not isinstance(open_action, ArrayObject):
                errors.append("OpenAction 不是无执行能力的页面视图目标")
        acroform = root.get("/AcroForm")
        form_fields = 0
        if acroform is not None:
            acroform = acroform.get_object() if isinstance(acroform, IndirectObject) else acroform
            fields = acroform.get("/Fields", []) if isinstance(acroform, DictionaryObject) else []
            form_fields = len(fields)
            if form_fields or (isinstance(acroform, DictionaryObject) and "/XFA" in acroform):
                errors.append("AcroForm 含字段或 XFA 数据")
        walk_pdf_objects(
            reader.trailer,
            f"{relative}:trailer",
            findings,
            features,
            identifiers,
            set(),
        )
        annotations = 0
        for page in reader.pages:
            annotation = page.get("/Annots")
            if annotation:
                annotation = annotation.get_object() if isinstance(annotation, IndirectObject) else annotation
                annotations += len(annotation) if isinstance(annotation, ArrayObject) else 1
        if annotations:
            errors.append(f"存在 {annotations} 个页面批注")
        pages = len(reader.pages)
    except Exception as exc:
        errors.append(f"PyPDF2 严格解析失败：{type(exc).__name__}: {exc}")
        metadata = {}
        pages = 0
        annotations = 0
        form_fields = 0
        open_action = None

    dangerous = {key: count for key, count in features.items() if key in DANGEROUS_KEYS and count}
    if dangerous:
        errors.append(f"存在主动或可执行 PDF 特性：{dangerous}")

    exif = run_tool(["exiftool", "-json", "-a", "-G1", "-s", str(path)])
    if exif.returncode != 0:
        errors.append(f"exiftool 失败（rc={exif.returncode}）")
        embedded_metadata: dict[str, object] = {}
    else:
        payload = json.loads(exif.stdout)[0]
        embedded_metadata = {
            key: value
            for key, value in payload.items()
            if not key.startswith(("System:", "File:", "ExifTool:")) and key != "SourceFile"
        }
        for key, value in embedded_metadata.items():
            scan_text(str(value), f"{relative}:exif:{key}", findings, identifiers)

    with tempfile.TemporaryDirectory(prefix="raw-pdf-text-") as directory:
        text_path = Path(directory) / "text.txt"
        extracted = run_tool(["pdftotext", "-enc", "UTF-8", str(path), str(text_path)])
        if extracted.returncode != 0:
            errors.append(f"pdftotext 失败（rc={extracted.returncode}）")
            extracted_text_bytes = 0
        else:
            text = text_path.read_text(encoding="utf-8", errors="replace") if text_path.exists() else ""
            extracted_text_bytes = len(text.encode("utf-8"))
            scan_text(text, f"{relative}:extracted-text", findings, identifiers)

    pxc_comments = [match.group(0).decode("ascii").strip() for match in PXC_COMMENT_RE.finditer(data)]
    # PXC version/date/checksum comments intentionally expose no path, user, host, or credential.
    unknown_pxc_markers = data.count(b"%PXC-Ver:") - len(pxc_comments)
    if unknown_pxc_markers:
        errors.append(f"存在 {unknown_pxc_markers} 条不符合合同的 PXC 注释")
    if findings:
        errors.append(f"隐私模式命中 {len(findings)} 项")

    return {
        "file": path.name,
        "status": "PASS" if not errors else "FAIL",
        "bytes": path.stat().st_size,
        "sha256": digest,
        "pages": pages,
        "encrypted": bool(getattr(locals().get("reader", None), "is_encrypted", False)),
        "metadata": metadata,
        "embedded_metadata_fields": sorted(embedded_metadata),
        "open_action": "page-view-destination" if open_action is not None else "none",
        "acroform_fields": form_fields,
        "annotations": annotations,
        "attachments": len(attachments),
        "digital_signatures": signature_count,
        "dangerous_features": dangerous,
        "pxc_integrity_comments": len(pxc_comments),
        "extracted_text_bytes": extracted_text_bytes,
        "qpdf": "PASS" if qpdf.returncode == 0 else "FAIL",
        "findings": findings,
        "errors": errors,
        "warnings": warnings,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    required_tools = ("qpdf", "pdfdetach", "pdfsig", "pdftotext", "exiftool")
    missing_tools = [tool for tool in required_tools if shutil.which(tool) is None]
    if missing_tools:
        print(f"ERROR: 缺少 PDF 审计工具：{missing_tools}", file=sys.stderr)
        return 2
    try:
        checksums = expected_checksums()
    except ValueError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    files = sorted(RAW.glob("*.pdf"))
    collection_errors: list[str] = []
    if len(files) != EXPECTED_PDFS:
        collection_errors.append(f"raw/ 应有 {EXPECTED_PDFS} 份 PDF，实际 {len(files)}")
    actual_names = {path.name for path in files}
    if actual_names != set(checksums):
        collection_errors.append("raw/ PDF 集合与 SHA256SUMS.txt 不一致")
    identifiers = local_identifiers()
    rows = [audit_pdf(path, checksums.get(path.name, ""), identifiers) for path in files]
    failed = [row for row in rows if row["status"] != "PASS"]
    summary = {
        "pdfs": len(rows),
        "passed": len(rows) - len(failed),
        "failed": len(failed),
        "bytes": sum(int(row["bytes"]) for row in rows),
        "pages": sum(int(row["pages"]) for row in rows),
        "encrypted": sum(bool(row["encrypted"]) for row in rows),
        "attachments": sum(int(row["attachments"]) for row in rows),
        "annotations": sum(int(row["annotations"]) for row in rows),
        "dangerous_features": sum(sum(row["dangerous_features"].values()) for row in rows),
        "privacy_findings": sum(len(row["findings"]) for row in rows),
        "qpdf_passed": sum(row["qpdf"] == "PASS" for row in rows),
        "collection_errors": len(collection_errors),
    }
    payload = {
        "schema_version": 1,
        "status": "PASS" if not collection_errors and not failed else "FAIL",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": "raw/*.pdf plus raw/SHA256SUMS.txt",
        "summary": summary,
        "collection_errors": collection_errors,
        "files": rows,
        "disclosure": {
            "allowed_metadata": "书名、编委会、PDF工具版本、创建/修改日期、文档随机ID",
            "pxc_comments": "PDF-XChange 版本、处理日期和校验摘要；不含用户或主机标识",
            "filesystem_mtime": "Git 不记录源文件系统 mtime",
        },
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    for error in collection_errors:
        print(f"ERROR: {error}", file=sys.stderr)
    for row in failed:
        for error in row["errors"]:
            print(f"ERROR: raw/{row['file']}: {error}", file=sys.stderr)
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
