#!/usr/bin/env python3
"""Build deterministic EPUB3 files from the 17 canonical Markdown sources."""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import posixpath
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
CATALOG_PATH = ROOT / "catalog.json"
CSS = ROOT / "epub" / "epub.css"
TABLE_FILTER = ROOT / "epub" / "wrap_tables.lua"
PANDOC_DATA = ROOT / "epub" / "pandoc-data"
BUILD_DIR = ROOT / ".build"
DEFAULT_OUTPUT = ROOT / "dist"
REPORT = ROOT / "reports" / "build-report.json"

AUTHOR = "数理化自学丛书编委会"
LANGUAGE = "zh-CN"
HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
MATH_SPAN = re.compile(
    r"\$\$(.*?)\$\$|\\\[(.*?)\\\]|\\\((.*?)\\\)|(?<!\\)\$(?!\$)(.*?)(?<!\\)\$",
    re.S,
)
ROOT_TAGS = {
    "html": re.compile(br"<html\b[^>]*>", re.I),
    "package": re.compile(br"<package\b[^>]*>", re.I),
    "ncx": re.compile(br"<ncx\b[^>]*>", re.I),
}
XML_LANG_ATTR = re.compile(br"\s+xml:lang\s*=\s*(['\"]).*?\1", re.I)
LANG_ATTR = re.compile(br"\s+lang\s*=\s*(['\"]).*?\1", re.I)

CONTAINER_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
OPF_NS = "http://www.idpf.org/2007/opf"
DC_NS = "http://purl.org/dc/elements/1.1/"
XHTML_NS = "http://www.w3.org/1999/xhtml"
EPUB_NS = "http://www.idpf.org/2007/ops"
NCX_NS = "http://www.daisy.org/z3986/2005/ncx/"
MATHML_NS = "http://www.w3.org/1998/Math/MathML"
XML_NS = "http://www.w3.org/XML/1998/namespace"


class BuildError(RuntimeError):
    pass


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_xml(data: bytes, name: str) -> ET.Element:
    try:
        return ET.fromstring(data)
    except ET.ParseError as exc:
        raise BuildError(f"{name}: XML 解析失败：{exc}") from exc


def load_catalog() -> dict:
    payload = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
    if len(payload.get("books", [])) != 17:
        raise BuildError("catalog.json 必须登记 17 册")
    return payload


def package_path(base: str, href: str) -> str:
    return posixpath.normpath(posixpath.join(posixpath.dirname(base), unquote(href)))


def locate_package(entries: dict[str, bytes]) -> tuple[str, ET.Element, dict[str, ET.Element], str, str]:
    container_name = "META-INF/container.xml"
    if container_name not in entries:
        raise BuildError("缺少 META-INF/container.xml")
    container = parse_xml(entries[container_name], container_name)
    rootfiles = container.findall(f".//{{{CONTAINER_NS}}}rootfile")
    if len(rootfiles) != 1:
        raise BuildError(f"container.xml rootfile 数量应为 1，实际 {len(rootfiles)}")
    opf_name = rootfiles[0].get("full-path") or ""
    if opf_name not in entries:
        raise BuildError(f"OPF 不存在：{opf_name}")
    opf = parse_xml(entries[opf_name], opf_name)
    manifest = opf.find(f"{{{OPF_NS}}}manifest")
    if manifest is None:
        raise BuildError("OPF 缺少 manifest")
    by_id = {item.get("id", ""): item for item in manifest.findall(f"{{{OPF_NS}}}item")}
    nav_items = [item for item in by_id.values() if "nav" in (item.get("properties") or "").split()]
    ncx_items = [item for item in by_id.values() if item.get("media-type") == "application/x-dtbncx+xml"]
    if len(nav_items) != 1 or len(ncx_items) != 1:
        raise BuildError(f"必须各有一个 nav/NCX，实际 nav={len(nav_items)} ncx={len(ncx_items)}")
    nav_name = package_path(opf_name, nav_items[0].get("href") or "")
    ncx_name = package_path(opf_name, ncx_items[0].get("href") or "")
    if nav_name not in entries or ncx_name not in entries:
        raise BuildError("nav 或 NCX 文件不存在")
    return opf_name, opf, by_id, nav_name, ncx_name


def normalize_text(node: ET.Element) -> str:
    return " ".join("".join(node.itertext()).split())


def canonical_target(from_name: str, href: str) -> str:
    parts = urlsplit(href)
    path = unquote(parts.path)
    target = from_name if not path else posixpath.normpath(posixpath.join(posixpath.dirname(from_name), path))
    return f"{target}#{unquote(parts.fragment)}" if parts.fragment else target


def nav_rows(data: bytes, nav_name: str) -> list[tuple[str, str, int]]:
    root = parse_xml(data, nav_name)
    toc = next(
        (
            node
            for node in root.iter(f"{{{XHTML_NS}}}nav")
            if "toc" in (node.get(f"{{{EPUB_NS}}}type") or "").split()
        ),
        None,
    )
    if toc is None:
        raise BuildError(f"{nav_name}: 缺少 epub:type=toc")
    ordered = toc.find(f"{{{XHTML_NS}}}ol")
    if ordered is None:
        raise BuildError(f"{nav_name}: toc 缺少 ol")
    rows: list[tuple[str, str, int]] = []

    def walk(ol: ET.Element, depth: int) -> None:
        for li in ol.findall(f"{{{XHTML_NS}}}li"):
            link = li.find(f"{{{XHTML_NS}}}a")
            if link is not None and link.get("href"):
                rows.append((normalize_text(link), canonical_target(nav_name, link.get("href") or ""), depth))
            nested = li.find(f"{{{XHTML_NS}}}ol")
            if nested is not None:
                walk(nested, depth + 1)

    walk(ordered, 1)
    return rows


def ncx_rows(data: bytes, ncx_name: str) -> list[tuple[str, str, int]]:
    root = parse_xml(data, ncx_name)
    nav_map = root.find(f"{{{NCX_NS}}}navMap")
    if nav_map is None:
        raise BuildError(f"{ncx_name}: 缺少 navMap")
    rows: list[tuple[str, str, int]] = []

    def walk(parent: ET.Element, depth: int) -> None:
        for point in parent.findall(f"{{{NCX_NS}}}navPoint"):
            label_node = point.find(f"{{{NCX_NS}}}navLabel/{{{NCX_NS}}}text")
            content = point.find(f"{{{NCX_NS}}}content")
            if label_node is None or content is None or not content.get("src"):
                raise BuildError(f"{ncx_name}: navPoint 缺少标签或目标")
            rows.append((normalize_text(label_node), canonical_target(ncx_name, content.get("src") or ""), depth))
            walk(point, depth + 1)

    walk(nav_map, 1)
    return rows


def patch_root_language(data: bytes, tag_name: str, include_plain_lang: bool) -> bytes:
    pattern = ROOT_TAGS[tag_name]
    match = pattern.search(data)
    if match is None:
        raise BuildError(f"缺少 <{tag_name}> 根标签")
    tag = XML_LANG_ATTR.sub(b"", match.group(0))
    if include_plain_lang:
        tag = LANG_ATTR.sub(b"", tag)
    attrs = b' lang="zh-CN" xml:lang="zh-CN"' if include_plain_lang else b' xml:lang="zh-CN"'
    replacement = tag[:-1] + attrs + b">"
    return data[: match.start()] + replacement + data[match.end() :]


def patch_nav_from_ncx(nav_data: bytes, nav_name: str, ncx_data: bytes, ncx_name: str) -> bytes:
    existing = nav_rows(nav_data, nav_name)
    truth = ncx_rows(ncx_data, ncx_name)
    if existing == truth:
        return nav_data
    if len(truth) != len(existing) + 1 or existing != truth[1:]:
        raise BuildError(f"nav/NCX 无法按题名页单项差异同步：nav={len(existing)} ncx={len(truth)}")
    label, canonical, depth = truth[0]
    target, separator, fragment = canonical.partition("#")
    if depth != 1 or not target.endswith("title_page.xhtml"):
        raise BuildError(f"NCX 首项不是题名页：{truth[0]}")
    href = posixpath.relpath(target, posixpath.dirname(nav_name))
    if separator:
        href += f"#{fragment}"
    text = nav_data.decode("utf-8")
    toc_match = re.search(r'<nav\b[^>]*\bepub:type=["\']toc["\'][^>]*>.*?</nav>', text, re.S | re.I)
    if toc_match is None:
        raise BuildError(f"{nav_name}: 无法定位 toc nav")
    toc_text = toc_match.group(0)
    ol_match = re.search(r"<ol\b[^>]*>", toc_text, re.I)
    if ol_match is None:
        raise BuildError(f"{nav_name}: 无法定位 toc ol")
    item = (
        '\n<li id="toc-li-titlepage"><a href="'
        + html.escape(href, quote=True)
        + '">'
        + html.escape(label)
        + "</a></li>"
    )
    patched_toc = toc_text[: ol_match.end()] + item + toc_text[ol_match.end() :]
    patched = (text[: toc_match.start()] + patched_toc + text[toc_match.end() :]).encode("utf-8")
    if nav_rows(patched, nav_name) != truth:
        raise BuildError(f"{nav_name}: 同步后仍与 NCX 不一致")
    return patched


def deterministic_pack(entries: dict[str, bytes], target: Path, epoch: int) -> None:
    if entries.get("mimetype") != b"application/epub+zip":
        raise BuildError("mimetype 内容不正确")
    timestamp = time.gmtime(max(epoch, 315532800))[:6]
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent, delete=False) as handle:
        temporary = Path(handle.name)
    try:
        with zipfile.ZipFile(temporary, "w") as archive:
            ordered = ["mimetype", *sorted(name for name in entries if name != "mimetype")]
            for name in ordered:
                directory = name.endswith("/")
                info = zipfile.ZipInfo(name, date_time=timestamp)
                info.create_system = 3
                mode = 0o040755 if directory else 0o100644
                info.external_attr = (mode & 0xFFFF) << 16
                info.compress_type = zipfile.ZIP_STORED if name == "mimetype" or directory else zipfile.ZIP_DEFLATED
                archive.writestr(
                    info,
                    entries[name],
                    compress_type=info.compress_type,
                    compresslevel=None if info.compress_type == zipfile.ZIP_STORED else 9,
                )
        os.replace(temporary, target)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def finalize_epub(candidate: Path, target: Path, epoch: int) -> None:
    with zipfile.ZipFile(candidate) as archive:
        if len(archive.namelist()) != len(set(archive.namelist())):
            raise BuildError(f"{candidate.name}: ZIP 存在重复条目")
        entries = {info.filename: archive.read(info.filename) for info in archive.infolist()}
    opf_name, _opf, by_id, nav_name, ncx_name = locate_package(entries)
    entries[opf_name] = patch_root_language(entries[opf_name], "package", False)
    entries[nav_name] = patch_nav_from_ncx(entries[nav_name], nav_name, entries[ncx_name], ncx_name)
    for item in by_id.values():
        if item.get("media-type") in {"application/xhtml+xml", "text/html"}:
            name = package_path(opf_name, item.get("href") or "")
            entries[name] = patch_root_language(entries[name], "html", True)
    entries[ncx_name] = patch_root_language(entries[ncx_name], "ncx", False)
    deterministic_pack(entries, target, epoch)


def metadata_values(metadata: ET.Element, tag: str) -> list[str]:
    return ["".join(node.itertext()).strip() for node in metadata.findall(tag)]


def validate_links(entries: dict[str, bytes], xml_names: list[str]) -> list[dict[str, str]]:
    errors: list[dict[str, str]] = []
    parsed: dict[str, ET.Element] = {}
    ids: dict[str, set[str]] = {}
    for name in xml_names:
        if name not in entries:
            continue
        root = parse_xml(entries[name], name)
        parsed[name] = root
        ids[name] = {
            value
            for node in root.iter()
            for value in (node.get("id"), node.get(f"{{{XML_NS}}}id"))
            if value
        }
    for name, root in parsed.items():
        for node in root.iter():
            for key, value in node.attrib.items():
                local = key.rsplit("}", 1)[-1]
                if local not in {"href", "src", "poster", "data"} or not value:
                    continue
                parts = urlsplit(value)
                if parts.scheme in {"http", "https", "mailto", "tel", "data"} or parts.netloc:
                    continue
                if parts.scheme or value.startswith("//"):
                    errors.append({"source": name, "value": value, "reason": "unsafe_scheme"})
                    continue
                path = unquote(parts.path)
                target = name if not path else posixpath.normpath(posixpath.join(posixpath.dirname(name), path))
                if target not in entries:
                    errors.append({"source": name, "value": value, "reason": "missing_file"})
                elif parts.fragment and target in ids and unquote(parts.fragment) not in ids[target]:
                    errors.append({"source": name, "value": value, "reason": "missing_fragment"})
    return errors


def validate_epub(path: Path, book: dict, expected_math: int) -> dict[str, object]:
    errors: list[str] = []
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        infos = archive.infolist()
        if bad:
            errors.append(f"CRC 错误：{bad}")
        if not infos or infos[0].filename != "mimetype" or infos[0].compress_type != zipfile.ZIP_STORED:
            errors.append("mimetype 不是首个且未压缩的条目")
        entries = {info.filename: archive.read(info.filename) for info in infos}
    try:
        opf_name, opf, by_id, nav_name, ncx_name = locate_package(entries)
    except BuildError as exc:
        return {"status": "FAIL", "errors": [str(exc)]}

    if opf.get(f"{{{XML_NS}}}lang") != LANGUAGE:
        errors.append("OPF package xml:lang 不是 zh-CN")
    metadata = opf.find(f"{{{OPF_NS}}}metadata")
    if metadata is None:
        errors.append("OPF 缺少 metadata")
    else:
        expected = {
            f"{{{DC_NS}}}title": [book["title"]],
            f"{{{DC_NS}}}creator": [AUTHOR],
            f"{{{DC_NS}}}language": [LANGUAGE],
            f"{{{DC_NS}}}identifier": [book["identifier"]],
        }
        for tag, wanted in expected.items():
            actual = metadata_values(metadata, tag)
            if actual != wanted:
                errors.append(f"OPF 元数据 {tag.rsplit('}', 1)[-1]}={actual!r}，应为 {wanted!r}")

    manifest_images = 0
    xml_names = ["META-INF/container.xml", opf_name, ncx_name]
    xhtml_names: list[str] = []
    for item in by_id.values():
        name = package_path(opf_name, item.get("href") or "")
        if name not in entries:
            errors.append(f"manifest 资源不存在：{name}")
        if (item.get("media-type") or "").startswith("image/"):
            manifest_images += 1
        if item.get("media-type") in {"application/xhtml+xml", "text/html"}:
            xhtml_names.append(name)
            xml_names.append(name)

    mathml = 0
    image_elements = 0
    empty_alt = 0
    escaped_comments = 0
    tables = 0
    for name in xhtml_names:
        root = parse_xml(entries[name], name)
        if root.get("lang") != LANGUAGE or root.get(f"{{{XML_NS}}}lang") != LANGUAGE:
            errors.append(f"{name}: 根语言不是 zh-CN")
        mathml += sum(1 for node in root.iter() if node.tag == f"{{{MATHML_NS}}}math")
        tables += sum(1 for node in root.iter() if node.tag == f"{{{XHTML_NS}}}table")
        for image in root.iter(f"{{{XHTML_NS}}}img"):
            image_elements += 1
            if not (image.get("alt") or "").strip():
                empty_alt += 1
        escaped_comments += entries[name].count(b"&lt;!--")
    if mathml != expected_math:
        errors.append(f"MathML 数量 {mathml}，源数学片段 {expected_math}")
    if empty_alt:
        errors.append(f"存在 {empty_alt} 个空图片替代文本")
    if escaped_comments:
        errors.append(f"存在 {escaped_comments} 个转义注释标记")

    link_errors = validate_links(entries, xml_names)
    if link_errors:
        errors.append(f"存在 {len(link_errors)} 个内部链接错误")
    try:
        nav_identity = nav_rows(entries[nav_name], nav_name)
        ncx_identity = ncx_rows(entries[ncx_name], ncx_name)
        nav_ncx_sync = nav_identity == ncx_identity
    except BuildError as exc:
        nav_ncx_sync = False
        errors.append(str(exc))
    if not nav_ncx_sync:
        errors.append("nav.xhtml 与 toc.ncx 不同步")

    return {
        "status": "PASS" if not errors else "FAIL",
        "bytes": path.stat().st_size,
        "sha256": sha256(path),
        "mathml_elements": mathml,
        "manifest_image_files": manifest_images,
        "image_elements": image_elements,
        "tables": tables,
        "xhtml_files": len(xhtml_names),
        "toc_items": len(nav_identity) if nav_ncx_sync else None,
        "nav_ncx_synchronized": nav_ncx_sync,
        "empty_image_alt": empty_alt,
        "escaped_comment_markers": escaped_comments,
        "link_errors": link_errors,
        "errors": errors,
    }


def prepare_source(source: Path, destination: Path) -> tuple[int, int, str]:
    text = source.read_text(encoding="utf-8")
    comments = HTML_COMMENT.findall(text)
    visible = HTML_COMMENT.sub("", text)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(visible, encoding="utf-8")
    return len(comments), len(MATH_SPAN.findall(visible)), sha256(source)


def pandoc_version() -> str:
    proc = subprocess.run(["pandoc", "--version"], text=True, capture_output=True, check=True)
    return proc.stdout.splitlines()[0]


def build_one(book: dict, output_dir: Path, epoch: int) -> tuple[str, dict[str, object]]:
    source = ROOT / book["file"]
    if not source.is_file():
        raise BuildError(f"源文件不存在：{source}")
    clean = BUILD_DIR / "clean" / source.name
    candidate = BUILD_DIR / "pandoc" / f"{source.stem}.epub"
    output = output_dir / f"{source.stem}.epub"
    comments, source_math, source_sha = prepare_source(source, clean)
    candidate.parent.mkdir(parents=True, exist_ok=True)
    command = [
        "pandoc",
        str(clean),
        f"--data-dir={PANDOC_DATA}",
        "--from=markdown+tex_math_dollars+tex_math_single_backslash-raw_html",
        "--to=epub3",
        "--standalone",
        "--toc",
        "--toc-depth=2",
        "--split-level=2",
        "--mathml",
        f"--lua-filter={TABLE_FILTER}",
        f"--css={CSS}",
        f"--resource-path={source.parent}",
        "--metadata",
        f"title={book['title']}",
        "--metadata",
        f"author={AUTHOR}",
        "--metadata",
        f"lang={LANGUAGE}",
        "--metadata",
        f"identifier={book['identifier']}",
        "--output",
        str(candidate),
    ]
    env = os.environ.copy()
    env["SOURCE_DATE_EPOCH"] = str(epoch)
    proc = subprocess.run(command, cwd=ROOT, env=env, text=True, capture_output=True)
    if proc.returncode != 0:
        raise BuildError(f"{source.name}: Pandoc 失败：\n{proc.stderr}")
    finalize_epub(candidate, output, epoch)
    validation = validate_epub(output, book, source_math)
    row: dict[str, object] = {
        "source": source.name,
        "source_sha256": source_sha,
        "source_comments_stripped": comments,
        "source_math_spans": source_math,
        "output": output.relative_to(ROOT).as_posix() if output.is_relative_to(ROOT) else str(output),
        "pandoc_warnings": [line for line in proc.stderr.splitlines() if line.strip()],
        "validation": validation,
    }
    if row["pandoc_warnings"]:
        validation["status"] = "FAIL"
        validation["errors"].append(f"Pandoc 产生 {len(row['pandoc_warnings'])} 条警告")
    return book["file"], row


def select_books(books: list[dict], selectors: list[str]) -> list[dict]:
    if not selectors:
        return books
    selected: list[dict] = []
    for selector in selectors:
        matches = [
            book
            for book in books
            if selector in {book["title"], book["file"], Path(book["file"]).stem}
        ]
        if len(matches) != 1:
            raise BuildError(f"书目选择器必须唯一匹配：{selector!r}，实际 {len(matches)}")
        if matches[0] not in selected:
            selected.append(matches[0])
    return selected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--book", action="append", default=[], help="按题名、源文件名或文件 stem 构建；可重复")
    parser.add_argument("--jobs", type=int, default=min(4, os.cpu_count() or 1))
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--verify-only", action="store_true", help="只验证已有 EPUB，不重新构建")
    args = parser.parse_args()

    if shutil.which("pandoc") is None and not args.verify_only:
        print("ERROR: 未找到 pandoc", file=sys.stderr)
        return 2
    catalog = load_catalog()
    books = select_books(catalog["books"], args.book)
    epoch = int(os.environ.get("SOURCE_DATE_EPOCH", catalog["source_date_epoch"]))
    output_dir = args.output_dir.resolve()
    rows_by_file: dict[str, dict[str, object]] = {}

    if args.verify_only:
        for book in books:
            source = ROOT / book["file"]
            visible = HTML_COMMENT.sub("", source.read_text(encoding="utf-8"))
            expected_math = len(MATH_SPAN.findall(visible))
            output = output_dir / f"{source.stem}.epub"
            if not output.is_file():
                rows_by_file[book["file"]] = {"validation": {"status": "FAIL", "errors": ["输出不存在"]}}
            else:
                rows_by_file[book["file"]] = {"validation": validate_epub(output, book, expected_math)}
    else:
        output_dir.mkdir(parents=True, exist_ok=True)
        BUILD_DIR.mkdir(parents=True, exist_ok=True)
        with ThreadPoolExecutor(max_workers=max(1, args.jobs)) as executor:
            futures = {executor.submit(build_one, book, output_dir, epoch): book for book in books}
            for future in as_completed(futures):
                book = futures[future]
                try:
                    name, row = future.result()
                except Exception as exc:
                    name = book["file"]
                    row = {"validation": {"status": "FAIL", "errors": [str(exc)]}}
                rows_by_file[name] = row
                print(f"{name}: {row['validation']['status']}")

    ordered_rows = {book["file"]: rows_by_file[book["file"]] for book in books}
    passed = sum(row["validation"]["status"] == "PASS" for row in ordered_rows.values())
    total_math = sum(int(row["validation"].get("mathml_elements", 0)) for row in ordered_rows.values())
    total_manifest_images = sum(int(row["validation"].get("manifest_image_files", 0)) for row in ordered_rows.values())
    total_image_elements = sum(int(row["validation"].get("image_elements", 0)) for row in ordered_rows.values())
    payload = {
        "schema_version": 1,
        "status": "PASS" if passed == len(books) else "FAIL",
        "mode": "verify-only" if args.verify_only else "build",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_date_epoch": epoch,
        "pandoc": pandoc_version() if shutil.which("pandoc") else None,
        "summary": {
            "books": len(books),
            "passed": passed,
            "failed": len(books) - passed,
            "mathml_elements": total_math,
            "manifest_image_files": total_manifest_images,
            "image_elements": total_image_elements,
            "nav_ncx_synchronized": sum(
                bool(row["validation"].get("nav_ncx_synchronized")) for row in ordered_rows.values()
            ),
        },
        "books": ordered_rows,
    }
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(payload["summary"], ensure_ascii=False))
    return 0 if payload["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
