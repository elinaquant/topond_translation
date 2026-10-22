#!/usr/bin/env python3
"""Build supercells from TOPOND PDB files for VESTA / VMD / JSmol."""

from __future__ import annotations

import argparse
import itertools
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator, Sequence

EPS = 1e-9
DEFAULT_CENTER_FRAC = (0.5, 0.5, 0.5)
DEFAULT_RANGE_FRAC = (-0.5, 0.5, -0.5, 0.5, -0.5, 0.5)


def _det3(m: Sequence[Sequence[float]]) -> float:
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _inv3(m: Sequence[Sequence[float]], det: float) -> list[list[float]]:
    inv_det = 1.0 / det
    return [
        [
            (m[1][1] * m[2][2] - m[1][2] * m[2][1]) * inv_det,
            (m[0][2] * m[2][1] - m[0][1] * m[2][2]) * inv_det,
            (m[0][1] * m[1][2] - m[0][2] * m[1][1]) * inv_det,
        ],
        [
            (m[1][2] * m[2][0] - m[1][0] * m[2][2]) * inv_det,
            (m[0][0] * m[2][2] - m[0][2] * m[2][0]) * inv_det,
            (m[0][2] * m[1][0] - m[0][0] * m[1][2]) * inv_det,
        ],
        [
            (m[1][0] * m[2][1] - m[1][1] * m[2][0]) * inv_det,
            (m[0][1] * m[2][0] - m[0][0] * m[2][1]) * inv_det,
            (m[0][0] * m[1][1] - m[0][1] * m[1][0]) * inv_det,
        ],
    ]


@dataclass(frozen=True)
class Lattice:
    a_vec: tuple[float, float, float]
    b_vec: tuple[float, float, float]
    c_vec: tuple[float, float, float]

    @classmethod
    def from_params(
        cls,
        a: float,
        b: float,
        c: float,
        alpha: float,
        beta: float,
        gamma: float,
    ) -> "Lattice":
        al, be, ga = (math.radians(value) for value in (alpha, beta, gamma))
        cx = c * math.cos(be)
        cy = c * (math.cos(al) - math.cos(be) * math.cos(ga)) / math.sin(ga)
        cz = math.sqrt(max(c * c - cx * cx - cy * cy, 0.0))
        return cls(
            (a, 0.0, 0.0),
            (b * math.cos(ga), b * math.sin(ga), 0.0),
            (cx, cy, cz),
        )

    @classmethod
    def scaled(cls, lattice: "Lattice", scale: Sequence[float]) -> "Lattice":
        return cls(
            tuple(scale[0] * value for value in lattice.a_vec),
            tuple(scale[1] * value for value in lattice.b_vec),
            tuple(scale[2] * value for value in lattice.c_vec),
        )

    def lengths(self) -> tuple[float, float, float]:
        return tuple(
            math.sqrt(sum(value * value for value in vector))
            for vector in (self.a_vec, self.b_vec, self.c_vec)
        )

    def params(self) -> tuple[float, float, float, float, float, float]:
        a, b, c = self.a_vec, self.b_vec, self.c_vec
        la, lb, lc = self.lengths()

        def angle(u: Sequence[float], v: Sequence[float]) -> float:
            du = math.sqrt(sum(t * t for t in u))
            dv = math.sqrt(sum(t * t for t in v))
            cos = sum(p * q for p, q in zip(u, v)) / (du * dv)
            return math.degrees(math.acos(max(-1.0, min(1.0, cos))))

        return la, lb, lc, angle(b, c), angle(a, c), angle(a, b)

    def cart_to_frac(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        m = [self.a_vec, self.b_vec, self.c_vec]
        mt = [[m[j][i] for j in range(3)] for i in range(3)]
        det = _det3(mt)
        if abs(det) < 1e-12:
            raise ValueError("degenerate lattice vectors")
        inv = _inv3(mt, det)
        return tuple(
            inv[i][0] * x + inv[i][1] * y + inv[i][2] * z for i in range(3)
        )

    def frac_to_cart(
        self, f0: float, f1: float, f2: float
    ) -> tuple[float, float, float]:
        return (
            f0 * self.a_vec[0] + f1 * self.b_vec[0] + f2 * self.c_vec[0],
            f0 * self.a_vec[1] + f1 * self.b_vec[1] + f2 * self.c_vec[1],
            f0 * self.a_vec[2] + f1 * self.b_vec[2] + f2 * self.c_vec[2],
        )


@dataclass
class PdbRecord:
    line: str
    record: str
    serial: int
    x: float
    y: float
    z: float
    resseq: int
    remark: str = ""


@dataclass
class BondGroup:
    resseq: int
    remark: str
    dots: list[PdbRecord] = field(default_factory=list)


@dataclass
class Supercell:
    mode: str
    lattice: Lattice
    input_mode: str = "fractional"
    origin: tuple[float, float, float] | None = None
    size: tuple[float, float, float] | None = None
    lower: tuple[float, float, float] | None = None
    upper: tuple[float, float, float] | None = None
    fmin: tuple[float, float, float] | None = None
    fmax: tuple[float, float, float] | None = None
    cryst1: str = ""

    def images(
        self, cart: tuple[float, float, float]
    ) -> Iterator[tuple[tuple[int, int, int], tuple[float, float, float]]]:
        frac = self.lattice.cart_to_frac(*cart)
        if self.mode == "cell":
            assert self.origin is not None and self.size is not None
            for t, fout in _cell_images(frac, self.origin, self.size):
                yield t, self.lattice.frac_to_cart(*fout)
            return
        assert self.lower is not None and self.upper is not None
        assert self.fmin is not None and self.fmax is not None
        yield from _xyz_images(
            cart, frac, self.lattice, self.lower, self.upper, self.fmin, self.fmax
        )

    def describe(self) -> str:
        if self.mode == "cell":
            assert self.origin is not None and self.size is not None
            upper = tuple(self.origin[i] + self.size[i] for i in range(3))
            return (
                f"mode=cell bounds_frac={_fmt_tuple(self.origin)}..{_fmt_tuple(upper)} "
                f"size_cells={_fmt_tuple(self.size)}"
            )
        assert self.lower is not None and self.upper is not None
        size = tuple(self.upper[i] - self.lower[i] for i in range(3))
        return (
            f"mode=xyz bounds_cart={_fmt_tuple(self.lower)}..{_fmt_tuple(self.upper)} "
            f"size_ang={_fmt_tuple(size)}"
        )


def _fmt_tuple(values: Sequence[float], digits: int = 4) -> str:
    return "(" + ", ".join(f"{value:.{digits}f}" for value in values) + ")"


def _cell_images(
    frac: tuple[float, float, float],
    origin: tuple[float, float, float],
    size: tuple[float, float, float],
) -> Iterator[tuple[tuple[int, int, int], tuple[float, float, float]]]:
    ranges: list[range] = []
    for i in range(3):
        lo = math.ceil(origin[i] - frac[i] - EPS)
        hi = math.floor(origin[i] + size[i] - frac[i] - EPS)
        if hi < lo:
            return
        ranges.append(range(lo, hi + 1))
    for t in itertools.product(*ranges):
        yield t, tuple(frac[i] + t[i] - origin[i] for i in range(3))


def _xyz_images(
    cart: tuple[float, float, float],
    frac: tuple[float, float, float],
    lattice: Lattice,
    lower: tuple[float, float, float],
    upper: tuple[float, float, float],
    fmin: tuple[float, float, float],
    fmax: tuple[float, float, float],
) -> Iterator[tuple[tuple[int, int, int], tuple[float, float, float]]]:
    ranges: list[range] = []
    for i in range(3):
        lo = math.ceil(fmin[i] - frac[i] - EPS)
        hi = math.floor(fmax[i] - frac[i] + EPS)
        if hi < lo:
            return
        ranges.append(range(lo, hi + 1))
    a, b, c = lattice.a_vec, lattice.b_vec, lattice.c_vec
    for t in itertools.product(*ranges):
        point = (
            cart[0] + t[0] * a[0] + t[1] * b[0] + t[2] * c[0],
            cart[1] + t[0] * a[1] + t[1] * b[1] + t[2] * c[1],
            cart[2] + t[0] * a[2] + t[1] * b[2] + t[2] * c[2],
        )
        if all(
            lower[i] - EPS <= point[i] < upper[i] - EPS for i in range(3)
        ):
            yield t, (
                point[0] - lower[0],
                point[1] - lower[1],
                point[2] - lower[2],
            )


def _parse_cryst1(line: str) -> Lattice:
    a = float(line[6:15])
    b = float(line[15:24])
    c = float(line[24:33])
    alpha = float(line[33:40])
    beta = float(line[40:47])
    gamma = float(line[47:54])
    return Lattice.from_params(a, b, c, alpha, beta, gamma)


def _parse_record(line: str) -> PdbRecord:
    line = line.ljust(78)
    return PdbRecord(
        line=line,
        record=line[:6].strip(),
        serial=int(line[6:11]) if line[6:11].strip() else 0,
        x=float(line[30:38]),
        y=float(line[38:46]),
        z=float(line[46:54]),
        resseq=int(line[22:26]) if line[22:26].strip() else 0,
    )


def _is_atom_line(line: str) -> bool:
    return line.startswith("ATOM") or line.startswith("HETATM")


def read_atoms(path: Path) -> tuple[Lattice, list[PdbRecord]]:
    lattice: Lattice | None = None
    records: list[PdbRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CRYST1"):
            lattice = _parse_cryst1(line)
        elif _is_atom_line(line):
            records.append(_parse_record(line))
    if lattice is None:
        raise ValueError(f"{path}: no CRYST1 record")
    if not records:
        raise ValueError(f"{path}: no ATOM/HETATM records")
    return lattice, records


def read_cps(path: Path) -> tuple[Lattice | None, list[PdbRecord]]:
    lattice: Lattice | None = None
    remarks: list[str] = []
    records: list[PdbRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CRYST1"):
            lattice = _parse_cryst1(line)
        elif line.startswith("REMARK"):
            if "CP N." in line:
                remarks.append(line.rstrip())
        elif _is_atom_line(line):
            record = _parse_record(line)
            index = len(records)
            if index < len(remarks):
                record.remark = remarks[index]
            records.append(record)
    if not records:
        raise ValueError(f"{path}: no CP records")
    return lattice, records


def read_bonds(
    path: Path,
) -> tuple[Lattice | None, list[str], list[BondGroup]]:
    lattice: Lattice | None = None
    global_remarks: list[str] = []
    groups: dict[int, BondGroup] = {}
    current_remark = ""
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("CRYST1"):
            lattice = _parse_cryst1(line)
        elif line.startswith("REMARK"):
            if line.startswith("REMARK   BOND"):
                current_remark = line.rstrip()
            else:
                global_remarks.append(line.rstrip())
        elif _is_atom_line(line):
            record = _parse_record(line)
            group = groups.get(record.resseq)
            if group is None:
                group = BondGroup(record.resseq, current_remark)
                groups[record.resseq] = group
            elif not group.remark and current_remark:
                group.remark = current_remark
            group.dots.append(record)
    if not groups:
        raise ValueError(f"{path}: no bond path dots")
    return lattice, global_remarks, list(groups.values())


def _lattices_match(left: Lattice, right: Lattice) -> bool:
    left_params = left.params()
    right_params = right.params()
    if any(abs(a - b) > 5e-3 for a, b in zip(left_params[:3], right_params[:3])):
        return False
    return all(abs(a - b) <= 5e-2 for a, b in zip(left_params[3:], right_params[3:]))


def _validate_offsets(values: Sequence[float], name: str) -> None:
    for axis, label in enumerate(("x/a", "y/b", "z/c")):
        low = values[2 * axis]
        high = values[2 * axis + 1]
        if not (math.isfinite(low) and math.isfinite(high)):
            raise ValueError(f"{name}: non-finite {label} offsets")
        if high <= low:
            raise ValueError(
                f"{name}: upper {label} offset must be greater than lower "
                f"({high} <= {low})"
            )


def _format_cryst1(
    a: float, b: float, c: float, alpha: float, beta: float, gamma: float
) -> str:
    return (
        f"CRYST1{a:9.3f}{b:9.3f}{c:9.3f}{alpha:7.2f}{beta:7.2f}{gamma:7.2f}"
        f" P 1           1"
    )


def _fractional_bounds_for_cart_box(
    lattice: Lattice,
    lower: tuple[float, float, float],
    upper: tuple[float, float, float],
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    corners = [
        lattice.cart_to_frac(x, y, z)
        for x in (lower[0], upper[0])
        for y in (lower[1], upper[1])
        for z in (lower[2], upper[2])
    ]
    fmin = tuple(min(corner[i] for corner in corners) for i in range(3))
    fmax = tuple(max(corner[i] for corner in corners) for i in range(3))
    return fmin, fmax


def build_supercell(lattice: Lattice, args: argparse.Namespace) -> Supercell:
    if args.center_ang is not None:
        center_ang = tuple(args.center_ang)
        offsets_ang = tuple(args.range_ang)
        if not all(math.isfinite(value) for value in center_ang):
            raise ValueError("--center-ang: non-finite coordinates")
        _validate_offsets(offsets_ang, "--range-ang")
        if args.ang_axes == "xyz":
            lower = (
                center_ang[0] + offsets_ang[0],
                center_ang[1] + offsets_ang[2],
                center_ang[2] + offsets_ang[4],
            )
            upper = (
                center_ang[0] + offsets_ang[1],
                center_ang[1] + offsets_ang[3],
                center_ang[2] + offsets_ang[5],
            )
            fmin, fmax = _fractional_bounds_for_cart_box(lattice, lower, upper)
            widths = tuple(upper[i] - lower[i] for i in range(3))
            return Supercell(
                mode="xyz",
                lattice=lattice,
                input_mode="angstrom-xyz",
                lower=lower,
                upper=upper,
                fmin=fmin,
                fmax=fmax,
                cryst1=_format_cryst1(
                    widths[0], widths[1], widths[2], 90.0, 90.0, 90.0
                ),
            )

        lengths = lattice.lengths()
        center_frac = lattice.cart_to_frac(*center_ang)
        offsets_frac = (
            offsets_ang[0] / lengths[0],
            offsets_ang[1] / lengths[0],
            offsets_ang[2] / lengths[1],
            offsets_ang[3] / lengths[1],
            offsets_ang[4] / lengths[2],
            offsets_ang[5] / lengths[2],
        )
        mode = "angstrom-cell"
    else:
        center_frac = tuple(args.center_frac)
        offsets_frac = tuple(args.range)
        if not all(math.isfinite(value) for value in center_frac):
            raise ValueError("--center-frac: non-finite coordinates")
        _validate_offsets(offsets_frac, "--range")
        mode = "fractional"

    origin = (
        center_frac[0] + offsets_frac[0],
        center_frac[1] + offsets_frac[2],
        center_frac[2] + offsets_frac[4],
    )
    size = (
        offsets_frac[1] - offsets_frac[0],
        offsets_frac[3] - offsets_frac[2],
        offsets_frac[5] - offsets_frac[4],
    )
    out_lattice = Lattice.scaled(lattice, size)
    return Supercell(
        mode="cell",
        lattice=lattice,
        input_mode=mode,
        origin=origin,
        size=size,
        cryst1=_format_cryst1(*out_lattice.params()),
    )


def _header_lines(supercell: Supercell, source: str) -> list[str]:
    return [
        f"REMARK   SUPERCELL mode={supercell_mode(supercell)} source={source}",
        f"REMARK   SUPERCELL {supercell.describe()}",
    ]


def supercell_mode(supercell: Supercell) -> str:
    return supercell.input_mode


class SerialCounter:
    def __init__(self) -> None:
        self.value = 0
        self.overflow = False

    def next(self) -> int:
        self.value += 1
        if self.value > 99999:
            self.overflow = True
        return (self.value - 1) % 99999 + 1


def _record_line(
    record: PdbRecord, serial: int, x: float, y: float, z: float
) -> str:
    line = record.line.ljust(78)
    return (
        f"{line[:6]}{serial:5d}{line[11:30]}"
        f"{x:8.3f}{y:8.3f}{z:8.3f}{line[54:]}"
    )


def write_atoms(
    path: Path,
    records: Sequence[PdbRecord],
    supercell: Supercell,
    header: Sequence[str],
) -> tuple[int, bool]:
    out = [supercell.cryst1, *header]
    serial = SerialCounter()
    count = 0
    for record in records:
        for _, point in supercell.images((record.x, record.y, record.z)):
            out.append(_record_line(record, serial.next(), *point))
            count += 1
    out.append("END")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return count, serial.overflow


def write_cps(
    path: Path,
    records: Sequence[PdbRecord],
    supercell: Supercell,
    header: Sequence[str],
) -> tuple[int, bool]:
    out = [supercell.cryst1, *header]
    serial = SerialCounter()
    count = 0
    for record in records:
        for t, point in supercell.images((record.x, record.y, record.z)):
            if record.remark:
                out.append(f"{record.remark}  cell=({t[0]},{t[1]},{t[2]})")
            out.append(_record_line(record, serial.next(), *point))
            count += 1
    out.append("END")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return count, serial.overflow


def write_bonds(
    path: Path,
    groups: Sequence[BondGroup],
    supercell: Supercell,
    header: Sequence[str],
    global_remarks: Sequence[str],
) -> tuple[int, int, bool]:
    out = [supercell.cryst1, *header, *global_remarks]
    serial = SerialCounter()
    line_count = 0
    dot_count = 0
    for group in groups:
        copies: dict[
            tuple[int, int, int], list[tuple[PdbRecord, tuple[float, float, float]]]
        ] = {}
        for dot in group.dots:
            for t, point in supercell.images((dot.x, dot.y, dot.z)):
                copies.setdefault(t, []).append((dot, point))
        for t in sorted(copies):
            if group.remark:
                out.append(f"{group.remark}  cell=({t[0]},{t[1]},{t[2]})")
            else:
                out.append(f"REMARK   BOND cell=({t[0]},{t[1]},{t[2]})")
            line_count += 1
            for dot, point in copies[t]:
                out.append(_record_line(dot, serial.next(), *point))
                dot_count += 1
    out.append("END")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return line_count, dot_count, serial.overflow


def _strict_cell_box(supercell: Supercell) -> bool:
    if supercell.mode != "cell":
        return False
    assert supercell.size is not None
    return all(abs(value - round(value)) < 1e-6 for value in supercell.size)


def _box_kind(supercell: Supercell) -> str:
    if supercell.mode == "xyz":
        return "Cartesian crop"
    return "strict supercell" if _strict_cell_box(supercell) else "non-periodic crop"


def _summary_text(
    args: argparse.Namespace,
    supercell: Supercell,
    paths: dict[str, Path],
    counts: dict[str, tuple[int, ...]],
    warnings: Sequence[str],
) -> str:
    lines = [
        "Supercell summary",
        "=================",
        f"input dir: {Path(args.indir)}",
        f"output dir: {Path(args.outdir)}",
        f"source: {args.source}",
        f"mode: {supercell_mode(supercell)}",
        f"box: {_box_kind(supercell)}",
        f"description: {supercell.describe()}",
        f"CRYST1: {supercell.cryst1}",
    ]
    if args.center_ang is not None:
        lines.extend(
            [
                f"center_ang: {_fmt_tuple(args.center_ang, 6)}",
                f"range_ang: {_fmt_tuple(args.range_ang, 6)}",
                f"ang_axes: {args.ang_axes}",
            ]
        )
    else:
        lines.extend(
            [
                f"center_frac: {_fmt_tuple(args.center_frac, 6)}",
                f"range_frac: {_fmt_tuple(args.range, 6)}",
            ]
        )
    lines.extend(["", "Files:"])
    for name, path in paths.items():
        stats = counts.get(name, ())
        if name == "atoms":
            detail = f"{stats[0]} atoms"
        elif name == "cps":
            detail = f"{stats[0]} CP"
        else:
            detail = f"{stats[0]} segments, {stats[1]} dots"
        lines.append(f"  {path.name}: {detail}")
    if warnings:
        lines.extend(["", "Warnings:"])
        lines.extend(f"  {warning}" for warning in warnings)
    return "\n".join(lines) + "\n"


def _input_paths(indir: Path, source: str) -> tuple[Path, Path, Path, Path]:
    if source == "symm":
        return (
            indir / "cell.pdb",
            indir / "critical_points_symm.pdb",
            indir / "crystal_BPs_symm.pdb",
            indir / "calc_BPs_symm.pdb",
        )
    return (
        indir / "cell.pdb",
        indir / "critical_points.pdb",
        indir / "crystal_BPs.pdb",
        indir / "calc_BPs.pdb",
    )


def _validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    for name, values in (
        ("--center-frac", args.center_frac),
        ("--center-ang", args.center_ang),
        ("--range", args.range),
        ("--range-ang", args.range_ang),
    ):
        if values is not None and not all(math.isfinite(value) for value in values):
            parser.error(f"{name}: coordinates/offsets must be finite")

    has_ang_center = args.center_ang is not None
    has_ang_range = args.range_ang is not None
    if has_ang_center or has_ang_range:
        if not has_ang_center or not has_ang_range:
            parser.error("--center-ang and --range-ang must be used together")
        if args.center_frac is not None or args.range is not None:
            parser.error("do not mix fractional and angstrom center/range options")
        if args.ang_axes is None:
            args.ang_axes = "cell"
        try:
            _validate_offsets(args.range_ang, "--range-ang")
        except ValueError as exc:
            parser.error(str(exc))
        return
    if args.ang_axes is not None:
        parser.error("--ang-axes is only valid with --range-ang")
    if args.center_frac is None:
        args.center_frac = list(DEFAULT_CENTER_FRAC)
    if args.range is None:
        args.range = list(DEFAULT_RANGE_FRAC)
    try:
        _validate_offsets(args.range, "--range")
    except ValueError as exc:
        parser.error(str(exc))


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Build a supercell from TOPOND PDB files."
    )
    parser.add_argument("--indir", default="results", help="input directory")
    parser.add_argument(
        "--outdir",
        default=None,
        help="output directory (default: <indir>_supercell)",
    )
    parser.add_argument(
        "--source",
        choices=("symm", "base"),
        default="symm",
        help="use symmetry-expanded *_symm.pdb files or base files",
    )
    parser.add_argument(
        "--center-frac",
        nargs=3,
        type=float,
        metavar=("CX", "CY", "CZ"),
        help="fractional center of the supercell",
    )
    parser.add_argument(
        "--range",
        nargs=6,
        type=float,
        metavar=("X0", "X1", "Y0", "Y1", "Z0", "Z1"),
        help="fractional offsets from center along a, b, c",
    )
    parser.add_argument(
        "--center-ang",
        nargs=3,
        type=float,
        metavar=("CX", "CY", "CZ"),
        help="Cartesian center of the supercell in angstrom",
    )
    parser.add_argument(
        "--range-ang",
        nargs=6,
        type=float,
        metavar=("X0", "X1", "Y0", "Y1", "Z0", "Z1"),
        help="offsets from center in angstrom",
    )
    parser.add_argument(
        "--ang-axes",
        choices=("cell", "xyz"),
        default=None,
        help="interpret --range-ang along cell axes a,b,c or Cartesian x,y,z",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    _validate_args(parser, args)

    indir = Path(args.indir)
    if args.outdir is None:
        args.outdir = indir.with_name(indir.name + "_supercell")
    outdir = Path(args.outdir)

    atom_path, cps_path, crystal_path, calc_path = _input_paths(
        indir, args.source
    )
    required = (atom_path, cps_path, crystal_path, calc_path)
    missing = [path for path in required if not path.is_file()]
    if missing:
        for path in missing:
            print(f"ERROR: input file not found: {path}", file=sys.stderr)
        return 1

    try:
        lattice, atoms = read_atoms(atom_path)
        cps_lattice, cps = read_cps(cps_path)
        crystal_lattice, crystal_global, crystal_groups = read_bonds(crystal_path)
        calc_lattice, calc_global, calc_groups = read_bonds(calc_path)
        supercell = build_supercell(lattice, args)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    warnings: list[str] = []
    for path, other in (
        (cps_path, cps_lattice),
        (crystal_path, crystal_lattice),
        (calc_path, calc_lattice),
    ):
        if other is None:
            warnings.append(f"{path.name}: no CRYST1 record; using {atom_path.name}")
        elif not _lattices_match(lattice, other):
            warnings.append(
                f"{path.name}: CRYST1 differs from {atom_path.name}; using {atom_path.name}"
            )

    if supercell.mode == "xyz":
        warnings.append(
            "--ang-axes xyz produces an axis-aligned Cartesian crop, "
            "not a periodic supercell"
        )
    elif not _strict_cell_box(supercell):
        assert supercell.size is not None
        warnings.append(
            "supercell widths "
            f"{_fmt_tuple(supercell.size, 6)} are not integer cell multiples; "
            "box edges may cut molecules and bond paths"
        )

    outdir.mkdir(parents=True, exist_ok=True)
    header = _header_lines(supercell, args.source)

    atom_out = outdir / "cell_supercell.pdb"
    cps_out = outdir / (cps_path.stem + "_supercell.pdb")
    crystal_out = outdir / (crystal_path.stem + "_supercell.pdb")
    calc_out = outdir / (calc_path.stem + "_supercell.pdb")
    summary_out = outdir / "summary_supercell.txt"

    atom_count, atom_overflow = write_atoms(atom_out, atoms, supercell, header)
    cps_count, cps_overflow = write_cps(cps_out, cps, supercell, header)
    crystal_lines, crystal_dots, crystal_overflow = write_bonds(
        crystal_out, crystal_groups, supercell, header, crystal_global
    )
    calc_lines, calc_dots, calc_overflow = write_bonds(
        calc_out, calc_groups, supercell, header, calc_global
    )
    if any(
        (atom_overflow, cps_overflow, crystal_overflow, calc_overflow)
    ):
        warnings.append(
            "output has more than 99999 records; PDB serial numbers were wrapped"
        )

    counts = {
        "atoms": (atom_count,),
        "cps": (cps_count,),
        "crystal": (crystal_lines, crystal_dots),
        "calc": (calc_lines, calc_dots),
    }
    paths = {
        "atoms": atom_out,
        "cps": cps_out,
        "crystal": crystal_out,
        "calc": calc_out,
    }
    summary = _summary_text(args, supercell, paths, counts, warnings)
    summary_out.write_text(summary, encoding="utf-8")

    print(summary)
    print(f"Summary written to: {summary_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
