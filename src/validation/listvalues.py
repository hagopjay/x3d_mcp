"""Read an X3D numeric field value number by number, the way its XSD pattern means it.

The XSD checks SF/MF numeric values with regular-expression patterns. On a list value that
does not match, libxml2's backtracking regexp engine can give up with an internal error
instead of rejecting it: an out-of-range colour past about the twelfth entry of an MFColor
is enough. validate.py then needs to know which value is wrong without the regexp. This
module answers that in linear time for the numeric types, and leaves every other type
(strings, images, nodes) alone.
"""

import re

# one number, as the XSD patterns write it
_FLOAT = re.compile(r"[+-]?((0|[1-9][0-9]*)(\.[0-9]*)?|\.[0-9]+)([Ee][+-]?[0-9]+)?\Z")
_INT = re.compile(r"[+-]?(0|[1-9][0-9]*)([Ee][+-]?[0-9]+)?\Z")
_TOKEN = re.compile(r"[^\s,]+|,")

# numbers per value
_WIDTH = {"Float": 1, "Double": 1, "Time": 1, "Int32": 1, "Bool": 1,
          "Vec2f": 2, "Vec2d": 2, "Vec3f": 3, "Vec3d": 3, "Vec4f": 4, "Vec4d": 4,
          "Rotation": 4, "Color": 3, "ColorRGBA": 4,
          "Matrix3f": 9, "Matrix3d": 9, "Matrix4f": 16, "Matrix4d": 16}
TYPES = frozenset(p + base for base in _WIDTH for p in ("SF", "MF"))


def _ok(token: str, base: str) -> bool:
    if base == "Bool":
        return token in ("true", "false")
    if base == "Int32":
        return bool(_INT.match(token))
    if not _FLOAT.match(token):
        return False
    return not base.startswith("Color") or 0.0 <= float(token) <= 1.0


def problem(value: str, ftype: str) -> str | None:
    """What is wrong with `value` as an `ftype` value, in words, or None when it is fine.

    Only the numeric types in TYPES are read; for any other type the answer is None.
    A comma may only follow a whole value, as in the XSD patterns.
    """
    if ftype not in TYPES:
        return None
    base, many = ftype[2:], ftype.startswith("MF")
    width = _WIDTH[base]
    nums: list[str] = []
    for tok in _TOKEN.findall(value):
        if tok != ",":
            nums.append(tok)
        elif not many or not nums or len(nums) % width:
            where = f"inside value {len(nums) // width + 1}" if many else "in a single value"
            return f"a comma {where}; commas may only separate whole {ftype} values"
    bad = next((j for j, tok in enumerate(nums) if not _ok(tok, base)), None)
    if bad is not None:
        tok = nums[bad]
        what = (f"'{tok}' is outside 0..1" if base.startswith("Color") and _FLOAT.match(tok)
                else f"'{tok}' is not a valid {'boolean' if base == 'Bool' else 'number'}")
        if not many:
            return what
        k = bad // width
        entry = " ".join(nums[k * width:(k + 1) * width])
        return (f"value {k + 1} of {-(-len(nums) // width)} is '{entry}': {what}" if width > 1
                else f"entry {k + 1} of {len(nums)}: {what}")
    if many and len(nums) % width:
        return f"{len(nums)} numbers is not a whole number of {ftype} values ({width} each)"
    if not many and len(nums) != width:
        return f"{ftype} takes {width} number{'s' if width > 1 else ''}, not {len(nums)}"
    return None


def placeholder(ftype: str) -> str:
    """A short value the XSD accepts for `ftype` (one value; an MF field may also be empty)."""
    base = ftype[2:]
    if base == "Bool":
        return "true"
    if base == "Rotation":
        return "0 0 1 0"
    if base == "ColorRGBA":
        return "0 0 0 1"
    return " ".join(["0"] * _WIDTH[base])
