"""ROS 2 CDR decoding in mcap_reader checked against an independent writer and reader: mcap-ros2-support (Foxglove).

Skipped when mcap-ros2-support is not installed (pip install mcap mcap-ros2-support). 200 messages of a type that holds
every ros2msg primitive, bounded and fixed arrays, nested and empty messages, plus 200 sensor_msgs/JointState, written
uncompressed, zstd and lz4; every decoded value must equal the oracle's. Five planted decoder faults must each fail.
Known disagreement, not tested: mcap-ros2-support reads `char` as int8, rosbags and this reader as uint8 (same byte).
"""
import importlib.util
import math
import random
from pathlib import Path

import pytest

pytest.importorskip("mcap_ros2")
from mcap.writer import CompressionType  # noqa: E402
from mcap_ros2.reader import read_ros2_messages  # noqa: E402
from mcap_ros2.writer import Writer as Ros2Writer  # noqa: E402

SRC = Path(__file__).resolve().parents[1] / "src" / "x3d_utils" / "mcap_reader.py"


def _load(text=None):
    spec = importlib.util.spec_from_file_location("mcap_reader_oracle", SRC)
    m = importlib.util.module_from_spec(spec)
    if text is None:
        spec.loader.exec_module(m)
    else:
        exec(compile(text, str(SRC), "exec"), m.__dict__)
    return m


ALL = """bool b
byte by
char c
int8 i8
uint8 u8
int16 i16
uint16 u16
int32 i32
uint32 u32
int64 i64
uint64 u64
float32 f32
float64 f64
string s
string<=8 sb
float64[3] fix
int16[] seq
float32[<=4] bseq
string[] strs
Inner[] inners
Inner one
Empty e
int32 KONST=7
uint8 withdefault 5
================================================================================
MSG: custom_msgs/Inner
uint8 a
float64 d
string tag
int64[2] pair
================================================================================
MSG: custom_msgs/Empty
"""
JS = """std_msgs/Header header
string[] name
float64[] position
float64[] velocity
float64[] effort
================================================================================
MSG: std_msgs/Header
builtin_interfaces/Time stamp
string frame_id
================================================================================
MSG: builtin_interfaces/Time
int32 sec
uint32 nanosec
"""
rng = random.Random(3)


def rs(n):
    return "".join(rng.choice("abcxyz_é9") for _ in range(rng.randint(0, n)))


def all_msg(i):
    return {"b": bool(i % 2), "by": rng.randint(0, 255), "c": rng.randint(0, 127), "i8": rng.randint(-128, 127), "u8": rng.randint(0, 255),
            "i16": rng.randint(-2**15, 2**15 - 1), "u16": rng.randint(0, 2**16 - 1), "i32": rng.randint(-2**31, 2**31 - 1),
            "u32": rng.randint(0, 2**32 - 1), "i64": rng.randint(-2**63, 2**63 - 1), "u64": rng.randint(0, 2**64 - 1),
            "f32": rng.uniform(-1e3, 1e3), "f64": rng.uniform(-1e9, 1e9), "s": rs(9), "sb": rs(8),
            "fix": [rng.random() for _ in range(3)], "seq": [rng.randint(-99, 99) for _ in range(rng.randint(0, 5))],
            "bseq": [rng.random() for _ in range(rng.randint(0, 4))], "strs": [rs(5) for _ in range(rng.randint(0, 3))],
            "inners": [{"a": rng.randint(0, 255), "d": rng.random(), "tag": rs(4), "pair": [rng.randint(-5, 5), 9]} for _ in range(rng.randint(0, 3))],
            "one": {"a": 1, "d": -0.5, "tag": "x", "pair": [1, 2]}, "e": {}, "withdefault": 5}


def todict(o):
    if isinstance(o, (bytes, bytearray)):
        return {"_bytes": len(o)}
    if isinstance(o, (list, tuple)):
        return [todict(x) for x in o]
    if hasattr(o, "__slots__") or hasattr(o, "__dict__"):
        names = getattr(o, "__slots__", None) or [k for k in vars(o) if not k.startswith("_")]
        return {k: todict(getattr(o, k)) for k in names if not k.startswith("_")}
    return o


def same(a, b, path=""):
    if isinstance(a, dict) and isinstance(b, dict):
        ka, kb = set(a), set(b)
        if ka != kb:
            return [f"{path}: keys {sorted(ka ^ kb)}"]
        return [d for k in a for d in same(a[k], b[k], path + "." + k)]
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return [f"{path}: len {len(a)} vs {len(b)}"]
        return [d for i, (x, y) in enumerate(zip(a, b)) for d in same(x, y, f"{path}[{i}]")]
    if isinstance(a, float) or isinstance(b, float):
        return [] if (a == b or (math.isnan(a) and math.isnan(b))) else [f"{path}: {a!r} vs {b!r}"]
    return [] if a == b else [f"{path}: {a!r} vs {b!r}"]




def _write(path, comp):
    with open(path, "wb") as fh:
        w = Ros2Writer(fh, compression=comp)
        s_all = w.register_msgdef("custom_msgs/msg/AllTypes", ALL)
        s_js = w.register_msgdef("sensor_msgs/msg/JointState", JS)
        for i in range(200):
            w.write_message("/all", s_all, all_msg(i), log_time=i * 10**7, publish_time=i * 10**7)
            n = rng.randint(1, 7)
            w.write_message("/joint_states", s_js, {"header": {"stamp": {"sec": i // 100, "nanosec": (i % 100) * 10**7}, "frame_id": "base"},
                             "name": [f"j{k}" for k in range(n)], "position": [rng.uniform(-3, 3) for _ in range(n)],
                             "velocity": [], "effort": []}, log_time=i * 10**7 + 1, publish_time=i * 10**7 + 1)
        w.finish()


def _diffs(M, path):
    m = M.read_mcap(path)
    assert m["problems"] == [] and m["skipped_chunks"] == 0
    theirs = [(r.channel.topic, todict(r.ros_msg)) for r in read_ros2_messages(str(path))]
    assert len(m["messages"]) == len(theirs) == 400
    return sum(len(same(M.decode_message(m, x), b)) for x, (_, b) in zip(m["messages"], theirs))


@pytest.mark.parametrize("comp", [CompressionType.NONE, CompressionType.ZSTD, CompressionType.LZ4])
def test_cdr_matches_mcap_ros2_support(tmp_path, comp):
    if comp is CompressionType.ZSTD:
        pytest.importorskip("zstandard")
    if comp is CompressionType.LZ4:
        pytest.importorskip("lz4")
    f = tmp_path / "o.mcap"
    _write(f, comp)
    assert _diffs(_load(), f) == 0


FAULTS = {
    "no alignment": ("pad = (-(r.o - 4)) % n", "pad = 0"),
    "alignment counted from byte 0": ("pad = (-(r.o - 4)) % n", "pad = (-r.o) % n"),
    "string length without the NUL": ("b = r.take(n)\n            return b[:-1]", "b = r.take(n - 1)\n            return b"),
    "empty message as zero bytes": ('prim("uint8")       # empty message', 'pass       # empty message'),
    "big endian assumed": ('end = "<" if data[1] == 1 else ">"', 'end = ">"'),
}


@pytest.mark.parametrize("label", list(FAULTS))
def test_planted_decoder_faults_are_caught(tmp_path, label):
    old, new = FAULTS[label]
    text = SRC.read_text()
    assert old in text
    f = tmp_path / "o.mcap"
    _write(f, CompressionType.NONE)
    try:
        d = _diffs(_load(text.replace(old, new)), f)
    except ValueError:
        return                       # the fault ran a field past its record: caught
    assert d > 0
