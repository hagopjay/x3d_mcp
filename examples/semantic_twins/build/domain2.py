"""Round 2 domain graphs, questions, rules and shapes: building (Brick), warehouse (GS1), city (GeoSPARQL),
concert (Music Ontology + Timeline + Event), firecode (BOT + code clauses), farm (AGROVOC + SOSA + QUDT)."""
import json, math, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from domain import prefixes, sparql_prefixes, DATA
from scenes2 import BUILDING, BINS, PALLETS, CITY, PARCEL, STAGE, FIRE, DOORS, BEDS, CROPS

ROOT = Path(__file__).resolve().parents[1]
X3DO = "https://www.web3d.org/specifications/X3dOntology4.1#"
J = json.dumps


def sc(name):
    return f"https://example.org/scen/{name}/scene.ttl#"


# SPARQL fragments: x / z (and y) of a Transform, and the size of its Box
# Each fragment is a sub-select over the scene: rdflib reorders a flat pattern badly when the focus node is not yet
# bound (pyshacl took 96 s on the city checks); as sub-selects the same checks run in well under a second.
def XZ(t):
    return (f"{{ SELECT ?{t} ?{t}x ?{t}z WHERE {{ ?{t} x3do:translation ?{t}L . ?{t}L rdf:first ?{t}x . ?{t}L rdf:rest ?{t}R . "
            f"?{t}R rdf:rest ?{t}R2 . ?{t}R2 rdf:first ?{t}z . }} }}")


def XYZ(t):
    return (f"{{ SELECT ?{t} ?{t}x ?{t}y ?{t}z WHERE {{ ?{t} x3do:translation ?{t}L . ?{t}L rdf:first ?{t}x . ?{t}L rdf:rest ?{t}R . "
            f"?{t}R rdf:first ?{t}y . ?{t}R rdf:rest ?{t}R2 . ?{t}R2 rdf:first ?{t}z . }} }}")


def BOX(t):
    return (f"{{ SELECT ?{t} ?{t}sx ?{t}sy ?{t}sz WHERE {{ ?{t} x3do:hasChildren ?{t}H . ?{t}H x3do:hasGeometry ?{t}B . ?{t}B x3do:size ?{t}S . "
            f"?{t}S rdf:first ?{t}sx . ?{t}S rdf:rest ?{t}SR . ?{t}SR rdf:first ?{t}sy . ?{t}SR rdf:rest ?{t}SR2 . ?{t}SR2 rdf:first ?{t}sz . }} }}")


SHP = """    PREFIX x3do: <https://www.web3d.org/specifications/X3dOntology4.1#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX twin: <https://example.org/scen/twin#>
"""


def shape(sid, target, message, select, extra_prefixes=""):
    return (f"twin:{sid} a sh:NodeShape ; {target} ;\n  sh:severity sh:Violation ;\n  sh:message {J(message)} ;\n"
            f"  sh:sparql [ sh:select \"\"\"\n{SHP}{extra_prefixes}    {select.replace('SELECT $this', 'SELECT DISTINCT $this', 1).replace('{ SELECT ?', '{ SELECT $this ?')}\"\"\" ] .\n\n")


# ================================================================== S4 BUILDING (Brick)
BX = {"brick": "https://brickschema.org/schema/Brick#", "unit": "http://qudt.org/vocab/unit/", "b": "https://example.org/scen/building/record#", "sc": sc("building")}
ROOM_STATE = {  # room: (zone air temp degC, setpoint degC, occupied 0/1)
    "301": (21.4, 21.0, 1), "302": (23.6, 21.0, 0), "303": (20.8, 21.0, 1), "304": (22.4, 21.0, 0), "305": (18.4, 21.0, 1), "306": (21.2, 21.0, 0)}


def building_domain(fault=False):
    t = prefixes(BX)
    t += "b:HQ a brick:Building ; rdfs:label \"Head office\" ; brick:hasPart b:Floor3 .\nb:Floor3 a brick:Floor ; rdfs:label \"Floor 3\" .\n"
    for a, rooms in (("AHU1", "301 302 303"), ("AHU2", "304 305 306")):
        t += f"b:{a} a brick:AHU ; rdfs:label \"Air handler {a[-1]}\" ; twin:depictedBy sc:{a} ; brick:feeds " + ", ".join(f"b:VAV{r}" for r in rooms.split()) + " .\n"
    for r, (temp, sp, occ) in ROOM_STATE.items():
        t += (f"b:Room{r} a brick:Room ; rdfs:label \"Room {r}\" ; twin:depictedBy sc:Room{r} ; brick:isPartOf b:Floor3 .\n"
              f"b:Zone{r} a brick:HVAC_Zone ; rdfs:label \"Zone {r}\" ; brick:hasPart b:Room{r} .\n"
              f"b:VAV{r} a brick:VAV ; rdfs:label \"VAV box {r}\" ; twin:depictedBy sc:VAV{r} ; brick:feeds b:Zone{r} ; brick:hasPoint b:SP{r} .\n"
              f"b:SP{r} a brick:Zone_Air_Temperature_Setpoint ; brick:value {sp} ; brick:hasUnit unit:DEG_C .\n"
              f"b:OS{r} a brick:Occupancy_Sensor ; brick:hasLocation b:Room{r} ; twin:depictedBy sc:OccSensor{r} ; brick:value {occ} .\n")
        if fault and r == "306":
            continue                         # planted: Room 306's temperature point was deleted from the model
        t += f"b:TS{r} a brick:Zone_Air_Temperature_Sensor ; brick:hasLocation b:Room{r} ; twin:depictedBy sc:TempSensor{r} ; brick:value {temp} ; brick:hasUnit unit:DEG_C .\n"
    return t


BQ = [
    {"id": "B1", "title": "Which empty rooms are being heated anyway?",
     "why": "Brick says which sensor reads which room and what the setpoint is; the scene says where the room is. A room at least 1 K over setpoint with nobody in it is energy paid for nothing, and it turns red on the plan.",
     "lights": "o", "sparql": """SELECT ?o ?room ?temp ?setpoint WHERE {
  ?ts a brick:Zone_Air_Temperature_Sensor ; brick:hasLocation ?r ; brick:value ?temp .
  ?os a brick:Occupancy_Sensor ; brick:hasLocation ?r ; brick:value 0 .
  ?vav brick:feeds/brick:hasPart ?r ; brick:hasPoint ?sp . ?sp a brick:Zone_Air_Temperature_Setpoint ; brick:value ?setpoint .
  ?r rdfs:label ?room ; twin:depictedBy ?o .
  FILTER(?temp >= ?setpoint + 1)
} ORDER BY ?room"""},
    {"id": "B2", "title": "If air handler 1 trips, what loses air?",
     "why": "The air path (AHU feeds VAV feeds zone, which contains the room) lives only in Brick. Walked as a path, it lights the boxes and rooms that go warm, before the complaint calls start.",
     "lights": "o", "sparql": """SELECT ?o ?thing WHERE {
  b:AHU1 brick:feeds+ ?x .
  { ?x twin:depictedBy ?o ; rdfs:label ?thing } UNION { ?x brick:hasPart ?r . ?r twin:depictedBy ?o ; rdfs:label ?thing }
} ORDER BY ?thing"""},
    {"id": "B3", "title": "Which rooms are more than 2 K off setpoint?",
     "why": "Comfort complaints, found before they are made. The answer is a heat map of the floor, warm and cold together.",
     "lights": "o", "sparql": """SELECT ?o ?room ?temp ?setpoint WHERE {
  ?ts a brick:Zone_Air_Temperature_Sensor ; brick:hasLocation ?r ; brick:value ?temp .
  ?vav brick:feeds/brick:hasPart ?r ; brick:hasPoint ?sp . ?sp a brick:Zone_Air_Temperature_Setpoint ; brick:value ?setpoint .
  ?r rdfs:label ?room ; twin:depictedBy ?o .
  FILTER(ABS(?temp - ?setpoint) > 2)
} ORDER BY ?room"""},
]
BINF = {"title": "Which rooms does each air handler serve?",
        "why": "Brick's feeds is a chain: air handler, VAV box, zone. One rule closes the chain, and every room lights in the colour of the unit that serves it.",
        "sparql": """SELECT ?o ?room ?ahu WHERE {
  ?a a brick:AHU ; rdfs:label ?ahu ; brick:feeds+ ?z . ?z brick:hasPart ?r . ?r rdfs:label ?room ; twin:depictedBy ?o .
} ORDER BY ?room""",
        "n3": "{ ?a brick:feeds ?b . ?b brick:feeds ?c } => { ?a brick:feeds ?c } ."}
BSHAPES = prefixes(BX) + shape("S_B1", "sh:targetClass brick:Zone_Air_Temperature_Sensor",
    "This temperature sensor is drawn outside the room it is recorded for, so the room is controlled by its neighbour's air.",
    """SELECT $this ?value WHERE {
      $this brick:hasLocation ?r ; twin:depictedBy ?value . ?r twin:depictedBy ?ro .
      """ + XZ("value") + " " + XZ("ro") + " " + BOX("ro") + """
      FILTER(ABS(?valuex - ?rox) > ?rosx / 2 || ABS(?valuez - ?roz) > ?rosz / 2)
    }""", "    PREFIX brick: <https://brickschema.org/schema/Brick#>\n") + shape("S_B2", "sh:targetClass brick:Room",
    "This room is drawn and served, but the model has no temperature sensor for it.",
    """SELECT $this WHERE {
      $this twin:depictedBy ?o .
      FILTER NOT EXISTS { ?ts a brick:Zone_Air_Temperature_Sensor ; brick:hasLocation $this }
    }""", "    PREFIX brick: <https://brickschema.org/schema/Brick#>\n")

# ================================================================== S5 WAREHOUSE (GS1)
WX = {"gs1": "https://ref.gs1.org/voc/", "w": "https://example.org/scen/warehouse/record#", "sc": sc("warehouse")}
PRODUCTS = {  # id: (gtin, name, chilled?)
    "Yoghurt": ("09506000134352", "Strawberry yoghurt 500 g", True), "Juice": ("09506000134369", "Orange juice 1 l", True),
    "Cereal": ("09506000134376", "Oat cereal 750 g", False), "Tomatoes": ("09506000134383", "Chopped tomatoes 400 g", False)}
LOTS = {"L-2291": ("Yoghurt", "2026-11-20"), "L-2304": ("Yoghurt", "2026-11-28"), "L-1180": ("Juice", "2026-11-19"),
        "L-0815": ("Cereal", "2027-03-01"), "L-0790": ("Cereal", "2027-01-15"), "L-0402": ("Tomatoes", "2028-06-01"), "L-0419": ("Tomatoes", "2028-07-01")}
PALLET_CASES = {  # pallet: [(case id, lot)]
    "P01": [("C011", "L-0815")], "P02": [("C021", "L-0402")], "P03": [("C031", "L-0790")], "P04": [("C041", "L-0402"), ("C042", "L-0815")],
    "P05": [("C051", "L-2291")], "P06": [("C061", "L-2291")], "P07": [("C071", "L-1180"), ("C072", "L-2291")], "P08": [("C081", "L-2304")],
    "P09": [("C091", "L-0419")]}


def warehouse_domain(fault=False):
    t = prefixes(WX)
    t += "w:DC1 a gs1:Place ; rdfs:label \"Distribution centre 1\" ; gs1:globalLocationNumber \"9506000000019\" .\n"
    t += "w:ColdRoom a gs1:Place ; rdfs:label \"Cold room (2 to 8 °C)\" ; twin:depictedBy sc:ColdRoom ; twin:minC 2 ; twin:maxC 8 .\n"
    for pid, (gtin, name, chilled) in PRODUCTS.items():
        t += (f"w:{pid} a gs1:Product ; gs1:gtin \"{gtin}\" ; gs1:productName {J(name)}@en ; rdfs:label {J(name)} ;"
              f" gs1:consumerHandlingStorage {J('Keep refrigerated, 2 to 8 C' if chilled else 'Store in a cool, dry place')}@en"
              + (" ; twin:storeMinC 2 ; twin:storeMaxC 8" if chilled else "") + " .\n")
    for lot, (pid, exp) in LOTS.items():
        t += f"w:lot_{lot.replace('-', '')} a twin:Lot ; rdfs:label \"{lot}\" ; twin:ofProduct w:{pid} ; gs1:hasBatchLotNumber \"{lot}\" ; gs1:expirationDate \"{exp}\"^^xsd:date .\n"
    for b in BINS:
        t += f"w:{b} a gs1:Place ; rdfs:label \"{b[3:]}\" ; twin:depictedBy sc:{b} .\n"
    for p, cases in PALLET_CASES.items():
        t += f"w:{p} a twin:Pallet ; rdfs:label \"Pallet {p}\" ; twin:depictedBy sc:{p} ; twin:storedIn w:{PALLETS[p]} ; twin:contains " + ", ".join(f"w:{c}" for c, _ in cases) + " .\n"
        for c, lot in cases:
            t += f"w:{c} a twin:Case ; rdfs:label \"Case {c}\" ; twin:contains w:lot_{lot.replace('-', '')} .\n"
    t += "w:Recall2291 a twin:Recall ; rdfs:label \"Recall of lot L-2291 (supplier notice, 16 Nov 2026)\" ; twin:recalls w:lot_L2291 .\n"
    t += "w:today twin:date \"2026-11-16\"^^xsd:date .\n"
    return t


WQ = [
    {"id": "W1", "title": "Where is every pallet holding lot L-2291?",
     "why": "The recall names a lot; the warehouse stores pallets. GS1 identifies the lot, the record says which case and pallet hold it, and the scene shows the bins to empty. Pallet P07 is mostly juice, but one case of the recalled yoghurt is on it.",
     "lights": "o", "sparql": """SELECT ?o ?pallet ?bin WHERE {
  w:Recall2291 twin:recalls ?lot .
  ?p a twin:Pallet ; twin:contains+ ?lot ; rdfs:label ?pallet ; twin:depictedBy ?o ; twin:storedIn/rdfs:label ?bin .
} ORDER BY ?pallet"""},
    {"id": "W2", "title": "Which cereal should be picked first (first expiry, first out)?",
     "why": "Two lots of the same cereal, two expiry dates, two bins. The pick list comes out in expiry order, and the first bin is lit for the picker.",
     "lights": "o", "sparql": """SELECT ?o ?pallet ?lot ?expires WHERE {
  ?p a twin:Pallet ; twin:contains+ ?l ; rdfs:label ?pallet ; twin:depictedBy ?o .
  ?l twin:ofProduct w:Cereal ; rdfs:label ?lot ; gs1:expirationDate ?expires .
} ORDER BY ?expires LIMIT 1"""},
    {"id": "W3", "title": "Which bins are free for put-away?",
     "why": "Absence again: a bin that no pallet record points to. The scene turns that into spots on the racks a driver can see.",
     "lights": "o", "sparql": """SELECT ?o ?bin WHERE {
  ?b a gs1:Place ; twin:depictedBy ?o ; rdfs:label ?bin .
  FILTER(STRSTARTS(STR(?b), STR(w:Bin)))
  FILTER NOT EXISTS { ?p twin:storedIn ?b }
} ORDER BY ?bin"""},
]
WINF = {"title": "A recall reaches through pallets and cases",
        "why": "Contains is transitive: a pallet holds cases, a case holds a lot. One rule closes the chain, so asking about the lot reaches every pallet, mixed ones included.",
        "sparql": """SELECT ?o ?pallet WHERE {
  ?p a twin:Pallet ; twin:contains ?c ; rdfs:label ?pallet ; twin:depictedBy ?o . ?c twin:contains w:lot_L2291 .
} ORDER BY ?pallet""",
        "n3": "{ ?a twin:contains ?b . ?b twin:contains ?c } => { ?a twin:contains ?c } ."}
WSHAPES = prefixes(WX) + shape("S_W1", "sh:targetClass twin:Pallet",
    "A pallet holding a chilled product is drawn outside the cold room.",
    """SELECT $this ?value WHERE {
      $this twin:contains+ ?lot ; twin:depictedBy ?value . ?lot twin:ofProduct ?prod . ?prod twin:storeMaxC ?max .
      ?cold twin:minC ?cmin ; twin:depictedBy ?co .
      """ + XZ("value") + " " + XZ("co") + " " + BOX("co") + """
      FILTER(ABS(?valuex - ?cox) > ?cosx / 2 || ABS(?valuez - ?coz) > ?cosz / 2)
    }""") + shape("S_W2", "sh:targetClass twin:Pallet",
    "The pallet is drawn in a different bin from the one the warehouse record assigns.",
    """SELECT $this ?value WHERE {
      $this twin:storedIn ?bin ; twin:depictedBy ?po . ?bin twin:depictedBy ?value .
      """ + XYZ("po") + " " + XYZ("value") + """
      FILTER(ABS(?pox - ?valuex) > 0.3 || ABS(?poz - ?valuez) > 0.3 || ABS(?poy - ?valuey) > 0.5)
    }""")

# ================================================================== S6 CITY (GeoSPARQL)
GX = {"geo": "http://www.opengis.net/ont/geosparql#", "c": "https://example.org/scen/city/record#", "sc": sc("city")}
E0, N0 = 583000.0, 4507000.0            # local origin in UTM 18N (EPSG:32618): x -> easting, -z -> northing
CRS = "<http://www.opengis.net/def/crs/EPSG/0/32618> "
DISTRICT = {"C4": ("Commercial district C4-7", 36, ["Tower5", "Bldg1", "Bldg3"]), "R6": ("Residential district R6", 24, ["Bldg2", "Bldg4", "Bldg6"])}
SUN = 90 - 40.7 - 23.44                 # noon solar elevation on the December solstice at 40.7 N, degrees


def wkt(x, z, w, d):
    e, n = E0 + x, N0 - z
    pts = [(e - w / 2, n - d / 2), (e + w / 2, n - d / 2), (e + w / 2, n + d / 2), (e - w / 2, n + d / 2), (e - w / 2, n - d / 2)]
    return CRS + "POLYGON((" + ", ".join(f"{a:.1f} {b:.1f}" for a, b in pts) + "))"


def city_domain(fault=False):
    t = prefixes(GX)
    t += (f"c:Sun a twin:SolarCondition ; rdfs:label \"Noon, December solstice, 40.7° N\" ; twin:elevationDeg {SUN:.2f} ;"
          f" twin:shadowPerMetre {1 / math.tan(math.radians(SUN)):.4f} .\n")
    for k, (label, mx, members) in DISTRICT.items():
        t += f"c:{k} a geo:Feature ; rdfs:label {J(label)} ; twin:maxHeightM {mx} .\n"
    t += "c:Block12 a geo:Feature ; rdfs:label \"Block 12\" .\n"
    for n, (x, z, w, d, h, p) in CITY.items():
        dist = next(k for k, v in DISTRICT.items() if n in v[2])
        t += (f"c:{p} a geo:Feature ; rdfs:label \"{p.replace('Parcel', 'Parcel ')}\" ; twin:depictedBy sc:{p} ; geo:sfWithin c:Block12, c:{dist} ;"
              f" geo:hasGeometry [ a geo:Geometry ; geo:asWKT \"{wkt(x, z, PARCEL, PARCEL)}\"^^geo:wktLiteral ] .\n")
        t += (f"c:{n} a geo:Feature ; rdfs:label \"{n.replace('Bldg', 'Building ').replace('Tower', 'Tower ')}\" ; twin:depictedBy sc:{n} ; geo:sfWithin c:{p} ;"
              f" twin:permitHeightM {h} ; geo:hasGeometry [ a geo:Geometry ; geo:asWKT \"{wkt(x, z, w, d)}\"^^geo:wktLiteral ] .\n")
    t += (f"c:Park a geo:Feature ; rdfs:label \"Washington Green (park)\" ; twin:depictedBy sc:Park ; geo:hasGeometry [ a geo:Geometry ; geo:asWKT \"{wkt(0, -80, 80, 40)}\"^^geo:wktLiteral ] .\n"
          f"c:Flood a geo:Feature ; rdfs:label \"1 % annual chance flood zone\" ; twin:depictedBy sc:FloodZone ; geo:hasGeometry [ a geo:Geometry ; geo:asWKT \"{wkt(65, 50, 70, 100)}\"^^geo:wktLiteral ] .\n"
          f"c:Stop a geo:Feature ; rdfs:label \"New light-rail stop\" ; twin:depictedBy sc:TransitStop ; twin:walkRadiusM 400 .\n")
    return t


CQ = [
    {"id": "C1", "title": "Whose noon shadow falls on the park at the winter solstice?",
     "why": "The X3D has each building's height and north face; the record has the sun (25.9° at noon on 21 December at 40.7° N, so a shadow runs 2.06 m per metre of height). A planning board sees which towers darken the park in the one season people most want sun.",
     "lights": "o", "sparql": """SELECT ?o ?building ?height ?shadow WHERE {
  c:Sun twin:shadowPerMetre ?k . c:Park twin:depictedBy ?pk .
  ?b twin:permitHeightM ?ph ; twin:depictedBy ?o ; rdfs:label ?building .
  """ + XZ("o") + " " + BOX("o") + " " + XZ("pk") + " " + BOX("pk") + """
  BIND(?osy AS ?height) BIND(?height * ?k AS ?shadow)
  FILTER(?oz - ?osz / 2 - ?shadow < ?pkz + ?pksz / 2 && ?oz - ?osz / 2 > ?pkz - ?pksz / 2)
  FILTER(ABS(?ox - ?pkx) < (?osx + ?pksx) / 2)
} ORDER BY ?building"""},
    {"id": "C2", "title": "Which buildings stand in the flood zone?",
     "why": "GeoSPARQL names the zone and its WKT; the scene gives the drawn footprint. (This page reads positions from the X3D, because none of its engines runs GeoSPARQL's geof: functions; a spatial store such as GraphDB or Jena with GeoSPARQL would test the WKT directly.)",
     "lights": "o", "sparql": """SELECT ?o ?building WHERE {
  c:Flood twin:depictedBy ?fz . ?b twin:permitHeightM ?h ; twin:depictedBy ?o ; rdfs:label ?building .
  """ + XZ("o") + " " + XZ("fz") + " " + BOX("fz") + """
  FILTER(ABS(?ox - ?fzx) < ?fzsx / 2 && ABS(?oz - ?fzz) < ?fzsz / 2)
} ORDER BY ?building"""},
    {"id": "C3", "title": "Which buildings are within a 400 m walk of the new stop?",
     "why": "Transit-oriented zoning starts from a radius. The record holds the policy (400 m), the scene holds the distances, and the answer is the set of lots that could be upzoned.",
     "lights": "o", "sparql": """SELECT ?o ?building WHERE {
  c:Stop twin:depictedBy ?st ; twin:walkRadiusM ?r . ?b twin:permitHeightM ?h ; twin:depictedBy ?o ; rdfs:label ?building .
  """ + XZ("o") + " " + XZ("st") + """
  BIND((?ox - ?stx) * (?ox - ?stx) + (?oz - ?stz) * (?oz - ?stz) AS ?d2)
  FILTER(?d2 <= ?r * ?r)
} ORDER BY ?building"""},
]
CINF = {"title": "Buildings inherit their district from their parcel",
        "why": "The record puts a building in a parcel and the parcel in a district. sfWithin chains, so a rule puts each building in its district, and the height rule below can read the district's limit.",
        "sparql": """SELECT ?o ?building ?district WHERE {
  ?b twin:permitHeightM ?h ; twin:depictedBy ?o ; rdfs:label ?building ; geo:sfWithin+ ?d . ?d twin:maxHeightM ?m ; rdfs:label ?district .
} ORDER BY ?building""",
        "n3": "{ ?a geo:sfWithin ?b . ?b geo:sfWithin ?c } => { ?a geo:sfWithin ?c } ."}
CSHAPES = prefixes(GX) + shape("S_C1", "sh:targetSubjectsOf twin:permitHeightM",
    "The drawn building is taller than its zoning district allows.",
    """SELECT $this ?value WHERE {
      $this twin:depictedBy ?o ; geo:sfWithin+ ?d . ?d twin:maxHeightM ?max .
      """ + BOX("o") + """
      BIND(?osy AS ?value) FILTER(?value > ?max)
    }""", "    PREFIX geo: <http://www.opengis.net/ont/geosparql#>\n") + shape("S_C2", "sh:targetSubjectsOf twin:permitHeightM",
    "The drawn building does not fit inside its parcel.",
    """SELECT $this ?value WHERE {
      $this twin:depictedBy ?o ; geo:sfWithin ?p . ?p twin:depictedBy ?pp .
      """ + XZ("o") + " " + BOX("o") + " " + XZ("pp") + " " + BOX("pp") + """
      FILTER(ABS(?ox - ?ppx) + ?osx / 2 > ?ppsx / 2 || ABS(?oz - ?ppz) + ?osz / 2 > ?ppsz / 2)
      BIND(?pp AS ?value)
    }""", "    PREFIX geo: <http://www.opengis.net/ont/geosparql#>\n")

# ================================================================== S7 CONCERT (Music Ontology + Timeline + Event)
MX = {"mo": "http://purl.org/ontology/mo/", "tl": "http://purl.org/NET/c4dm/timeline.owl#", "event": "http://purl.org/NET/c4dm/event.owl#",
      "foaf": "http://xmlns.com/foaf/0.1/", "m": "https://example.org/scen/concert/record#", "sc": sc("concert")}
ARTISTS = {"Vox": "Lead vocal", "Gtr": "Electric guitar", "Bass": "Bass", "Drums": "Drums", "Keys": "Keys"}
INSTR = {"GuitarAmp": ("Electric guitar rig", "MicGtrAmp", "Gtr"), "BassAmp": ("Bass rig", "DIBass", "Bass"), "DrumKit": ("Drum kit", "MicKick", "Drums"),
         "KeysRig": ("Keyboard rig", "DIKeys", "Keys"), "AcousticGtr": ("Acoustic guitar", "MicAcoustic", "Vox")}
WEDGE = {"Vox": "WedgeVox", "Gtr": "WedgeGtr", "Bass": "WedgeBass", "Drums": "DrumFill", "Keys": "WedgeKeys"}
SONGS = [  # n, title, set, start s, end s, performers, instruments, followspots {spot: performer}
    (1, "Lantern Weather", 1, 0, 270, "Vox Gtr Bass Drums", "GuitarAmp BassAmp DrumKit", {"Spot1": "Vox", "Spot2": "Gtr"}),
    (2, "Copper Hours", 1, 270, 560, "Vox Gtr Bass Drums Keys", "GuitarAmp BassAmp DrumKit KeysRig", {"Spot1": "Vox", "Spot2": "Keys"}),
    (3, "Saltwater Clock", 1, 560, 840, "Vox Gtr Bass Drums", "GuitarAmp BassAmp DrumKit", {"Spot1": "Vox", "Spot2": "Drums"}),
    (4, "Small Lights", 2, 900, 1180, "Vox Bass Keys", "AcousticGtr BassAmp KeysRig", {"Spot1": "Vox", "Spot2": "Keys"}),
    (5, "Loom", 2, 1180, 1500, "Vox Gtr Bass Drums Keys", "GuitarAmp BassAmp DrumKit KeysRig", {"Spot1": "Gtr", "Spot2": "Vox"})]


def dur(s):
    return f"PT{s // 60}M{s % 60}S" if s % 60 else f"PT{s // 60}M"


def concert_domain(fault=False):
    t = prefixes(MX)
    t += "m:ShowTL a tl:TimeLine ; rdfs:label \"Show clock (0 = doors down)\" .\n"
    t += "m:Show a mo:Performance ; rdfs:label \"Loomsong live, 16 Nov 2026\" ; event:sub_event m:Set1, m:Set2 .\n"
    t += "m:Set1 a mo:Performance ; rdfs:label \"Set 1\" ; event:sub_event " + ", ".join(f"m:Song{n}" for n, *_ in SONGS[:3]) + " .\n"
    t += "m:Set2 a mo:Performance ; rdfs:label \"Set 2\" ; event:sub_event " + ", ".join(f"m:Song{n}" for n, *_ in SONGS[3:]) + " .\n"
    for a, role in ARTISTS.items():
        t += f"m:{a} a mo:MusicArtist ; foaf:name {J(role)} ; rdfs:label {J(role)} ; twin:depictedBy sc:Perf{a} ; twin:monitor sc:{WEDGE[a]} .\n"
    for i, (label, mic, who) in INSTR.items():
        t += f"m:{i} a mo:Instrument ; rdfs:label {J(label)} ; twin:depictedBy sc:{i} ; twin:channel sc:{mic} .\n"
    for n, title, st, s0, s1, perf, ins, spots in SONGS:
        t += (f"m:Song{n} a mo:Performance ; rdfs:label {J(f'{n}. {title}')} ; twin:order {n} ;"
              f" event:time [ a tl:Interval ; tl:onTimeLine m:ShowTL ; tl:beginsAtDuration \"{dur(s0)}\"^^xsd:duration ; tl:durationXSD \"{dur(s1 - s0)}\"^^xsd:duration ] ;"
              f" twin:startSec {s0} ; twin:endSec {s1} ; mo:performer " + ", ".join(f"m:{p}" for p in perf.split()) +
              " ; mo:instrument " + ", ".join(f"m:{i}" for i in ins.split()) + " .\n")
        for sp, who in spots.items():
            t += f"m:Song{n} twin:followspot [ twin:spot sc:{sp} ; twin:follows m:{who} ] .\n"
    t += "m:Now twin:showSec 750 ; rdfs:label \"00:12:30 into the show\" .\n"
    return t


MQ = [
    {"id": "M1", "title": "Who is playing at 00:12:30?",
     "why": "The Timeline says which song is running; the Music Ontology says who performs it and on what; the scene shows where they stand. The lighting desk and the stage manager see the same answer.",
     "lights": "o", "sparql": """SELECT ?o ?who ?song WHERE {
  m:Now twin:showSec ?t . ?s twin:startSec ?a ; twin:endSec ?b ; rdfs:label ?song . FILTER(?t >= ?a && ?t < ?b)
  { ?s mo:performer ?x } UNION { ?s mo:instrument ?x }
  ?x twin:depictedBy ?o ; rdfs:label ?who .
} ORDER BY ?who"""},
    {"id": "M2", "title": "What does the crew change between songs 3 and 4?",
     "why": "Song 4 is the acoustic one. Set difference on the setlist gives the changeover list (in and out), and the stage plot shows the crew where to walk.",
     "lights": "o", "sparql": """SELECT ?o ?instrument ?change WHERE {
  { m:Song4 mo:instrument ?i . FILTER NOT EXISTS { m:Song3 mo:instrument ?i } BIND("bring on" AS ?change) }
  UNION
  { m:Song3 mo:instrument ?i . FILTER NOT EXISTS { m:Song4 mo:instrument ?i } BIND("take off" AS ?change) }
  ?i twin:depictedBy ?o ; rdfs:label ?instrument .
} ORDER BY ?change ?instrument"""},
    {"id": "M3", "title": "Who do the followspots pick up in song 4?",
     "why": "The cue sheet lives in the record; the throw from each spot to its target is in the scene. The spots and their targets light together.",
     "lights": "o", "sparql": """SELECT ?o ?what WHERE {
  m:Song4 twin:followspot ?c . ?c twin:spot ?s ; twin:follows ?p .
  { ?s x3do:DEF ?what . BIND(?s AS ?o) } UNION { ?p twin:depictedBy ?o ; rdfs:label ?what }
} ORDER BY ?what"""},
]
MINF = {"title": "Everything on stage in Set 2",
        "why": "event:sub_event nests songs in sets in the show. One rule flattens the nesting, so asking about Set 2 reaches every performer and instrument of its songs.",
        "sparql": """SELECT DISTINCT ?o ?what WHERE {
  m:Set2 event:sub_event+ ?s . { ?s mo:performer ?x } UNION { ?s mo:instrument ?x } ?x twin:depictedBy ?o ; rdfs:label ?what .
} ORDER BY ?what""",
        "n3": "{ ?a event:sub_event ?b . ?b event:sub_event ?c } => { ?a event:sub_event ?c } ."}
MSHAPES = prefixes(MX) + shape("S_M1", "sh:targetClass mo:Instrument",
    "An instrument on the setlist has no mic or DI within 1.0 m on the stage plot.",
    """SELECT $this ?value WHERE {
      ?song mo:instrument $this . $this twin:depictedBy ?o ; twin:channel ?value .
      """ + XYZ("o") + " " + XYZ("value") + """
      FILTER((?ox - ?valuex) * (?ox - ?valuex) + (?oy - ?valuey) * (?oy - ?valuey) + (?oz - ?valuez) * (?oz - ?valuez) > 1.0)
    }""", "    PREFIX mo: <http://purl.org/ontology/mo/>\n") + shape("S_M2", "sh:targetClass mo:MusicArtist",
    "A performer's monitor wedge is more than 2.5 m away from where they stand.",
    """SELECT $this ?value WHERE {
      $this twin:depictedBy ?o ; twin:monitor ?value .
      """ + XZ("o") + " " + XZ("value") + """
      FILTER((?ox - ?valuex) * (?ox - ?valuex) + (?oz - ?valuez) * (?oz - ?valuez) > 6.25)
    }""")

# ================================================================== S8 FIRE CODE (BOT + code clauses as data)
FX2 = {"bot": "https://w3id.org/bot#", "code": "https://example.org/scen/firecode/code#", "f": "https://example.org/scen/firecode/record#", "sc": sc("firecode")}


def firecode_domain(fault=False):
    t = prefixes(FX2)
    t += ("code:IBC2021 a twin:Code ; rdfs:label \"International Building Code 2021 (as read for this example)\" .\n"
          "code:LoadFactorB a twin:Clause ; twin:partOf code:IBC2021 ; rdfs:label \"Table 1004.5: business areas, 150 gross sq ft per occupant\" ; twin:m2PerOccupant 13.94 .\n"
          "code:TwoExits a twin:Clause ; twin:partOf code:IBC2021 ; rdfs:label \"Table 1006.2.1: two exits above 49 occupants (B)\" ; twin:maxOccupantsOneExit 49 .\n"
          "code:TravelB a twin:Clause ; twin:partOf code:IBC2021 ; rdfs:label \"Table 1017.2: exit access travel 300 ft, sprinklered B\" ; twin:maxTravelM 91.44 .\n"
          "code:DoorWidth a twin:Clause ; twin:partOf code:IBC2021 ; rdfs:label \"1010.1.1: 32 in clear door width\" ; twin:minWidthM 0.813 .\n")
    t += "f:Tower a bot:Building ; bot:hasStorey f:Floor4 .\nf:Floor4 a bot:Storey ; rdfs:label \"Floor 4 (sprinklered, business)\" ; bot:hasSpace f:Corridor4, f:StairA, f:StairB"
    t += "".join(f", f:{r}" for r in FIRE) + " .\n"
    t += "f:Corridor4 a bot:Space ; rdfs:label \"Corridor\" ; twin:depictedBy sc:Corridor4 .\n"
    for s in ("StairA", "StairB"):
        st = "locked" if (fault and s == "StairB") else "open"
        t += f"f:{s} a bot:Space, twin:Exit ; rdfs:label \"Exit stair {s[-1]}\" ; twin:depictedBy sc:{s} ; twin:exitDoor f:Door{s} ; twin:status \"{st}\" .\n"
    for r in FIRE:
        t += f"f:{r} a bot:Space ; rdfs:label \"Room {r[4:]}\" ; twin:depictedBy sc:{r} ; twin:occupancy \"B\" .\n"
    for d, (x, z, w, ax, rooms) in DOORS.items():
        spaces = ", ".join(f"f:{r}" for r in rooms) + ", f:Corridor4"
        t += f"f:{d} a bot:Interface ; rdfs:label \"Door {d[4:]}\" ; twin:depictedBy sc:{d} ; bot:interfaceOf {spaces} ; twin:widthAxis {ax} ; twin:egress true .\n"
    return t


FQ2 = [
    {"id": "E1", "title": "How many people can each room hold, by the code?",
     "why": "Occupant load is floor area divided by the code's load factor. The area comes from the drawn room, the factor from the clause, both kept as data, so changing either recomputes the floor. Rooms over 49 need a second exit (question E3).",
     "lights": "o", "sparql": """SELECT ?o ?room ?area ?occupants WHERE {
  code:LoadFactorB twin:m2PerOccupant ?k . ?r twin:occupancy "B" ; twin:depictedBy ?o ; rdfs:label ?room .
  """ + BOX("o") + """
  BIND(ROUND(?osx * ?osz) AS ?area) BIND(CEIL(?osx * ?osz / ?k) AS ?occupants)
} ORDER BY ?room"""},
    {"id": "E2", "title": "How far is each room from its nearest open exit?",
     "why": "Distance along the corridor and into the room (x plus z, from the scene) to each open exit door; whether a stair is open, from the record. A code review measures the true walking path, so this is a screening number, not a finding.",
     "lights": None, "sparql": """SELECT ?room (ROUND(MIN(?d)) AS ?metres) WHERE {
  ?r twin:occupancy "B" ; twin:depictedBy ?o ; rdfs:label ?room .
  ?ex a twin:Exit ; twin:status "open" ; twin:exitDoor/twin:depictedBy ?eo .
  """ + XZ("o") + " " + XZ("eo") + """
  BIND(ABS(?ox - ?eox) + ABS(?oz - ?eoz) AS ?d)
} GROUP BY ?room ORDER BY ?room"""},
    {"id": "E3", "title": "Does every room over 49 people have two ways out?",
     "why": "Counting doors is a record question (BOT interfaces); which rooms need two is a scene question (area). The room lights green when it has them.",
     "lights": "o", "sparql": """SELECT ?o ?room (COUNT(DISTINCT ?door) AS ?doors) WHERE {
  code:LoadFactorB twin:m2PerOccupant ?k . code:TwoExits twin:maxOccupantsOneExit ?max .
  ?r twin:occupancy "B" ; twin:depictedBy ?o ; rdfs:label ?room . ?door bot:interfaceOf ?r ; twin:egress true .
  """ + BOX("o") + """
  FILTER(?osx * ?osz / ?k > ?max)
} GROUP BY ?o ?room"""},
]
FINF2 = {"title": "Every space that can reach Exit stair A",
         "why": "BOT says which spaces each door joins. A rule turns doors into connections, and the path query lights everything that can reach the stair.",
         "sparql": """SELECT DISTINCT ?o ?space WHERE {
  f:StairA (^bot:interfaceOf/bot:interfaceOf)+ ?s . ?s twin:depictedBy ?o ; rdfs:label ?space . FILTER(?s != f:StairA)
} ORDER BY ?space""",
         "n3": "{ ?d bot:interfaceOf ?a . ?d bot:interfaceOf ?b } => { ?a twin:connects ?b } ."}
FSHAPES2 = prefixes(FX2) + shape("S_E1", "sh:targetClass bot:Interface",
    "This egress door is drawn narrower than the code's 32 in (0.813 m) clear width.",
    """SELECT $this ?value WHERE {
      $this twin:egress true ; twin:widthAxis ?ax ; twin:depictedBy ?o . code:DoorWidth twin:minWidthM ?min .
      """ + BOX("o") + """
      BIND(IF(?ax = 0, ?osx, ?osz) AS ?value) FILTER(?value < ?min)
    }""", "    PREFIX code: <https://example.org/scen/firecode/code#>\n") + shape("S_E2", "sh:targetSubjectsOf twin:occupancy",
    "No open exit lies within the code's 300 ft (91.4 m) travel distance of this room (measured along x plus z).",
    """SELECT $this WHERE {
      $this twin:depictedBy ?o . code:TravelB twin:maxTravelM ?max .
      """ + XZ("o") + """
      FILTER NOT EXISTS {
        ?ex a twin:Exit ; twin:status "open" ; twin:exitDoor/twin:depictedBy ?eo .
        ?eo x3do:translation ?eL . ?eL rdf:first ?ex1 . ?eL rdf:rest ?eR . ?eR rdf:rest ?eR2 . ?eR2 rdf:first ?ez1 .
        FILTER(ABS(?ox - ?ex1) + ABS(?oz - ?ez1) <= ?max)
      }
    }""", "    PREFIX code: <https://example.org/scen/firecode/code#>\n")

# ================================================================== S9 FARM (AGROVOC + SOSA + QUDT)
AX = {"agrovoc": "http://aims.fao.org/aos/agrovoc/", "skos": "http://www.w3.org/2004/02/skos/core#", "sosa": "http://www.w3.org/ns/sosa/",
      "qudt": "http://qudt.org/schema/qudt/", "unit": "http://qudt.org/vocab/unit/", "g": "https://example.org/scen/farm/record#", "sc": sc("farm")}
CROP_URI = {"tomatoes": "c_7805", "cucumbers": "c_10195", "lettuces": "c_12151", "basil": "c_9093"}
NEEDS = {"tomatoes": 30, "cucumbers": 32, "lettuces": 28, "basil": 25}          # volumetric water content %, example thresholds
MOIST = {"A1": 34, "A2": 27, "A3": 33, "B1": 22, "B2": 30, "B3": 26, "C1": 31, "C2": 29, "C3": 24}
RH = {"RH1": ("A", 88), "RH2": ("C", 72)}


def farm_domain(fault=False):
    chain = json.loads((ROOT / "dl" / "agrovoc" / "chain.json").read_text())
    t = prefixes(AX)
    for uri, (label, broader) in sorted(chain.items()):
        c = uri.rsplit("/", 1)[1]
        t += f"agrovoc:{c} a skos:Concept ; skos:prefLabel {J(label)}@en" + "".join(f" ; skos:broader agrovoc:{b.rsplit('/', 1)[1]}" for b in broader) + " .\n"
    for crop, thr in NEEDS.items():
        t += f"agrovoc:{CROP_URI[crop]} twin:minSoilWaterPct {thr}" + (" ; twin:susceptibleTo agrovoc:c_13557" if crop in ("tomatoes", "cucumbers") else "") + " .\n"
    t += "g:Greenhouse2 rdfs:label \"Greenhouse 2\" ; twin:depictedBy sc:Greenhouse2 .\n"
    for bed, (x, z) in BEDS.items():
        k = bed[3:]
        t += f"g:{bed} a sosa:FeatureOfInterest ; rdfs:label \"Bed {k}\" ; twin:row \"{k[0]}\" ; twin:depictedBy sc:Crop{k} ; twin:crop agrovoc:{CROP_URI[CROPS[bed]]} ; twin:areaM2 7.2 ; twin:rootDepthM 0.3 .\n"
        t += (f"g:Moist{k} a sosa:Sensor ; twin:depictedBy sc:Moist{k} ; sosa:observes agrovoc:c_7208 .\n"
              f"g:Moist{k}_obs a sosa:Observation ; sosa:madeBySensor g:Moist{k} ; sosa:hasFeatureOfInterest g:{bed} ; sosa:observedProperty agrovoc:c_7208 ;"
              f" sosa:hasResult [ qudt:numericValue {MOIST[k]} ; qudt:unit unit:PERCENT ] .\n")
        if fault and k == "C2":
            continue                         # planted: Bed C2's valve was never entered in the irrigation record
        t += f"g:Valve{k} a sosa:Actuator ; rdfs:label \"Valve {k}\" ; twin:depictedBy sc:Valve{k} ; twin:waters g:{bed} .\n"
    for s, (row, v) in RH.items():
        t += (f"g:{s} a sosa:Sensor ; rdfs:label \"Humidity sensor row {row}\" ; twin:depictedBy sc:{s} ; twin:coversRow \"{row}\" ; sosa:observes agrovoc:c_6496 .\n"
              f"g:{s}_obs a sosa:Observation ; sosa:madeBySensor g:{s} ; sosa:observedProperty agrovoc:c_6496 ; sosa:hasResult [ qudt:numericValue {v} ; qudt:unit unit:PERCENT ] .\n")
    t += "g:VentA twin:depictedBy sc:VentA ; twin:ventsRow \"A\" ; rdfs:label \"Roof vent over row A\" .\ng:VentC twin:depictedBy sc:VentC ; twin:ventsRow \"C\" ; rdfs:label \"Roof vent over row C\" .\n"
    t += "g:MildewRule twin:rhAbovePct 85 ; rdfs:label \"Powdery mildew risk above 85 % relative humidity (example rule)\" .\n"
    return t


AQ = [
    {"id": "A1", "title": "Which beds are thirsty, and how much water do they need?",
     "why": "SOSA gives each probe's reading and bed; AGROVOC names the crop, and the crop carries its threshold; the scene shows where to walk. Litres = shortfall x bed area x root depth.",
     "lights": "o", "sparql": """SELECT ?o ?bed ?crop ?reading ?needs ?litres WHERE {
  ?obs sosa:hasFeatureOfInterest ?b ; sosa:observedProperty agrovoc:c_7208 ; sosa:hasResult/qudt:numericValue ?reading .
  ?b rdfs:label ?bed ; twin:depictedBy ?o ; twin:crop ?c ; twin:areaM2 ?a ; twin:rootDepthM ?d . ?c skos:prefLabel ?crop ; twin:minSoilWaterPct ?needs .
  FILTER(?reading < ?needs) BIND(ROUND((?needs - ?reading) / 100 * ?a * ?d * 1000) AS ?litres)
} ORDER BY ?bed"""},
    {"id": "A2", "title": "Where is powdery mildew likely, and which vent should open?",
     "why": "Humidity is read per row (SOSA); susceptibility belongs to the crop concept (AGROVOC's powdery mildews, c_13557); the vents are in the scene. High humidity over a susceptible crop lights the beds and the vent above them.",
     "lights": "o", "sparql": """SELECT ?o ?what ?rh WHERE {
  g:MildewRule twin:rhAbovePct ?lim . ?s twin:coversRow ?row . ?obs sosa:madeBySensor ?s ; sosa:hasResult/qudt:numericValue ?rh . FILTER(?rh > ?lim)
  { ?b twin:row ?row ; twin:crop/twin:susceptibleTo agrovoc:c_13557 ; twin:depictedBy ?o ; rdfs:label ?what }
  UNION { ?v twin:ventsRow ?row ; twin:depictedBy ?o ; rdfs:label ?what }
} ORDER BY ?what"""},
    {"id": "A3", "title": "Which valves should open now?",
     "why": "The valve-to-bed map is in the record; the valves are in the scene. Thirsty beds become a list of valves a grower can walk to, or an irrigation controller can be sent.",
     "lights": "o", "sparql": """SELECT ?o ?valve ?bed WHERE {
  ?obs sosa:hasFeatureOfInterest ?b ; sosa:observedProperty agrovoc:c_7208 ; sosa:hasResult/qudt:numericValue ?r .
  ?b twin:crop/twin:minSoilWaterPct ?n ; rdfs:label ?bed . FILTER(?r < ?n)
  ?v twin:waters ?b ; rdfs:label ?valve ; twin:depictedBy ?o .
} ORDER BY ?valve"""},
]
AINF = {"title": "All vegetable beds (and why basil is not one)",
        "why": "AGROVOC is a SKOS thesaurus: tomatoes are fruit vegetables, which are vegetables; basil is a culinary herb, which is a plant product but not a vegetable. One rule over skos:broader answers a buyer's 'show me the vegetables' with the right beds.",
        "sparql": """SELECT ?o ?bed ?crop WHERE {
  ?b twin:crop ?c ; twin:depictedBy ?o ; rdfs:label ?bed . ?c skos:broader+ agrovoc:c_8174 ; skos:prefLabel ?crop .
} ORDER BY ?bed""",
        "n3": "{ ?a skos:broader ?b . ?b skos:broader ?c } => { ?a skos:broader ?c } ."}
ASHAPES = prefixes(AX) + shape("S_A1", "sh:targetClass sosa:Observation",
    "The probe is drawn outside the bed its readings are filed under.",
    """SELECT $this ?value WHERE {
      $this sosa:madeBySensor ?s ; sosa:hasFeatureOfInterest ?b . ?s twin:depictedBy ?value . ?b twin:depictedBy ?bo .
      """ + XZ("value") + " " + XZ("bo") + " " + BOX("bo") + """
      FILTER(ABS(?valuex - ?box) > ?bosx / 2 || ABS(?valuez - ?boz) > ?bosz / 2 + 0.3)
    }""", "    PREFIX sosa: <http://www.w3.org/ns/sosa/>\n") + shape("S_A2", "sh:targetSubjectsOf twin:crop",
    "A planted bed has no irrigation valve in the record.",
    """SELECT $this WHERE {
      $this twin:depictedBy ?o . FILTER NOT EXISTS { ?v twin:waters $this }
    }""")

SCEN2 = {
    "building": {"title": "Building: Floor 3 heats an empty room", "pfx": BX, "domain": building_domain, "q": BQ, "inf": BINF, "shapes": BSHAPES,
                 "fault_text": "Room 303's temperature sensor is mounted in Room 304, and Room 306's temperature point has been deleted from the model."},
    "warehouse": {"title": "Warehouse: recall lot L-2291 before lunch", "pfx": WX, "domain": warehouse_domain, "q": WQ, "inf": WINF, "shapes": WSHAPES,
                  "fault_text": "The mixed pallet P07, holding one case of chilled yoghurt, is put away in ambient Rack B, and P03 is drawn one bin over from its recorded bin."},
    "city": {"title": "City: the block that knows its shadows", "pfx": GX, "domain": city_domain, "q": CQ, "inf": CINF, "shapes": CSHAPES,
             "fault_text": "Tower 5 is drawn 48 m tall in a 36 m district, and Building 2 is drawn 8 m east, overhanging its parcel."},
    "concert": {"title": "Concert: a stage plot that knows the setlist", "pfx": MX, "domain": concert_domain, "q": MQ, "inf": MINF, "shapes": MSHAPES,
                "fault_text": "The guitar amp's mic was kicked 1.65 m downstage, and the keys wedge was set on the bass side of the stage."},
    "firecode": {"title": "Fire code: can everyone get out of Floor 4?", "pfx": FX2, "domain": firecode_domain, "q": FQ2, "inf": FINF2, "shapes": FSHAPES2,
                 "fault_text": "Door 402 is drawn 0.76 m (30 in) wide, and the record says Exit stair B is locked."},
    "farm": {"title": "Farm: Greenhouse 2 waters what is thirsty", "pfx": AX, "domain": farm_domain, "q": AQ, "inf": AINF, "shapes": ASHAPES,
             "fault_text": "Bed B2's moisture probe is pushed into Bed B3, and Bed C2's valve was never entered in the irrigation record."},
}

if __name__ == "__main__":
    for name, s in SCEN2.items():
        d = DATA / name; d.mkdir(parents=True, exist_ok=True)
        (d / "domain.ttl").write_text(s["domain"](False))
        (d / "domain-fault.ttl").write_text(s["domain"](True))
        (d / "shapes.ttl").write_text(s["shapes"])
        (d / "questions.json").write_text(json.dumps({"title": s["title"], "prefixes": sparql_prefixes(s["pfx"]), "questions": s["q"], "inference": s["inf"],
                                                      "fault_text": s["fault_text"]}, indent=1))
    print("ok")
