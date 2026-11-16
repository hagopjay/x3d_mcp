"""Wall-clock timing of the scene_evidence engine call, as quoted in docs/scene-evidence.md ("Measured").

    python3 -I evaluation/time_scene_evidence.py

Builds scenes of N blocks (a Transform with a Material and a Box each), one claim per block quoting a sentence of a prompt that states
the block's translation, and times x3d_utils.scene_evidence.scene_evidence with BIND on and off, expect="inventory", at the default
200-item cap. Prints one row per case: scene bytes, claims, median milliseconds over RUNS calls, and the size of the rendered answer.
Same machine, same process: absolute numbers move between runs; the ratios and the linear growth do not.
"""
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from x3d_utils import scene_evidence as ev  # noqa: E402
from x3d_utils import x3dtrace as xt  # noqa: E402

RUNS = 7


def build(blocks: int) -> tuple[str, str, list[dict]]:
    body = "".join(
        '<Transform DEF="b%d" translation="%d 0 0"><Shape><Appearance><Material diffuseColor="0.8 0.2 0.2"/></Appearance>'
        '<Box size="1 2 1"/></Shape></Transform>\n' % (i, i)
        for i in range(blocks))
    scene = '<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Immersive" version="4.0"><Scene>\n' + body + "</Scene></X3D>"
    prompt = "".join("Block %d sits %d metres along the x axis, at %d 0 0.\n" % (i, i, i) for i in range(blocks))
    claims = [{"field": "b%d@translation" % i, "value": "%d 0 0" % i, "quote": "at %d 0 0" % i, "source": "prompt"}
              for i in range(blocks)]
    return scene, prompt, claims


def median_ms(fn) -> float:
    fn()  # warm: the first call builds the per-scene caches
    ts = []
    for _ in range(RUNS):
        t0 = time.perf_counter()
        fn()
        ts.append((time.perf_counter() - t0) * 1000)
    return statistics.median(ts)


def main() -> None:
    print("engine sha256 prefix", xt.__file__ and __import__("hashlib").sha256(Path(xt.__file__).read_bytes()).hexdigest()[:8],
          "python", sys.version.split()[0], "runs", RUNS)
    print("%-26s %7s %12s %14s %14s" % ("scene", "claims", "with BIND ms", "without BIND ms", "answer bytes"))
    for blocks, nclaims in ((4, 4), (600, 100), (600, 600), (6000, 100), (6000, 600)):
        scene, prompt, claims = build(blocks)
        use = claims[:nclaims]
        items = [{"id": "prompt", "text": prompt}]
        t_bind = median_ms(lambda: ev.scene_evidence(scene, use, items, expect="inventory", bind=True))
        t_free = median_ms(lambda: ev.scene_evidence(scene, use, items, expect="inventory", bind=False))
        size = len(ev.render(ev.scene_evidence(scene, use, items, expect="inventory", bind=True)).encode())
        print("%-26s %7d %12.1f %14.1f %14s" % ("%s B (%d blocks)" % ("{:,}".format(len(scene.encode())), blocks), nclaims,
                                               t_bind, t_free, "{:,}".format(size)))


if __name__ == "__main__":
    main()
