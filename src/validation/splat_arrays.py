"""Vectorised GaussianSplats array checks, the same rules as validation.semantic._check_gaussian_splats.

The oversize-document path of validate_xml calls `check_splat_arrays`. On a 500,000-splat node it takes about one
second where the list-walking check in semantic.py takes about four, and a differential test pins the two to the same
findings, so semantic.py can adopt it later without a change of behaviour.
"""

import re
from dataclasses import dataclass


SPLAT_ARRAYS = {"positions": 3, "scales": 3, "orientations": 4, "opacities": 1}
SPLAT_SH = {(d, c): f"sphericalHarmonicsDegree{d}Coef{c}" for d in range(4) for c in range(2 * d + 1)}
ORDER_MIN = 100                      # too few splats to guess the quaternion order below this
_SEP = re.compile(r"[\s,]+")
_NUMBER_CHARS = b"0123456789.+-eE \t\r\n,"


class ArrayError(ValueError):
    """An MF attribute value that is not a list of numbers."""


@dataclass
class Finding:
    level: str       # "error" | "warning"
    check: str
    message: str


_TOKEN = re.compile(r"[^\s,]+")


def count_numbers(text: str) -> int:
    """Number of tokens in an MF attribute value (whitespace or comma separated); no float is parsed, no list is built."""
    return sum(1 for _ in _TOKEN.finditer(text))


def parse_numbers(text: str, *, strict: bool = False):
    """MF attribute value -> float64 vector (numpy). A bad token gives an empty vector, or ArrayError when `strict`."""
    import numpy as np
    flat = text.replace(",", " ") if "," in text else text
    if not flat.strip():
        return np.empty(0)      # np.fromstring("  ", sep=" ") returns [-1.]
    try:
        return np.fromstring(flat, dtype=np.float64, sep=" ")
    except ValueError as exc:
        if strict:
            raise ArrayError(f"not a number: {(first_bad_token(flat) or '?')[:40]!r}") from exc
        return np.empty(0)


def _is_number(token: str) -> bool:
    try:
        float(token)
        return True
    except ValueError:
        return False


def first_bad_token(text: str) -> str | None:
    """The first token of an MF value that is not a number, or None. Plain Python, so it works without numpy."""
    return next((t for t in _SEP.split(text.strip()) if t and not _is_number(t)), None)


def looks_numeric(text: str) -> bool:
    """Cheap character-class test (about 1 GB/s): only digits, sign, point, exponent and separators."""
    return not text.encode("ascii", "replace").translate(None, _NUMBER_CHARS)


def _vector(value: str):
    """float64 vector of an MF value, or None when numpy is missing or a token is not a number."""
    try:
        import numpy as np
    except ImportError:
        return None
    flat = value.replace(",", " ") if "," in value else value
    if not flat.strip():
        return np.empty(0)      # np.fromstring("  ", sep=" ") returns [-1.]
    try:
        return np.fromstring(flat, dtype=np.float64, sep=" ")
    except ValueError:
        return None


def check_splat_arrays(attrs: dict[str, str | None], *, strict_numbers: bool = False,
                       counts_out: dict[str, int] | None = None, values: bool = True) -> list[Finding]:
    """Findings for one GaussianSplats node given its attribute strings (missing attributes are None or absent).

    Rules and messages are those of validation.semantic._check_gaussian_splats, in the same order. With
    `strict_numbers` a value holding a token that is not a number is reported as `splat-array-syntax` (the XSD
    would do that; the oversize path has no XSD to lean on). `counts_out` receives the number count per attribute.
    Each value is parsed once, which is what makes a 500,000-splat node cost about a second. With `values` False
    nothing is parsed to floats (numbers are only counted): memory stays flat for text too big to hold as float64.
    """
    out: list[Finding] = []
    arrays = dict(SPLAT_ARRAYS, **{name: 3 for name in SPLAT_SH.values()})
    counts: dict[str, int] = {}
    vectors: dict[str, object] = {}
    for name, size in arrays.items():
        value = attrs.get(name)
        if value is None:
            continue
        vec = _vector(value) if values else None
        if vec is not None:
            n = int(vec.size)
            vectors[name] = vec
        else:
            n = count_numbers(value)
            bad = first_bad_token(value) if strict_numbers and values else None
            if bad is not None:
                out.append(Finding("error", "splat-array-syntax", f"{name}: not a number: {bad[:40]!r}."))
        if counts_out is not None:
            counts_out[name] = n
        if n % size:
            out.append(Finding("error", "tuple-count", f"{name} has {n} numbers, not a multiple of {size}."))
        counts[name] = n // size
    if not counts:
        return out
    expected = counts.get("positions", max(counts.values()))
    for name, n in counts.items():
        if n != expected:
            out.append(Finding("error", "splat-array-length",
                               f"{name} holds {n} splats but positions holds {expected}. "
                               "Every per-splat array needs one entry per splat."))
    present = {d for (d, c), name in SPLAT_SH.items() if name in counts}
    for d in sorted(present):
        missing = [name for (dd, c), name in SPLAT_SH.items() if dd == d and name not in counts]
        if missing:
            out.append(Finding("warning", "splat-sh-partial",
                               f"SH degree {d} is missing {', '.join(missing)}. Renderers drop a partial "
                               "degree (X_ITE 16.4.3 treats it as absent), so write all 2d+1 coefficients or none."))
        elif d and d - 1 not in present:
            out.append(Finding("warning", "splat-sh-partial", f"SH degree {d} is written but degree {d - 1} is not."))
    if 0 not in present:
        out.append(Finding("warning", "splat-no-color", "no sphericalHarmonicsDegree0Coef0, so the splats have no base colour."))
    try:
        import numpy as np
    except ImportError:
        out.append(Finding("warning", "splat-values-unchecked",
                           "numpy is not installed, so scale, opacity and quaternion values were not checked "
                           "(pip install 'x3d-mcp[splat]')."))
        return out
    if not values:
        out.append(Finding("warning", "splat-values-unchecked",
                           "the arrays are too large to parse here, so only the counts were checked."))
        return out
    empty = np.empty(0)
    scales = vectors.get("scales", empty)
    if scales.size and scales.min() < 0:
        out.append(Finding("error", "splat-scale-space",
                           f"{int((scales < 0).sum())} of {scales.size} scale values are negative. X3D scales are linear "
                           "standard deviations; a 3DGS PLY stores ln(scale), so apply exp() when converting."))
    opacities = vectors.get("opacities", empty)
    if opacities.size and (opacities.min() < 0 or opacities.max() > 1):
        bad = int(((opacities < 0) | (opacities > 1)).sum())
        out.append(Finding("error", "splat-opacity-space",
                           f"{bad} of {opacities.size} opacities are outside 0 to 1. A 3DGS PLY stores "
                           "logit(opacity), so apply the sigmoid when converting."))
    quats = vectors.get("orientations", empty)
    if quats.size >= 4:
        q = quats[: quats.size // 4 * 4].reshape(-1, 4)
        norms = np.sqrt((q * q).sum(1))
        zero = int((norms == 0).sum())
        off = int((np.abs(norms - 1) > 1e-3).sum()) - zero
        if zero:
            out.append(Finding("error", "splat-quaternion-norm", f"{zero} orientations are all zero (no rotation is defined)."))
        if off:
            out.append(Finding("warning", "splat-quaternion-norm",
                               f"{off} of {len(norms)} orientations are not unit length (|q| off by more than 1e-3); "
                               "renderers normalise them, but a unit quaternion round-trips exactly."))
        if len(norms) >= ORDER_MIN:
            lead = np.bincount(np.abs(q).argmax(1), minlength=4)
            if lead[0] > 0.6 * len(norms) and lead[3] < 0.2 * len(norms):
                out.append(Finding("warning", "splat-quaternion-order",
                                   f"{int(lead[0])} of {len(norms)} orientations have their largest component first and "
                                   f"only {int(lead[3])} last. Trained splats usually stay near the identity (0 0 0 1), "
                                   "so these look written w x y z; X3D 4.1 reads x y z w."))
    return out
