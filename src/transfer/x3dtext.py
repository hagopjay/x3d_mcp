"""X3D 4.1 text for GaussianSplats: number formatting, nodes, chunk files and the master scene."""

import math
from dataclasses import dataclass

import numpy as np

from .cloud import SplatCloud, SplatError

START_TAG_BUDGET = 8_000_000     # bytes of one GaussianSplats start tag we allow; libxml2 stops at 10,000,000
FIELD_ORDER = ["positions", "scales", "orientations", "opacities", "sphericalHarmonicsDegree0Coef0"]

# printf spec per field.  "bridge" is what the Blender bridge writes (six decimals for everything);
# "exact" keeps float32 round-trippable; "lattice" is derived from the SPZ quantisation steps by `lattice_digits`.
_BRIDGE = dict(positions="%.6f", scales="%.6f", orientations="%.6f", opacities="%.6f", dc="%.6f", sh="%.6f")
_EXACT = dict(positions="%.9g", scales="%.9g", orientations="%.9g", opacities="%.9g", dc="%.9g", sh="%.9g")


def _decimals(step: float) -> int:
    """Fewest decimals whose rounding error (half a unit in the last place) is at most a tenth of `step`."""
    return max(0, math.ceil(-math.log10(step / 5)))


def lattice_digits(fractional_bits: int) -> dict[str, str]:
    """printf specs that keep every SPZ-quantised value recoverable: the rounding error stays within a tenth of
    the quantisation step of the field it came from (positions 2**-fractionalBits, a 9-bit rotation magnitude
    0.7071/511, an 8-bit opacity 1/255, a colour byte 1/(255*0.15), an SH byte 1/128).  Scales are stored as
    ln(scale) in steps of 1/16, a 6.5% step in linear space, so three significant digits are plenty."""
    return dict(positions=f"%.{_decimals(2.0 ** -fractional_bits)}f", scales="%.3g",
                orientations=f"%.{_decimals(0.7071067811865476 / 511)}f", opacities=f"%.{_decimals(1 / 255)}f",
                dc=f"%.{_decimals(1 / (255 * 0.15))}f", sh=f"%.{_decimals(1 / 128)}f")


def digit_specs(precision: str, cloud: SplatCloud) -> dict[str, str]:
    if precision == "auto":
        precision = "lattice" if cloud.source_format.startswith("spz") else "bridge"
    if precision == "lattice":
        if not cloud.source_format.startswith("spz"):
            raise SplatError("precision 'lattice' needs an SPZ source (it is derived from the SPZ quantisation steps)")
        return lattice_digits(cloud.info["fractional_bits"])
    if precision == "bridge":
        return dict(_BRIDGE)
    if precision == "exact":
        return dict(_EXACT)
    raise SplatError(f"unknown precision {precision!r} (auto, lattice, bridge, exact)")


def format_values(a: np.ndarray, spec: str, block: int = 60000) -> str:
    """Space-separated text of `a` (any shape, row-major) with one printf spec; blocked so memory stays flat."""
    flat = np.asarray(a, dtype=np.float64).ravel().tolist()
    out = []
    for i in range(0, len(flat), block):
        part = flat[i:i + block]
        out.append(((spec + " ") * len(part)) % tuple(part))
    return "".join(out)[:-1] if flat else ""


def field_spec(name: str, specs: dict[str, str]) -> str:
    if name == "positions":
        return specs["positions"]
    if name == "scales":
        return specs["scales"]
    if name == "orientations":
        return specs["orientations"]
    if name == "opacities":
        return specs["opacities"]
    return specs["dc"] if name.endswith("Degree0Coef0") else specs["sh"]


def bytes_per_splat(cloud: SplatCloud, specs: dict[str, str], sample: int = 4000) -> float:
    """Average size of one splat's attribute text, measured on an evenly spaced sample (names and quotes included)."""
    n = len(cloud)
    if n == 0:
        return 0.0
    step = max(1, n // sample)
    sub = cloud.take(np.arange(0, n, step))
    total = 0
    for name, arr in sub.arrays().items():
        total += len(format_values(arr, field_spec(name, specs))) + len(name) + 4      # ` name=""`
    return total / len(sub)


def max_splats_per_node(cloud: SplatCloud, specs: dict[str, str], budget: int = START_TAG_BUDGET) -> int:
    """Splats that fit in one start tag of `budget` bytes at these digits."""
    return max(1, int(budget // max(1.0, bytes_per_splat(cloud, specs))))


@dataclass
class Box:
    center: np.ndarray
    size: np.ndarray

    @staticmethod
    def of(points: np.ndarray, pad: float = 0.0) -> "Box":
        lo, hi = points.min(0).astype(np.float64), points.max(0).astype(np.float64)
        return Box((lo + hi) / 2, (hi - lo) + 2 * pad)

    def attrs(self, digits: int = 4) -> str:
        f = lambda v: " ".join(f"{x:.{digits}f}" for x in v)
        return f'bboxCenter="{f(self.center)}" bboxSize="{f(self.size)}"'


def splat_node(cloud: SplatCloud, specs: dict[str, str], *, def_name: str = "", box: Box | None = None, indent: str = "    ") -> str:
    attrs = []
    if def_name:
        attrs.append(f'DEF="{def_name}"')
    if box is not None:
        attrs.append(box.attrs())
    arrays = cloud.arrays()
    for name in FIELD_ORDER + [k for k in arrays if k not in FIELD_ORDER]:
        if name in arrays:
            attrs.append(f'{name}="{format_values(arrays[name], field_spec(name, specs))}"')
    return f"{indent}<GaussianSplats " + " ".join(attrs) + "/>"


def x3d_document(body: str, *, title: str = "", metas: dict[str, str] | None = None) -> str:
    head = ['<component name="GaussianSplats" level="1"/>']
    if title:
        head.append(f'<meta name="title" content="{_esc(title)}"/>')
    for k, v in (metas or {}).items():
        head.append(f'<meta name="{_esc(k)}" content="{_esc(v)}"/>')
    return ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D version="4.1" profile="Immersive">\n  <head>\n    '
            + "\n    ".join(head) + "\n  </head>\n  <Scene>\n" + body + "\n  </Scene>\n</X3D>\n")


def _esc(s: str) -> str:
    return s.replace("&", "&amp;").replace('"', "&quot;").replace("<", "&lt;").replace(">", "&gt;")
