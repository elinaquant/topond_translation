#!/usr/bin/env python3
"""Export CRYSTAL17/TOPOND critical points (CP) for VESTA / VMD / JSmol.

The script scans a CRYSTAL17 .out file (TOPOND section), extracts the unit
cell, the cell atoms and all critical points, and writes PDB/XYZ files that
can be loaded together in a visualizer.

Repeated blocks (caused by parallel runs) are deduplicated:
  * atoms  - by atom number (coordinates as a tie-breaker);
  * CP     - by key (type, round(x_A, 6), round(y_A, 6), round(z_A, 6)).

Example:
    python topond_cp_export.py CP_BPs_dedup_final.out --outdir results
    python topond_cp_export.py CP_BPs_dedup_final.out --outdir results --align
    python topond_cp_export.py CP_BPs_dedup_final.out --outdir results \
        --space-group P12_1/n1 --symmetry-image shift
    python topond_cp_export.py CP.out --outdir results --dummy-element C --wrap

By default the CP coordinates are written exactly as parsed from the output
(raw).  Note that CRYSTAL prints atoms centered around the origin while TOPOND
CP coordinates use the corner image [0,1), so in VMD/JSmol (which draw a
single cell) many CP may look shifted.  Use --align to translate every CP by
a whole lattice vector into the same image as the atoms.  --wrap instead folds
the coordinates into the fractional box [0,1).

Output files (in --outdir):
    cell.pdb                  - unit cell atoms (+ CRYST1)
    critical_points.pdb       - all CP as HETATM, residue CPM1/CPP1/CPP3/CPN3
    critical_points.xyz       - all CP
    critical_points_3m1.xyz   - bond CP      (3,-1)  residue CPM1
    critical_points_3p1.xyz   - ring CP      (3,+1)  residue CPP1
    critical_points_3p3.xyz   - cage CP      (3,+3)  residue CPP3
    critical_points_3m3.xyz   - atomic CP    (3,-3)  residue CPN3
    crystal_BPs.pdb           - BCP -> explicitly found (3,-3) attractor
                               (residue CBP, segid CRYS)
    calc_BPs.pdb              - BCP -> nearest nucleus, if not already in
                               crystal_BPs and angle >= --bp-angle
                               (residue KBP, segid CALC)
    critical_points_symm.*    - symmetry-expanded CP (with --space-group)
    crystal_BPs_symm.pdb      - symmetry-expanded crystal BPs
    calc_BPs_symm.pdb         - symmetry-expanded calculated BPs
    summary.txt               - short summary and rejection diagnostics

Bond lines are written whenever atoms, lattice and BCPs are available.
Crystal BPs use only the (3,-3) attractors explicitly listed in
'SEARCH OF BOND PATH ATTRACTORS' whose terminus atom is present in the full
'CLUSTER OF ATOMS AROUND THE CP' table.  Calculated BPs use the two nearest
nuclei from the cluster table: each missing nucleus is connected only if it
is not already present in crystal_BPs and the BCP->nucleus/nucleus angle is at
least
--bp-angle (default 140 deg).  Every line is a cloud of dots ~0.05 A apart.
The BCP and its endpoint are translated together into the atom cell image
(independently of --align), so lines connect the drawn atoms.

Requires Python 3.10+.  The core export uses the standard library only;
optional symmetry expansion (--space-group) additionally needs spglib.
"""

from __future__ import annotations

import argparse
import math
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from topond_logging import log_action, log_error, setup

BOHR_TO_ANG = 0.529177210903

# spacing (angstrom) of the dots that approximate a bond path segment
BP_SPACING = 0.05

ELEMENTS = (
    "H He Li Be B C N O F Ne Na Mg Al Si P S Cl Ar K Ca Sc Ti V Cr Mn Fe Co "
    "Ni Cu Zn Ga Ge As Se Br Kr Rb Sr Y Zr Nb Mo Tc Ru Rh Pd Ag Cd In Sn Sb "
    "Te I Xe Cs Ba La Ce Pr Nd Pm Sm Eu Gd Tb Dy Ho Er Tm Yb Lu Hf Ta W Re Os "
    "Ir Pt Au Hg Tl Pb Bi Po At Rn Fr Ra Ac Th Pa U Np Pu Am Cm Bk Cf Es Fm "
    "Md No Lr Rf Db Sg Bh Hs Mt Ds Rg Cn Nh Fl Mc Lv Ts Og"
).split()

# (sign, magnitude) as parsed from "CP TYPE : (3,-1)" -> (3, -1)
CP_CLASS_NAMES = {
    (3, -1): ("BCP", "CPM1"),
    (3, 1): ("RCP", "CPP1"),
    (3, 3): ("CCP", "CPP3"),
    (3, -3): ("NCP", "CPN3"),
}

CP_TAG = {  # (3,-1) -> "3m1"
    (3, -1): "3m1",
    (3, 1): "3p1",
    (3, 3): "3p3",
    (3, -3): "3m3",
}

ATOM_LINE_RE = re.compile(
    r"^\s*(?P<num>\d+)\s+(?P<z>\d+)\s+(?P<sym>[A-Za-z]{1,2}\d*)\s+"
    r"(?P<shell>\d+)\s+(?P<x>[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+"
    r"(?P<y>[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+"
    r"(?P<zc>[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+\S+\s+\S+\s*$"
)
ATOM_HEADER_RE = re.compile(r"^\s*ATOM\s+N\.AT\.\s+SHELL")
CP_NUM_RE = re.compile(r"^\s*CP\s+N\.\s+(\d+)\s*$")
CP_TYPE_RE = re.compile(r"CP\s+TYPE\s*:\s*\(\s*(\d+)\s*,\s*([+-]?\d+)\s*\)")
CP_COORD_RE = re.compile(
    r"COORD\(AU\)\s*\(X\s*Y\s*Z\)\s*:\s*"
    r"(\S+)\s+(\S+)\s+(\S+)"
)
CP_PROPS_RE = re.compile(r"PROPERTIES\s*\(RHO,GRHO,LAP\)\s*:\s*(\S+)\s+(\S+)\s+(\S+)")
CP_GK_RE = re.compile(r"KINETIC\s+ENERGY\s+DENSITIES\s*\(G,K\)\s*:\s*(\S+)\s+(\S+)")
CP_VIRIAL_RE = re.compile(r"VIRIAL\s+DENSITY\s*:\s*(\S+)")
CP_ELF_RE = re.compile(r"ELF\([^)]*\)\s*:\s*(\S+)")
CP_ELLIP_RE = re.compile(r"ELLIPTICITY\s*:\s*(\S+)")
CLUSTER_RE = re.compile(r"CLUSTER OF ATOMS AROUND THE CP")
SEARCH_BOND_RE = re.compile(r"SEARCH OF BOND PATH ATTRACTORS")
TERMINUS_RE = re.compile(
    r"CLUSTER OF ATOMS AROUND THE TERMINUS OF THE PATH"
)
ATTRACTOR_TYPE_RE = re.compile(
    r"ATTRACTOR\s+CP\s+TYPE\s*:\s*\(\s*(\d+)\s*,\s*([+-]?\d+)\s*\)"
)
UNABLE_ATTRACTOR_RE = re.compile(
    r"UNABLE\s+TO\s+FIND\s+THE\s+PATH\s+ATTRACTOR\s+(\d+)"
)
PHASE_SEARCH_RE = re.compile(r"SEARCH\s+OF\s+\(\s*[-+0-9.,]+\s*\)\s*CPS")
# NEW  OLD  CELL(3)  AT.NU.  X(AU) Y(AU) Z(AU)  DISTANCE(ANG)
CLUSTER_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+"
    r"(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$"
)
FLOAT_RE = re.compile(r"[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?")


@dataclass
class Lattice:
    """Direct lattice vectors (rows, angstrom)."""

    a_vec: tuple[float, float, float]
    b_vec: tuple[float, float, float]
    c_vec: tuple[float, float, float]

    def params(self) -> tuple[float, float, float, float, float, float]:
        a, b, c = self.a_vec, self.b_vec, self.c_vec
        la, lb, lc = (math.sqrt(sum(v * v for v in u)) for u in (a, b, c))

        def angle(u, v):
            du = math.sqrt(sum(t * t for t in u))
            dv = math.sqrt(sum(t * t for t in v))
            cos = sum(p * q for p, q in zip(u, v)) / (du * dv)
            return math.degrees(math.acos(max(-1.0, min(1.0, cos))))

        return (
            la,
            lb,
            lc,
            angle(b, c),
            angle(a, c),
            angle(a, b),
        )

    def cart_to_frac(self, x: float, y: float, z: float) -> tuple[float, float, float]:
        # cart = M^T . f  (lattice vectors are rows), so f = (M^T)^-1 . cart
        m = [self.a_vec, self.b_vec, self.c_vec]
        mt = [[m[j][i] for j in range(3)] for i in range(3)]
        det = _det3(mt)
        if abs(det) < 1e-12:
            raise ValueError("degenerate lattice vectors")
        inv = _inv3(mt, det)
        return tuple(
            inv[i][0] * x + inv[i][1] * y + inv[i][2] * z for i in range(3)
        )  # type: ignore[return-value]

    def frac_to_cart(self, f0: float, f1: float, f2: float) -> tuple[float, float, float]:
        return (
            f0 * self.a_vec[0] + f1 * self.b_vec[0] + f2 * self.c_vec[0],
            f0 * self.a_vec[1] + f1 * self.b_vec[1] + f2 * self.c_vec[1],
            f0 * self.a_vec[2] + f1 * self.b_vec[2] + f2 * self.c_vec[2],
        )


@dataclass
class Atom:
    num: int
    z: int
    symbol: str
    x: float
    y: float
    z_: float


@dataclass
class Attractor:
    """A (3,-3) attractor found in "SEARCH OF BOND PATH ATTRACTORS"."""

    cp_type: tuple[int, int]
    x: float  # angstrom, attractor CP coordinate
    y: float
    z_: float
    atom: Atom | None = None  # terminus nucleus from the cluster table


@dataclass
class CP:
    num: int  # original "CP N." of the first occurrence
    cp_type: tuple[int, int]
    x: float  # angstrom
    y: float
    z_: float
    rho: float | None = None
    grho: float | None = None
    lap: float | None = None
    g: float | None = None
    k: float | None = None
    virial: float | None = None
    elf: float | None = None
    ellipticity: float | None = None
    # first two rows of "CLUSTER OF ATOMS AROUND THE CP" (nearest nuclei)
    attractors: list[Atom] | None = None
    sym_op: int | None = None  # index of the space-group operation, if expanded
    cell: int | None = None
    # full "CLUSTER OF ATOMS AROUND THE CP" table (usually 10 rows)
    cluster_atoms: list[Atom] | None = None
    # (3,-3) attractors explicitly listed in SEARCH OF BOND PATH ATTRACTORS
    bp_attractors: list[Attractor] | None = None
    # indices from "THE CODE WAS UNABLE TO FIND THE PATH ATTRACTOR n"
    missing_attractor_ids: list[int] | None = None

    @property
    def key(self) -> tuple[tuple[int, int], float, float, float]:
        return (self.cp_type, round(self.x, 6), round(self.y, 6), round(self.z_, 6))

    @property
    def kind(self) -> str:
        return CP_CLASS_NAMES.get(self.cp_type, ("UNK", "CPUNK"))[0]

    @property
    def residue(self) -> str:
        return CP_CLASS_NAMES.get(self.cp_type, ("UNK", "CPUNK"))[1]

    @property
    def tag(self) -> str:
        return CP_TAG.get(self.cp_type, f"{self.cp_type[0]}x{self.cp_type[1]:+d}".replace("+", "p").replace("-", "m"))


@dataclass
class BondPath:
    """One BCP: two segments BCP->attractor, approximated by dots."""

    cp: CP  # BCP position (already in the atom cell image)
    atom1: Atom  # attractors (already in the same image as cp)
    atom2: Atom
    d1: float  # BCP-attractor distances (angstrom)
    d2: float
    dots: list[tuple[float, float, float]]  # all dots, both segments
    residual: float = 0.0  # max endpoint distance to the nearest drawn atom
    from_file: bool = True  # attractor pair taken from the CP cluster table


@dataclass
class BondLine:
    """One BCP -> (3,-3) attractor (or nucleus) line as a dot cloud."""

    cp_num: int
    cp_type: tuple[int, int]
    bcp: tuple[float, float, float]  # angstrom, atom cell image
    end: tuple[float, float, float]  # angstrom, same image as bcp
    dots: list[tuple[float, float, float]]
    source: str  # "crystal" or "calc"
    end_num: int | None = None
    end_symbol: str | None = None
    length: float = 0.0
    angle: float | None = None  # for calculated BPs
    sym_op: int | None = None
    note: str | None = None


def _det3(m):
    return (
        m[0][0] * (m[1][1] * m[2][2] - m[1][2] * m[2][1])
        - m[0][1] * (m[1][0] * m[2][2] - m[1][2] * m[2][0])
        + m[0][2] * (m[1][0] * m[2][1] - m[1][1] * m[2][0])
    )


def _inv3(m, det):
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


def normalize_symbol(raw: str) -> str:
    """'CL' -> 'Cl', 'I' -> 'I'."""
    s = re.sub(r"\d+$", "", raw)
    return s[:1].upper() + s[1:].lower() if s else "X"


def symbol_from_z(z: int) -> str:
    if 1 <= z <= len(ELEMENTS):
        return ELEMENTS[z - 1]
    return "X"


def z_from_symbol(sym: str, nat: int) -> int:
    norm = normalize_symbol(sym)
    try:
        return ELEMENTS.index(norm) + 1
    except ValueError:
        pass
    if nat > 200:  # CRYSTAL ECP notation: Z + 200 (e.g. I -> 253)
        return nat - 200
    return nat


# ---------------------------------------------------------------- parsing ---


def parse_lattice(text: str) -> Lattice | None:
    """First correct 'DIRECT LATTICE VECTOR COMPONENTS (ANGSTROM)' block."""
    lines = text.splitlines()
    for i, line in enumerate(lines):
        if "DIRECT LATTICE VECTOR COMPONENTS" in line.upper():
            vecs = []
            for raw in lines[i + 1 : i + 4]:
                nums = FLOAT_RE.findall(raw)
                if len(nums) >= 3:
                    vecs.append(tuple(float(n) for n in nums[:3]))
            if len(vecs) == 3:
                return Lattice(*vecs)

    # Fallback: 'LATTICE PARAMETERS (ANGSTROM AND DEGREES)' value line.
    for i, line in enumerate(lines):
        if "LATTICE PARAMETERS" in line and "ANGSTROM" in line.upper():
            for raw in lines[i + 1 : i + 4]:
                nums = FLOAT_RE.findall(raw)
                if len(nums) >= 6:
                    a, b, c, alpha, beta, gamma = (float(n) for n in nums[:6])
                    a_vec, b_vec, c_vec = _vectors_from_params(
                        a, b, c, alpha, beta, gamma
                    )
                    return Lattice(a_vec, b_vec, c_vec)
    return None


def _vectors_from_params(a, b, c, alpha, beta, gamma):
    """Standard crystallographic setting: a || x, b in xy-plane."""
    al, be, ga = (math.radians(t) for t in (alpha, beta, gamma))
    cx = c * math.cos(be)
    cy = c * (math.cos(al) - math.cos(be) * math.cos(ga)) / math.sin(ga)
    cz = math.sqrt(max(c * c - cx * cx - cy * cy, 0.0))
    return (a, 0.0, 0.0), (b * math.cos(ga), b * math.sin(ga), 0.0), (cx, cy, cz)


def parse_atoms(text: str, keep_duplicates: bool = False) -> list[Atom]:
    """Cell atoms after 'ATOM N.AT. SHELL ...' headers, deduplicated."""
    lines = text.splitlines()
    atoms: list[Atom] = []
    index_by_num: dict[int, int] = {}
    index_by_coord: dict[tuple, int] = {}
    pending = False

    for line in lines:
        if ATOM_HEADER_RE.match(line):
            pending = True
            continue
        if set(line.strip()) == {"*"} and line.strip():
            continue  # separator line, keeps the current block open
        m = ATOM_LINE_RE.match(line)
        if m and pending:
            num = int(m.group("num"))
            x, y, zc = (float(m.group(g)) for g in ("x", "y", "zc"))
            symbol = normalize_symbol(m.group("sym"))
            atom = Atom(num, z_from_symbol(symbol, int(m.group("z"))), symbol, x, y, zc)

            if keep_duplicates:
                atoms.append(atom)
                continue
            if num in index_by_num:
                prev = atoms[index_by_num[num]]
                if (
                    abs(prev.x - x) < 1e-4
                    and abs(prev.y - y) < 1e-4
                    and abs(prev.z_ - zc) < 1e-4
                ):
                    continue  # exact duplicate from a repeated block
            ckey = (round(x, 4), round(y, 4), round(zc, 4))
            if ckey in index_by_coord:
                continue
            index_by_coord[ckey] = len(atoms)
            if num not in index_by_num:
                index_by_num[num] = len(atoms)
            atoms.append(atom)
            continue
        if line.strip() and not ATOM_LINE_RE.match(line):
            pending = False

    return atoms


def _cluster_atom(row: re.Match) -> Atom:
    """Atom from a CLUSTER table row (NEW OLD CELL AT.NU. X Y Z DIST)."""
    z = int(row.group(6))
    if z > 200:  # CRYSTAL ECP notation
        z -= 200
    return Atom(
        int(row.group(2)), z, symbol_from_z(z),
        float(row.group(7)) * BOHR_TO_ANG,
        float(row.group(8)) * BOHR_TO_ANG,
        float(row.group(9)) * BOHR_TO_ANG,
    )


def _merge_cp_data(dst: CP, src: CP) -> None:
    """Merge two copies of the same CP without losing attractor data."""
    if dst.rho is None and src.rho is not None:
        dst.rho = src.rho
    for attr in ("grho", "lap", "g", "k", "virial", "elf", "ellipticity"):
        if getattr(dst, attr) is None and getattr(src, attr) is not None:
            setattr(dst, attr, getattr(src, attr))
    if dst.attractors is None and src.attractors:
        dst.attractors = src.attractors
    if dst.cluster_atoms is None and src.cluster_atoms:
        dst.cluster_atoms = src.cluster_atoms
    if src.bp_attractors:
        existing = dst.bp_attractors or []
        by_coord = {
            (round(a.x, 4), round(a.y, 4), round(a.z_, 4)): a
            for a in existing
        }
        for attr in src.bp_attractors:
            key = (round(attr.x, 4), round(attr.y, 4), round(attr.z_, 4))
            old = by_coord.get(key)
            if old is None:
                existing.append(attr)
                by_coord[key] = attr
            elif old.atom is None and attr.atom is not None:
                old.atom = attr.atom
        dst.bp_attractors = existing
    if src.missing_attractor_ids:
        dst.missing_attractor_ids = sorted(
            set(dst.missing_attractor_ids or [])
            | set(src.missing_attractor_ids)
        )


def parse_cps(text: str, keep_duplicates: bool = False) -> list[CP]:
    """Parse all 'CP N. <n>' blocks.

    Besides the CP type/coordinates/properties and the first two rows of
    ``CLUSTER OF ATOMS AROUND THE CP``, this also stores the full cluster
    table and, for (3,-1) points, every ``(3,-3)`` attractor explicitly
    listed in ``SEARCH OF BOND PATH ATTRACTORS``.  Coordinates are converted
    to angstrom in the same frame as the CP.
    """
    lines = text.splitlines()
    starts = [
        i for i, line in enumerate(lines) if CP_NUM_RE.match(line)
    ]
    raw_cps: list[CP] = []

    for pos, start in enumerate(starts):
        end = starts[pos + 1] if pos + 1 < len(starts) else len(lines)
        block = lines[start:end]
        num = int(CP_NUM_RE.match(block[0]).group(1))  # type: ignore[union-attr]
        cp_type: tuple[int, int] | None = None
        coord: tuple[float, float, float] | None = None
        rho: float | None = None
        grho: float | None = None
        lap: float | None = None
        g: float | None = None
        k: float | None = None
        virial: float | None = None
        elf: float | None = None
        ellipticity: float | None = None
        cluster_atoms: list[Atom] = []
        bp_attractors: list[Attractor] = []
        missing: list[int] = []

        in_cp_cluster = False
        in_search = False
        in_terminus = False
        expect_attr_coord = False
        attr_type: tuple[int, int] | None = None
        attr_coord: tuple[float, float, float] | None = None
        attr_atom: Atom | None = None
        attr_by_coord: dict[tuple, Attractor] = {}

        def flush_attr() -> None:
            nonlocal attr_type, attr_coord, attr_atom
            nonlocal expect_attr_coord, in_terminus
            if attr_type is not None and attr_coord is not None:
                key = (
                    round(attr_coord[0], 4),
                    round(attr_coord[1], 4),
                    round(attr_coord[2], 4),
                )
                old = attr_by_coord.get(key)
                if old is None:
                    item = Attractor(attr_type, *attr_coord, atom=attr_atom)
                    attr_by_coord[key] = item
                    bp_attractors.append(item)
                elif old.atom is None and attr_atom is not None:
                    old.atom = attr_atom
            attr_type = None
            attr_coord = None
            attr_atom = None
            expect_attr_coord = False
            in_terminus = False

        for line in block[1:]:
            if cp_type is None:
                m = CP_TYPE_RE.search(line)
                if m:
                    cp_type = (int(m.group(1)), int(m.group(2)))
                    continue
            if coord is None:
                m = CP_COORD_RE.search(line)
                if m:
                    coord = tuple(  # type: ignore[assignment]
                        float(v) * BOHR_TO_ANG for v in m.groups()
                    )
                    continue
            m = CP_PROPS_RE.search(line)
            if m and rho is None:
                try:
                    rho = float(m.group(1))
                    grho = float(m.group(2))
                    lap = float(m.group(3))
                except ValueError:
                    pass
                continue
            m = CP_GK_RE.search(line)
            if m and g is None:
                try:
                    g = float(m.group(1))
                    k = float(m.group(2))
                except ValueError:
                    pass
                continue
            m = CP_VIRIAL_RE.search(line)
            if m and virial is None:
                try:
                    virial = float(m.group(1))
                except ValueError:
                    virial = None
                continue
            if elf is None:
                m = CP_ELF_RE.search(line)
                if m:
                    try:
                        elf = float(m.group(1))
                    except ValueError:
                        elf = None
                    continue
            if ellipticity is None:
                m = CP_ELLIP_RE.search(line)
                if m:
                    try:
                        ellipticity = float(m.group(1))
                    except ValueError:
                        ellipticity = None
                    continue

            if CLUSTER_RE.search(line):
                in_cp_cluster = True
                in_search = False
                continue
            if in_cp_cluster:
                row = CLUSTER_ROW_RE.match(line)
                if row:
                    cluster_atoms.append(_cluster_atom(row))
                    continue
                if line.strip() and not line.lstrip().startswith("NEW"):
                    in_cp_cluster = False
                else:
                    continue

            if cp_type == (3, -1) and SEARCH_BOND_RE.search(line):
                in_search = True
                continue
            if not in_search:
                continue
            if PHASE_SEARCH_RE.search(line):
                flush_attr()
                in_search = False
                continue

            m = ATTRACTOR_TYPE_RE.search(line)
            if m:
                flush_attr()
                attr_type = (int(m.group(1)), int(m.group(2)))
                expect_attr_coord = True
                continue
            if expect_attr_coord:
                m = CP_COORD_RE.search(line)
                if m:
                    attr_coord = tuple(  # type: ignore[assignment]
                        float(v) * BOHR_TO_ANG for v in m.groups()
                    )
                    expect_attr_coord = False
                    continue

            m = UNABLE_ATTRACTOR_RE.search(line)
            if m:
                flush_attr()
                missing.append(int(m.group(1)))
                continue

            if TERMINUS_RE.search(line):
                in_terminus = True
                continue
            if in_terminus:
                row = CLUSTER_ROW_RE.match(line)
                if row:
                    attr_atom = _cluster_atom(row)
                    flush_attr()
                    continue
                if line.strip() and not line.lstrip().startswith("NEW"):
                    in_terminus = False
                continue

            if line.lstrip().startswith("BPL (ANG)"):
                flush_attr()

        flush_attr()
        if cp_type is None or coord is None:
            continue
        cp = CP(
            num, cp_type, *coord,
            rho=rho,
            grho=grho,
            lap=lap,
            g=g,
            k=k,
            virial=virial,
            elf=elf,
            ellipticity=ellipticity,
            attractors=cluster_atoms[:2] or None,
            cluster_atoms=cluster_atoms or None,
            bp_attractors=bp_attractors or None,
            missing_attractor_ids=sorted(set(missing)) or None,
        )
        raw_cps.append(cp)

    if keep_duplicates:
        return raw_cps

    cps: list[CP] = []
    seen: dict[tuple, int] = {}
    for cp in raw_cps:
        index = seen.get(cp.key)
        if index is None:
            seen[cp.key] = len(cps)
            cps.append(cp)
        else:
            _merge_cp_data(cps[index], cp)
    return cps


# ---------------------------------------------------------------- output ---


def format_pdb_atom(
    serial: int,
    name: str,
    resname: str,
    resseq: int,
    x: float,
    y: float,
    z: float,
    element: str,
    chain: str = "A",
    occupancy: float = 1.00,
    bfactor: float = 0.00,
    record: str = "ATOM",
    segid: str = "",
) -> str:
    """PDB record with strict column layout.

    Note: residue names CPM1/CPP1/CPP3/CPN3 are 4 characters, they occupy
    columns 18-21 (the 4th char sits in the usually-blank column 21) so that
    chain/resSeq/x/y/z/element stay at their standard positions. The full
    tag is also stored in the SEGID field (cols 73-76).
    """
    name_field = f"{name:<4}"[:4]
    segid_field = f"{segid:<4}"[:4]
    return (
        f"{record:<6}{serial:5d} {name_field} "
        f"{resname:<4}{chain:1s}{resseq:4d}    "
        f"{x:8.3f}{y:8.3f}{z:8.3f}{occupancy:6.2f}{bfactor:6.2f}"
        f"      {segid_field}{element:>2}"
    )


def write_pdb(
    path: Path,
    points: list[Atom] | list[CP],
    lattice: Lattice | None = None,
    dummy_element: str = "X",
    as_cp: bool = False,
    remarks: list[str] | None = None,
) -> None:
    """Write PDB: cell atoms (as_cp=False) or critical points (as_cp=True)."""
    out: list[str] = []
    if lattice is not None:
        a, b, c, alpha, beta, gamma = lattice.params()
        out.append(
            f"CRYST1{a:9.3f}{b:9.3f}{c:9.3f}{alpha:7.2f}{beta:7.2f}{gamma:7.2f}"
            f" P 1           1"
        )
    for rem in remarks or []:
        out.append(f"REMARK   {rem}")

    if as_cp:
        for i, cp in enumerate(points, start=1):  # type: ignore[arg-type]
            out.append(
                format_pdb_atom(
                    i, " CP", cp.residue, 1, cp.x, cp.y, cp.z_,
                    dummy_element, record="HETATM", segid=cp.residue,
                )
            )
    else:
        for i, at in enumerate(points, start=1):  # type: ignore[arg-type]
            out.append(
                format_pdb_atom(
                    i, at.symbol if len(at.symbol) == 2 else f" {at.symbol}",
                    at.symbol.upper(), at.num, at.x, at.y, at.z_,
                    at.symbol, segid="CELL",
                )
            )
    out.append("END")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def write_xyz(path: Path, cps: list[CP], dummy_element: str = "X") -> None:
    lines = [str(len(cps)), f"critical points ({dummy_element}) from TOPOND"]
    for cp in cps:
        lines.append(f"{dummy_element} {cp.x:.6f} {cp.y:.6f} {cp.z_:.6f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def wrap_cp(cp: CP, lattice: Lattice) -> CP:
    f = lattice.cart_to_frac(cp.x, cp.y, cp.z_)
    wrapped = []
    for t in f:
        w = t - math.floor(t)
        if w > 1.0 - 1e-9:  # snap values like -1e-16 (-> 1.0) back to 0
            w = 0.0
        wrapped.append(w)
    x, y, z = lattice.frac_to_cart(*wrapped)
    return CP(cp.num, cp.cp_type, x, y, z, cp.rho, cp.elf, cp.attractors,
              cp.sym_op)


def center_wrap_cp(cp: CP, lattice: Lattice) -> CP:
    """Wrap into the fractional box [-0.5, 0.5) (fallback without atoms)."""
    f = lattice.cart_to_frac(cp.x, cp.y, cp.z_)
    f = tuple(t - round(t) for t in f)
    x, y, z = lattice.frac_to_cart(*f)
    return CP(cp.num, cp.cp_type, x, y, z, cp.rho, cp.elf, cp.attractors,
              cp.sym_op)


def align_cp_to_atoms(
    cp: CP, lattice: Lattice, atoms: list[Atom]
) -> tuple[CP, tuple[int, int, int]]:
    """Translate CP by a whole lattice vector so it lands in the same cell
    image as the drawn atoms.

    Atoms in the CRYSTAL output are printed centered around the origin, while
    TOPOND CP coordinates live in the corner image [0,1); without alignment
    many CP appear floating outside the drawn cell in VMD/JSmol.  Translating
    by an integer combination of lattice vectors does not change the physical
    position of the point (same periodic image), only its bookkeeping.

    Returns ``(new_cp, (n1, n2, n3))`` where the shift is
    ``n1*a + n2*b + n3*c``.  Without atoms falls back to ``center_wrap_cp``.
    """
    if not atoms:
        return center_wrap_cp(cp, lattice), (0, 0, 0)

    best_n = _atom_image_shift((cp.x, cp.y, cp.z_), lattice, atoms)
    if best_n == (0, 0, 0):
        return cp, best_n
    n1, n2, n3 = best_n
    dx = n1 * lattice.a_vec[0] + n2 * lattice.b_vec[0] + n3 * lattice.c_vec[0]
    dy = n1 * lattice.a_vec[1] + n2 * lattice.b_vec[1] + n3 * lattice.c_vec[1]
    dz = n1 * lattice.a_vec[2] + n2 * lattice.b_vec[2] + n3 * lattice.c_vec[2]
    best_q = (cp.x + dx, cp.y + dy, cp.z_ + dz)
    shifted = None
    if cp.attractors:
        shifted = [
            Atom(a.num, a.z, a.symbol, a.x + dx, a.y + dy, a.z_ + dz)
            for a in cp.attractors
        ]
    return CP(cp.num, cp.cp_type, *best_q, cp.rho, cp.elf, shifted, cp.sym_op), best_n


def align_cps_to_atoms(
    cps: list[CP], lattice: Lattice, atoms: list[Atom]
) -> tuple[list[CP], dict[tuple[int, int, int], int]]:
    """Translate every CP into the atom cell image (whole lattice vectors).

    Returns ``(aligned_cps, moved)`` where ``moved`` maps each non-zero
    integer shift ``(n1, n2, n3)`` to how many CP used it.  Attractors are
    carried along so bond paths elsewhere stay consistent.
    """
    moved: dict[tuple[int, int, int], int] = {}
    aligned: list[CP] = []
    cache: dict[tuple, tuple[CP, tuple[int, int, int]]] = {}
    for cp in cps:
        hit = cache.get(cp.key)
        if hit is None:
            hit = align_cp_to_atoms(cp, lattice, atoms)
            cache[cp.key] = hit
        new_cp, shift = hit
        aligned.append(
            CP(cp.num, cp.cp_type, new_cp.x, new_cp.y, new_cp.z_,
               cp.rho, cp.elf, new_cp.attractors, cp.sym_op)
        )
        if shift != (0, 0, 0):
            moved[shift] = moved.get(shift, 0) + 1
    return aligned, moved


# ----------------------------------------------------------- bond paths ---


def find_attractors(cp: CP, atoms: list[Atom], k: int = 2) -> list[tuple[float, Atom]]:
    """The k nearest distinct nuclei (attractors) of a bond critical point.

    Distinct = not coincident (duplicated atom copies from repeated blocks
    would otherwise be picked twice for the same nucleus).
    """
    picked: list[tuple[float, Atom]] = []
    for dist, atom in sorted(
        ((math.dist((cp.x, cp.y, cp.z_), (a.x, a.y, a.z_)), a) for a in atoms),
        key=lambda t: t[0],
    ):
        if any(
            abs(atom.x - other.x) < 1e-6
            and abs(atom.y - other.y) < 1e-6
            and abs(atom.z_ - other.z_) < 1e-6
            for _, other in picked
        ):
            continue
        picked.append((dist, atom))
        if len(picked) >= k:
            break
    return picked


def _segment_dots(
    p0: tuple[float, float, float],
    p1: tuple[float, float, float],
    spacing: float,
) -> list[tuple[float, float, float]]:
    """Dots along the segment p0->p1, both endpoints included."""
    length = math.dist(p0, p1)
    n = max(1, round(length / spacing))
    return [
        tuple(p0[i] + (p1[i] - p0[i]) * k / n for i in range(3))
        for k in range(n + 1)
    ]


def generate_bond_paths(
    cps: list[CP],
    atoms: list[Atom],
    lattice: Lattice,
    spacing: float = BP_SPACING,
) -> list[BondPath]:
    """Bond paths for every unique BCP: dots along BCP -> attractor segments.

    The attractor pair is taken from the 'CLUSTER OF ATOMS AROUND THE CP'
    table of the CRYSTAL output (first two rows: element, atom number and
    coordinates of the two nuclei).  The BCP and its attractors are first
    translated together into the atom cell image (align_cp_to_atoms) so that
    the path connects the nuclei as drawn in cell.pdb, regardless of whether
    --align was given.  Each BCP yields two segments (BCP -> attractor1,
    BCP -> attractor2); the shared BCP dot is written once.

    If a BCP has no cluster table (rare), the two nearest distinct nuclei
    are used instead (``from_file=False`` in the result).
    """
    bonds: list[BondPath] = []
    seen: set[tuple] = set()
    atom_xyz = [(a.x, a.y, a.z_) for a in atoms]
    for cp in cps:
        if cp.cp_type != (3, -1) or cp.key in seen:
            continue
        seen.add(cp.key)
        from_file = cp.attractors is not None and len(cp.attractors) >= 2
        aligned, _ = align_cp_to_atoms(cp, lattice, atoms)
        if aligned.attractors is not None and len(aligned.attractors) >= 2:
            atom1, atom2 = aligned.attractors[0], aligned.attractors[1]
        else:
            near = find_attractors(aligned, atoms)
            if len(near) < 2:
                continue
            atom1, atom2 = near[0][1], near[1][1]
        d1 = math.dist((aligned.x, aligned.y, aligned.z_),
                       (atom1.x, atom1.y, atom1.z_))
        d2 = math.dist((aligned.x, aligned.y, aligned.z_),
                       (atom2.x, atom2.y, atom2.z_))
        bcp = (aligned.x, aligned.y, aligned.z_)
        dots = _segment_dots(bcp, (atom1.x, atom1.y, atom1.z_), spacing)
        dots += _segment_dots(bcp, (atom2.x, atom2.y, atom2.z_), spacing)[1:]
        residual = max(
            min(math.dist((a.x, a.y, a.z_), p) for p in atom_xyz)
            for a in (atom1, atom2)
        )
        bonds.append(BondPath(aligned, atom1, atom2, d1, d2, dots,
                              residual=residual, from_file=from_file))
    return bonds


def write_bond_paths(
    path: Path,
    bonds: list[BondPath],
    lattice: Lattice | None = None,
    dummy_element: str = "X",
) -> None:
    """Write bond paths as a cloud of dots (PDB HETATM records).

    Dots are spaced ~BP_SPACING angstrom apart, so in VMD/JSmol they merge
    into a line when drawn as points / small spheres.  Both segments of one
    BCP share a residue (resname BP1, resseq = bond index), so a single
    bond path can be selected separately.  One REMARK line per bond names
    the attractor pair (element and atom number from the ATOM table).
    """
    out: list[str] = []
    if lattice is not None:
        a, b, c, alpha, beta, gamma = lattice.params()
        out.append(
            f"CRYST1{a:9.3f}{b:9.3f}{c:9.3f}{alpha:7.2f}{beta:7.2f}{gamma:7.2f}"
            f" P 1           1"
        )
    n_dots = sum(len(bond.dots) for bond in bonds)
    out.append(
        f"REMARK   BOND PATHS from TOPOND: {len(bonds)} bonds x 2 segments, "
        f"{n_dots} dots, spacing ~{BP_SPACING:.2f} A"
    )
    serial = 0
    for index, bond in enumerate(bonds, start=1):
        note = "" if bond.from_file else " (nearest nuclei: no cluster table)"
        out.append(
            f"REMARK   BOND {index:2d}: CP N. {bond.cp.num}  attractors "
            f"{bond.atom1.num}({bond.atom1.symbol}) + "
            f"{bond.atom2.num}({bond.atom2.symbol})  "
            f"d={bond.d1:.3f}/{bond.d2:.3f} A{note}"
        )
        if bond.residual > 0.5:
            out.append(
                f"REMARK          end on a periodic image of the nucleus "
                f"(crosses the cell boundary), {bond.residual:.2f} A "
                f"from the drawn atom"
            )
        for x, y, z in bond.dots:
            serial += 1
            out.append(
                format_pdb_atom(
                    serial, " BP", "BP1", index, x, y, z,
                    dummy_element, record="HETATM", segid="PATH",
                )
            )
    out.append("END")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


def _bond_length(
    lattice: Lattice,
    p0: tuple[float, float, float],
    p1: tuple[float, float, float],
) -> float:
    """Shortest periodic distance between two points (angstrom)."""
    f0 = lattice.cart_to_frac(*p0)
    df = _min_image_frac(lattice.cart_to_frac(*p1), f0)
    return math.dist((0.0, 0.0, 0.0), lattice.frac_to_cart(*df))


def _angle_deg(
    a: tuple[float, float, float],
    b: tuple[float, float, float],
    center: tuple[float, float, float],
) -> float:
    v1 = tuple(a[i] - center[i] for i in range(3))
    v2 = tuple(b[i] - center[i] for i in range(3))
    n1 = math.sqrt(sum(v * v for v in v1))
    n2 = math.sqrt(sum(v * v for v in v2))
    if n1 < 1e-12 or n2 < 1e-12:
        return 0.0
    cosang = sum(v1[i] * v2[i] for i in range(3)) / (n1 * n2)
    return math.degrees(math.acos(max(-1.0, min(1.0, cosang))))


def _cluster_atom_for_attractor(
    cp: CP, attr: Attractor
) -> Atom | None:
    """Terminus atom from the CP cluster table, if present."""
    if attr.atom is not None:
        return attr.atom
    if not cp.cluster_atoms:
        return None
    best: tuple[float, Atom] | None = None
    for atom in cp.cluster_atoms:
        dist = math.dist(
            (attr.x, attr.y, attr.z_),
            (atom.x, atom.y, atom.z_),
        )
        if best is None or dist < best[0]:
            best = (dist, atom)
    if best is not None and best[0] <= 0.05:
        return best[1]
    return None


def _make_bond_line(
    cp: CP,
    bcp: tuple[float, float, float],
    end: tuple[float, float, float],
    end_num: int | None,
    end_symbol: str | None,
    source: str,
    lattice: Lattice,
    atoms: list[Atom],
    spacing: float = BP_SPACING,
    angle: float | None = None,
    image: str = "shift",
    sym_op: int | None = None,
    note: str | None = None,
) -> BondLine:
    """Build one BCP->attractor line, aligned with the drawn atom image."""
    shift = _atom_image_shift(bcp, lattice, atoms) if atoms else (0, 0, 0)
    n1, n2, n3 = shift
    dx = n1 * lattice.a_vec[0] + n2 * lattice.b_vec[0] + n3 * lattice.c_vec[0]
    dy = n1 * lattice.a_vec[1] + n2 * lattice.b_vec[1] + n3 * lattice.c_vec[1]
    dz = n1 * lattice.a_vec[2] + n2 * lattice.b_vec[2] + n3 * lattice.c_vec[2]
    bcp2 = (bcp[0] + dx, bcp[1] + dy, bcp[2] + dz)
    end2 = (end[0] + dx, end[1] + dy, end[2] + dz)
    # draw the physically short BCP->attractor segment (nearest periodic image)
    fb = lattice.cart_to_frac(*bcp2)
    fe = lattice.cart_to_frac(*end2)
    df = _min_image_frac(fe, fb)
    end2 = lattice.frac_to_cart(*tuple(fb[i] + df[i] for i in range(3)))
    if image == "cut":
        box = _frac_box(atoms, lattice)
        dots = _dots_cut(bcp2, end2, lattice, box, spacing)
        length = _bond_length(lattice, bcp2, end2)
    else:
        dots = _segment_dots(bcp2, end2, spacing)
        length = math.dist(bcp2, end2)
    return BondLine(
        cp_num=cp.num,
        cp_type=cp.cp_type,
        bcp=bcp2,
        end=end2,
        dots=dots,
        source=source,
        end_num=end_num,
        end_symbol=end_symbol,
        length=length,
        angle=angle,
        sym_op=sym_op,
        note=note,
    )


def generate_crystal_bond_lines(
    cps: list[CP],
    atoms: list[Atom],
    lattice: Lattice,
    spacing: float = BP_SPACING,
) -> tuple[list[BondLine], dict]:
    """Crystal BPs from explicitly found (3,-3) attractors.

    Only attractors whose terminus atom is present in the full
    ``CLUSTER OF ATOMS AROUND THE CP`` table are kept.  The five foreign
    attractors in the deduplicated sample file are therefore excluded.
    """
    lines: list[BondLine] = []
    stats: dict = {
        "found": 0,
        "kept": 0,
        "farther": 0,
        "excluded": [],
        "no_cluster": 0,
    }
    for cp in cps:
        if cp.cp_type != (3, -1):
            continue
        cluster = cp.cluster_atoms or []
        cluster_nums = {atom.num for atom in cluster}
        near_nums = {atom.num for atom in (cp.attractors or [])}
        for attr in cp.bp_attractors or []:
            stats["found"] += 1
            atom = _cluster_atom_for_attractor(cp, attr)
            if not cluster:
                stats["no_cluster"] += 1
            if atom is None or atom.num not in cluster_nums:
                stats["excluded"].append({
                    "cp": cp.num,
                    "atom": atom.num if atom is not None else None,
                    "symbol": atom.symbol if atom is not None else "?",
                    "distance": _bond_length(
                        lattice,
                        (cp.x, cp.y, cp.z_),
                        (attr.x, attr.y, attr.z_),
                    ),
                    "attr": (attr.x, attr.y, attr.z_),
                })
                continue
            if atom.num not in near_nums:
                stats["farther"] += 1
            lines.append(_make_bond_line(
                cp,
                (cp.x, cp.y, cp.z_),
                (attr.x, attr.y, attr.z_),
                atom.num,
                atom.symbol,
                "crystal",
                lattice,
                atoms,
                spacing=spacing,
                note="found (3,-3) attractor",
            ))
    stats["kept"] = len(lines)
    return lines, stats


def generate_calc_bond_lines(
    cps: list[CP],
    crystal_lines: list[BondLine],
    atoms: list[Atom],
    lattice: Lattice,
    angle_min: float = 140.0,
    spacing: float = BP_SPACING,
) -> tuple[list[BondLine], dict]:
    """Calculated BPs for the two nearest nuclei of every BCP.

    A line is added only if the nucleus was not already used by a crystal BP,
    the BCP->nucleus vector makes an angle >= ``angle_min`` with the other
    nearest nucleus, and the nucleus is not in the crystal set.
    """
    found_by_cp: dict[int, set[int]] = {}
    for line in crystal_lines:
        if line.end_num is not None:
            found_by_cp.setdefault(line.cp_num, set()).add(line.end_num)

    lines: list[BondLine] = []
    stats: dict = {
        "bcp": 0,
        "candidates": 0,
        "added": 0,
        "rejected": [],
        "skipped_no_pair": [],
        "already_found": 0,
    }
    for cp in cps:
        if cp.cp_type != (3, -1):
            continue
        stats["bcp"] += 1
        near = cp.attractors or []
        if len(near) < 2:
            stats["skipped_no_pair"].append(cp.num)
            continue
        found = found_by_cp.get(cp.num, set())
        missing = [atom for atom in near if atom.num not in found]
        if not missing:
            continue
        stats["candidates"] += len(missing)
        bcp = (cp.x, cp.y, cp.z_)
        angle = _angle_deg(
            (near[0].x, near[0].y, near[0].z_),
            (near[1].x, near[1].y, near[1].z_),
            bcp,
        )
        if angle < angle_min:
            stats["rejected"].append(
                (cp.num, angle, [atom.num for atom in missing])
            )
            continue
        for atom in missing:
            if atom.num in found:
                stats["already_found"] += 1
                continue
            lines.append(_make_bond_line(
                cp,
                bcp,
                (atom.x, atom.y, atom.z_),
                atom.num,
                atom.symbol,
                "calc",
                lattice,
                atoms,
                spacing=spacing,
                angle=angle,
                note="calculated from cluster",
            ))
            found.add(atom.num)
            stats["added"] += 1
    return lines, stats


def write_bond_lines(
    path: Path,
    lines: list[BondLine],
    lattice: Lattice | None,
    resname: str,
    segid: str,
    title: str,
    dummy_element: str = "X",
) -> None:
    """Write BCP->attractor lines as clouds of dots (PDB HETATM)."""
    out: list[str] = []
    if lattice is not None:
        a, b, c, alpha, beta, gamma = lattice.params()
        out.append(
            f"CRYST1{a:9.3f}{b:9.3f}{c:9.3f}{alpha:7.2f}{beta:7.2f}{gamma:7.2f}"
            f" P 1           1"
        )
    n_dots = sum(len(line.dots) for line in lines)
    out.append(
        f"REMARK   {title}: {len(lines)} lines, {n_dots} dots, "
        f"spacing ~{BP_SPACING:.2f} A"
    )
    serial = 0
    for index, line in enumerate(lines, start=1):
        note = (
            f"CP N. {line.cp_num} ({line.cp_type[0]},{line.cp_type[1]}) -> "
        )
        if line.end_num is not None:
            note += f"atom {line.end_num}({line.end_symbol})"
        else:
            note += "attractor"
        note += f"  d={line.length:.3f} A"
        if line.angle is not None:
            note += f"  angle={line.angle:.1f} deg"
        if line.sym_op is not None:
            note += f"  symop={line.sym_op}"
        if line.note:
            note += f"  {line.note}"
        out.append(f"REMARK   BOND {index:3d}: {note}")
        for x, y, z in line.dots:
            serial += 1
            out.append(
                format_pdb_atom(
                    serial, " BP", resname, index, x, y, z,
                    dummy_element, record="HETATM", segid=segid,
                )
            )
    out.append("END")
    path.write_text("\n".join(out) + "\n", encoding="utf-8")


# ------------------------------------------------------------- symmetry ---


def _spglib():
    try:
        import spglib  # type: ignore
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "spglib is required for symmetry expansion "
            "(--space-group / --list-space-groups); install it with "
            "'pip install spglib'"
        ) from exc
    return spglib


def _norm_symbol(text: str) -> str:
    """Normalised full Hermann-Mauguin symbol: no spaces, as in spglib."""
    return text.replace(" ", "")


# ITA-standard setting preference (lower rank = more standard).
_SETTING_RANK = (
    "", "b1", "b2", "b3", "b", "c1", "c2", "c3", "c", "a1", "a2", "a3", "a",
    "-b1", "-b2", "-b3", "-c1", "-c2", "-c3", "-a1", "-a2", "-a3",
)


def space_group_table(all_settings: bool = False):
    """Space-group settings from spglib.

    Returns a list of ``(number, full_symbol, hall_number, choice, n_ops)``
    where ``full_symbol`` is the full Hermann-Mauguin symbol without spaces
    (unique in the spglib database -> exactly one setting).  By default only
    the canonical (ITA-standard) setting of each of the 230 groups is
    returned; with ``all_settings=True`` all 530 settings are listed.
    """
    spglib = _spglib()
    records = []
    for hall in range(1, 531):
        st = spglib.get_spacegroup_type(hall)
        n_ops = len(spglib.get_symmetry_from_database(hall)["rotations"])
        records.append(
            (st.number, _norm_symbol(st.international_full), hall,
             st.choice, n_ops)
        )
    if all_settings:
        return records
    best: dict[int, tuple[int, tuple]] = {}
    for rec in records:
        rank = (
            _SETTING_RANK.index(rec[3])
            if rec[3] in _SETTING_RANK else len(_SETTING_RANK)
        )
        prev = best.get(rec[0])
        if prev is None or rank < prev[0]:
            best[rec[0]] = (rank, rec)
    return [best[n][1] for n in sorted(best)]


def symmetry_operations(symbol: str):
    """``(ops, hall_number, full_symbol)`` for a full HM symbol.

    ``ops`` is a list of ``(rotation, translation)`` pairs, rotations are
    integer 3x3 lists, translations are fractional 3-tuples.  The symbol must
    be the full Hermann-Mauguin symbol without spaces (e.g. ``P12_1/n1``);
    short symbols such as ``P21/n`` are ambiguous and rejected.
    """
    want = _norm_symbol(symbol)
    matches = [rec for rec in space_group_table(all_settings=True)
               if rec[1] == want]
    if not matches:
        raise ValueError(
            f"unknown or non-standard space-group symbol '{symbol}' "
            f"(use the full Hermann-Mauguin symbol without spaces, "
            f"see --list-space-groups)"
        )
    # A few full symbols still admit two origin choices (identical symbol,
    # different hall_number).  Prefer the ITA-standard origin.
    matches.sort(key=lambda rec: (
        _SETTING_RANK.index(rec[3])
        if rec[3] in _SETTING_RANK else len(_SETTING_RANK),
        rec[2],
    ))
    hall = matches[0][2]
    spglib = _spglib()
    sym = spglib.get_symmetry_from_database(hall)
    ops = [
        ([list(int(v) for v in row) for row in R],
         tuple(float(t) for t in tr))
        for R, tr in zip(sym["rotations"], sym["translations"])
    ]
    return ops, hall, matches[0][1]


def _apply_op(op, frac: tuple[float, float, float]) -> tuple[float, float, float]:
    R, t = op
    g = tuple(
        sum(R[i][j] * frac[j] for j in range(3)) + t[i] for i in range(3)
    )
    return tuple(v % 1.0 for v in g)


def _apply_op_raw(op, frac: tuple[float, float, float]) -> tuple[float, float, float]:
    """Apply ``g = R*f + t`` *without* wrapping.

    Needed for expanding a bond path (BCP + two attractors) as one unit:
    wrapping each point independently can move two nearby points to opposite
    cell faces and tear the bond apart.  The triple is reduced to a common
    image afterwards.
    """
    R, t = op
    return tuple(
        sum(R[i][j] * frac[j] for j in range(3)) + t[i] for i in range(3)
    )


_IMAGE_SEARCH = 2


def _atom_image_shift(
    p: tuple[float, float, float],
    lattice: Lattice,
    atoms: list[Atom],
    search: int = _IMAGE_SEARCH,
) -> tuple[int, int, int]:
    """Integer lattice vector ``(n1,n2,n3)`` moving ``p`` closest to a drawn
    atom (the atom cell image).  ``(0,0,0)`` if no atoms are given."""
    if not atoms:
        return (0, 0, 0)
    atom_xyz = [(a.x, a.y, a.z_) for a in atoms]
    best_d2: float | None = None
    best_n = (0, 0, 0)
    for n1 in range(-search, search + 1):
        for n2 in range(-search, search + 1):
            for n3 in range(-search, search + 1):
                q = tuple(
                    p[i]
                    + n1 * lattice.a_vec[i]
                    + n2 * lattice.b_vec[i]
                    + n3 * lattice.c_vec[i]
                    for i in range(3)
                )
                d2 = min(
                    (q[0] - ax) ** 2 + (q[1] - ay) ** 2 + (q[2] - az) ** 2
                    for ax, ay, az in atom_xyz
                )
                if best_d2 is None or d2 < best_d2:
                    best_d2, best_n = d2, (n1, n2, n3)
    return best_n


def _frac_box(
    atoms: list[Atom], lattice: Lattice
) -> tuple[tuple[float, float, float], tuple[float, float, float]]:
    """The drawn cell as a fractional parallelepiped ``(lo, hi)``.

    The atoms are printed centred on the origin, so their fractional
    coordinates (reduced into ``[-0.5, 0.5)``) span at most one unit cell.
    The box is that cell, widened only if an atom would fall outside.
    """
    fr = [lattice.cart_to_frac(a.x, a.y, a.z_) for a in atoms]
    red = [tuple((f[i] + 0.5) % 1.0 - 0.5 for i in range(3)) for f in fr]
    out_lo = []
    out_hi = []
    for i in range(3):
        lo = min(-0.5, min(f[i] for f in red))
        hi = max(0.5, max(f[i] for f in red))
        out_lo.append(lo)
        out_hi.append(hi)
    return tuple(out_lo), tuple(out_hi)


def _residual_to_drawn(endpoints, atoms: list[Atom]) -> float:
    """Max distance of the given endpoints to the nearest drawn atom."""
    atom_xyz = [(a.x, a.y, a.z_) for a in atoms]
    return max(
        min(math.dist((e.x, e.y, e.z_), p) for p in atom_xyz)
        for e in endpoints
    )


def _dots_cut(
    p0: tuple[float, float, float],
    p1: tuple[float, float, float],
    lattice: Lattice,
    box,
    spacing: float,
) -> list[tuple[float, float, float]]:
    """Dots from ``p0`` to ``p1`` kept inside the fractional ``box``.

    The segment is first reduced to its minimum-image displacement (so it is
    short and physical), then split wherever it leaves the box; the removed
    part is translated by a whole lattice vector to enter through the
    opposite face.  Returns a flat dot list (endpoints included).
    """
    lo, hi = box
    f0 = lattice.cart_to_frac(*p0)
    # minimum-image displacement of p1 relative to p0 (short, physical)
    df = _min_image_frac(lattice.cart_to_frac(*p1), f0)
    # true (short) length in cartesian, used to place the dots
    p1_im = lattice.frac_to_cart(*tuple(f0[i] + df[i] for i in range(3)))
    length = math.dist(p0, p1_im)
    n = max(1, round(length / spacing))
    dots: list[tuple[float, float, float]] = []
    for k in range(n + 1):
        f = tuple(f0[i] + df[i] * k / n for i in range(3))
        # wrap this single dot into the box (nearest image)
        ff = tuple(f[i] - math.floor(f[i] - lo[i]) for i in range(3))
        ff = tuple(min(ff[i], hi[i]) if abs(ff[i] - hi[i]) < 1e-9 else ff[i]
                   for i in range(3))
        dots.append(lattice.frac_to_cart(*ff))
    # deduplicate consecutive identical dots (boundary jumps)
    clean: list[tuple[float, float, float]] = []
    for d in dots:
        if not clean or math.dist(d, clean[-1]) > 1e-9:
            clean.append(d)
    return clean


def _min_image_frac(
    a: tuple[float, float, float], b: tuple[float, float, float]
) -> tuple[float, float, float]:
    return tuple(
        (a[i] - b[i]) - round(a[i] - b[i]) for i in range(3)
    )


class _FracDedup:
    """Deduplicate fractional points by min-image distance within a tolerance.

    Coordinates are wrapped to ``[0,1)`` and bucketed on a fine grid; two
    points are considered equal when every fractional component differs by
    less than ``tol`` (minimum-image).  Handles cell-boundary values (0 and
    1) and special positions, keeping each point once.
    """

    def __init__(self, tol: float):
        self.tol = tol
        self.cell = max(tol, 1e-6)
        self.buckets: dict[tuple, list[tuple]] = {}

    def _wrap(self, f: tuple[float, float, float]) -> tuple[float, float, float]:
        w = []
        for v in f:
            x = v % 1.0
            if x > 1.0 - self.tol:
                x = 0.0
            w.append(x)
        return tuple(w)

    def _bucket(self, f: tuple[float, float, float]) -> tuple:
        return tuple(int(round(v / self.cell)) for v in f)

    def add(self, f: tuple[float, float, float]) -> bool:
        """Return True and store if not a duplicate, else False."""
        return self.add_group([f])

    def add_group(self, points: list[tuple[float, float, float]]) -> bool:
        """Store a set of points as one unit (all must be new).

        Used for bond paths: the triple (BCP, attractor1, attractor2) is
        deduplicated as a whole so the segment structure is preserved.
        """
        wrapped = [self._wrap(p) for p in points]
        b = self._bucket(wrapped[0])
        for d1 in (-1, 0, 1):
            for d2 in (-1, 0, 1):
                for d3 in (-1, 0, 1):
                    key = (b[0] + d1, b[1] + d2, b[2] + d3)
                    for stored in self.buckets.get(key, ()):
                        if all(
                            all(abs(v) < self.tol
                                for v in _min_image_frac(w, s))
                            for w, s in zip(wrapped, stored)
                        ):
                            return False
        self.buckets.setdefault(b, []).append(wrapped)
        return True


def expand_cps_by_symmetry(
    cps: list[CP], lattice: Lattice, ops, tol: float = 1e-3
) -> list[CP]:
    """Multiply CPs over the whole cell by the space-group operations.

    Each CP is converted to fractional coordinates, every operation
    ``g = R*f + t (mod 1)`` is applied, the results are wrapped into ``[0,1)``
    and converted back to angstrom.  Points that land on top of each other
    (including special positions and the cell boundary) are kept once.
    """
    out: list[CP] = []
    seen_by_type: dict[tuple[int, int], _FracDedup] = {}
    for cp in cps:
        f = lattice.cart_to_frac(cp.x, cp.y, cp.z_)
        seen = seen_by_type.setdefault(cp.cp_type, _FracDedup(tol))
        for op_index, op in enumerate(ops):
            g = _apply_op(op, f)
            if not seen.add(g):
                continue
            out.append(CP(cp.num, cp.cp_type, *lattice.frac_to_cart(*g),
                          cp.rho, cp.elf, sym_op=op_index))
    return out


def expand_bond_paths_by_symmetry(
    cps: list[CP],
    lattice: Lattice,
    ops,
    atoms: list[Atom],
    image: str = "shift",
    tol: float = 1e-3,
) -> list[BondPath]:
    """Multiply bond paths by symmetry, keeping each bond physical.

    Unlike :func:`expand_cps_by_symmetry`, every bond is expanded as a
    **consistent triple** ``(BCP, attractor 1, attractor 2)``: the whole
    triple is transformed by the *same* operation and then translated by the
    *same* whole lattice vector into the image of the drawn atoms, so a
    symmetry copy still connects the BCP with the symmetry images of exactly
    the two nuclei listed in the ``CLUSTER OF ATOMS AROUND THE CP`` table
    (``Atom.num`` = ``OLD`` number).  Copies that coincide are written once.

    ``image`` selects how a segment that leaves the atom cell is drawn:

    * ``"shift"`` (default): keep one segment; the endpoint may lie on a
      periodic image of the nucleus (it is drawn one cell away);
    * ``"cut"``: split the segment at the cell boundary and continue it on
      the opposite face, so every dot stays inside the cell image.
    """
    out: list[BondPath] = []
    seen = _FracDedup(tol)
    # fractional box of the drawn (centred) atoms; used for "cut"
    box = _frac_box(atoms, lattice)

    for cp in cps:
        if cp.cp_type != (3, -1):
            continue
        if not cp.attractors or len(cp.attractors) < 2:
            continue
        fb = lattice.cart_to_frac(cp.x, cp.y, cp.z_)
        f1 = lattice.cart_to_frac(cp.attractors[0].x, cp.attractors[0].y,
                                  cp.attractors[0].z_)
        f2 = lattice.cart_to_frac(cp.attractors[1].x, cp.attractors[1].y,
                                  cp.attractors[1].z_)
        for op_index, op in enumerate(ops):
            gb = _apply_op_raw(op, fb)
            g1 = _apply_op_raw(op, f1)
            g2 = _apply_op_raw(op, f2)
            # keep the triple compact: reduce attractors to the BCP image
            g1 = tuple(gb[i] + _min_image_frac(g1, gb)[i] for i in range(3))
            g2 = tuple(gb[i] + _min_image_frac(g2, gb)[i] for i in range(3))
            # canonical order of the two attractors so that a bond and its
            # inversion twin (ends exchanged) deduplicate to one entry
            if g2 < g1:
                g1, g2 = g2, g1
                f_swap = True
            else:
                f_swap = False
            # deduplicate the whole raw triple (translation-invariant); the
            # cell wrapping happens afterwards and must not affect identity
            if not seen.add_group([gb, g1, g2]):
                continue
            # wrap the triple into [0,1) for output, keeping it together
            gbw = tuple(v % 1.0 for v in gb)
            off = tuple(round(gbw[i] - gb[i]) for i in range(3))
            g1w = tuple(g1[i] + off[i] for i in range(3))
            g2w = tuple(g2[i] + off[i] for i in range(3))
            # fractional -> cartesian, whole triple together
            bcp = lattice.frac_to_cart(*gbw)
            a1 = lattice.frac_to_cart(*g1w)
            a2 = lattice.frac_to_cart(*g2w)
            attr = list(cp.attractors[:2])
            if f_swap:
                attr = [attr[1], attr[0]]
            # centre the whole triple on the atom image (shift by BCP image)
            shift = _atom_image_shift(bcp, lattice, atoms)
            dx = (shift[0] * lattice.a_vec[0] + shift[1] * lattice.b_vec[0]
                  + shift[2] * lattice.c_vec[0])
            dy = (shift[0] * lattice.a_vec[1] + shift[1] * lattice.b_vec[1]
                  + shift[2] * lattice.c_vec[1])
            dz = (shift[0] * lattice.a_vec[2] + shift[1] * lattice.b_vec[2]
                  + shift[2] * lattice.c_vec[2])
            bcp = (bcp[0] + dx, bcp[1] + dy, bcp[2] + dz)
            a1 = (a1[0] + dx, a1[1] + dy, a1[2] + dz)
            a2 = (a2[0] + dx, a2[1] + dy, a2[2] + dz)
            atom1 = Atom(attr[0].num, attr[0].z, attr[0].symbol, *a1)
            atom2 = Atom(attr[1].num, attr[1].z, attr[1].symbol, *a2)
            new_cp = CP(cp.num, (3, -1), *bcp, cp.rho, cp.elf, sym_op=op_index)
            if image == "cut":
                dots = _dots_cut(bcp, a1, lattice, box, BP_SPACING)
                dots += _dots_cut(bcp, a2, lattice, box, BP_SPACING)[1:]
            else:
                dots = _segment_dots(bcp, a1, BP_SPACING)
                dots += _segment_dots(bcp, a2, BP_SPACING)[1:]
            d1 = math.dist(bcp, a1)
            d2 = math.dist(bcp, a2)
            residual = _residual_to_drawn((atom1, atom2), atoms)
            out.append(BondPath(new_cp, atom1, atom2, d1, d2, dots,
                                residual=residual, from_file=True))
    return out


def expand_bond_lines_by_symmetry(
    lines: list[BondLine],
    lattice: Lattice,
    ops,
    atoms: list[Atom],
    image: str = "shift",
    tol: float = 1e-3,
) -> list[BondLine]:
    """Multiply BCP->attractor lines by symmetry.

    Every line is transformed as a consistent pair ``(BCP, attractor)`` and
    then translated by one whole lattice vector into the drawn atom image.
    The BCP-to-attractor distance and the optional BCP angle are preserved.
    """
    out: list[BondLine] = []
    seen = _FracDedup(tol)
    box = _frac_box(atoms, lattice)

    for line in lines:
        fb = lattice.cart_to_frac(*line.bcp)
        fe = lattice.cart_to_frac(*line.end)
        for op_index, op in enumerate(ops):
            gb = _apply_op_raw(op, fb)
            ge = _apply_op_raw(op, fe)
            ge = tuple(gb[i] + _min_image_frac(ge, gb)[i] for i in range(3))
            if not seen.add_group([gb, ge]):
                continue
            gbw = tuple(v % 1.0 for v in gb)
            off = tuple(round(gbw[i] - gb[i]) for i in range(3))
            gew = tuple(ge[i] + off[i] for i in range(3))
            bcp = lattice.frac_to_cart(*gbw)
            end = lattice.frac_to_cart(*gew)
            shift = _atom_image_shift(bcp, lattice, atoms)
            dx = (shift[0] * lattice.a_vec[0] + shift[1] * lattice.b_vec[0]
                  + shift[2] * lattice.c_vec[0])
            dy = (shift[0] * lattice.a_vec[1] + shift[1] * lattice.b_vec[1]
                  + shift[2] * lattice.c_vec[1])
            dz = (shift[0] * lattice.a_vec[2] + shift[1] * lattice.b_vec[2]
                  + shift[2] * lattice.c_vec[2])
            bcp = (bcp[0] + dx, bcp[1] + dy, bcp[2] + dz)
            end = (end[0] + dx, end[1] + dy, end[2] + dz)
            if image == "cut":
                dots = _dots_cut(bcp, end, lattice, box, BP_SPACING)
                length = _bond_length(lattice, bcp, end)
            else:
                dots = _segment_dots(bcp, end, BP_SPACING)
                length = math.dist(bcp, end)
            out.append(BondLine(
                cp_num=line.cp_num,
                cp_type=line.cp_type,
                bcp=bcp,
                end=end,
                dots=dots,
                source=line.source,
                end_num=line.end_num,
                end_symbol=line.end_symbol,
                length=length,
                angle=line.angle,
                sym_op=op_index,
                note=line.note,
            ))
    return out


# ------------------------------------------------------------------ main ---


def build_summary(
    input_path: Path,
    lattice: Lattice | None,
    atoms: list[Atom],
    cps: list[CP],
    files: list[Path],
    warnings: list[str],
    deduplicated: bool = True,
    align_note: str | None = None,
    bond_note: str | None = None,
    sym_note: str | None = None,
) -> str:
    lines = ["CP-TOPOND export summary", f"Input: {input_path}", ""]
    if lattice is not None:
        a, b, c, alpha, beta, gamma = lattice.params()
        lines += [
            "Cell (angstrom / degrees):",
            f"  a={a:.5f}  b={b:.5f}  c={c:.5f}  "
            f"alpha={alpha:.4f}  beta={beta:.4f}  gamma={gamma:.4f}",
        ]
    else:
        lines.append("Cell: NOT FOUND")
    label = " (unique, duplicates removed)" if deduplicated else " (duplicates kept)"
    lines += ["", f"Atoms in cell: {len(atoms)}", f"CP total: {len(cps)}{label}"]
    for cp_type in ((3, -1), (3, 1), (3, 3), (3, -3)):
        kind, res = CP_CLASS_NAMES[cp_type]
        n = sum(1 for cp in cps if cp.cp_type == cp_type)
        lines.append(f"  {kind} {str(cp_type):>7} residue {res}: {n}")
    other = len(cps) - sum(
        1 for cp in cps if cp.cp_type in CP_CLASS_NAMES
    )
    if other:
        lines.append(f"  other types: {other}")
    if align_note:
        lines += ["", align_note]
    if bond_note:
        lines += [bond_note]
        if not align_note:
            lines += [
                "  (BCPs were aligned to the atom cell image internally "
                "for the bond paths; other files keep the raw coordinates)"
            ]
    if sym_note:
        lines += [sym_note]
    lines += ["", "Files created:"]
    lines += [f"  {p}" for p in files]
    if warnings:
        lines += ["", *warnings]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        description="Export CRYSTAL17/TOPOND critical points to PDB/XYZ "
        "for VESTA / VMD / JSmol.",
        epilog=(
            "example: python topond_cp_export.py "
            "CP_BPs_dedup_final.out --outdir results"
        ),
    )
    ap.add_argument("input", type=Path, nargs="?",
                    help="CRYSTAL17 .out file with TOPOND")
    ap.add_argument("--outdir", type=Path, default=Path("results"),
                    help="output directory (default: results)")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--wrap", action="store_true",
                      help="wrap CP coordinates into the unit cell [0,1)")
    mode.add_argument("--align", action="store_true",
                      help="translate each CP by a lattice vector so it falls "
                           "into the same cell image as the atoms "
                           "(fixes the apparent shift in VMD/JSmol)")
    ap.add_argument("--dummy-element", default="X",
                    help="element symbol for CP records (default: X)")
    ap.add_argument("--keep-duplicates", action="store_true",
                    help="do not deduplicate atoms and CP")
    ap.add_argument("--space-group", metavar="SYMBOL", default=None,
                    help="full Hermann-Mauguin symbol without spaces "
                         "(e.g. P12_1/n1); multiplies CP and bond paths over "
                         "the whole cell. Short symbols (P21/n) are ambiguous "
                         "and rejected; see --list-space-groups")
    ap.add_argument("--symmetry-tol", type=float, default=1e-3,
                    help="deduplication tolerance in fractional coordinates "
                         "for symmetry expansion (default: 1e-3)")
    ap.add_argument("--bp-angle", type=float, default=140.0, metavar="DEG",
                    help="minimum BCP->nucleus/nucleus angle for calculated "
                         "BPs (default: 140 degrees)")
    ap.add_argument("--symmetry-image", choices=("shift", "cut"), default=None,
                    help="how symmetry-expanded bond paths cross the cell "
                         "boundary: 'shift' keeps one segment (endpoint may "
                         "lie on a periodic image of its nucleus), 'cut' "
                         "splits the segment at the boundary so every dot "
                         "stays inside the cell. "
                         "Default when --space-group is given: shift")
    ap.add_argument("--list-space-groups", action="store_true",
                    help="print the 230 unambiguous space-group commands and "
                         "exit")
    ap.add_argument("--all-settings", action="store_true",
                    help="with --list-space-groups: list all 530 settings")
    logger = setup("export")
    try:
        args = ap.parse_args(argv)
    except SystemExit as exc:
        if exc.code not in (0, None):
            log_error(logger, "EXP-E008", "invalid command-line arguments",
                      argv=" ".join(sys.argv[1:] if argv is None else argv),
                      exit_code=exc.code)
        raise
    log_action(logger, "start", input=str(args.input) if args.input else None,
               outdir=str(args.outdir), wrap=args.wrap, align=args.align,
               space_group=args.space_group, keep_duplicates=args.keep_duplicates)

    if args.list_space_groups:
        try:
            rows = space_group_table(all_settings=args.all_settings)
        except RuntimeError as exc:
            log_error(logger, "EXP-E001", "failed to build space-group table",
                      exc=exc)
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        width = max(len(r[1]) for r in rows)
        print("# number  full HM symbol (no spaces)       hall  ops")
        for number, symbol, hall, choice, n_ops in rows:
            note = f"  [{choice}]" if choice else ""
            print(f"  {number:3d}    {symbol:<{width}}  {hall:4d}  "
                  f"{n_ops:3d}{note}")
        log_action(logger, "space_groups", rows=len(rows),
                   all_settings=args.all_settings)
        return 0

    if not 0.0 <= args.bp_angle <= 180.0:
        log_error(logger, "EXP-E002", "--bp-angle must be between 0 and 180",
                  bp_angle=args.bp_angle)
        print("ERROR: --bp-angle must be between 0 and 180 degrees",
              file=sys.stderr)
        return 2
    if args.symmetry_image is None and args.space_group:
        args.symmetry_image = "shift"

    if args.input is None:
        log_error(logger, "EXP-E003", "input file is required")
        print("ERROR: input file is required", file=sys.stderr)
        return 2
    if not args.input.is_file():
        log_error(logger, "EXP-E003", "input file not found", input=str(args.input))
        print(f"ERROR: input file not found: {args.input}", file=sys.stderr)
        return 2
    try:
        text = args.input.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log_error(logger, "EXP-E004", "cannot read input", exc=exc,
                  input=str(args.input))
        print(f"ERROR: cannot read {args.input}: {exc}", file=sys.stderr)
        return 2

    errors: list[str] = []
    warnings: list[str] = []

    lattice = parse_lattice(text)
    if lattice is None:
        errors.append("ERROR: cell not found (no DIRECT LATTICE VECTOR "
                      "COMPONENTS / LATTICE PARAMETERS block)")

    atoms = parse_atoms(text, keep_duplicates=args.keep_duplicates)
    if not atoms:
        errors.append("ERROR: no cell atoms found "
                      "(no 'ATOM N.AT. SHELL ...' table)")

    cps = parse_cps(text, keep_duplicates=args.keep_duplicates)
    if not cps:
        errors.append("ERROR: no critical points found "
                      "(no 'CP N. <number>' blocks)")

    # keep the untouched (raw) points for symmetry expansion: operations are
    # applied to the raw fractional coordinates, never to a centred image.
    raw_cps = list(cps)

    dummy = args.dummy_element.strip()[:2].upper() or "X"

    if args.wrap and lattice is not None and cps:
        cps = [wrap_cp(cp, lattice) for cp in cps]

    align_note: str | None = None
    if args.align:
        if lattice is None:
            errors.append("ERROR: --align requires the lattice "
                          "(cell not found) - keeping raw coordinates")
        elif not atoms:
            cps = [center_wrap_cp(cp, lattice) for cp in cps]
            align_note = ("Align: no atoms parsed - CP wrapped into the "
                          "fractional box [-0.5, 0.5)")
        else:
            cps, moved = align_cps_to_atoms(cps, lattice, atoms)
            n_moved = sum(moved.values())
            if n_moved:
                vecs = ", ".join(
                    f"({shift[0]},{shift[1]},{shift[2]})x{count}"
                    for shift, count in sorted(
                        moved.items(), key=lambda kv: -kv[1]
                    )
                )
                align_note = (f"Aligned to the atom cell: {n_moved} of "
                              f"{len(cps)} CP translated "
                              f"(lattice vectors: {vecs})")
            else:
                align_note = ("Aligned to the atom cell: "
                              "no translation needed")

    outdir: Path = args.outdir
    outdir.mkdir(parents=True, exist_ok=True)
    files: list[Path] = []

    if atoms:
        p = outdir / "cell.pdb"
        write_pdb(p, atoms, lattice=lattice)
        files.append(p)

    remarks: list[str] = []
    for cp in cps:
        bits = [f"CP N. {cp.num}", f"type {cp.cp_type} ({cp.kind})"]
        if cp.rho is not None:
            bits.append(f"RHO={cp.rho:.6g}")
        if cp.elf is not None:
            bits.append(f"ELF={cp.elf:.6g}")
        remarks.append(" ".join(bits))

    if cps:
        p = outdir / "critical_points.pdb"
        write_pdb(p, cps, lattice=lattice, dummy_element=dummy, as_cp=True,
                  remarks=remarks)
        files.append(p)

        p = outdir / "critical_points.xyz"
        write_xyz(p, cps, dummy)
        files.append(p)

        for cp_type, tag in CP_TAG.items():
            group = [cp for cp in cps if cp.cp_type == cp_type]
            if not group:
                continue
            p = outdir / f"critical_points_{tag}.xyz"
            write_xyz(p, group, dummy)
            files.append(p)

    bond_note: str | None = None
    crystal_lines: list[BondLine] = []
    calc_lines: list[BondLine] = []
    crystal_stats: dict = {}
    calc_stats: dict = {}
    if raw_cps and atoms and lattice is not None:
        crystal_lines, crystal_stats = generate_crystal_bond_lines(
            raw_cps, atoms, lattice)
        calc_lines, calc_stats = generate_calc_bond_lines(
            raw_cps, crystal_lines, atoms, lattice,
            angle_min=args.bp_angle)

        p = outdir / "crystal_BPs.pdb"
        write_bond_lines(
            p, crystal_lines, lattice, "CBP", "CRYS",
            "CRYSTAL BOND PATHS (found (3,-3) attractors)",
            dummy_element=dummy,
        )
        files.append(p)

        p = outdir / "calc_BPs.pdb"
        write_bond_lines(
            p, calc_lines, lattice, "KBP", "CALC",
            "CALCULATED BOND PATHS",
            dummy_element=dummy,
        )
        files.append(p)

        bond_note = (
            f"Crystal BPs: {len(crystal_lines)} lines "
            f"(found {crystal_stats.get('found', 0)}, "
            f"kept {crystal_stats.get('kept', 0)}) -> crystal_BPs.pdb\n"
            f"Calc BPs: {len(calc_lines)} lines "
            f"(candidates {calc_stats.get('candidates', 0)}, "
            f"added {calc_stats.get('added', 0)}, "
            f"angle >= {args.bp_angle:g} deg) -> calc_BPs.pdb"
        )
        excluded = crystal_stats.get("excluded", [])
        if excluded:
            bond_note += (
                f"\n  Excluded from crystal_BPs: {len(excluded)} found "
                f"attractor(s) whose terminus atom is absent from the full "
                f"CLUSTER table:"
            )
            for item in excluded:
                atom_txt = (
                    f"{item['atom']}({item['symbol']})"
                    if item["atom"] is not None else "?"
                )
                ax, ay, az = item["attr"]
                bond_note += (
                    f"\n    CP {item['cp']} -> atom {atom_txt}, "
                    f"d={item['distance']:.2f} A, "
                    f"attr=({ax:.3f},{ay:.3f},{az:.3f})"
                )
        if crystal_stats.get("farther"):
            bond_note += (
                f"\n  Kept crystal lines ending at CLUSTER rows 3-10 "
                f"(farther than the two nearest nuclei): "
                f"{crystal_stats['farther']}"
            )
        if crystal_stats.get("no_cluster"):
            bond_note += (
                f"\n  Found attractors without a full CLUSTER table: "
                f"{crystal_stats['no_cluster']}"
            )
        rejected = calc_stats.get("rejected", [])
        if rejected:
            bond_note += (
                f"\n  Calc BPs rejected by angle < {args.bp_angle:g} deg: "
                f"{len(rejected)} BCP(s):"
            )
            for cp_num, angle, atoms_missing in rejected:
                bond_note += (
                    f"\n    CP {cp_num}: angle={angle:.1f} deg, "
                    f"missing={atoms_missing}"
                )
        if calc_stats.get("skipped_no_pair"):
            bond_note += (
                f"\n  BCP without two nearest nuclei: "
                f"{calc_stats['skipped_no_pair']}"
            )
        if calc_stats.get("already_found"):
            bond_note += (
                f"\n  Calc candidates already present in crystal_BPs: "
                f"{calc_stats['already_found']}"
            )
    elif raw_cps:
        errors.append("ERROR: bond paths not built "
                      "(cell atoms / lattice not found)")

    # --- optional symmetry expansion (separate files) --------------------
    sym_note: str | None = None
    if args.space_group:
        if lattice is None:
            errors.append("ERROR: --space-group requires the lattice "
                          "(cell not found)")
        elif not cps:
            errors.append("ERROR: --space-group requires critical points")
        else:
            try:
                ops, hall, full = symmetry_operations(args.space_group)
            except (ValueError, RuntimeError) as exc:
                print(f"ERROR: {exc}", file=sys.stderr)
                return 2
            n_in = len(raw_cps)
            sym_cps = expand_cps_by_symmetry(raw_cps, lattice, ops,
                                             args.symmetry_tol)
            if args.align and atoms:
                sym_cps, _ = align_cps_to_atoms(sym_cps, lattice, atoms)
            sym_note = (
                f"Symmetry: {full} (hall {hall}, {len(ops)} ops): "
                f"CP {n_in} -> {len(sym_cps)} unique "
                f"(+{len(sym_cps) - n_in})"
            )
            if args.align and atoms:
                sym_note += "; centred to the atom cell image (--align)"
            sym_remarks = []
            for cp in sym_cps:
                bits = [f"CP N. {cp.num}", f"type {cp.cp_type} ({cp.kind})"]
                if cp.sym_op is not None:
                    bits.append(f"sym_op {cp.sym_op}")
                if cp.rho is not None:
                    bits.append(f"RHO={cp.rho:.6g}")
                if cp.elf is not None:
                    bits.append(f"ELF={cp.elf:.6g}")
                sym_remarks.append(" ".join(bits))

            p = outdir / "critical_points_symm.pdb"
            write_pdb(p, sym_cps, lattice=lattice, dummy_element=dummy,
                      as_cp=True, remarks=sym_remarks)
            files.append(p)

            p = outdir / "critical_points_symm.xyz"
            write_xyz(p, sym_cps, dummy)
            files.append(p)

            for cp_type, tag in CP_TAG.items():
                group = [cp for cp in sym_cps if cp.cp_type == cp_type]
                if not group:
                    continue
                p = outdir / f"critical_points_symm_{tag}.xyz"
                write_xyz(p, group, dummy)
                files.append(p)

            n_ncp_sym = sum(1 for cp in sym_cps if cp.cp_type == (3, -3))
            if atoms and n_ncp_sym != len(atoms):
                warnings.append(
                    f"WARNING: after symmetry expansion the number of NCP "
                    f"((3,-3)) is {n_ncp_sym}, but the cell contains "
                    f"{len(atoms)} atoms (expected one NCP per atom)"
                )
            sym_note += f"; NCP {n_ncp_sym} (atoms {len(atoms)})"

            sym_crystal: list[BondLine] = []
            sym_calc: list[BondLine] = []
            if atoms:
                sym_crystal = expand_bond_lines_by_symmetry(
                    crystal_lines, lattice, ops, atoms,
                    image=(args.symmetry_image or "shift"),
                    tol=args.symmetry_tol,
                )
                sym_calc = expand_bond_lines_by_symmetry(
                    calc_lines, lattice, ops, atoms,
                    image=(args.symmetry_image or "shift"),
                    tol=args.symmetry_tol,
                )
                # remove calc lines that coincide with a crystal line after
                # symmetry (BCP + attractor pair, translation-invariant)
                seen_lines = _FracDedup(args.symmetry_tol)
                for line in sym_crystal:
                    seen_lines.add_group([
                        lattice.cart_to_frac(*line.bcp),
                        lattice.cart_to_frac(*line.end),
                    ])
                filtered: list[BondLine] = []
                for line in sym_calc:
                    if seen_lines.add_group([
                        lattice.cart_to_frac(*line.bcp),
                        lattice.cart_to_frac(*line.end),
                    ]):
                        filtered.append(line)
                sym_calc = filtered

            p = outdir / "crystal_BPs_symm.pdb"
            write_bond_lines(
                p, sym_crystal, lattice, "CBP", "CRYS",
                "SYMMETRY-EXPANDED CRYSTAL BOND PATHS",
                dummy_element=dummy,
            )
            files.append(p)

            p = outdir / "calc_BPs_symm.pdb"
            write_bond_lines(
                p, sym_calc, lattice, "KBP", "CALC",
                "SYMMETRY-EXPANDED CALCULATED BOND PATHS",
                dummy_element=dummy,
            )
            files.append(p)

            sym_note += (
                f"; crystal BPs {len(crystal_lines)} -> "
                f"{len(sym_crystal)} unique"
                f"; calc BPs {len(calc_lines)} -> {len(sym_calc)} unique"
                f" (image={args.symmetry_image or 'shift'})"
            )

    for msg in errors:
        log_error(logger, "EXP-E005", "export error", detail=msg)
        print(msg, file=sys.stderr)
    for msg in warnings:
        logger.warning("code=EXP-W001 %s", msg)
        print(msg, file=sys.stderr)

    summary_path = outdir / "summary.txt"
    files.append(summary_path)
    summary = build_summary(args.input, lattice, atoms, cps, files, warnings,
                            deduplicated=not args.keep_duplicates,
                            align_note=align_note,
                            bond_note=bond_note,
                            sym_note=sym_note)
    try:
        summary_path.write_text(summary, encoding="utf-8")
    except OSError as exc:
        log_error(logger, "EXP-E006", "cannot write summary", exc=exc,
                  summary=str(summary_path))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    log_action(logger, "written", outdir=str(outdir), files=len(files),
               cps=len(cps), atoms=len(atoms), errors=len(errors),
               warnings=len(warnings), summary=str(summary_path))
    print(summary, end="")

    if not cps:
        log_error(logger, "EXP-E007", "no critical points found",
                  input=str(args.input))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
