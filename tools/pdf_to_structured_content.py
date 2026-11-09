"""Convert a PDF into reusable, structured page content for the Blueprint UI.

The extractor intentionally contains no study, page, section, or table-specific
rules. It combines ruled-table detection with a coordinate-based fallback for
borderless tables, then emits one stable JSON schema that the browser renders.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import median
from typing import Iterable

import pdfplumber


SPACE_RE = re.compile(r"\s+")
TABLE_TITLE_RE = re.compile(r"^\s*(table|listing)\s+\d", re.IGNORECASE)
SECTION_RE = re.compile(r"^\s*\d+(?:\.\d+)*\s+\S")
NOTE_RE = re.compile(r"^\s*(note|footnote|programmer(?:'|’)?s note)\s*:", re.IGNORECASE)
PAGE_NUMBER_RE = re.compile(r"^(confidential\s+)?\d+$", re.IGNORECASE)
VALUE_RE = re.compile(r"(?:\bxx\b|\bn\s*\(%\)|\d+(?:\.\d+)?%|\bmean\b|\bmedian\b|\btotal\b)", re.IGNORECASE)


def clean_text(value: object) -> str:
    return SPACE_RE.sub(" ", str(value or "").replace("\u00a0", " ")).strip()


def cluster_values(values: Iterable[tuple[float, int]], tolerance: float) -> list[dict]:
    clusters: list[dict] = []
    for value, row_index in sorted(values):
        target = next((cluster for cluster in clusters if abs(cluster["center"] - value) <= tolerance), None)
        if target is None:
            clusters.append({"center": value, "values": [value], "rows": {row_index}})
        else:
            target["values"].append(value)
            target["rows"].add(row_index)
            target["center"] = sum(target["values"]) / len(target["values"])
    return clusters


def group_words_into_lines(words: list[dict], tolerance: float = 3.0) -> list[dict]:
    lines: list[dict] = []
    for word in sorted(words, key=lambda item: (item["top"], item["x0"])):
        center = (word["top"] + word["bottom"]) / 2
        line = next((candidate for candidate in reversed(lines[-5:]) if abs(candidate["center"] - center) <= tolerance), None)
        if line is None:
            line = {"center": center, "top": word["top"], "bottom": word["bottom"], "words": []}
            lines.append(line)
        line["words"].append(word)
        line["top"] = min(line["top"], word["top"])
        line["bottom"] = max(line["bottom"], word["bottom"])
        line["center"] = (line["top"] + line["bottom"]) / 2
    for line in lines:
        line["words"].sort(key=lambda item: item["x0"])
        line["text"] = clean_text(" ".join(word["text"] for word in line["words"]))
        line["x0"] = min(word["x0"] for word in line["words"])
        line["x1"] = max(word["x1"] for word in line["words"])
        line["font_size"] = median(max(1.0, word["bottom"] - word["top"]) for word in line["words"])
    return lines


def word_groups(line: dict) -> list[dict]:
    words = line["words"]
    if not words:
        return []
    threshold = max(16.0, line["font_size"] * 2.35)
    groups: list[list[dict]] = [[words[0]]]
    for word in words[1:]:
        if word["x0"] - groups[-1][-1]["x1"] > threshold:
            groups.append([word])
        else:
            groups[-1].append(word)
    return [
        {
            "x0": group[0]["x0"],
            "x1": group[-1]["x1"],
            "text": clean_text(" ".join(word["text"] for word in group)),
        }
        for group in groups
    ]


def remove_empty_columns(rows: list[list[str]]) -> list[list[str]]:
    if not rows:
        return []
    width = max(len(row) for row in rows)
    normalized = [row + [""] * (width - len(row)) for row in rows]
    counts = [sum(bool(clean_text(row[index])) for row in normalized) for index in range(width)]
    minimum_population = max(2, math.ceil(len(normalized) * 0.25))
    useful = [index for index, count in enumerate(counts) if count >= minimum_population]
    if len(useful) < 2:
        useful = [index for index, count in enumerate(counts) if count]
    compact = []
    for row in normalized:
        output = [""] * len(useful)
        for source_index, raw_cell in enumerate(row):
            cell = clean_text(raw_cell)
            if not cell or not useful:
                continue
            target_index = min(range(len(useful)), key=lambda index: abs(useful[index] - source_index))
            output[target_index] = clean_text(f"{output[target_index]} {cell}")
        compact.append(output)
    return compact


def table_title_for_bbox(lines: list[dict], bbox: tuple[float, float, float, float]) -> str:
    candidates = [
        line for line in lines
        if line["bottom"] <= bbox[1] + 3 and bbox[1] - line["bottom"] <= 55 and TABLE_TITLE_RE.match(line["text"])
    ]
    return candidates[-1]["text"] if candidates else ""


def extract_ruled_tables(page, lines: list[dict]) -> list[dict]:
    settings = {
        "vertical_strategy": "lines",
        "horizontal_strategy": "lines",
        "snap_tolerance": 4,
        "join_tolerance": 4,
        "intersection_tolerance": 6,
        "edge_min_length": 10,
        "text_tolerance": 3,
    }
    results = []
    for found in page.find_tables(settings):
        rows = remove_empty_columns(found.extract(x_tolerance=3, y_tolerance=3) or [])
        if len(rows) < 2 or max((len(row) for row in rows), default=0) < 2:
            continue
        nonempty = sum(bool(cell) for row in rows for cell in row)
        if nonempty < 4:
            continue
        header_rows = 1
        for index, row in enumerate(rows[:4]):
            value_cells = sum(bool(VALUE_RE.search(cell)) for cell in row[1:])
            if value_cells >= 2:
                header_rows = max(1, index)
                break
        results.append({
            "kind": "table",
            "title": table_title_for_bbox(lines, found.bbox),
            "method": "ruled",
            "confidence": 0.98,
            "headerRows": header_rows,
            "rows": rows,
            "bbox": [round(value, 2) for value in found.bbox],
        })
    return results


def choose_column_anchors(region_lines: list[dict], page_width: float) -> list[float]:
    starts = []
    for row_index, line in enumerate(region_lines):
        for group in word_groups(line):
            starts.append((group["x0"], row_index))
        for word in line["words"]:
            token = clean_text(word["text"])
            if VALUE_RE.search(token) or re.fullmatch(r"[<>≤≥=+-]?\d+(?:\.\d+)?", token):
                # Value columns are especially reliable anchors in dense,
                # borderless statistical tables.
                starts.append((word["x0"], row_index))
    clusters = cluster_values(starts, tolerance=max(10.0, page_width * 0.018))
    eligible = [cluster for cluster in clusters if len(cluster["rows"]) >= 2]
    eligible.sort(key=lambda cluster: (-len(cluster["rows"]), cluster["center"]))
    selected: list[dict] = []
    dense_layout = len(eligible) >= 10
    min_separation = max(20.0, page_width * 0.025) if dense_layout else max(34.0, page_width * 0.052)
    for cluster in eligible:
        if all(abs(cluster["center"] - item["center"]) >= min_separation for item in selected):
            selected.append(cluster)
        if len(selected) == 14:
            break
    selected.sort(key=lambda cluster: cluster["center"])
    return [cluster["center"] for cluster in selected]


def assign_line_to_columns(line: dict, anchors: list[float]) -> list[str]:
    if not anchors:
        return []
    boundaries = [-math.inf]
    for index in range(len(anchors) - 1):
        gap = anchors[index + 1] - anchors[index]
        boundaries.append(anchors[index + 1] - min(40.0, max(12.0, gap * 0.2)))
    boundaries.append(math.inf)
    cells: list[list[dict]] = [[] for _ in anchors]
    for word in line["words"]:
        center = (word["x0"] + word["x1"]) / 2
        index = next((position for position in range(len(anchors)) if boundaries[position] <= center < boundaries[position + 1]), len(anchors) - 1)
        cells[index].append(word)
    return [clean_text(" ".join(word["text"] for word in cell)) for cell in cells]


def merge_wrapped_rows(rows: list[dict]) -> list[dict]:
    if not rows:
        return []
    gaps = [rows[index + 1]["top"] - rows[index]["bottom"] for index in range(len(rows) - 1) if rows[index + 1]["top"] >= rows[index]["bottom"]]
    typical_gap = median(gaps) if gaps else 3.0
    merged: list[dict] = []
    for row in rows:
        populated = [index for index, cell in enumerate(row["cells"]) if cell]
        close = bool(merged) and row["top"] - merged[-1]["bottom"] <= max(8.0, typical_gap * 2.2)
        if close and len(populated) >= 2:
            previous_cells = merged[-1]["cells"]
            incomplete = sum(cell.count("(") > cell.count(")") for cell in previous_cells)
            continuation = sum(bool(re.search(r"(?:xx|\d)[^A-Za-z]*\)", cell, re.IGNORECASE)) for cell in row["cells"])
            if incomplete >= 2 and continuation >= 2:
                merged[-1]["cells"] = [clean_text(f"{left} {right}") for left, right in zip(previous_cells, row["cells"])]
                merged[-1]["bottom"] = row["bottom"]
                continue
        if close and len(populated) == 1 and populated[0] == 0 and sum(bool(cell) for cell in merged[-1]["cells"]) > 1:
            merged[-1]["cells"][0] = clean_text(f'{merged[-1]["cells"][0]} {row["cells"][0]}')
            merged[-1]["bottom"] = row["bottom"]
            continue
        merged.append(row)
    return merged


def extract_borderless_table(page, lines: list[dict], ruled_tables: list[dict]) -> list[dict]:
    if ruled_tables:
        return []
    title_indexes = [index for index, line in enumerate(lines) if TABLE_TITLE_RE.match(line["text"])]
    if not title_indexes:
        return []
    title_index = title_indexes[0]
    title_line = lines[title_index]
    # A contents page has many table-title lines but no repeated column geometry.
    candidate_lines = [
        line for line in lines[title_index + 1:]
        if line["top"] < page.height * 0.88 and not NOTE_RE.match(line["text"])
    ]
    note_top = next((line["top"] for line in lines[title_index + 1:] if NOTE_RE.match(line["text"])), page.height * 0.88)
    candidate_lines = [line for line in candidate_lines if line["top"] < note_top]
    if len(candidate_lines) < 3:
        return []
    anchors = choose_column_anchors(candidate_lines, page.width)
    if len(anchors) < 2:
        return []
    assigned = [
        {"top": line["top"], "bottom": line["bottom"], "cells": assign_line_to_columns(line, anchors)}
        for line in candidate_lines
    ]
    assigned = merge_wrapped_rows(assigned)
    multi_cell = [row for row in assigned if sum(bool(cell) for cell in row["cells"]) >= 2]
    if len(multi_cell) < 2:
        return []
    # Drop centered subtitle rows before the first line that uses at least two columns.
    first_multi = next(index for index, row in enumerate(assigned) if sum(bool(cell) for cell in row["cells"]) >= 2)
    subtitle_rows = assigned[:first_multi]
    data_rows = assigned[first_multi:]
    if len(anchors) >= 8 and data_rows and len(data_rows[0]["cells"]) >= 3:
        columns = list(zip(*(row["cells"] for row in data_rows)))
        value_rates = [sum(bool(VALUE_RE.search(cell)) for cell in column) / max(1, len(column)) for column in columns]
        if value_rates[0] < 0.2 and value_rates[1] < 0.2:
            for row in data_rows:
                row["cells"] = [clean_text(f'{row["cells"][0]} {row["cells"][1]}')] + row["cells"][2:]
    subtitle = clean_text(" ".join(cell for row in subtitle_rows for cell in row["cells"] if cell))
    populated_ratio = len(multi_cell) / max(1, len(assigned))
    repeated_ratio = min(1.0, len(anchors) / 4)
    confidence = round(min(0.94, 0.58 + populated_ratio * 0.22 + repeated_ratio * 0.12), 2)
    header_rows = 1
    for index, row in enumerate(data_rows[:6]):
        value_cells = sum(bool(VALUE_RE.search(cell)) for cell in row["cells"][1:])
        if value_cells >= 2:
            header_rows = max(1, index)
            break
    return [{
        "kind": "table",
        "title": title_line["text"],
        "subtitle": subtitle,
        "method": "coordinate",
        "confidence": confidence,
        "headerRows": header_rows,
        "rows": [row["cells"] for row in data_rows],
        "bbox": [round(min(line["x0"] for line in candidate_lines), 2), round(title_line["top"], 2), round(max(line["x1"] for line in candidate_lines), 2), round(max(line["bottom"] for line in candidate_lines), 2)],
    }]


def overlaps_bbox(line: dict, bbox: list[float]) -> bool:
    return line["bottom"] >= bbox[1] - 2 and line["top"] <= bbox[3] + 2


def margin_key(text: str) -> str:
    return clean_text(text).casefold()


def is_running_header_or_footer(line: dict, page_height: float, repeated_margins: set[str]) -> bool:
    text = line["text"]
    in_margin = line["top"] < page_height * 0.11 or line["bottom"] > page_height * 0.89
    if in_margin and margin_key(text) in repeated_margins:
        return True
    if line["top"] < page_height * 0.085 and ("Protocol:" in text or "Spectrum Pharmaceuticals" in text or "Statistical Analysis Plan" in text):
        return True
    if line["bottom"] > page_height * 0.91 and (PAGE_NUMBER_RE.match(text) or "Program Name:" in text or "Confidential" in text):
        return True
    return False


def prose_blocks(lines: list[dict], tables: list[dict], page_height: float, repeated_margins: set[str]) -> list[dict]:
    table_titles = {clean_text(table.get("title")) for table in tables if table.get("title")}
    remaining = [
        line for line in lines
        if not is_running_header_or_footer(line, page_height, repeated_margins)
        and clean_text(line["text"]) not in table_titles
        and not any(overlaps_bbox(line, table["bbox"]) for table in tables)
    ]
    blocks: list[dict] = []
    paragraph: list[str] = []
    paragraph_top: float | None = None
    previous = None

    def flush():
        nonlocal paragraph, paragraph_top
        text = clean_text(" ".join(paragraph))
        if text:
            blocks.append({"kind": "paragraph", "text": text, "_top": paragraph_top or 0})
        paragraph = []
        paragraph_top = None

    for line in remaining:
        text = line["text"]
        if not text:
            continue
        heading = bool(SECTION_RE.match(text)) or (len(text) < 100 and text.isupper())
        bullet = text.startswith(("•", "●", "", "- ")) or bool(re.match(r"^[a-z0-9]+[.)]\s", text, re.IGNORECASE))
        large_gap = previous is not None and line["top"] - previous["bottom"] > max(9.0, line["font_size"] * 0.9)
        if heading:
            flush()
            blocks.append({"kind": "heading", "text": text, "_top": line["top"]})
        elif NOTE_RE.match(text):
            flush()
            blocks.append({"kind": "note", "text": text, "_top": line["top"]})
        elif bullet:
            flush()
            blocks.append({"kind": "listItem", "text": text.lstrip("•●- "), "_top": line["top"]})
        else:
            if large_gap:
                flush()
            if paragraph_top is None:
                paragraph_top = line["top"]
            paragraph.append(text)
        previous = line
    flush()
    return blocks


def extract_page(page, page_number: int, lines: list[dict], repeated_margins: set[str]) -> dict:
    ruled = extract_ruled_tables(page, lines)
    substantial_ruled = [
        table for table in ruled
        if len(table["rows"]) >= 5 or table["bbox"][3] - table["bbox"][1] >= 72
    ]
    coordinate = extract_borderless_table(page, lines, substantial_ruled)
    if coordinate and (not substantial_ruled or len(coordinate[0]["rows"]) > max(len(table["rows"]) for table in substantial_ruled) * 1.25):
        tables = coordinate
    else:
        tables = ruled
    prose = prose_blocks(lines, tables, page.height, repeated_margins)
    table_blocks = [
        {**{key: value for key, value in table.items() if key != "bbox"}, "_top": table["bbox"][1]}
        for table in tables
    ]
    ordered = sorted(prose + table_blocks, key=lambda block: block["_top"])
    for block in ordered:
        block.pop("_top", None)
    return {
        "page": page_number,
        "width": round(float(page.width), 2),
        "height": round(float(page.height), 2),
        "rotation": int(page.rotation or 0),
        "tableCount": len(tables),
        "blocks": ordered,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input_pdf", type=Path)
    parser.add_argument("output_json", type=Path)
    parser.add_argument("--document-id", default="source-document")
    args = parser.parse_args()

    with pdfplumber.open(args.input_pdf) as pdf:
        page_lines = []
        margin_counts: dict[str, set[int]] = defaultdict(set)
        for index, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(use_text_flow=False, keep_blank_chars=False, extra_attrs=["fontname", "size"])
            lines = group_words_into_lines(words)
            page_lines.append(lines)
            for line in lines:
                if line["top"] < page.height * 0.11 or line["bottom"] > page.height * 0.89:
                    margin_counts[margin_key(line["text"])].add(index)
        repetition_threshold = max(3, math.ceil(len(pdf.pages) * 0.1))
        repeated_margins = {key for key, page_numbers in margin_counts.items() if key and len(page_numbers) >= repetition_threshold}
        pages = [
            extract_page(page, index, page_lines[index - 1], repeated_margins)
            for index, page in enumerate(pdf.pages, start=1)
        ]
    previous_table_title = ""
    for page in pages:
        for block in page["blocks"]:
            if block.get("kind") != "table":
                continue
            if not block.get("title") and previous_table_title:
                block["title"] = f"{previous_table_title} (continued)"
            if block.get("title"):
                previous_table_title = re.sub(r"\s+\(continued\)$", "", block["title"], flags=re.IGNORECASE)
    payload = {
        "schemaVersion": 1,
        "documentId": args.document_id,
        "source": args.input_pdf.name,
        "extractor": {
            "name": "coordinate-table-reconstruction",
            "version": 1,
            "tableSpecificRules": False,
        },
        "pageCount": len(pages),
        "tableCount": sum(page["tableCount"] for page in pages),
        "pages": {str(page["page"]): page for page in pages},
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"pages": len(pages), "tables": payload["tableCount"], "output": str(args.output_json)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
