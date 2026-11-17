"""Round 2 scenes: building, warehouse, city, concert, firecode, farm (X3D 4.0, Immersive + Shape 2).
Same rule as round 1: flat, unrotated Transforms under Root, so world position = translation."""
import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenes import obj, doc, OUT

WALL, GLASS, STEEL, WHITE = (0.55, 0.6, 0.68), (0.55, 0.8, 1.0), (0.45, 0.48, 0.52), (0.9, 0.92, 0.94)
SENS, RED, BLUE, GREEN, AMBER = (0.95, 0.95, 0.95), (0.9, 0.2, 0.2), (0.25, 0.5, 0.95), (0.3, 0.8, 0.45), (1.0, 0.7, 0.25)

# ------------------------------------------------------------------ S4 building (Brick)
ROOM_X = [-12.5, -7.5, -2.5, 2.5, 7.5, 12.5]
BUILDING = {"rooms": {f"Room30{i+1}": (x, 1.5, -3.0) for i, x in enumerate(ROOM_X)}, "room_size": (4.9, 3.0, 5.9)}


def building(fault=False):
    b = obj("Corridor3", (0, 1.5, 1.5), "box", (30, 3, 3), WALL, 0.85, emis=0.05)
    for i, x in enumerate(ROOM_X):
        n = f"30{i+1}"
        b += obj(f"Room{n}", (x, 1.5, -3.0), "box", BUILDING["room_size"], WALL, 0.7, emis=0.06)
        b += obj(f"Window{n}", (x, 1.6, -5.97), "box", (3, 1.4, 0.06), GLASS, 0.4, emis=0.2)
        b += obj(f"VAV{n}", (x, 3.2, -1.0), "box", (0.8, 0.4, 0.6), STEEL, 0.0, emis=0.1, metal=0.6)
        tx = x + 1.5
        if fault and n == "303":
            tx = 1.5                       # commissioning error: Room 303's sensor is mounted in Room 304
        b += obj(f"TempSensor{n}", (tx, 1.4, -5.5), "box", (0.12, 0.12, 0.05), SENS, 0.0, emis=0.3)
        b += obj(f"OccSensor{n}", (x, 2.95, -3.0), "cyl", (0.1, 0.06), SENS, 0.0, emis=0.3)
    b += obj("AHU1", (-8, 4.3, 4.2), "box", (3, 1.2, 1.6), STEEL, 0.0, emis=0.08, metal=0.5)
    b += obj("AHU2", (8, 4.3, 4.2), "box", (3, 1.2, 1.6), STEEL, 0.0, emis=0.08, metal=0.5)
    b += obj("Duct1", (-7.5, 3.6, 1.0), "box", (15, 0.35, 0.35), STEEL, 0.3, emis=0.05)
    b += obj("Duct2", (7.5, 3.6, 1.0), "box", (15, 0.35, 0.35), STEEL, 0.3, emis=0.05)
    views = [("V_Overview", "Floor 3 from the south", (0, 17, 26), (1, 0, 0, -0.58), (0, 1.5, -1)),
             ("V_Plan", "Plan view", (0, 34, 0.01), (-1, 0, 0, 1.5708), (0, 0, -1))]
    return doc("Floor 3 heats an empty room", "https://example.org/scen/building/scene.x3d",
               "Office floor with air handlers, VAV boxes and room sensors; the points and the air path are in Brick.",
               ((0.03, 0.04, 0.07), (0.08, 0.08, 0.09)), views, b)


# ------------------------------------------------------------------ S5 warehouse (GS1)
def bins():
    out = {}
    for r, z in (("A", -4.0), ("B", 0.0)):
        for bay, x in ((1, -4.0), (2, -2.7), (3, -1.4)):
            for lvl, y in ((1, 0.65), (2, 1.95)):
                out[f"Bin{r}{lvl}{bay}"] = (x, y, z)
    for k, z in ((1, -2.4), (2, -0.8), (3, 0.8), (4, 2.4)):
        out[f"BinK{k}"] = (5.0, 0.65, z)
    return out


BINS = bins()
PALLETS = {"P01": "BinA11", "P02": "BinA12", "P03": "BinA13", "P04": "BinB11", "P05": "BinK1", "P06": "BinK2", "P07": "BinK3", "P08": "BinK4", "P09": "BinA21"}


def warehouse(fault=False):
    b = obj("ColdRoom", (5.0, 1.5, 0.0), "box", (3.0, 3.0, 7.0), BLUE, 0.82, emis=0.15)
    b += obj("Dock1", (-7.0, 1.6, -2.0), "box", (0.15, 3.2, 3.0), AMBER, 0.2, emis=0.15)
    b += obj("Dock2", (-7.0, 1.6, 2.5), "box", (0.15, 3.2, 3.0), AMBER, 0.2, emis=0.15)
    for n, (x, y, z) in BINS.items():
        b += obj(n, (x, y, z), "box", (1.25, 1.25, 1.05), STEEL, 0.88, emis=0.05)
    PAL = {"P01": (0.8, 0.65, 0.4), "P02": (0.75, 0.35, 0.3), "P03": (0.8, 0.65, 0.4), "P04": (0.6, 0.6, 0.62),
           "P05": (0.95, 0.95, 0.9), "P06": (0.95, 0.95, 0.9), "P07": (1.0, 0.6, 0.2), "P08": (0.95, 0.95, 0.9), "P09": (0.75, 0.35, 0.3)}
    for p, bn in PALLETS.items():
        x, y, z = BINS[bn]
        if fault and p == "P07":
            x, y, z = BINS["BinB22"]       # the mixed pallet with yoghurt is put away in ambient Rack B
        if fault and p == "P03":
            x, y, z = BINS["BinA23"]       # drawn one bin over from where the record says it is
        b += obj(p, (x, y - 0.12, z), "box", (1.0, 0.95, 0.9), PAL[p], 0.0, emis=0.12, rough=0.8)
    views = [("V_Overview", "Warehouse aisle", (0, 9, 13), (1, 0, 0, -0.55), (0, 1, 0)),
             ("V_Plan", "Plan view", (0, 16, 0.01), (-1, 0, 0, 1.5708), (0, 0, 0))]
    return doc("Recall lot L-2291 before lunch", "https://example.org/scen/warehouse/scene.x3d",
               "Racks, bins, pallets and a cold room; products, lots and expiry dates are in the GS1 Web Vocabulary.",
               ((0.04, 0.04, 0.06), (0.1, 0.1, 0.1)), views, b)


# ------------------------------------------------------------------ S6 city (GeoSPARQL)
CITY = {  # building: (x, z, width_x, depth_z, height, parcel)
    "Bldg1": (-60, -20, 20, 20, 12, "Parcel1"), "Bldg2": (-20, 10, 20, 20, 18, "Parcel2"), "Tower5": (20, -20, 20, 20, 36, "Parcel5"),
    "Bldg3": (60, 10, 20, 20, 9, "Parcel3"), "Bldg4": (-60, 50, 20, 20, 24, "Parcel4"), "Bldg6": (40, 60, 20, 20, 15, "Parcel6")}
PARCEL = 30.0


def city(fault=False):
    b = obj("Park", (0, 0.1, -80), "box", (80, 0.2, 40), GREEN, 0.0, emis=0.15)
    b += obj("FloodZone", (65, 0.3, 50), "box", (70, 0.4, 100), BLUE, 0.75, emis=0.2)
    b += obj("TransitStop", (-400, 4, 100), "cyl", (6, 8), AMBER, 0.0, emis=0.4)
    b += obj("Street", (0, 0.05, 30), "box", (200, 0.1, 10), (0.25, 0.25, 0.28), 0.0, emis=0.05)
    for n, (x, z, w, d, h, p) in CITY.items():
        b += obj(p, (x, 0.15, z), "box", (PARCEL, 0.1, PARCEL), (0.4, 0.42, 0.36), 0.3, emis=0.05)
        if fault and n == "Tower5":
            h = 48                          # drawn 48 m in a 36 m district
        if fault and n == "Bldg2":
            x = x + 8                       # overhangs its parcel by 3 m
        b += obj(n, (x, h / 2, z), "box", (w, h, d), (0.78, 0.76, 0.72), 0.0, emis=0.08, rough=0.7)
    views = [("V_Overview", "The block and the park", (60, 140, 190), (-0.98, 0.18, 0.05, 0.62), (0, 0, 0)),
             ("V_Transit", "Out to the transit stop", (-260, 160, 300), (-0.8, -0.58, -0.17, 0.62), (-200, 0, 50))]
    return doc("The block that knows its shadows", "https://example.org/scen/city/scene.x3d",
               "A city block: parcels, buildings, a park, a flood zone and a transit stop; features and zoning in GeoSPARQL.",
               ((0.05, 0.07, 0.12), (0.06, 0.07, 0.06)), views, b)


# ------------------------------------------------------------------ S7 concert (Music Ontology)
STAGE = {
    "PerfVox": ((0, 0.9, 1.0), "cyl", (0.3, 1.8)), "PerfGtr": ((-3, 0.9, 0.0), "cyl", (0.3, 1.8)), "PerfBass": ((3, 0.9, 0.0), "cyl", (0.3, 1.8)),
    "PerfDrums": ((0, 1.3, -2.5), "cyl", (0.3, 1.2)), "PerfKeys": ((-4.5, 0.9, -2.0), "cyl", (0.3, 1.8)),
    "GuitarAmp": ((-3.5, 0.4, -1.0), "box", (0.7, 0.8, 0.35)), "BassAmp": ((3.5, 0.5, -1.0), "box", (0.7, 1.0, 0.45)),
    "DrumKit": ((0, 0.75, -2.0), "cyl", (0.8, 0.5)), "KeysRig": ((-4.5, 0.9, -1.4), "box", (1.3, 0.1, 0.4)),
    "AcousticGtr": ((-1.8, 0.6, 0.6), "box", (0.4, 1.0, 0.12)),
    "MicVox": ((0, 1.5, 1.5), "sphere", (0.06,)), "MicGtrAmp": ((-3.5, 0.4, -0.65), "sphere", (0.05,)), "DIBass": ((3.5, 0.15, -0.6), "box", (0.15, 0.08, 0.1)),
    "MicKick": ((0, 0.35, -1.4), "sphere", (0.05,)), "DIKeys": ((-4.5, 0.6, -1.1), "box", (0.15, 0.08, 0.1)), "MicAcoustic": ((-1.8, 0.9, 1.0), "sphere", (0.05,)),
    "WedgeVox": ((0, 0.2, 2.6), "box", (0.7, 0.35, 0.5)), "WedgeGtr": ((-3, 0.2, 1.6), "box", (0.7, 0.35, 0.5)), "WedgeBass": ((3, 0.2, 1.6), "box", (0.7, 0.35, 0.5)),
    "WedgeKeys": ((-4.5, 0.2, -0.4), "box", (0.7, 0.35, 0.5)), "DrumFill": ((1.3, 0.9, -2.6), "box", (0.6, 0.8, 0.5)),
    "Spot1": ((-8, 7, 10), "cyl", (0.25, 0.6)), "Spot2": ((8, 7, 10), "cyl", (0.25, 0.6)), "FOH": ((0, 0.6, 18), "box", (2.4, 1.2, 1.2)),
}


def concert(fault=False):
    b = obj("Stage", (0, -0.1, 0), "box", (12, 0.2, 8), (0.2, 0.2, 0.24), 0.0, emis=0.05)
    b += obj("DrumRiser", (0, 0.2, -2.5), "box", (3, 0.4, 2.2), (0.25, 0.25, 0.3), 0.0, emis=0.05)
    C = {"Perf": (0.95, 0.75, 0.55), "Mic": SENS, "DI": SENS, "Wedge": (0.3, 0.3, 0.34), "Spot": WHITE}
    for n, (p, k, dims) in STAGE.items():
        if fault and n == "MicGtrAmp":
            p = (-3.5, 0.4, 1.0)            # the amp mic was kicked 2 m downstage
        if fault and n == "WedgeKeys":
            p = (4.5, 0.2, 1.6)             # the keys wedge was set on the bass side
        col = next((v for k2, v in C.items() if n.startswith(k2)), (0.6, 0.45, 0.3))
        b += obj(n, p, k, dims, col, 0.0, emis=0.15)
    views = [("V_Overview", "From the house", (0, 6, 16), (-1, 0, 0, 0.3), (0, 1, 0)),
             ("V_Plot", "Stage plot (plan)", (0, 18, 2), (-1, 0, 0, 1.5708), (0, 0, 2))]
    return doc("A stage plot that knows the setlist", "https://example.org/scen/concert/scene.x3d",
               "Stage plot: performers, instruments, mics, wedges and followspots; the show, sets and songs in the Music Ontology.",
               ((0.02, 0.01, 0.04), (0.04, 0.03, 0.05)), views, b)


# ------------------------------------------------------------------ S8 fire code (BOT + code clauses)
FIRE = {  # room: (x, z, width_x, depth_z)
    "Room401": (-50, -7.2, 20, 9.6), "Room402": (-30, -7.2, 20, 9.6), "Room403": (-10, -7.2, 20, 9.6),
    "Room404": (10, -7.2, 20, 9.6), "Room405": (30, -7.2, 20, 9.6), "Room406": (50, -7.2, 20, 9.6),
    "Room407": (-20, 7.2, 80, 9.6), "Room408": (40, 7.2, 40, 9.6)}
DOORS = {  # door: (x, z, width, axis, rooms)
    "Door401": (-50, -2.4, 0.91, 0, ("Room401",)), "Door402": (-30, -2.4, 0.91, 0, ("Room402",)), "Door403": (-10, -2.4, 0.91, 0, ("Room403",)),
    "Door404": (10, -2.4, 0.91, 0, ("Room404",)), "Door405": (30, -2.4, 0.91, 0, ("Room405",)), "Door406": (50, -2.4, 0.91, 0, ("Room406",)),
    "Door407a": (-40, 2.4, 0.91, 0, ("Room407",)), "Door407b": (0, 2.4, 0.91, 0, ("Room407",)), "Door408": (40, 2.4, 0.91, 0, ("Room408",)),
    "DoorStairA": (-60, 0, 1.0, 2, ("StairA",)), "DoorStairB": (60, 0, 1.0, 2, ("StairB",))}


def firecode(fault=False):
    b = obj("Corridor4", (0, 1.5, 0), "box", (120, 3, 4.8), WALL, 0.85, emis=0.05)
    for n, (x, z, w, d) in FIRE.items():
        b += obj(n, (x, 1.5, z), "box", (w - 0.2, 3, d - 0.2), WALL, 0.75, emis=0.06)
    b += obj("StairA", (-63, 1.5, 0), "box", (6, 3, 6), GREEN, 0.5, emis=0.2)
    b += obj("StairB", (63, 1.5, 0), "box", (6, 3, 6), GREEN, 0.5, emis=0.2)
    for n, (x, z, wd, ax, _) in DOORS.items():
        if fault and n == "Door402":
            wd = 0.76                       # a 30 in door where the code asks for 32 in clear
        size = (wd, 2.1, 0.12) if ax == 0 else (0.12, 2.1, wd)
        b += obj(n, (x, 1.05, z), "box", size, AMBER, 0.0, emis=0.25)
    views = [("V_Overview", "Floor 4", (0, 95, 115), (1, 0, 0, -0.69), (0, 0, 0)),
             ("V_Plan", "Plan view", (0, 110, 0.01), (-1, 0, 0, 1.5708), (0, 0, 0))]
    return doc("Can everyone get out of Floor 4?", "https://example.org/scen/firecode/scene.x3d",
               "Office floor: rooms, corridor, doors and two exit stairs; spaces and doors in BOT, code clauses as data.",
               ((0.04, 0.03, 0.03), (0.08, 0.07, 0.07)), views, b)


# ------------------------------------------------------------------ S9 farm (AGROVOC + SOSA)
BEDS = {f"Bed{r}{c}": (x, z) for r, z in (("A", -4.0), ("B", 0.0), ("C", 4.0)) for c, x in ((1, -7.0), (2, 0.0), (3, 7.0))}
CROP_COL = {"tomatoes": (0.85, 0.25, 0.2), "cucumbers": (0.35, 0.7, 0.3), "lettuces": (0.55, 0.85, 0.35), "basil": (0.25, 0.55, 0.3)}
CROPS = {"BedA1": "tomatoes", "BedA2": "tomatoes", "BedA3": "cucumbers", "BedB1": "lettuces", "BedB2": "lettuces", "BedB3": "basil",
         "BedC1": "tomatoes", "BedC2": "lettuces", "BedC3": "basil"}


def farm(fault=False):
    b = obj("Greenhouse2", (0, 2.0, 0), "box", (24, 4, 12), GLASS, 0.88, emis=0.1)
    for n, (x, z) in BEDS.items():
        b += obj(n, (x, 0.2, z), "box", (6, 0.4, 1.2), (0.36, 0.25, 0.16), 0.0, emis=0.05, rough=0.9)
        b += obj(f"Crop{n[3:]}", (x, 0.6, z), "box", (5.6, 0.4, 0.9), CROP_COL[CROPS[n]], 0.0, emis=0.15)
        sx = x + 2.0
        if fault and n == "BedB2":
            sx = 7.0 + 2.0                  # Bed B2's moisture probe is pushed into Bed B3
        b += obj(f"Moist{n[3:]}", (sx, 0.45, z + 0.45), "cyl", (0.03, 0.5), SENS, 0.0, emis=0.3)
        b += obj(f"Valve{n[3:]}", (x - 3.3, 0.3, z), "cyl", (0.08, 0.2), BLUE, 0.0, emis=0.3)
    b += obj("RH1", (-5, 1.6, -4.0), "box", (0.15, 0.15, 0.1), SENS, 0.0, emis=0.3)
    b += obj("RH2", (5, 1.6, 4.0), "box", (0.15, 0.15, 0.1), SENS, 0.0, emis=0.3)
    b += obj("VentA", (0, 4.05, -4.0), "box", (8, 0.1, 1.5), STEEL, 0.4, emis=0.1)
    b += obj("VentC", (0, 4.05, 4.0), "box", (8, 0.1, 1.5), STEEL, 0.4, emis=0.1)
    views = [("V_Overview", "Greenhouse 2", (14, 12, 16), (-0.75, 0.62, 0.23, 0.85), (0, 0.5, 0)),
             ("V_Plan", "Plan view", (0, 22, 0.01), (-1, 0, 0, 1.5708), (0, 0, 0))]
    return doc("Greenhouse 2 waters what is thirsty", "https://example.org/scen/farm/scene.x3d",
               "Greenhouse beds with moisture probes, valves, humidity sensors and vents; crops as AGROVOC concepts, readings in SOSA.",
               ((0.05, 0.08, 0.06), (0.08, 0.07, 0.05)), views, b)


CASES2 = (("building", building), ("warehouse", warehouse), ("city", city), ("concert", concert), ("firecode", firecode), ("farm", farm))

if __name__ == "__main__":
    out = {}
    for name, fn in CASES2:
        for flt in (False, True):
            p = OUT / f"{name}{'-fault' if flt else ''}.x3d"
            p.write_text(fn(flt)); out[p.name] = p.stat().st_size
    print(json.dumps(out))
