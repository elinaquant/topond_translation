#!/usr/bin/env python3
"""Local JSmol viewer for TOPOND cell, critical points and bond paths."""

from __future__ import annotations

import argparse
import html
import json
import math
import mimetypes
import os
import re
import socketserver
import sys
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from collections import defaultdict

from make_supercell import _parse_cryst1
from topond_logging import get_logger, install_excepthook, log_action, log_error, setup

BASE_DIR = Path(__file__).resolve().parent
ASSET_DIR = BASE_DIR / "topond_jsmol"
JSMOL_DIR = ASSET_DIR / "jsmol"
LOGGER = get_logger("viewer")
APPLET_ID = "topondJmol"

CP_CHAINS = {
    "bcp": "B",
    "rcp": "C",
    "ccp": "D",
    "ncp": "E",
}
BP_CHAINS = {
    "crystal": "F",
    "calc": "G",
}


class ViewerError(Exception):
    pass


def _pad_record(line: str) -> str:
    line = line.rstrip("\r\n")
    if len(line) < 80:
        line = line.ljust(80)
    return line[:80]


def _set_chain(line: str, chain: str) -> str:
    return line[:21] + chain + line[22:]


def _set_serial(line: str, serial: int) -> str:
    return line[:6] + f"{serial % 100000:5d}" + line[11:]


def _record_tag(line: str) -> str:
    resname = line[17:20].strip().upper()
    segid = line[72:76].strip().upper() if len(line) >= 76 else ""
    if segid.startswith("CP") or segid in {"CRYS", "CALC"}:
        return segid
    return resname


def _cp_chain(line: str) -> tuple[str, str] | None:
    tag = _record_tag(line)
    if tag.startswith("CPM") or tag.startswith("BCP"):
        return "bcp", CP_CHAINS["bcp"]
    if tag.startswith("CPP1") or tag.startswith("RCP"):
        return "rcp", CP_CHAINS["rcp"]
    if tag.startswith("CPP3") or tag.startswith("CCP"):
        return "ccp", CP_CHAINS["ccp"]
    if tag.startswith("CPN") or tag.startswith("NCP"):
        return "ncp", CP_CHAINS["ncp"]
    return None


def _bp_kind(line: str) -> str | None:
    tag = _record_tag(line)
    if tag in {"CBP", "CRYS"}:
        return "crystal"
    if tag in {"KBP", "CALC"}:
        return "calc"
    return None


def _read_pdb(path: Path) -> tuple[str | None, list[str]]:
    cryst1: str | None = None
    records: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if line.startswith("CRYST1"):
                    if cryst1 is None:
                        cryst1 = _pad_record(line)
                    continue
                if line.startswith("ATOM  ") or line.startswith("HETATM"):
                    records.append(_pad_record(line))
    except OSError as exc:
        raise ViewerError(f"Не удалось прочитать {path}: {exc}") from exc
    return cryst1, records


def _bp_point(record: str) -> tuple[float, float, float]:
    return (
        float(record[30:38]),
        float(record[38:46]),
        float(record[46:54]),
    )


def _dot_group_segments(
    records: list[str],
    max_gap: float = 0.5,
) -> list[list[list[float]]]:
    """Straight segments for one bond-path line (a group of dots).

    Dots of one line are collinear, so the first and last dot define the
    segment.  A jump larger than ``max_gap`` means the line was split at the
    cell boundary (``--symmetry-image cut``); each continuous piece then
    becomes its own segment.
    """
    if not records:
        return []
    points = [_bp_point(record) for record in records]
    segments: list[list[list[float]]] = []

    def add(start, end) -> None:
        if math.dist(start, end) <= 1e-6:
            return
        segments.append([
            [round(value, 4) for value in start],
            [round(value, 4) for value in end],
        ])

    start = points[0]
    previous = points[0]
    for point in points[1:]:
        if math.dist(point, previous) > max_gap:
            add(start, previous)
            start = point
        previous = point
    add(start, previous)
    return segments


def _bp_segments(records: list[str]) -> list[list[list[float]]]:
    """Group bond-path dots by residue and return one segment per line."""
    segments: list[list[list[float]]] = []
    group: list[str] = []
    last_key: tuple[str, str, str] | None = None
    for record in records:
        key = (record[17:20], record[21], record[22:26])
        if group and key != last_key:
            segments.extend(_dot_group_segments(group))
            group = []
        group.append(record)
        last_key = key
    if group:
        segments.extend(_dot_group_segments(group))
    return segments


def _cell_box_edges(
    cryst1: str | None,
    cell_records: list[str],
) -> list[list[list[float]]]:
    if not cryst1:
        return []
    try:
        lattice = _parse_cryst1(cryst1)
    except (TypeError, ValueError):
        return []

    fractions: list[tuple[float, float, float]] = []
    for line in cell_records:
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except (TypeError, ValueError):
            continue
        fractions.append(lattice.cart_to_frac(x, y, z))
    if not fractions:
        return []

    origin = tuple(
        sum(point[axis] for point in fractions) / len(fractions) - 0.5
        for axis in range(3)
    )
    corners: dict[tuple[int, int, int], tuple[float, float, float]] = {}
    for i in (0, 1):
        for j in (0, 1):
            for k in (0, 1):
                corners[(i, j, k)] = lattice.frac_to_cart(
                    origin[0] + i,
                    origin[1] + j,
                    origin[2] + k,
                )

    edges: list[list[list[float]]] = []
    for (i, j, k), start in corners.items():
        for axis, (di, dj, dk) in enumerate(
            ((1, 0, 0), (0, 1, 0), (0, 0, 1))
        ):
            if (i, j, k)[axis]:
                continue
            end = corners[(i + di, j + dj, k + dk)]
            edges.append(
                [
                    [round(value, 4) for value in start],
                    [round(value, 4) for value in end],
                ]
            )
    return edges


def _align_cp_points(
    points: list[tuple[str, float, float, float]],
    cryst1: str | None,
    cell_records: list[str],
) -> tuple[list[tuple[str, float, float, float]], dict]:
    info = {
        "enabled": True,
        "moved": 0,
        "reason": "",
    }
    if not points:
        info["reason"] = "no_cp"
        return points, info
    if not cryst1:
        info["reason"] = "no_cryst1"
        return points, info
    try:
        lattice = _parse_cryst1(cryst1)
    except (TypeError, ValueError):
        info["reason"] = "invalid_cryst1"
        return points, info

    atom_fractions: list[tuple[float, float, float]] = []
    for line in cell_records:
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except (TypeError, ValueError):
            continue
        atom_fractions.append(lattice.cart_to_frac(x, y, z))
    if not atom_fractions:
        info["reason"] = "no_atoms"
        return points, info

    ax, ay, az = lattice.a_vec
    bx, by, bz = lattice.b_vec
    cx, cy, cz = lattice.c_vec
    aligned: list[tuple[str, float, float, float]] = []
    moved = 0
    for key, x, y, z in points:
        fx, fy, fz = lattice.cart_to_frac(x, y, z)
        best_d2: float | None = None
        best_n = (0, 0, 0)
        for afx, afy, afz in atom_fractions:
            n1 = round(afx - fx)
            n2 = round(afy - fy)
            n3 = round(afz - fz)
            dfx = afx - fx - n1
            dfy = afy - fy - n2
            dfz = afz - fz - n3
            dx = dfx * ax + dfy * bx + dfz * cx
            dy = dfx * ay + dfy * by + dfz * cy
            dz = dfx * az + dfy * bz + dfz * cz
            d2 = dx * dx + dy * dy + dz * dz
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best_n = (n1, n2, n3)
        n1, n2, n3 = best_n
        if best_n != (0, 0, 0):
            moved += 1
            x += n1 * ax + n2 * bx + n3 * cx
            y += n1 * ay + n2 * by + n3 * cy
            z += n1 * az + n2 * bz + n3 * cz
        aligned.append((key, x, y, z))
    info["moved"] = moved
    if moved == 0:
        info["reason"] = "already_aligned"
    return aligned, info


def _find_topond_out(search_paths: list[Path]) -> Path | None:
    names = ["CP_BPs_dedup_final.out", "CP_BPs_dedup.out", "CP_BPs.out"]
    for base in search_paths:
        if not base:
            continue
        if base.is_file() and base.suffix == ".out":
            return base
        if base.is_dir():
            for name in names:
                p = base / name
                if p.is_file():
                    return p
            for p in sorted(base.glob("*.out")):
                if p.is_file():
                    return p
    return None

class ViewerError(Exception):
    pass


def _pad_record(line: str) -> str:
    line = line.rstrip("\r\n")
    if len(line) < 80:
        line = line.ljust(80)
    return line[:80]


def _set_chain(line: str, chain: str) -> str:
    return line[:21] + chain + line[22:]


def _set_serial(line: str, serial: int) -> str:
    return line[:6] + f"{serial % 100000:5d}" + line[11:]


def _record_tag(line: str) -> str:
    resname = line[17:20].strip().upper()
    segid = line[72:76].strip().upper() if len(line) >= 76 else ""
    if segid.startswith("CP") or segid in {"CRYS", "CALC"}:
        return segid
    return resname


def _cp_chain(line: str) -> tuple[str, str] | None:
    tag = _record_tag(line)
    if tag.startswith("CPM") or tag.startswith("BCP"):
        return "bcp", CP_CHAINS["bcp"]
    if tag.startswith("CPP1") or tag.startswith("RCP"):
        return "rcp", CP_CHAINS["rcp"]
    if tag.startswith("CPP3") or tag.startswith("CCP"):
        return "ccp", CP_CHAINS["ccp"]
    if tag.startswith("CPN") or tag.startswith("NCP"):
        return "ncp", CP_CHAINS["ncp"]
    return None


def _bp_kind(line: str) -> str | None:
    tag = _record_tag(line)
    if tag in {"CBP", "CRYS"}:
        return "crystal"
    if tag in {"KBP", "CALC"}:
        return "calc"
    return None


def _read_pdb(path: Path) -> tuple[str | None, list[str]]:
    cryst1: str | None = None
    records: list[str] = []
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for raw in handle:
                line = raw.rstrip("\r\n")
                if line.startswith("CRYST1"):
                    if cryst1 is None:
                        cryst1 = _pad_record(line)
                    continue
                if line.startswith("ATOM  ") or line.startswith("HETATM"):
                    records.append(_pad_record(line))
    except OSError as exc:
        raise ViewerError(f"Не удалось прочитать {path}: {exc}") from exc
    return cryst1, records


def _bp_point(record: str) -> tuple[float, float, float]:
    return (
        float(record[30:38]),
        float(record[38:46]),
        float(record[46:54]),
    )


def _dot_group_segments(
    records: list[str],
    max_gap: float = 0.5,
) -> list[list[list[float]]]:
    """Straight segments for one bond-path line (a group of dots).

    Dots of one line are collinear, so the first and last dot define the
    segment.  A jump larger than ``max_gap`` means the line was split at the
    cell boundary (``--symmetry-image cut``); each continuous piece then
    becomes its own segment.
    """
    if not records:
        return []
    points = [_bp_point(record) for record in records]
    segments: list[list[list[float]]] = []

    def add(start, end) -> None:
        if math.dist(start, end) <= 1e-6:
            return
        segments.append([
            [round(value, 4) for value in start],
            [round(value, 4) for value in end],
        ])

    start = points[0]
    previous = points[0]
    for point in points[1:]:
        if math.dist(point, previous) > max_gap:
            add(start, previous)
            start = point
        previous = point
    add(start, previous)
    return segments


def _bp_segments(records: list[str]) -> list[list[list[float]]]:
    """Group bond-path dots by residue and return one segment per line."""
    segments: list[list[list[float]]] = []
    group: list[str] = []
    last_key: tuple[str, str, str] | None = None
    for record in records:
        key = (record[17:20], record[21], record[22:26])
        if group and key != last_key:
            segments.extend(_dot_group_segments(group))
            group = []
        group.append(record)
        last_key = key
    if group:
        segments.extend(_dot_group_segments(group))
    return segments


def _cell_box_edges(
    cryst1: str | None,
    cell_records: list[str],
) -> list[list[list[float]]]:
    if not cryst1:
        return []
    try:
        lattice = _parse_cryst1(cryst1)
    except (TypeError, ValueError):
        return []

    fractions: list[tuple[float, float, float]] = []
    for line in cell_records:
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except (TypeError, ValueError):
            continue
        fractions.append(lattice.cart_to_frac(x, y, z))
    if not fractions:
        return []

    origin = tuple(
        sum(point[axis] for point in fractions) / len(fractions) - 0.5
        for axis in range(3)
    )
    corners: dict[tuple[int, int, int], tuple[float, float, float]] = {}
    for i in (0, 1):
        for j in (0, 1):
            for k in (0, 1):
                corners[(i, j, k)] = lattice.frac_to_cart(
                    origin[0] + i,
                    origin[1] + j,
                    origin[2] + k,
                )

    edges: list[list[list[float]]] = []
    for (i, j, k), start in corners.items():
        for axis, (di, dj, dk) in enumerate(
            ((1, 0, 0), (0, 1, 0), (0, 0, 1))
        ):
            if (i, j, k)[axis]:
                continue
            end = corners[(i + di, j + dj, k + dk)]
            edges.append(
                [
                    [round(value, 4) for value in start],
                    [round(value, 4) for value in end],
                ]
            )
    return edges


def _align_cp_points(
    points: list[tuple[str, float, float, float]],
    cryst1: str | None,
    cell_records: list[str],
) -> tuple[list[tuple[str, float, float, float]], dict]:
    info = {
        "enabled": True,
        "moved": 0,
        "reason": "",
    }
    if not points:
        info["reason"] = "no_cp"
        return points, info
    if not cryst1:
        info["reason"] = "no_cryst1"
        return points, info
    try:
        lattice = _parse_cryst1(cryst1)
    except (TypeError, ValueError):
        info["reason"] = "invalid_cryst1"
        return points, info

    atom_fractions: list[tuple[float, float, float]] = []
    for line in cell_records:
        try:
            x = float(line[30:38])
            y = float(line[38:46])
            z = float(line[46:54])
        except (TypeError, ValueError):
            continue
        atom_fractions.append(lattice.cart_to_frac(x, y, z))
    if not atom_fractions:
        info["reason"] = "no_atoms"
        return points, info

    ax, ay, az = lattice.a_vec
    bx, by, bz = lattice.b_vec
    cx, cy, cz = lattice.c_vec
    aligned: list[tuple[str, float, float, float]] = []
    moved = 0
    for key, x, y, z in points:
        fx, fy, fz = lattice.cart_to_frac(x, y, z)
        best_d2: float | None = None
        best_n = (0, 0, 0)
        for afx, afy, afz in atom_fractions:
            n1 = round(afx - fx)
            n2 = round(afy - fy)
            n3 = round(afz - fz)
            dfx = afx - fx - n1
            dfy = afy - fy - n2
            dfz = afz - fz - n3
            dx = dfx * ax + dfy * bx + dfz * cx
            dy = dfx * ay + dfy * by + dfz * cy
            dz = dfx * az + dfy * bz + dfz * cz
            d2 = dx * dx + dy * dy + dz * dz
            if best_d2 is None or d2 < best_d2:
                best_d2 = d2
                best_n = (n1, n2, n3)
        n1, n2, n3 = best_n
        if best_n != (0, 0, 0):
            moved += 1
            x += n1 * ax + n2 * bx + n3 * cx
            y += n1 * ay + n2 * by + n3 * cy
            z += n1 * az + n2 * bz + n3 * cz
        aligned.append((key, x, y, z))
    info["moved"] = moved
    if moved == 0:
        info["reason"] = "already_aligned"
    return aligned, info


def _find_topond_out(search_paths: list[Path]) -> Path | None:
    names = ["CP_BPs_dedup_final.out", "CP_BPs_dedup.out", "CP_BPs.out"]
    for base in search_paths:
        if not base:
            continue
        if base.is_file() and base.suffix == ".out":
            return base
        if base.is_dir():
            for name in names:
                p = base / name
                if p.is_file():
                    return p
            for p in sorted(base.glob("*.out")):
                if p.is_file():
                    return p
    return None


def build_viewer_data(
    cell_path: Path,
    cp_path: Path | None = None,
    crystal_bp_path: Path | None = None,
    calc_bp_path: Path | None = None,
    title: str | None = None,
    align_cp: bool = True,
    topond_out: Path | None = None,
) -> tuple[bytes, dict]:
    cell_cryst1, cell_records = _read_pdb(cell_path)
    if not cell_records:
        raise ViewerError(f"В файле нет ATOM/HETATM записей: {cell_path}")

    counts = {
        "atoms": len(cell_records),
        "bcp": 0,
        "rcp": 0,
        "ccp": 0,
        "ncp": 0,
        "bp_crystal": 0,
        "bp_calc": 0,
        "bp_crystal_shown": 0,
        "bp_calc_shown": 0,
        "unknown_cp": 0,
    }
    atoms: list[str] = []
    cp_points: list[tuple[str, float, float, float]] = []
    cp_orig_indices: list[int] = []
    serial = 0

    for line in cell_records:
        serial += 1
        atoms.append(_set_chain(_set_serial(line, serial), "A"))

    fallback_cryst1: str | None = None

    if cp_path is not None and cp_path.is_file():
        cryst1, cp_records = _read_pdb(cp_path)
        fallback_cryst1 = fallback_cryst1 or cryst1
        cp_meta_by_index: list[dict] = []
        for line in cp_records:
            mapped = _cp_chain(line)
            if mapped is None:
                counts["unknown_cp"] += 1
                continue
            key, chain = mapped
            counts[key] += 1
            serial += 1
            cp_points.append(
                (
                    key,
                    float(line[30:38]),
                    float(line[38:46]),
                    float(line[46:54]),
                )
            )
            cp_meta_by_index.append({"key": key, "line": line})

    segment_data: dict[str, list[list[list[float]]]] = {
        "bp_crystal": [],
        "bp_calc": [],
    }
    for kind, path in (
        ("crystal", crystal_bp_path),
        ("calc", calc_bp_path),
    ):
        if path is None or not path.is_file():
            continue
        cryst1, bp_records = _read_pdb(path)
        fallback_cryst1 = fallback_cryst1 or cryst1
        classified = [
            line for line in bp_records if _bp_kind(line) == kind
        ]
        segments = _bp_segments(classified)
        counts[f"bp_{kind}"] = len(segments)
        counts[f"bp_{kind}_shown"] = len(segments)
        segment_data[f"bp_{kind}"] = segments

    cryst1 = cell_cryst1 or fallback_cryst1
    if align_cp:
        cp_points, cp_align = _align_cp_points(
            cp_points,
            cryst1,
            cell_records,
        )
    else:
        cp_align = {
            "enabled": False,
            "moved": 0,
            "reason": "disabled",
        }
    cell_box = _cell_box_edges(cryst1, cell_records)
    output: list[str] = []
    if cryst1 is not None:
        output.append(cryst1)
    output.append("MODEL        1")
    output.extend(atoms)
    output.append("ENDMDL")
    output.append("END")
    data = ("\n".join(output) + "\n").encode("utf-8")
    point_data: dict[str, list[list[float]]] = {
        "bcp": [],
        "rcp": [],
        "ccp": [],
        "ncp": [],
    }
    for key, x, y, z in cp_points:
        point_data[key].append(
            [round(x, 4), round(y, 4), round(z, 4)]
        )

    files = {
        "cell": cell_path.name,
        "cp": cp_path.name if cp_path is not None else "",
        "crystal_bp": crystal_bp_path.name if crystal_bp_path is not None else "",
        "calc_bp": calc_bp_path.name if calc_bp_path is not None else "",
    }
    cpMeta = []
    cpProps = {}
    type_map = {"bcp": "(3,-1)", "rcp": "(3,+1)", "ccp": "(3,+3)", "ncp": "(3,-3)"}
    cp_remarks: list[int] = []
    if cp_path is not None and cp_path.is_file():
        try:
            cp_text = cp_path.read_text(encoding="utf-8", errors="ignore")
            for line in cp_text.splitlines():
                m = re.search(r"REMARK\s+CP\s+N\.\s+(\d+)", line)
                if m:
                    cp_remarks.append(int(m.group(1)))
        except Exception:
            cp_remarks = []
    records = []
    for idx, (key, x, y, z) in enumerate(cp_points):
        num = cp_remarks[idx] if idx < len(cp_remarks) else None
        if num is None:
            num = idx + 1
        records.append({
            "num": num,
            "kind": key,
            "type": type_map.get(key, key),
            "coords": [round(float(x), 3), round(float(y), 3), round(float(z), 3)],
            "bp": [],
        })
    order = ["bcp", "rcp", "ccp", "ncp"]
    grouped = []
    for kind in order:
        grouped.extend([r for r in records if r["kind"] == kind])
    for extra in records:
        if extra not in grouped:
            grouped.append(extra)
    copy_count: dict[int, int] = {}
    for idx, rec in enumerate(grouped):
        num = rec["num"]
        copy_count[num] = copy_count.get(num, 0) + 1
        rec["copyNum"] = copy_count[num]
        rec["copyIndex"] = copy_count[num]
        rec["drawIndex"] = idx
        cpMeta.append(rec)
    atom_coords: dict[int, list[float]] = {}
    for line in cell_records:
        try:
            atom_coords[int(line[6:11])] = [
                round(float(line[30:38]), 3),
                round(float(line[38:46]), 3),
                round(float(line[46:54]), 3),
            ]
        except (ValueError, TypeError):
            continue
    bp_by_num: dict[int, list[dict]] = {}
    for bpkind, bppath in (("crystal", crystal_bp_path), ("calc", calc_bp_path)):
        if not bppath or not bppath.is_file():
            continue
        try:
            bp_text = bppath.read_text(encoding="utf-8", errors="ignore")
            for line in bp_text.splitlines():
                mr = re.search(
                    r"REMARK\s+BOND\s+\d+:\s+CP\s+N\.\s+(\d+)"
                    r".*?->\s+atom\s+(\d+)\(([A-Za-z]+)\)",
                    line,
                )
                if not mr:
                    continue
                cpn = int(mr.group(1))
                an = int(mr.group(2))
                ae = mr.group(3)
                entries = bp_by_num.setdefault(cpn, [])
                if any(e["atomNum"] == an and e["source"] == bpkind for e in entries):
                    continue
                entries.append({
                    "source": bpkind,
                    "atomNum": an,
                    "element": ae,
                    "atomCoords": atom_coords.get(an),
                })
        except Exception:
            continue
    for rec in cpMeta:
        bp_list = bp_by_num.get(rec["num"])
        if bp_list:
            rec["bp"] = [dict(b) for b in bp_list]
    outp = topond_out
    if outp is None or not outp.is_file():
        for cand in [
            cell_path.parent / "CP_BPs_dedup_final.out",
            cell_path.parent / "CP_BPs_dedup.out",
            cell_path.parent / "CP_BPs.out",
            Path(__file__).parent / "CP_BPs_dedup_final.out",
        ]:
            if cand.is_file():
                outp = cand
                break
    if outp and outp.is_file():
        try:
            from topond_cp_export import parse_cps
            cps = parse_cps(outp.read_text(encoding="utf-8", errors="ignore"))
            for c in cps:
                cpProps[int(c.num)] = {
                    "RHO": getattr(c, "rho", None),
                    "GRHO": getattr(c, "grho", None),
                    "LAP": getattr(c, "lap", None),
                    "G": getattr(c, "g", None),
                    "K": getattr(c, "k", None),
                    "VIRIAL DENSITY": getattr(c, "virial", None),
                    "ELF": getattr(c, "elf", None),
                    "ELLIPTICITY": getattr(c, "ellipticity", None),
                }
        except Exception:
            pass
    config = {
        "title": title or cell_path.parent.name,
        "appletId": APPLET_ID,
        "files": files,
        "counts": counts,
        "hasCell": bool(cell_box),
        "cellBox": cell_box,
        "cpAlign": cp_align,
        "points": point_data,
        "bpSegments": segment_data,
        "cpMeta": cpMeta,
        "bpMeta": [],
        "cpProps": cpProps,
    }
    return data, config


def _load_asset(name: str) -> bytes:
    path = ASSET_DIR / name
    try:
        return path.read_bytes()
    except OSError as exc:
        raise ViewerError(f"Не найден ресурс viewer: {path}: {exc}") from exc


class ViewerServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(
        self,
        address: tuple[str, int],
        handler: type[BaseHTTPRequestHandler],
        *,
        html_page: bytes,
        config_js: bytes,
        pdb_data: bytes,
        assets: dict[str, tuple[bytes, str]],
    ) -> None:
        super().__init__(address, handler)
        self.html_page = html_page
        self.config_js = config_js
        self.pdb_data = pdb_data
        self.assets = assets


class ViewerHandler(BaseHTTPRequestHandler):
    server: ViewerServer

    def do_GET(self) -> None:
        self._serve(False)

    def do_HEAD(self) -> None:
        self._serve(True)

    def _not_found(self) -> None:
        LOGGER.warning("code=VIEW-W001 HTTP 404 path=%s", self.path)
        print(f"HTTP 404 {self.path}", file=sys.stderr, flush=True)
        self.send_error(404)

    def _serve(self, head: bool) -> None:
        path = urllib.parse.urlsplit(self.path).path
        cache_control = "no-store"
        if path in ("/", "/index.html", "/viewer.html"):
            body = self.server.html_page
            content_type = "text/html; charset=utf-8"
        elif path == "/config.js":
            body = self.server.config_js
            content_type = "application/javascript; charset=utf-8"
        elif path == "/data/combined.pdb":
            body = self.server.pdb_data
            content_type = "chemical/x-pdb"
        elif path == "/favicon.ico":
            self.send_response(204)
            self.end_headers()
            return
        elif path.startswith("/jsmol/"):
            root = JSMOL_DIR.resolve()
            relative = urllib.parse.unquote(path[len("/jsmol/"):])
            target = (root / relative).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                self._not_found()
                return
            if not target.is_file():
                self._not_found()
                return
            try:
                body = target.read_bytes()
            except OSError:
                self._not_found()
                return
            content_type = (
                mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            )
            if target.suffix == ".js":
                content_type = "application/javascript; charset=utf-8"
            cache_control = "public, max-age=86400"
        else:
            asset = self.server.assets.get(path)
            if asset is None:
                self._not_found()
                return
            body, content_type = asset

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", cache_control)
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, fmt: str, *args) -> None:
        return


def _render_page(template: str, jsmol_base: str, title: str) -> bytes:
    page = template.replace("__JSMOL_BASE__", html.escape(jsmol_base.rstrip("/")))
    page = page.replace("__TITLE__", html.escape(title))
    return page.encode("utf-8")


def _config_script(config: dict) -> bytes:
    return (
        "window.TOPOND_CONFIG = "
        + json.dumps(config, ensure_ascii=False)
        + ";\n"
    ).encode("utf-8")


def _run_qt(
    url: str,
    title: str,
    *,
    screenshot: Path | None = None,
    screenshot_delay: int = 8000,
    eval_script: str | None = None,
    eval_delay: int = 6000,
) -> int:
    try:
        from PySide6.QtCore import QTimer, QUrl, Qt
        from PySide6.QtWebEngineCore import QWebEnginePage, QWebEngineSettings
        from PySide6.QtWebEngineWidgets import QWebEngineView
        from PySide6.QtWidgets import QApplication
    except Exception as exc:
        print(
            "ERROR: для окна JSmol нужен PySide6 с QtWebEngine. "
            f"Не удалось импортировать: {exc}",
            file=sys.stderr,
        )
        return 4

    class LoggingPage(QWebEnginePage):
        def javaScriptConsoleMessage(self, level, message, line_number, source_id):
            if level != QWebEnginePage.JavaScriptConsoleMessageLevel.ErrorMessageLevel:
                return
            print(
                f"JS[{level}] {source_id}:{line_number}: {message}",
                file=sys.stderr,
                flush=True,
            )

    QApplication.setAttribute(
        Qt.ApplicationAttribute.AA_ShareOpenGLContexts,
        True,
    )
    app = QApplication(sys.argv[:1])
    view = QWebEngineView()
    page = LoggingPage(view)
    view.setPage(page)

    download_dir = Path.cwd()

    def on_download(item) -> None:
        try:
            name = item.suggestedFileName() or ""
            if not name:
                name = f"jsmol-export-{int(__import__('time').time())}.png"
            download_dir.mkdir(parents=True, exist_ok=True)
            item.setDownloadDirectory(str(download_dir))
            item.setDownloadFileName(name)

            def finished() -> None:
                state = item.state()
                label = getattr(state, "name", str(state))
                if label == "DownloadCompleted":
                    log_action(LOGGER, "download", path=str(download_dir / name))
                    print(
                        f"DOWNLOAD {download_dir / name}",
                        flush=True,
                    )
                else:
                    log_error(
                        LOGGER, "VIEW-E004", "download not completed",
                        state=label, path=str(download_dir / name),
                    )
                    print(
                        f"ERROR: экспорт не завершён ({label})",
                        file=sys.stderr,
                        flush=True,
                    )

            item.isFinishedChanged.connect(finished)
            item.accept()
        except Exception as exc:
            log_error(LOGGER, "VIEW-E004", "cannot save downloaded file", exc=exc)
            print(f"ERROR: не удалось сохранить файл: {exc}", file=sys.stderr, flush=True)

    try:
        profile = page.profile()
        profile.downloadRequested.connect(on_download)
    except Exception as exc:
        log_error(LOGGER, "VIEW-E007", "download handler unavailable", exc=exc)
        print(f"ERROR: обработчик загрузок недоступен: {exc}", file=sys.stderr, flush=True)
    view.setWindowTitle(f"JSmol — {title}")
    view.resize(1280, 860)

    settings = view.settings()
    settings.setAttribute(QWebEngineSettings.WebAttribute.JavascriptEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.LocalStorageEnabled, True)
    settings.setAttribute(QWebEngineSettings.WebAttribute.WebGLEnabled, True)

    def probe() -> None:
        page.runJavaScript(
            "window.__topondViewerState ? JSON.stringify(window.__topondViewerState()) : '{}'",
            lambda value: print(f"STATE {value}", flush=True),
        )

    if eval_script:
        def evaluate() -> None:
            page.runJavaScript(
                "window.__topondViewerState ? JSON.stringify(window.__topondViewerState()) : '{}'",
                lambda state_value: print(f"STATE {state_value}", flush=True),
            )
            page.runJavaScript(
                eval_script,
                lambda value: print(f"EVAL {value}", flush=True),
            )

        QTimer.singleShot(eval_delay, evaluate)

    if screenshot is not None:
        def capture() -> None:
            def save_and_quit(value=None) -> None:
                print(f"STATE {value}", flush=True)
                try:
                    screenshot.parent.mkdir(parents=True, exist_ok=True)
                    pixmap = view.grab()
                    if pixmap.save(str(screenshot)):
                        log_action(LOGGER, "screenshot", path=str(screenshot))
                        print(f"SCREENSHOT {screenshot}", flush=True)
                    else:
                        log_error(LOGGER, "VIEW-E004", "cannot save screenshot",
                                  path=str(screenshot))
                        print(
                            f"ERROR: не удалось сохранить {screenshot}",
                            file=sys.stderr,
                        )
                finally:
                    app.quit()

            page.runJavaScript(
                "window.__topondViewerState ? JSON.stringify(window.__topondViewerState()) : '{}'",
                save_and_quit,
            )

        QTimer.singleShot(screenshot_delay, capture)

    view.load(QUrl(url))
    view.show()
    log_action(LOGGER, "ready", url=url)
    print(f"VIEWER_READY {url}", flush=True)
    return app.exec()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="JSmol viewer for TOPOND cell, CP and bond-path PDB files.",
    )
    parser.add_argument("--cell", type=Path)
    parser.add_argument("--cp", type=Path)
    parser.add_argument("--crystal-bp", type=Path)
    parser.add_argument("--calc-bp", type=Path)
    parser.add_argument("--topond-out", type=Path, dest="topond_out")
    parser.add_argument("--title")
    parser.add_argument(
        "--align-cp",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Move CP by whole lattice vectors to the atom cell image.",
    )
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--summary", action="store_true")
    parser.add_argument("--screenshot", type=Path)
    parser.add_argument("--screenshot-delay", type=int, default=8000)
    parser.add_argument("--eval", dest="eval_script")
    parser.add_argument("--eval-delay", type=int, default=6000)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup("viewer")
    install_excepthook(LOGGER, "VIEW-E006")
    log_action(
        LOGGER, "start",
        cell=str(args.cell) if args.cell else None,
        cp=str(args.cp) if args.cp else None,
        crystal_bp=str(args.crystal_bp) if args.crystal_bp else None,
        calc_bp=str(args.calc_bp) if args.calc_bp else None,
        screenshot=str(args.screenshot) if args.screenshot else None,
        align_cp=args.align_cp,
    )
    if args.check:
        try:
            from PySide6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        except Exception as exc:
            log_error(LOGGER, "VIEW-E005", "PySide6/QtWebEngine unavailable", exc=exc)
            print(str(exc), file=sys.stderr)
            return 1
        required = (
            JSMOL_DIR / "JSmol.min.js",
            JSMOL_DIR / "j2s" / "core" / "corejmol.z.js",
        )
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            log_error(LOGGER, "VIEW-E002", "local JSmol files missing",
                      missing=missing)
            print(
                "ERROR: не найдены локальные файлы JSmol: "
                + ", ".join(missing),
                file=sys.stderr,
            )
            return 1
        print("1")
        return 0

    if args.cell is None or not args.cell.is_file():
        log_error(LOGGER, "VIEW-E001", "cell PDB not found",
                  cell=str(args.cell))
        print(f"ERROR: не найден cell PDB: {args.cell}", file=sys.stderr)
        return 2

    try:
        pdb_data, config = build_viewer_data(
            args.cell,
            args.cp,
            args.crystal_bp,
            args.calc_bp,
            args.title,
            align_cp=args.align_cp,
            topond_out=args.topond_out,
        )
        if args.summary:
            summary = {
                key: value
                for key, value in config.items()
                if key not in {"points", "bpSegments"}
            }
            print(json.dumps(summary, ensure_ascii=False))
            return 0

        jsmol_base = os.environ.get("TOPOND_JSMOL_CDN", "/jsmol")
        config["jsmolBase"] = jsmol_base
        template = _load_asset("viewer.html").decode("utf-8")
        assets = {
            "/viewer.js": (
                _load_asset("viewer.js"),
                "application/javascript; charset=utf-8",
            ),
            "/viewer.css": (_load_asset("viewer.css"), "text/css; charset=utf-8"),
        }
        server = ViewerServer(
            ("127.0.0.1", 8000),
            ViewerHandler,
            html_page=_render_page(template, jsmol_base, config["title"]),
            config_js=_config_script(config),
            pdb_data=pdb_data,
            assets=assets,
        )
    except ViewerError as exc:
        log_error(LOGGER, "VIEW-E006", "failed to load viewer data", exc=exc)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except OSError as exc:
        log_error(LOGGER, "VIEW-E003", "cannot start local HTTP server",
                  exc=exc, host="127.0.0.1", port=8000)
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_address[1]}/viewer.html"
    log_action(LOGGER, "server", url=url, port=server.server_address[1],
               atoms=config["counts"]["atoms"], bcp=config["counts"]["bcp"],
               rcp=config["counts"]["rcp"], ccp=config["counts"]["ccp"],
               ncp=config["counts"]["ncp"],
               bp_crystal=config["counts"]["bp_crystal"],
               bp_calc=config["counts"]["bp_calc"])
    print(
        f"Загружено: {config['counts']['atoms']} атомов, "
        f"{config['counts']['bcp']} BCP, {config['counts']['rcp']} RCP, "
        f"{config['counts']['ccp']} CCP, {config['counts']['ncp']} NCP, "
        f"{config['counts']['bp_crystal']} crystal BP, "
        f"{config['counts']['bp_calc']} calc BP.",
        flush=True,
    )
    cp_align = config.get("cpAlign", {})
    if cp_align.get("enabled"):
        if cp_align.get("moved"):
            print(
                f"CP выровнены с атомами: сдвинуто "
                f"{cp_align['moved']} точек.",
                flush=True,
            )
        elif cp_align.get("reason") in {
            "no_cryst1",
            "invalid_cryst1",
            "no_atoms",
        }:
            print(
                "CP не выровнены: нет подходящей ячейки или атомов.",
                flush=True,
            )
    if config["counts"]["bp_crystal"] or config["counts"]["bp_calc"]:
        print(
            "Bond paths: "
            f"{config['counts']['bp_crystal']} crystal + "
            f"{config['counts']['bp_calc']} calc отрезков.",
            flush=True,
        )
    try:
        return _run_qt(
            url,
            config["title"],
            screenshot=args.screenshot,
            screenshot_delay=args.screenshot_delay,
            eval_script=args.eval_script,
            eval_delay=args.eval_delay,
        )
    finally:
        log_action(LOGGER, "shutdown", url=url)
        server.shutdown()
        server.server_close()


if __name__ == "__main__":
    raise SystemExit(main())