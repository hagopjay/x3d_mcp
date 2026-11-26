"""Write every tests/usd_parity.py fixture scene (a failing and a passing one per stateable mistake) to a folder:
    python tests/usd_q12/dump_scenes.py SCENES_DIR
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import usd_parity as u  # noqa: E402
out = Path(sys.argv[1]); out.mkdir(parents=True, exist_ok=True)
for n, (_by, _chk, bad, good) in u.F.items():
    (out / f"{n}__bad.x3d").write_text(bad); (out / f"{n}__good.x3d").write_text(good)
print(2 * len(u.F), "scenes in", out)
