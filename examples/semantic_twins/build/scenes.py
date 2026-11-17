"""Write the three scenario scenes (X3D 4.0, Immersive). Every object is a flat, unrotated Transform
under one root at the origin, so its world position is its own translation."""
import json, sys
from pathlib import Path
from xml.sax.saxutils import quoteattr

OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "scenes"
OUT.mkdir(parents=True, exist_ok=True)


def f(*v):
    return " ".join(("%.4g" % x) for x in v)


def mat(c, trans=0.0, emis=0.25, rough=0.6, metal=0.0):
    return ("<Appearance><PhysicalMaterial baseColor='%s' emissiveColor='%s' roughness='%.3g' metallic='%.3g' transparency='%.3g'/></Appearance>"
            % (f(*c), f(*[x * emis for x in c]), rough, metal, trans))


def geom(kind, dims):
    if kind == "box":
        return "<Box size='%s'/>" % f(*dims)
    if kind == "cyl":
        return "<Cylinder radius='%.4g' height='%.4g'/>" % dims
    if kind == "sphere":
        return "<Sphere radius='%.4g'/>" % dims[0]
    if kind == "cone":
        return "<Cone bottomRadius='%.4g' height='%.4g'/>" % dims
    raise ValueError(kind)


def obj(defn, pos, kind, dims, color, trans=0.0, emis=0.25, rough=0.6, metal=0.0, extra=""):
    return ("<Transform DEF='%s' translation='%s'><Shape DEF='%s_Shape'>%s%s</Shape>%s</Transform>"
            % (defn, f(*pos), defn, mat(color, trans, emis, rough, metal), geom(kind, dims), extra))


def doc(title, ident, desc, bg, views, body, nav="EXAMINE"):
    head = ("<head><component name='Shape' level='2'/><meta name='title' content=%s/><meta name='identifier' content=%s/><meta name='description' content=%s/>"
            "<meta name='creator' content='HagopJay and Claude'/><meta name='license' content='CC0 geometry'/></head>"
            % (quoteattr(title), quoteattr(ident), quoteattr(desc)))
    vp = "".join("<Viewpoint DEF=%s description=%s position='%s' orientation='%s' centerOfRotation='%s'/>"
                 % (quoteattr(d), quoteattr(t), f(*p), f(*o), f(*c)) for d, t, p, o, c in views)
    return ("<?xml version='1.0' encoding='UTF-8'?>\n<!DOCTYPE X3D PUBLIC 'ISO//Web3D//DTD X3D 4.0//EN' 'https://www.web3d.org/specifications/x3d-4.0.dtd'>\n"
            "<X3D profile='Immersive' version='4.0' xmlns:xsd='http://www.w3.org/2001/XMLSchema-instance' "
            "xsd:noNamespaceSchemaLocation='https://www.web3d.org/specifications/x3d-4.0.xsd'>\n%s\n<Scene>"
            "<NavigationInfo type='\"%s\" \"ANY\"' headlight='true'/>"
            "<Background skyColor='%s' groundColor='%s'/>%s"
            "<DirectionalLight direction='-0.4 -1 -0.5' intensity='0.8'/>"
            "<Transform DEF='Root'>%s</Transform></Scene>\n</X3D>\n" % (head, nav, f(*bg[0]), f(*bg[1]), vp, body))


# ---------------------------------------------------------------- heritage (Giza)
D, T, S = (0.24, 0.90, 0.82), (1.0, 0.70, 0.28), (1.0, 0.31, 0.85)
STONE, WATER, GRANITE = (0.72, 0.62, 0.45), (0.20, 0.45, 0.85), (0.42, 0.38, 0.40)
yB = -9.62 - 13.25                      # floor of Shaft B = top of Chamber B region (model frame)
H = [
    # DEF, pos, kind, dims, colour, transparency
    ("OsirisShaftA", (0, -4.81, 0), "box", (1.2, 9.62, 1.2), D, 0.35),
    ("OsirisChamberA", (0, -8.27, 2.6), "box", (5, 2.7, 4), D, 0.55),
    ("OsirisShaftB", (4, -9.62 - 6.625, 0), "box", (1.2, 13.25, 1.2), D, 0.35),
    ("OsirisChamberB", (4, yB + 1.3, 0), "box", (6.80, 2.60, 3.65), D, 0.55),
    ("OsirisShaftC", (6, yB - 3.75, 0), "box", (1.2, 7.5, 1.2), D, 0.35),
    ("OsirisLevel3Water", (6, yB - 6.5, 0), "box", (9, 2.8, 6.4), WATER, 0.45),
    ("SphinxBody", (40, 10, 0), "box", (57, 20, 19), STONE, 0.0),
    ("SphinxPawVoid", (75, -5, 6), "box", (12, 3, 9), T, 0.5),
    ("HallOfRecords", (85, -15, 14), "box", (14, 6, 10), S, 0.55),
    ("KhufuKingsChamber", (-120, 46, -60), "box", (10.47, 5.97, 5.24), D, 0.4),
    ("KhufuBigVoid", (-120, 62, -60), "box", (30, 8, 2), D, 0.6),
    ("UndergroundLink", (40, -20, 7), "box", (80, 0.6, 0.6), S, 0.3),
]
# planted fault: Shaft A drawn 12 m instead of 9.62 m (scene contradicts the documented measurement)
heritage_views = [
    ("V_Overview", "Overview: Khufu, the Osiris Shaft, the Sphinx", (-15, 90, 260), (1, 0, 0, -0.316), (-15, 5, 0)),
    ("V_Osiris", "Osiris Shaft, side section", (3, -14, 42), (0, 1, 0, 0), (3, -14, 0)),
    ("V_Sphinx", "Sphinx enclosure and the paw anomaly", (60, 5, 70), (-0.2, 0.97, 0.05, 0.3), (60, -5, 5)),
]


def heritage(fault=False):
    body = ""
    for d, p, k, dims, c, tr in H:
        if fault and d == "OsirisShaftA":
            dims = (1.2, 12.0, 1.2); p = (0, -6.0, 0)
        body += obj(d, p, k, dims, c, tr, emis=0.45 if c in (D, T, S) else 0.1, rough=0.8)
    return doc("Osiris Shaft as a living record", "https://example.org/scen/heritage/scene.x3d",
               "Schematic Giza study; every element is tagged documented, theorized or speculative in its CIDOC-CRM record.",
               ((0.01, 0.015, 0.03), (0.05, 0.04, 0.03)), heritage_views, body)


# ---------------------------------------------------------------- factory (Cell 7)
STEEL, ORANGE, BELT, GREEN, FENCE = (0.6, 0.62, 0.66), (0.95, 0.45, 0.10), (0.15, 0.15, 0.17), (0.2, 0.8, 0.4), (0.95, 0.8, 0.1)
F = [
    ("Robot1Base", (0, 0.4, 0), "cyl", (0.35, 0.8), ORANGE, 0.0, 0.4, 0.2),
    ("Robot1Arm", (0.5, 1.3, 0), "box", (1.2, 0.18, 0.18), ORANGE, 0.0, 0.4, 0.2),
    ("Conveyor1", (0, 0.45, -1.6), "box", (6, 0.1, 0.6), BELT, 0.0, 0.7, 0.1),
    ("Station1", (-2.2, 0.5, 1.3), "box", (0.8, 1.0, 0.8), STEEL, 0.0, 0.35, 0.8),
    ("Station2", (0, 0.5, 1.5), "box", (0.8, 1.0, 0.8), STEEL, 0.0, 0.35, 0.8),
    ("Station3", (1.6, 0.5, 0.9), "box", (0.8, 1.0, 0.8), STEEL, 0.0, 0.35, 0.8),
    ("PickPoint", (-1.2, 0.55, -1.6), "sphere", (0.08,), GREEN, 0.0, 0.3, 0.0),
    ("PlacePoint", (1.6, 1.05, 0.9), "sphere", (0.08,), GREEN, 0.0, 0.3, 0.0),
    ("TempSensor1", (0, 0.95, 0.3), "box", (0.08, 0.08, 0.08), (0.9, 0.9, 0.9), 0.0, 0.3, 0.1),
    ("TempSensor2", (1.6, 1.1, 1.3), "box", (0.08, 0.08, 0.08), (0.9, 0.9, 0.9), 0.0, 0.3, 0.1),
    ("VibSensor1", (0, 0.55, -1.25), "box", (0.08, 0.08, 0.08), (0.9, 0.9, 0.9), 0.0, 0.3, 0.1),
    ("VibSensor2", (-2.2, 1.05, 1.3), "box", (0.08, 0.08, 0.08), (0.9, 0.9, 0.9), 0.0, 0.3, 0.1),
    ("LightCurtain", (3.2, 1.0, 0), "box", (0.05, 2.0, 1.2), (0.9, 0.15, 0.15), 0.6, 0.3, 0.0),
    ("SafetyFence", (0, 1.0, 3.0), "box", (7, 2.0, 0.05), FENCE, 0.5, 0.6, 0.3),
]
factory_views = [
    ("V_Overview", "Cell 7 overview", (5.5, 5.5, 7.5), (-0.58, 0.76, 0.29, 0.95), (0, 0.6, 0)),
    ("V_Robot", "Robot reach", (0, 6, 0.01), (-1, 0, 0, 1.5708), (0, 0.6, 0)),
]


def factory(fault=False):
    body = ""
    for d, p, k, dims, c, tr, rough, metal in F:
        if fault and d in ("Station3", "PlacePoint"):
            p = (p[0] + 0.9, p[1], p[2] + 0.4)
        body += obj(d, p, k, dims, c, tr, emis=0.12, rough=rough, metal=metal)
    # a slow joint motion on the arm, so the twin also has a ROUTE chain
    body += ("<TimeSensor DEF='ArmClock' cycleInterval='6' loop='true'/>"
             "<PositionInterpolator DEF='ArmSwing' key='0 0.5 1' keyValue='0.5 1.3 0 0.4 1.3 0.5 0.5 1.3 0'/>"
             "<ROUTE fromNode='ArmClock' fromField='fraction_changed' toNode='ArmSwing' toField='set_fraction'/>"
             "<ROUTE fromNode='ArmSwing' fromField='value_changed' toNode='Robot1Arm' toField='set_translation'/>")
    return doc("Cell 7, the line that checks its own layout", "https://example.org/scen/factory/scene.x3d",
               "Assembly cell; each asset is described by an Asset Administration Shell, sensors by SOSA with QUDT units.",
               ((0.06, 0.07, 0.09), (0.12, 0.12, 0.13)), factory_views, body)


# ---------------------------------------------------------------- hospital (OR 4)
WHITE, BLUE, TEAL, GREY = (0.92, 0.94, 0.96), (0.25, 0.45, 0.85), (0.2, 0.75, 0.7), (0.55, 0.58, 0.62)
Hs = [
    ("OR4Room", (0, 1.5, 0), "box", (7, 3, 6), (0.85, 0.9, 0.92), 0.85),
    ("SterileField", (0, 0.02, 0), "cyl", (1.6, 0.02), TEAL, 0.6),
    ("ORTable", (0, 0.45, 0), "box", (2.0, 0.9, 0.6), GREY, 0.0),
    ("AnesthesiaMachine", (-1.1, 0.8, -2.0), "box", (0.7, 1.6, 0.6), BLUE, 0.0),
    ("SurgicalLight1", (0.4, 2.6, 0), "cyl", (0.35, 0.1), WHITE, 0.0),
    ("SurgicalLight2", (-0.5, 2.6, 0.3), "cyl", (0.3, 0.1), WHITE, 0.0),
    ("CArm", (2.2, 1.0, 1.6), "box", (0.6, 2.0, 0.6), WHITE, 0.0),
    ("InstrumentTable", (0.9, 0.5, -0.9), "box", (0.8, 1.0, 0.5), GREY, 0.0),
    ("SupplyCart", (3.0, 0.5, -2.4), "box", (0.6, 1.0, 0.5), (0.7, 0.55, 0.35), 0.0),
    ("ScrubSink", (-3.2, 0.5, 2.4), "box", (0.5, 1.0, 1.2), WHITE, 0.0),
    ("DoorMain", (3.5, 1.1, 0), "box", (0.1, 2.2, 1.6), (0.5, 0.55, 0.6), 0.3),
]
hospital_views = [
    ("V_Overview", "Operating Room 4", (6, 6, 8), (-0.6, 0.75, 0.27, 0.9), (0, 0.8, 0)),
    ("V_Plan", "Plan view with the sterile field", (0, 11, 0.01), (-1, 0, 0, 1.5708), (0, 0, 0)),
]


def hospital(fault=False):
    body = ""
    for d, p, k, dims, c, tr in Hs:
        if fault and d == "SupplyCart":
            p = (1.1, 0.5, 0.6)          # non-sterile cart pushed inside the sterile field
        body += obj(d, p, k, dims, c, tr, emis=0.08, rough=0.5)
    return doc("Operating Room 4, ready or not", "https://example.org/scen/hospital/scene.x3d",
               "Operating room; spaces by BOT, devices and the scheduled procedure by HL7 FHIR.",
               ((0.08, 0.1, 0.12), (0.15, 0.16, 0.17)), hospital_views, body)


if __name__ == "__main__":
    out = {}
    for name, fn in (("heritage", heritage), ("factory", factory), ("hospital", hospital)):
        for flt in (False, True):
            p = OUT / (f"{name}{'-fault' if flt else ''}.x3d")
            p.write_text(fn(flt))
            out[p.name] = p.stat().st_size
    print(json.dumps(out))
