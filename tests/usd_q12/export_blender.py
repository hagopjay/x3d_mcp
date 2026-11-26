"""Stage 1 of Q12: import each X3D scene with the x3d-blender-io add-on and export it with Blender's wm.usd_export.

    python export_blender.py SCENES_DIR OUT_DIR ADDON_SOURCE_DIR RESULT.json

Needs `bpy` (PyPI `bpy==5.3.0`, Python 3.13). ADDON_SOURCE_DIR is x3d-blender-io's `source/`.
A scene the add-on cannot import, or Blender cannot export, is recorded as an error, never skipped silently.
"""
import glob, json, os, sys, tempfile, importlib
scenes, out, addon, dest = (os.path.abspath(a) for a in sys.argv[1:5])
pkg = tempfile.mkdtemp(); os.symlink(addon, os.path.join(pkg, "io_scene_x3d"))
sys.path.insert(0, pkg)
import bpy
importlib.import_module("io_scene_x3d").register()
os.makedirs(out, exist_ok=True)
res = {"blender": bpy.app.version_string, "scenes": {}}
for f in sorted(glob.glob(os.path.join(scenes, "*.x3d"))):
    n = os.path.basename(f)[:-4]
    bpy.ops.wm.read_factory_settings(use_empty=True)
    try:
        r = bpy.ops.import_scene.x3d(filepath=f)
        r2 = bpy.ops.wm.usd_export(filepath=os.path.join(out, n + ".usdc"))
        res["scenes"][n] = {"import": sorted(r), "objects": len(bpy.data.objects), "export": sorted(r2)}
    except Exception as e:
        res["scenes"][n] = {"error": str(e)[:200]}
json.dump(res, open(dest, "w"), indent=1)
print(sum(1 for v in res["scenes"].values() if "error" in v), "errors of", len(res["scenes"]), "scenes; Blender", res["blender"])
