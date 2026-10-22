#!/usr/bin/env python3
"""Clean the interleaved CRYSTAL17/TOPOND critical-point output.

The raw file is the interleaved stdout of several parallel processes: every
block of the report is printed once per process and the copies are mixed with
each other.  This tool rebuilds a single clean copy, keeping every piece of
information exactly once:

  * preamble - banner, lattice, atom table, computational conditions, cluster
    tables, pair tables, neighbour lists;
  * results  - one "CP N. <n>" block per critical point, assembled from all
    the interleaved copies of that block;
  * tail     - the final list of the critical points and the termination
    block.

Every column header, separator and counter is taken from the input file, so
the tool works with the output of the same program family for any system.

Usage:
    python3 dedup_cp_bps.py INPUT.out [-o OUTPUT.out] [--report] [--verify]
"""
import argparse
import collections
import math
import os
import re
import sys

BOHR2ANG = 0.529177210903
CLUSTER_TOL = 0.05          # Angstrom, consistency of two copies of a cluster table

RE_CP = re.compile(r'^\s*CP N\.\s+(\d+)\s*$')
RE_ATTR = re.compile(r'^\s*ATTRACTOR CP TYPE\s*:')
RE_UNABLE = re.compile(r'THE CODE WAS UNABLE TO FIND THE PATH ATTRACTOR\s+(\d+)')
RE_BPL = re.compile(r'^\s*BPL \(ANG\)')
RE_PHASE = re.compile(r'^\s*SEARCH OF\s+\(\s*([-+0-9.,]+)\s*\)\s*CPS\s*$')
RE_SUBSEC = re.compile(r'CP SEARCH AMONG ATOM PAIRS')
RE_COORD = re.compile(r'COORD\s*\(AU\)\s*\(X\s*Y\s*Z\)\s*:\s*(\S+)\s+(\S+)\s+(\S+)')
RE_TYPE = re.compile(r'CP TYPE\s*:\s*\(\s*([-+]?\d+)\s*,\s*([-+]?\d+)\s*\)')
RE_SUMTYPE = re.compile(r'^\s+(\d+)\)\s+.*\(\s*([-+]?\d+)\s*,\s*([-+]?\d+)\s*\)')
RE_ROW = re.compile(r'^\s*(\d+)\s+(\d+)\s+(-?\d+)\s+(-?\d+)\s+(-?\d+)\s+(\d+)\s+'
                    r'(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$')
RE_SUMMARY = re.compile(r'^\s+(\d+)\)\s')
RE_BANNER = re.compile(r'^\s\*')
RE_FRAME = re.compile(r'^\s*\*+\s*$')
RE_LATT1 = re.compile(r'DIRECT LATTICE VECTOR COMPONENTS')
RE_LATT2 = re.compile(r'LATTICE PARAMETERS')
RE_ATOMHDR = re.compile(r'^\s*ATOM\s+N\.AT\.\s+SHELL')
RE_NEQUIV = re.compile(r'^\s*NON-EQUIV\. ATOM\s+(\d+)\s+([A-Z]{1,2})\s*\(N\.\s*(\d+)')
RE_NEQUIV2 = re.compile(r'^\s*NON EQUIV\. ATOM\s+(\d+)\s+([A-Z]{1,2})\s*$')
RE_CLUSTERS = re.compile(r'^\s*CLUSTERS AROUND EACH OF THE NON-EQUIVALENT ATOMS')
RE_TOPO = re.compile(r'T\s+O\s+P\s+O\s+L\s+O\s+G\s+I\s+C\s+A\s+L')
RE_COND = re.compile(r'^\s*COMPUTATIONAL CONDITIONS')
RE_PAIRS = re.compile(r'TOTAL NUMBER OF PAIRS CONSIDERED')
RE_PROC = re.compile(r'^\s*PROCESS\s+\d+\s+OF\s+\d+\s+WORKING')
RE_START = re.compile(r'^\s*E+\s+STARTING\s+DATE')
RE_NOISE = re.compile(r'^\s+\d+\s+[A-Z]{1,2}\s+\d+\s+[-0-9.]+\s+[-0-9.]+\s+'
                      r'\d+\s+[A-Z]{1,2}\s+[-0-9 ]+$')
RE_PAIRROW = re.compile(r'^\s*(\d+)\s+([A-Z]{1,2})\s+(\d+)\s+(-?\d)\s+(-?\d)\s+'
                         r'(-?\d)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+(-?[\d.]+)\s+([\d.]+)\s*$')
RE_NATOMS = re.compile(r'^(?P<pre>.*N\. OF ATOMS PER CELL\s+)(?P<n>\d+)')
RE_NEQUIV_LINE = re.compile(
    r'^(?P<pre>\s*NON-EQUIV\.\s+ATOM\s+)(?P<idx>\d+)(?P<mid>\s+)(?P<el>[A-Za-z]{1,2}\d?)'
    r'(?P<gap>\s*\(N\.\s*)(?P<nat>\d+)(?P<mid2>.*?CLUSTER OF\s+)(?P<nclu>\d+)(?P<post>.*)$')
RE_NEQUIV2_LINE = re.compile(
    r'^(?P<pre>\s*NON\s+EQUIV\.\s+ATOM\s+)(?P<idx>\d+)(?P<mid>\s+)(?P<el>\S+)(?P<post>.*)$')
ATOM_LINE_RE = re.compile(
    r"^\s*(?P<num>\d+)\s+(?P<z>\d+)\s+(?P<sym>[A-Za-z]{1,2}\d*)\s+"
    r"(?P<shell>\d+)\s+(?P<x>[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+"
    r"(?P<y>[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+(?P<zc>[-+]?\d*\.?\d+(?:[Ee][-+]?\d+)?)\s+"
    r"\S+\s+\S+\s*$")


def load(path):
    text = open(path, encoding='utf-8', errors='replace').read()
    lines = text.split('\n')
    if lines and lines[-1] == '':
        lines.pop()
    return lines


def is_banner(line):
    return bool(RE_BANNER.match(line)) and not RE_FRAME.match(line)


def pad(new, old):
    """right-justify `new` in the width of `old` (the source is right aligned)"""
    return new.rjust(len(old))


def coords_of(block):
    for l in block:
        m = RE_COORD.search(l)
        if m:
            try:
                return tuple(round(float(v), 4) for v in m.groups())
            except ValueError:
                return None
    return None


def type_of(block):
    for l in block:
        m = RE_TYPE.search(l)
        if m:
            return (int(m.group(1)), int(m.group(2)))
    return None


def cluster_rows(block):
    """{row number: line} of the 'CLUSTER OF ATOMS AROUND THE CP' table"""
    rows = {}
    inside = False
    for l in block:
        if 'CLUSTER OF ATOMS AROUND THE CP' in l:
            inside = True
            continue
        if inside:
            m = RE_ROW.match(l)
            if m:
                rows[int(m.group(1))] = l
            elif l.strip() and not l.strip().startswith('NEW'):
                inside = False
    return rows


def split_block(block):
    """split a CP block into (head, items, bpl)

    head  : everything up to the first attractor item
    items : [(candidate index, 'attr'|'unable', payload)]
    bpl   : list of BPL lines
    """
    head = []
    items = []
    bpl = []
    i = 0
    n = len(block)
    while i < n:
        if i > 0 and RE_CP.match(block[i]):
            break
        if RE_ATTR.match(block[i]) or RE_UNABLE.search(block[i]):
            break
        if RE_BPL.match(block[i]):
            bpl.append(block[i])
            i += 1
            continue
        head.append(block[i])
        i += 1
    counter = 1
    while i < n:
        if RE_CP.match(block[i]):
            break
        if RE_ATTR.match(block[i]):
            j = i + 1
            while j < n and not RE_ATTR.match(block[j]) and \
                    not RE_BPL.match(block[j]) and not RE_UNABLE.search(block[j]) and \
                    not RE_CP.match(block[j]):
                j += 1
            items.append((counter, 'attr', block[i:j]))
            counter += 1
            i = j
            continue
        m = RE_UNABLE.search(block[i])
        if m:
            idx = int(m.group(1))
            items.append((idx, 'unable', block[i]))
            counter = idx + 1
            i += 1
            continue
        if RE_BPL.match(block[i]):
            bpl.append(block[i])
            i += 1
            continue
        head.append(block[i])
        i += 1
    return head, items, bpl


def cluster_error_of(c, rows):
    """max deviation (Angstrom) between the CP position and the cluster table"""
    if c is None or not rows:
        return 1e9
    err = 0.0
    for k, l in rows.items():
        tok = l.split()
        try:
            x, y, z, d = (float(v) for v in tok[-4:])
        except ValueError:
            return 1e9
        dist = math.sqrt((x - c[0]) ** 2 + (y - c[1]) ** 2 + (z - c[2]) ** 2) * BOHR2ANG
        err = max(err, abs(dist - d))
    return err


def cluster_error(block):
    return cluster_error_of(coords_of(block), cluster_rows(block))


def quality(block):
    """ranking of one copy of a CP block (higher is better)"""
    head, items, bpl = split_block(block)
    n_attr = sum(1 for it in items if it[1] == 'attr')
    props = sum(1 for l in head if any(p in l for p in
                ('CP TYPE', 'COORD(AU)', 'COORD FRACT', 'PROPERTIES (RHO',
                 'KINETIC ENERGY', 'VIRIAL DENSITY', 'ELF(PAA)',
                 'SPECTRAL DECOMP', 'EIGENVALUES', 'EIGENVECTORS')))
    rows = len(cluster_rows(block))
    seq = []
    for l in block:
        s = l.strip()
        if not s or s.startswith('*'):
            continue
        if RE_ATTR.match(l):
            seq.append(21)
        elif 'SEARCH OF BOND PATH ATTRACTORS' in s:
            seq.append(20)
        elif RE_BPL.match(l):
            seq.append(30)
        elif RE_UNABLE.search(l):
            seq.append(27)
        elif 'CP TYPE' in s:
            seq.append(3)
        elif 'COORD(AU)' in s.upper():
            seq.append(4)
        elif 'COORD FRACT' in s:
            seq.append(5)
        elif 'PROPERTIES (RHO' in s:
            seq.append(6)
        elif 'KINETIC ENERGY' in s:
            seq.append(7)
        elif 'VIRIAL DENSITY' in s:
            seq.append(8)
        elif 'ELF(PAA)' in s:
            seq.append(9)
        elif 'SPECTRAL DECOMP' in s:
            seq.append(10)
        elif 'EIGENVALUES' in s:
            seq.append(11)
        elif 'EIGENVECTORS' in s:
            seq.append(12)
        elif 'CLUSTER OF ATOMS AROUND THE CP' in s:
            seq.append(13)
        elif s.startswith('NEW'):
            seq.append(14)
        elif re.match(r'^\s*\d+\s+\d+\s+-?\d+\s+-?\d+\s+-?\d+\s+\d+\s+', l):
            seq.append(15)
    canonical = all(seq[i] <= seq[i + 1] for i in range(len(seq) - 1))
    err = cluster_error(block)
    return (-err, canonical, props, rows, n_attr, bool(bpl), len(block))


class Layout:
    """All the headers and separators, taken from the input file."""

    def __init__(self, lines):
        self.lines = lines
        self.frame = self._first(lambda l: bool(RE_FRAME.match(l)), ' ' + '*' * 80)
        self.banner = self._banner()
        self.lattice1 = self._block_after(RE_LATT1, 3)
        self.lattice2 = self._block_after(RE_LATT2, 2)
        self.atom_hdr = self._first(lambda l: bool(RE_ATOMHDR.match(l)),
                                    '   ATOM N.AT.  SHELL    X(A)      Y(A)      Z(A)      EXAD       N.ELECT.')
        self.natoms_prefix = self._natoms_prefix()
        self.topo_line = self._first(lambda l: bool(RE_TOPO.search(l)), '')
        self.clusters_hdr = self._first(lambda l: bool(RE_CLUSTERS.match(l)),
                                        ' CLUSTERS AROUND EACH OF THE NON-EQUIVALENT ATOMS')
        self.nequiv_tmpl = self._first(lambda l: bool(RE_NEQUIV_LINE.match(l)), '')
        self.cluster_tbl_hdr = self._after(self.nequiv_tmpl, lambda l: l.strip().startswith('NEW'))
        self.nequiv2_tmpl = self._first(lambda l: bool(RE_NEQUIV2_LINE.match(l)), '')
        self.pair_tbl_hdr = self._after(self.nequiv2_tmpl, lambda l: l.strip().startswith('NEW'))
        self.topo_hdr = self._first(lambda l: 'UNIQUE ATOM PAIRS CONSIDERED' in l, '')
        self.neigh_hdr = self._first(lambda l: 'NEIGHBORS OF THE NON-EQUIVALENT ATOMS' in l, '')
        self.neigh_legend = self._first(lambda l: 'NUMBER OF NEIGHBORS AT DISTANCE R' in l, '')
        self.neigh_cols = self._first(lambda l: 'R/ANG' in l and 'R/AU' in l, '')
        self.pairs_line = self._first(lambda l: bool(RE_PAIRS.search(l)), '')
        self.n_pairs_line = self._first(lambda l: 'NUMBER OF ATOM PAIRS INVESTIGATED' in l, '')
        self.n_cp_line = self._first(lambda l: 'NUMBER OF UNIQUE CRI. POINT FOUND' in l, '')
        self.cp_title = self._first(lambda l: 'C R I T I C A L' in l, '')
        self.cp_hdr = self._first(lambda l: bool(re.match(r'^\s*CP N\.\s+X\(', l)), '')
        self.cp_frame = self._first_entry_frame()
        self.phase_frame = self._phase_frame()
        self.attr_title, self.attr_frame = self._attractor_frames()
        self.cp_type_tmpl = self._first(lambda l: bool(RE_TYPE.search(l)), ' CP TYPE : (%d,%d)')

    def _first(self, pred, default=''):
        for l in self.lines:
            if pred(l):
                return l
        return default

    def _first_idx(self, pred):
        for i, l in enumerate(self.lines):
            if pred(l):
                return i
        return None

    def _banner(self):
        idx = self._first_idx(is_banner)
        if idx is None:
            return []
        end = idx
        while end + 1 < len(self.lines) and is_banner(self.lines[end + 1]):
            end += 1
        return self.lines[idx:end + 1]

    def _block_after(self, rx, n):
        idx = self._first_idx(lambda l: bool(rx.search(l)))
        if idx is None:
            return []
        return self.lines[idx:idx + 1 + n]

    def _after(self, anchor, pred):
        idx = self._first_idx(lambda l: l == anchor)
        if idx is None:
            return ''
        for l in self.lines[idx + 1:idx + 6]:
            if pred(l):
                return l
        return ''

    def _natoms_prefix(self):
        for l in self.lines:
            m = RE_NATOMS.match(l)
            if m:
                return m.group('pre')
        return ' N. OF ATOMS PER CELL       '

    def _first_entry_frame(self):
        idx = self._first_idx(lambda l: bool(RE_SUMMARY.match(l)))
        if idx is None:
            return self.frame
        for j in range(idx - 1, -1, -1):
            if RE_FRAME.match(self.lines[j]):
                return self.lines[j]
        return self.frame

    def _phase_frame(self):
        idx = self._first_idx(lambda l: bool(RE_PHASE.match(l)))
        if idx is None:
            return self.frame
        if idx > 0 and RE_FRAME.match(self.lines[idx - 1]):
            return self.lines[idx - 1]
        return self.frame

    def _attractor_frames(self):
        idx = self._first_idx(lambda l: 'SEARCH OF BOND PATH ATTRACTORS' in l)
        if idx is None:
            return (' SEARCH OF BOND PATH ATTRACTORS', self.frame)
        title = self.lines[idx]
        frame = self.lines[idx + 1] if idx + 1 < len(self.lines) and \
            RE_FRAME.match(self.lines[idx + 1]) else self.frame
        return (title, frame)

    def natoms_line(self, n):
        return f'{self.natoms_prefix}{n}'

    def cp_type_line(self, ctype):
        return re.sub(r'\(\s*[-+]?\d+\s*,\s*[-+]?\d+\s*\)', f'({ctype[0]},{ctype[1]})',
                      self.cp_type_tmpl)

    def cluster_hdr_line(self, idx, el, nat, nclu):
        m = RE_NEQUIV_LINE.match(self.nequiv_tmpl)
        if not m:
            return f' NON-EQUIV. ATOM {idx:>6} {el} (N. {nat:>4} IN THE UNIT CELL), ' \
                   f'CLUSTER OF {nclu:>2} ATOMS'
        return (m.group('pre') + pad(str(idx), m.group('idx')) + m.group('mid') + el +
                m.group('gap') + pad(str(nat), m.group('nat')) + m.group('mid2') +
                pad(str(nclu), m.group('nclu')) + m.group('post'))

    def pair_hdr_line(self, idx, el):
        m = RE_NEQUIV2_LINE.match(self.nequiv2_tmpl)
        if not m:
            return f' NON EQUIV. ATOM {idx:>6} {el}'
        return (m.group('pre') + pad(str(idx), m.group('idx')) + m.group('mid') +
                el + m.group('post'))


def merge_cp(copies, lay, ctype=None, emitted=None):
    """merge all copies of one CP number into a single clean block

    emitted : set of the attractor positions that are already written in the
              output file (an attractor block is written only once)
    """
    copies = sorted(copies, key=quality, reverse=True)
    best = copies[0]
    head, items, bpl = split_block(best)

    rows = cluster_rows(best)
    base_err = cluster_error(best)
    for c in copies[1:]:
        if cluster_error(c) > CLUSTER_TOL:
            continue
        for k, l in cluster_rows(c).items():
            rows.setdefault(k, l)
    if rows and cluster_error_of(coords_of(best), rows) > base_err + 1e-6:
        rows = cluster_rows(best)
    if rows:
        newhead = []
        inside = False
        for l in head:
            if 'CLUSTER OF ATOMS AROUND THE CP' in l:
                inside = True
                newhead.append(l)
                if lay.cluster_tbl_hdr:
                    newhead.append(lay.cluster_tbl_hdr)
                for k in sorted(rows):
                    newhead.append(rows[k])
                continue
            if inside:
                if l.strip().startswith('NEW') or RE_ROW.match(l):
                    continue
                inside = False
            newhead.append(l)
        head = newhead

    attrs = {}
    attr_idx = {}
    fails = {}
    for c in copies:
        _, its, _ = split_block(c)
        for idx, kind, payload in its:
            if kind == 'attr':
                key = coords_of(payload)
                if key is not None and key not in attrs:
                    attrs[key] = payload
                    attr_idx[key] = idx
            else:
                fails.setdefault(idx, payload)
    if (attrs or fails) and not any('SEARCH OF BOND PATH ATTRACTORS' in l for l in head):
        head.append('')
        head.append(lay.attr_frame)
        head.append(lay.attr_title)
        head.append(lay.attr_frame)
        head.append('')
    done = set()
    for c in copies:
        _, its, _ = split_block(c)
        for idx, kind, payload in its:
            if kind != 'attr':
                continue
            key = coords_of(payload)
            if key in done or key not in attrs:
                continue
            done.add(key)
            if emitted is not None:
                if key in emitted:
                    continue
                emitted.add(key)
            head.append('')
            head.extend(attrs[key])
    for idx in sorted(fails):
        if idx in attr_idx.values():
            continue
        head.append('')
        head.append(fails[idx])

    if ctype is not None:
        for i, l in enumerate(head):
            if RE_TYPE.search(l):
                head[i] = lay.cp_type_line(ctype)
                break

    seen = set()
    for c in copies:
        for l in split_block(c)[2]:
            if l not in seen:
                seen.add(l)
                head.append(l)
    return head


def build(lines, report):
    lay = Layout(lines)

    first_phase = next((i for i, l in enumerate(lines) if RE_PHASE.match(l)), len(lines))
    first_sum = next((i for i, l in enumerate(lines) if RE_SUMMARY.match(l)), len(lines))
    pre = lines[:first_phase]
    out = []

    # ---- 1. CRYSTAL17 banner --------------------------------------------
    report['banner lines'] = len(lay.banner)

    # ---- 2. atom table ---------------------------------------------------
    atoms = {}
    conflicts = 0
    for l in pre:
        m = ATOM_LINE_RE.match(l)
        if not m:
            continue
        num = int(m.group('num'))
        if num in atoms:
            if atoms[num] != l:
                conflicts += 1
        else:
            atoms[num] = l
    report['atoms'] = len(atoms)
    report['atom conflicts'] = conflicts

    # ---- 3. cluster tables and pair tables -------------------------------
    clusters = collections.OrderedDict()
    pairtables = collections.OrderedDict()
    i = 0
    while i < len(pre):
        l = pre[i]
        m = RE_NEQUIV.match(l)
        m2 = RE_NEQUIV2.match(l)
        if m or m2:
            if m:
                key = (int(m.group(1)), m.group(2), int(m.group(3)))
                rows = clusters.setdefault(key, {})
            else:
                key = (int(m2.group(1)), m2.group(2))
                rows = pairtables.setdefault(key, {})
            j = i + 1
            while j < len(pre) and not RE_NEQUIV.match(pre[j]) and \
                    not RE_NEQUIV2.match(pre[j]) and not RE_PHASE.match(pre[j]):
                rm = RE_ROW.match(pre[j]) if m else RE_PAIRROW.match(pre[j])
                if rm:
                    rows[int(rm.group(1))] = pre[j]
                j += 1
            i = j
            continue
        i += 1
    report['cluster tables'] = len(clusters)
    report['pair tables'] = len(pairtables)

    # ---- 4. neighbour lines ----------------------------------------------
    seen = set()
    neighbours = []
    for l in pre:
        if RE_NOISE.match(l) and l not in seen:
            seen.add(l)
            neighbours.append(l)
    report['neighbour lines'] = len(neighbours)

    # ---- 5. computational conditions --------------------------------------
    cond = []
    for i, l in enumerate(pre):
        if RE_COND.match(l):
            j = i + 1
            while j < len(pre) and pre[j].strip() and not RE_NEQUIV.match(pre[j]) \
                    and not RE_NEQUIV2.match(pre[j]) and not RE_TOPO.search(pre[j]):
                cond.append(pre[j])
                j += 1
            break
    pairs_line = lay.pairs_line
    proc_line = next((l for l in pre if RE_PROC.match(l)), '')
    start_line = next((l for l in pre if RE_START.match(l)), '')

    # ---- assemble the preamble -------------------------------------------
    out.append(proc_line)
    out.append(start_line)
    out.extend(lay.banner)
    out.append('')
    out.extend(lay.lattice1)
    out.append('')
    out.extend(lay.lattice2)
    out.append(lay.frame)
    out.append(lay.natoms_line(len(atoms)))
    out.append(lay.frame)
    out.append(lay.atom_hdr)
    out.append(lay.frame)
    for num in sorted(atoms):
        out.append(atoms[num])
    out.append(lay.frame)
    out.append(lay.topo_line)
    out.append('')
    out.append(' COMPUTATIONAL CONDITIONS :')
    out.extend(cond)
    out.append('')
    out.append(lay.clusters_hdr)
    for (idx_, el, nat), rows in clusters.items():
        out.append(lay.cluster_hdr_line(idx_, el, nat, len(rows)))
        out.append(lay.cluster_tbl_hdr)
        for k in sorted(rows):
            out.append(rows[k])
        out.append('')
    out.append(lay.topo_hdr)
    for (idx_, el), rows in pairtables.items():
        out.append(lay.pair_hdr_line(idx_, el))
        out.append('')
        out.append(lay.pair_tbl_hdr)
        for k in sorted(rows):
            out.append(rows[k])
        out.append('')
    out.append(lay.neigh_hdr)
    out.append('')
    out.append(lay.neigh_legend)
    out.append(lay.neigh_cols)
    out.append('')
    out.extend(neighbours)
    out.append('')
    out.append(pairs_line)
    out.append('')

    # the blocks of the file are delimited by the headers of the next block
    stop_re = re.compile(r'(?:%s)|(?:%s)|(?:%s)|(?:%s)|NUMBER OF ATOM PAIRS INVESTIGATED'
                          r'|^\s*TTTT|^\s*EEEE|CP N\.\s+X\(' %
                          (RE_CP.pattern, RE_PHASE.pattern, RE_SUBSEC.pattern,
                           RE_SUMMARY.pattern))
    stops = [i for i, l in enumerate(lines) if stop_re.search(l)]

    # ---- 6. the final list of the critical points -------------------------
    entries = collections.defaultdict(list)
    for i, l in enumerate(lines):
        m = RE_SUMMARY.match(l)
        if not m:
            continue
        j = i + 1
        while j < len(lines) and not stop_re.search(lines[j]):
            j += 1
        entries[int(m.group(1))].append(lines[i:j])

    def entry_score(blk):
        s = 0
        for l in blk:
            t = l.strip()
            if not t or t.startswith('*') or t.startswith('TTTT') or \
                    t.startswith('EEEE') or 'CPU TIME' in t or 'TELAPSE' in t:
                continue
            s += 1
        return s

    best_entry = {num: max(entries[num], key=entry_score) for num in entries}
    ctype = {}
    for num, blk in best_entry.items():
        m = RE_SUMTYPE.search(blk[0]) if blk else None
        if m:
            ctype[num] = (int(m.group(2)), int(m.group(3)))
    report['summary entries'] = len(entries)
    report['CP types from summary'] = len(ctype)

    # ---- 7. CP blocks -----------------------------------------------------
    copies = collections.defaultdict(list)
    first_pos = {}
    for k, i in enumerate(stops):
        m = RE_CP.match(lines[i])
        if not m:
            continue
        num = int(m.group(1))
        j = stops[k + 1] if k + 1 < len(stops) else len(lines)
        copies[num].append(lines[i:j])
        if num not in first_pos:
            first_pos[num] = i
    report['CP blocks in source'] = sum(len(v) for v in copies.values())
    report['unique CP'] = len(copies)

    order = sorted(first_pos, key=lambda n: first_pos[n])

    phases = collections.OrderedDict()
    for num in order:
        t = ctype.get(num) or type_of(max(copies[num], key=quality))
        phases.setdefault(t, []).append(num)
    report['phases'] = {('%d,%d' % k): len(v) for k, v in phases.items()}

    emitted = set()
    out.append(lay.phase_frame)
    for phase, nums in phases.items():
        out.append(f' SEARCH OF ({phase[0]},{phase[1]}) CPS')
        out.append(lay.phase_frame)
        out.append('')
        for num in nums:
            out.extend(merge_cp(copies[num], lay, ctype.get(num), emitted))
            out.append('')

    # ---- 8. final list of the critical points ----------------------------
    if lay.n_pairs_line:
        out.append(lay.n_pairs_line)
    out.append(lay.n_cp_line or f' NUMBER OF UNIQUE CRI. POINT FOUND:   {len(best_entry)}')
    out.append('')
    out.append(lay.cp_title)
    out.append('')
    out.append(lay.cp_hdr)
    out.append('')
    out.append(lay.cp_frame)
    for num in sorted(best_entry):
        for l in best_entry[num]:
            t = l.strip()
            if not t or t.startswith('*') or t.startswith('TTTT') or \
                    t.startswith('EEEE') or 'CPU TIME' in t or 'TELAPSE' in t:
                continue
            out.append(l)
        out.append('')

    # ---- 9. termination ---------------------------------------------------
    term = []
    for l in lines[first_sum:]:
        if 'TERMINATION' in l or 'TOTAL CPU TIME' in l or 'CPU TIME =' in l or \
                l.strip().startswith('TTTTT') or l.strip().startswith('EEEE'):
            term.append(l)
    last = 0
    for i, l in enumerate(term):
        if l.strip().startswith('TTTTT') and 'TRHO' in l:
            last = i
    out.extend(term[last:])
    out.append(lay.cp_frame)

    clean = []
    for l in out:
        if l.strip() == '' and clean and clean[-1].strip() == '':
            continue
        clean.append(l)
    return clean, lay


def cp_blocks(lines):
    starts = [i for i, l in enumerate(lines) if RE_CP.match(l)]
    for k, i in enumerate(starts):
        j = starts[k + 1] if k + 1 < len(starts) else len(lines)
        yield int(RE_CP.match(lines[i]).group(1)), i, j


def collect(lines):
    cps = {}
    attrs = {}
    atoms = {}
    for num, s, e in cp_blocks(lines):
        t = c = None
        for l in lines[s:e]:
            if t is None:
                m = RE_TYPE.search(l)
                if m:
                    t = (int(m.group(1)), int(m.group(2)))
            if c is None:
                m = RE_COORD.search(l)
                if m:
                    c = tuple(round(float(v), 4) for v in m.groups())
        cps[num] = (t, c)
    for i, l in enumerate(lines):
        if RE_ATTR.match(l):
            c = None
            for l2 in lines[i:i + 6]:
                m = RE_COORD.search(l2)
                if m:
                    c = tuple(round(float(v), 4) for v in m.groups())
                    break
            attrs[c] = attrs.get(c, 0) + 1
    for l in lines:
        m = ATOM_LINE_RE.match(l)
        if m:
            atoms[int(m.group('num'))] = (round(float(m.group('x')), 4),
                                          round(float(m.group('y')), 4),
                                          round(float(m.group('zc')), 4))
    return cps, attrs, atoms


def verify(src_lines, out_lines):
    problems = []
    ocps, oattrs, oatoms = collect(src_lines)
    fcps, fattrs, fatoms = collect(out_lines)

    miss = [n for n in ocps if n not in fcps]
    extra = [n for n in fcps if n not in ocps]
    if miss or extra:
        problems.append(f'CP missing in output: {miss[:10]}, extra: {extra[:10]}')

    badc = [(n, ocps[n][1], fcps[n][1]) for n in ocps
            if n in fcps and ocps[n][1] != fcps[n][1]]
    if badc:
        problems.append(f'CP with changed coordinates: {len(badc)} {badc[:5]}')

    amiss = [a for a in oattrs if a not in fattrs]
    if amiss:
        problems.append(f'attractors missing in output: {amiss[:5]}')

    dupattr = {a: n for a, n in fattrs.items() if n > 1}
    if dupattr:
        problems.append(f'attractors written more than once: {len(dupattr)}')

    abad = [(n, oatoms[n], fatoms[n]) for n in oatoms if n in fatoms and oatoms[n] != fatoms[n]]
    if len(oatoms) != len(fatoms) or abad:
        problems.append(f'atoms: {len(oatoms)} in source, {len(fatoms)} in output, '
                        f'changed: {len(abad)} {abad[:5]}')

    def blocks_of(ls):
        marks = [i for i, l in enumerate(ls) if RE_CP.match(l) or RE_ATTR.match(l)]
        res = []
        for k, i in enumerate(marks):
            j = marks[k + 1] if k + 1 < len(marks) else len(ls)
            res.append('\n'.join(ls[i:j]))
        return res

    cnt = collections.Counter(blocks_of(out_lines))
    rep = {b: c for b, c in cnt.items() if c > 1}
    if rep:
        problems.append(f'identical CP/attractor blocks repeated: {len(rep)}')

    bad_cp = []
    for num, s, e in cp_blocks(out_lines):
        blk = out_lines[s:e]
        has_type = any('CP TYPE' in l for l in blk)
        has_coord = any(RE_COORD.search(l) for l in blk)
        nrows = len(cluster_rows(blk))
        if not (has_type and has_coord and nrows >= 2):
            bad_cp.append((num, has_type, has_coord, nrows))
    if bad_cp:
        problems.append(f'CP blocks without type/coords/cluster rows: {len(bad_cp)} {bad_cp[:5]}')

    return problems


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('input', help='interleaved .out file of the TOPOND run')
    ap.add_argument('-o', '--output', help='clean output file '
                    '(default: <input>_dedup.out)')
    ap.add_argument('--report', action='store_true', help='print the detailed report')
    ap.add_argument('--verify', action='store_true',
                    help='check the output against the input and report the problems')
    args = ap.parse_args(argv)

    src = args.input
    dst = args.output or (os.path.splitext(src)[0] + '_dedup.out')
    if os.path.abspath(src) == os.path.abspath(dst):
        ap.error('the output file must differ from the input file')
    if not os.path.isfile(src):
        ap.error(f'input file not found: {src}')

    lines = load(src)
    report = collections.OrderedDict()
    out, lay = build(lines, report)

    with open(dst, 'w', encoding='utf-8') as f:
        f.write('\n'.join(out) + '\n')

    print(f'written {dst}: {len(out)} lines (source {len(lines)})')
    if args.report:
        for k, v in report.items():
            print(f'  {k}: {v}')

    if args.verify:
        problems = verify(lines, out)
        for p in problems:
            print('PROBLEM:', p)
        print('RESULT:', 'OK' if not problems else 'PROBLEMS FOUND')
        return 1 if problems else 0
    return 0


if __name__ == '__main__':
    sys.exit(main())
