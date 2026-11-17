"""Domain graphs, bridges, questions and shapes for the three scenarios.
Writes data/<scenario>/{domain.ttl, shapes.ttl, questions.json} and data/<scenario>/domain-fault.ttl where the fault is in the record."""
import json, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

PFX = {
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "rdfs": "http://www.w3.org/2000/01/rdf-schema#",
    "xsd": "http://www.w3.org/2001/XMLSchema#",
    "sh": "http://www.w3.org/ns/shacl#",
    "x3do": "https://www.web3d.org/specifications/X3dOntology4.1#",
    "twin": "https://example.org/scen/twin#",
    "dcterms": "http://purl.org/dc/terms/",
}


def prefixes(extra):
    d = dict(PFX); d.update(extra)
    return "".join(f"@prefix {k}: <{v}> .\n" for k, v in d.items()) + "\n"


def sparql_prefixes(extra):
    d = dict(PFX); d.update(extra)
    return "".join(f"PREFIX {k}: <{v}>\n" for k, v in d.items())


# Coordinates: read x, y, z of a Transform's translation list (flat scenes: world = local)
XYZ = "?{t} x3do:translation ?{t}L . ?{t}L rdf:first ?{t}x ; rdf:rest ?{t}R1 . ?{t}R1 rdf:first ?{t}y ; rdf:rest/rdf:first ?{t}z ."
SIZE = "?{s} x3do:size ?{s}L . ?{s}L rdf:first ?{s}sx ; rdf:rest ?{s}R1 . ?{s}R1 rdf:first ?{s}sy ; rdf:rest/rdf:first ?{s}sz ."


def xyz(t): return XYZ.format(t=t)
def size(s): return SIZE.format(s=s)


# ============================================================ HERITAGE
HX = {"crm": "http://www.cidoc-crm.org/cidoc-crm/", "h": "https://example.org/scen/heritage/record#",
      "sc": "https://example.org/scen/heritage/scene.ttl#", "unit": "http://qudt.org/vocab/unit/"}
# tier per object: D documented, T theorized, S speculative; grade C/L/S; source document
HOBJ = [
    # id, label, scene DEF, tier, grade, source key
    ("OsirisShaftA", "Osiris Shaft, Shaft A", "OsirisShaftA", "D", "S", "hawass2007"),
    ("OsirisChamberA", "Osiris Shaft, Chamber A (size schematic)", "OsirisChamberA", "D", "S", "hawass2007"),
    ("OsirisShaftB", "Osiris Shaft, Shaft B", "OsirisShaftB", "D", "S", "hawass2007"),
    ("OsirisChamberB", "Osiris Shaft, Chamber B with six side chambers", "OsirisChamberB", "D", "S", "hawass2007"),
    ("OsirisShaftC", "Osiris Shaft, Shaft C", "OsirisShaftC", "D", "S", "hawass2007"),
    ("OsirisLevel3", "Osiris Shaft, flooded Level 3", "OsirisLevel3Water", "D", "S", "hawass2007"),
    ("Sphinx", "Great Sphinx (massing only)", "SphinxBody", "D", "C", "lehner2008"),
    ("PawVoid", "Reported void beneath the paws", "SphinxPawVoid", "T", "S", "popular"),
    ("HallOfRecords", "Hall of Records (Cayce readings)", "HallOfRecords", "S", "L", "cayce"),
    ("KingsChamber", "King's Chamber, Great Pyramid", "KhufuKingsChamber", "D", "C", "petrie1883"),
    ("BigVoid", "Big Void found by muon imaging", "KhufuBigVoid", "D", "L", "morishima2017"),
    ("OneSystem", "All of it as one connected system", "UndergroundLink", "S", "S", "none"),
]
HSRC = {
    "hawass2007": ("Hawass 2007, The Discovery of the Osiris Shaft at Giza, ASAE 36", "https://giza.fas.harvard.edu/pubdocs/476/full/"),
    "lehner2008": ("Lehner 2008, The Complete Pyramids", ""),
    "popular": ("Popular accounts of the Dobecki and Schoch 1992 survey", ""),
    "cayce": ("Wikipedia, Hall of Records (quoting the Cayce readings)", "https://en.wikipedia.org/wiki/Hall_of_Records"),
    "petrie1883": ("Petrie 1883, The Pyramids and Temples of Gizeh", ""),
    "morishima2017": ("Morishima et al. 2017, Nature, doi 10.1038/nature24647", "https://doi.org/10.1038/nature24647"),
}
# recorded dimensions (E16 Measurement -> E54 Dimension in metres), by source
HDIM = [
    ("dimShaftA", "OsirisShaftA", "height", 9.62, "hawass2007"),
    ("dimShaftB", "OsirisShaftB", "height", 13.25, "hawass2007"),
    ("dimShaftC", "OsirisShaftC", "height", 7.5, "hawass2007"),
    ("dimChamberBx", "OsirisChamberB", "length", 6.80, "hawass2007"),
    ("dimKingsChamberx", "KingsChamber", "length", 10.47, "petrie1883"),
]
# competing accounts of total depth: one feature, three measurements
HDEPTH = [("depth25", 25.0, "hawass_osiris1", "One Hawass write-up (last level)"),
          ("depth30", 30.0, "whiting2021", "About 30 m / 100 ft (Hawass, Whiting)"),
          ("depth4148", 41.48, "diagram", "Diagram of unknown origin")]
HSRC.update({"hawass_osiris1": ("Hawass, guardians.net osiris1", "http://guardians.net/hawass/osiris1.htm"),
             "whiting2021": ("Whiting 2021, MS thesis, University of Montana", "https://scholarworks.umt.edu/etd/11693"),
             "diagram": ("A depth diagram of unknown origin", "")})


def heritage_domain(fault=False):
    t = prefixes(HX)
    t += "h:tierDocumented a crm:E55_Type ; rdfs:label \"documented\" .\nh:tierTheorized a crm:E55_Type ; rdfs:label \"theorized\" .\nh:tierSpeculative a crm:E55_Type ; rdfs:label \"speculative\" .\n"
    t += "h:Giza a crm:E27_Site ; rdfs:label \"Giza plateau\" ; crm:P46_is_composed_of h:OsirisShaft, h:Sphinx, h:GreatPyramid, h:HallOfRecords, h:OneSystem .\n"
    t += "h:OsirisShaft a crm:E25_Human-Made_Feature ; rdfs:label \"Osiris Shaft\" ; crm:P46_is_composed_of h:Level1, h:Level2, h:Level3 .\n"
    t += "h:Level1 a crm:E25_Human-Made_Feature ; rdfs:label \"Osiris Shaft, Level 1\" ; crm:P46_is_composed_of h:OsirisShaftA, h:OsirisChamberA .\n"
    t += "h:Level2 a crm:E25_Human-Made_Feature ; rdfs:label \"Osiris Shaft, Level 2\" ; crm:P46_is_composed_of h:OsirisShaftB, h:OsirisChamberB .\n"
    t += "h:Level3 a crm:E25_Human-Made_Feature ; rdfs:label \"Osiris Shaft, Level 3\" ; crm:P46_is_composed_of h:OsirisShaftC, h:OsirisLevel3 .\n"
    t += "h:GreatPyramid a crm:E22_Human-Made_Object ; rdfs:label \"Great Pyramid of Khufu\" ; crm:P46_is_composed_of h:KingsChamber, h:BigVoid .\n"
    t += "h:Sphinx crm:P46_is_composed_of h:PawVoid .\n"
    for k, (title, url) in HSRC.items():
        t += f"h:src_{k} a crm:E31_Document ; rdfs:label {json.dumps(title)}" + (f" ; dcterms:source <{url}>" if url else "") + " .\n"
    tiers = {"D": "h:tierDocumented", "T": "h:tierTheorized", "S": "h:tierSpeculative"}
    for oid, label, d, tier, grade, src in HOBJ:
        cls = "crm:E25_Human-Made_Feature" if oid not in ("Sphinx",) else "crm:E22_Human-Made_Object"
        if tier != "D":
            cls = "crm:E89_Propositional_Object" if oid == "OneSystem" else cls
        t += f"h:{oid} a {cls} ; rdfs:label {json.dumps(label)} ; crm:P138i_has_representation sc:{d} .\n"
        t += f"sc:{d} a crm:E36_Visual_Item ; crm:P138_represents h:{oid} .\n"
        if fault and oid == "HallOfRecords":
            continue                    # planted fault: a speculative element with no attribution at all
        t += (f"h:attr_{oid} a crm:E13_Attribute_Assignment ; crm:P140_assigned_attribute_to h:{oid} ; crm:P141_assigned {tiers[tier]} ;"
              f" crm:P177_assigned_property_of_type crm:P2_has_type ; twin:grade \"{grade}\"" + (f" ; crm:P70i_is_documented_in h:src_{src}" if src != "none" else "") + " .\n")
    for mid, oid, axis, val, src in HDIM:
        t += (f"h:{mid} a crm:E16_Measurement ; crm:P39_measured h:{oid} ; crm:P40_observed_dimension h:{mid}_v ; crm:P70i_is_documented_in h:src_{src} .\n"
              f"h:{mid}_v a crm:E54_Dimension ; crm:P2_has_type h:axis_{axis} ; crm:P90_has_value {val} ; crm:P91_has_unit unit:M .\n")
    t += "h:axis_height a crm:E55_Type ; rdfs:label \"height (y)\" ; twin:axis 1 .\nh:axis_length a crm:E55_Type ; rdfs:label \"length (x)\" ; twin:axis 0 .\n"
    for mid, val, src, note in HDEPTH:
        t += (f"h:{mid} a crm:E16_Measurement ; crm:P39_measured h:OsirisShaft ; rdfs:comment {json.dumps(note)} ; crm:P40_observed_dimension h:{mid}_v ; crm:P70i_is_documented_in h:src_{src} .\n"
              f"h:{mid}_v a crm:E54_Dimension ; crm:P2_has_type h:axis_totalDepth ; crm:P90_has_value {val} ; crm:P91_has_unit unit:M .\n")
    t += "h:axis_totalDepth a crm:E55_Type ; rdfs:label \"total depth\" .\n"
    return t


HQ = [
    {"id": "H1", "title": "Show everything whose only support is speculative",
     "why": "The scene draws a Hall of Records; only the record says it is speculative. The query finds every drawn element whose attribution is speculative, so the picture can never pass a guess off as a find.",
     "lights": "o",
     "sparql": """SELECT ?o ?label ?grade WHERE {
  ?a a crm:E13_Attribute_Assignment ; crm:P140_assigned_attribute_to ?thing ;
     crm:P141_assigned h:tierSpeculative ; twin:grade ?grade .
  ?thing rdfs:label ?label ; crm:P138i_has_representation ?o .
  ?o a x3do:Transform .
} ORDER BY ?label"""},
    {"id": "H2", "title": "Where do the sources disagree about a dimension, and by how much?",
     "why": "Three accounts give three total depths. Linked as three measurements of one feature, the disagreement becomes a number the page can draw beside the shaft.",
     "lights": None,
     "sparql": """SELECT ?feature (COUNT(?m) AS ?accounts) (MIN(?v) AS ?low) (MAX(?v) AS ?high) ((MAX(?v) - MIN(?v)) AS ?spread) WHERE {
  ?m a crm:E16_Measurement ; crm:P39_measured ?f ; crm:P40_observed_dimension ?d .
  ?d crm:P2_has_type h:axis_totalDepth ; crm:P90_has_value ?v .
  ?f rdfs:label ?feature .
} GROUP BY ?feature"""},
    {"id": "H3", "title": "What does the model draw against what the record measured?",
     "why": "Only the X3D knows the drawn size; only the record knows the measured one. Joined, every documented dimension gets a drawn-versus-recorded check.",
     "lights": "o",
     "sparql": """SELECT ?o ?label ?recorded ?drawn WHERE {
  ?m a crm:E16_Measurement ; crm:P39_measured ?thing ; crm:P40_observed_dimension ?d .
  ?d crm:P2_has_type ?axis ; crm:P90_has_value ?recorded .
  ?axis twin:axis ?ax .
  ?thing rdfs:label ?label ; crm:P138i_has_representation ?o .
  ?o x3do:hasChildren ?shape . ?shape x3do:hasGeometry ?box .
  """ + size("box") + """
  BIND(IF(?ax = 0, ?boxsx, ?boxsy) AS ?drawn)
} ORDER BY ?label"""},
]
HINF = {"title": "Everything inside Level 2, or inside the whole shaft",
        "why": "CIDOC-CRM's RDFS file does not declare P46 transitive, so a plain reasoner adds nothing here. One rule (a property path) makes part-of chains collapse, and a click on the shaft lights every part below it.",
        "sparql": """SELECT ?o ?label WHERE {
  h:OsirisShaft crm:P46_is_composed_of+ ?part .
  ?part rdfs:label ?label ; crm:P138i_has_representation ?o .
} ORDER BY ?label""",
        "n3": "{ ?a crm:P46_is_composed_of ?b . ?b crm:P46_is_composed_of ?c } => { ?a crm:P46_is_composed_of ?c } ."}

HSHAPES = prefixes(HX) + """
twin:S_H1 a sh:NodeShape ; sh:targetClass crm:E36_Visual_Item ;
  sh:severity sh:Violation ;
  sh:message "Drawn element has no attribution with a tier: the picture shows something the record does not vouch for." ;
  sh:sparql [ sh:select \"\"\"
    PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
    SELECT $this WHERE {
      $this crm:P138_represents ?thing .
      FILTER NOT EXISTS { ?a crm:P140_assigned_attribute_to ?thing ; crm:P141_assigned ?tier . }
    }\"\"\" ] .

twin:S_H2 a sh:NodeShape ; sh:targetClass crm:E16_Measurement ;
  sh:severity sh:Violation ;
  sh:message "The drawn size differs from the documented measurement by more than 5 %." ;
  sh:sparql [ sh:select \"\"\"
    PREFIX crm: <http://www.cidoc-crm.org/cidoc-crm/>
    PREFIX x3do: <https://www.web3d.org/specifications/X3dOntology4.1#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX twin: <https://example.org/scen/twin#>
    SELECT $this ?value WHERE {
      $this crm:P39_measured ?thing ; crm:P40_observed_dimension ?d .
      ?d crm:P2_has_type ?axis ; crm:P90_has_value ?rec . ?axis twin:axis ?ax .
      ?thing crm:P138i_has_representation ?o . ?o x3do:hasChildren ?shape . ?shape x3do:hasGeometry ?box .
      ?box x3do:size ?L . ?L rdf:first ?sx ; rdf:rest/rdf:first ?sy .
      BIND(IF(?ax = 0, ?sx, ?sy) AS ?value)
      FILTER(ABS(?value - ?rec) > 0.05 * ?rec)
    }\"\"\" ] .
"""

# ============================================================ FACTORY
FX = {"aas": "https://admin-shell.io/aas/3/2/", "shell": "https://admin-shell.io/aas/3/2/AssetAdministrationShell/", "ref": "https://admin-shell.io/aas/3/2/Referable/", "sm": "https://admin-shell.io/aas/3/2/Submodel/", "prop": "https://admin-shell.io/aas/3/2/Property/", "sosa": "http://www.w3.org/ns/sosa/", "qudt": "http://qudt.org/schema/qudt/",
      "unit": "http://qudt.org/vocab/unit/", "qk": "http://qudt.org/vocab/quantitykind/", "f": "https://example.org/scen/factory/record#",
      "sc": "https://example.org/scen/factory/scene.ttl#"}
# QUDT conversion factors copied from the QUDT unit vocabulary (downloaded 16 Nov 2026) for the units used here
FUNITS = {"DEG_C": (1.0, 273.15, "Temperature"), "DEG_F": (0.5555555555555556, 459.67, "Temperature"),
          "MilliM-PER-SEC": (0.001, 0.0, "Velocity"), "IN-PER-SEC": (0.0254, 0.0, "Velocity"), "M": (1.0, 0.0, "Length")}
FASSETS = [
    # id, label, scene DEF, submodel properties {idShort: (value, unit)}
    ("Robot1", "6-axis robot R-1", "Robot1Base", {"MaxReach": (2.1, "M"), "Payload": (12, None)}),
    ("Conveyor1", "Conveyor C-1", "Conveyor1", {}),
    ("Station1", "Press station S-1", "Station1", {"RatedTemperature": (60, "DEG_C")}),
    ("Station2", "Screw station S-2", "Station2", {"RatedTemperature": (55, "DEG_C")}),
    ("Station3", "Test station S-3", "Station3", {"RatedTemperature": (50, "DEG_C")}),
]
FSENSORS = [
    # id, scene DEF, hosted by asset, observed property, value, unit
    ("TempSensor1", "TempSensor1", "Station2", "Temperature", 52.0, "DEG_C"),
    ("TempSensor2", "TempSensor2", "Station3", "Temperature", 131.0, "DEG_F"),   # 55 degC: above S-3's 50 degC
    ("VibSensor1", "VibSensor1", "Conveyor1", "Velocity", 4.2, "MilliM-PER-SEC"),
    ("VibSensor2", "VibSensor2", "Station1", "Velocity", 0.31, "IN-PER-SEC"),
]
FMAINT = {"Robot1", "Conveyor1", "Station1", "Station3"}   # assets with a maintenance submodel
FFEEDS = [("Station1", "Conveyor1"), ("Conveyor1", "Station2"), ("Station2", "Station3")]


def factory_domain(fault=False):
    t = prefixes(FX)
    for u, (mul, off, qk) in FUNITS.items():
        t += f"unit:{u} qudt:conversionMultiplier {mul} ; qudt:conversionOffset {off} ; qudt:hasQuantityKind qk:{qk} .\n"
    for aid, label, d, props in FASSETS:
        t += (f"f:{aid} a aas:AssetAdministrationShell ; ref:idShort \"{aid}\" ; rdfs:label {json.dumps(label)} ; twin:depictedBy sc:{d} ;"
              f" shell:submodels f:{aid}_TD" + (f", f:{aid}_Maint" if aid in FMAINT else "") + " .\n")
        t += f"f:{aid}_TD a aas:Submodel ; ref:idShort \"TechnicalData\""
        if props:
            t += " ; sm:submodelElements " + ", ".join(f"f:{aid}_{k}" for k in props)
        t += " .\n"
        for k, (v, u) in props.items():
            t += f"f:{aid}_{k} a aas:Property ; ref:idShort \"{k}\" ; prop:value {v}" + (f" ; qudt:unit unit:{u}" if u else "") + " .\n"
        if aid in FMAINT:
            t += f"f:{aid}_Maint a aas:Submodel ; ref:idShort \"Maintenance\" .\n"
    for sid, d, host, prop, v, u in FSENSORS:
        if fault and sid == "VibSensor1":
            u = "DEG_C"                 # planted fault: a vibration reading recorded in degrees Celsius
        t += (f"f:{sid} a sosa:Sensor ; twin:depictedBy sc:{d} ; sosa:isHostedBy f:{host} ; sosa:observes qk:{prop} .\n"
              f"f:{sid}_obs a sosa:Observation ; sosa:madeBySensor f:{sid} ; sosa:observedProperty qk:{prop} ; sosa:hasFeatureOfInterest f:{host} ;"
              f" sosa:hasResult [ qudt:numericValue {v} ; qudt:unit unit:{u} ] .\n")
    for a, b in FFEEDS:
        t += f"f:{a} twin:feeds f:{b} .\n"
    t += "f:Robot1 twin:picksAt sc:PickPoint ; twin:placesAt sc:PlacePoint .\n"
    return t


FQ = [
    {"id": "F1", "title": "Which machines run hotter than their datasheet allows, in one unit?",
     "why": "One sensor reports in degrees Celsius, one in Fahrenheit. QUDT's conversion factors bring both to kelvin inside the query, the AAS datasheet gives each station's limit, and the X3D tells the page which box to turn red.",
     "lights": "o",
     "sparql": """SELECT ?o ?asset ?readK ?limitK WHERE {
  ?obs sosa:hasFeatureOfInterest ?a ; sosa:observedProperty qk:Temperature ; sosa:hasResult ?r .
  ?r qudt:numericValue ?v ; qudt:unit ?u . ?u qudt:conversionMultiplier ?m ; qudt:conversionOffset ?off .
  ?a shell:submodels/sm:submodelElements ?p ; rdfs:label ?asset ; twin:depictedBy ?o .
  ?p ref:idShort "RatedTemperature" ; prop:value ?lim ; qudt:unit ?lu .
  ?lu qudt:conversionMultiplier ?lm ; qudt:conversionOffset ?loff .
  BIND((?v + ?off) * ?m AS ?readK) BIND((?lim + ?loff) * ?lm AS ?limitK)
  FILTER(?readK > ?limitK)
}"""},
    {"id": "F2", "title": "If the press station stops, what else stops?",
     "why": "The feeds-into chain lives in the record, the layout in the scene. A path query walks the chain and the downstream line lights up.",
     "lights": "o",
     "sparql": """SELECT ?o ?asset WHERE {
  f:Station1 twin:feeds+ ?a . ?a rdfs:label ?asset ; twin:depictedBy ?o .
}"""},
    {"id": "F3", "title": "Which assets have no maintenance record?",
     "why": "Absence is a question RDF can ask with NOT EXISTS; the answer is a spot on the floor plan, not a row in a spreadsheet.",
     "lights": "o",
     "sparql": """SELECT ?o ?asset WHERE {
  ?a a aas:AssetAdministrationShell ; rdfs:label ?asset ; twin:depictedBy ?o .
  FILTER NOT EXISTS { ?a shell:submodels/ref:idShort "Maintenance" }
}"""},
]
FINF = {"title": "A failing sensor's whole line",
        "why": "SOSA says which asset hosts the sensor; the feeds chain says what that asset supplies. Chained, one red reading lights every machine it affects.",
        "sparql": """SELECT DISTINCT ?o ?asset WHERE {
  f:TempSensor1 sosa:isHostedBy ?h . ?h twin:feeds* ?a . ?a rdfs:label ?asset ; twin:depictedBy ?o .
}""",
        "n3": "{ ?s sosa:isHostedBy ?a . ?a twin:feeds ?b } => { ?s twin:affects ?b } ."}

FSHAPES = prefixes(FX) + """
twin:S_F1 a sh:NodeShape ; sh:targetSubjectsOf twin:placesAt ;
  sh:severity sh:Violation ;
  sh:message "A pick or place point is beyond the robot's maximum reach in its Asset Administration Shell." ;
  sh:sparql [ sh:select \"\"\"
    PREFIX shell: <https://admin-shell.io/aas/3/2/AssetAdministrationShell/>
    PREFIX ref: <https://admin-shell.io/aas/3/2/Referable/>
    PREFIX sm: <https://admin-shell.io/aas/3/2/Submodel/>
    PREFIX prop: <https://admin-shell.io/aas/3/2/Property/>
    PREFIX x3do: <https://www.web3d.org/specifications/X3dOntology4.1#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX twin: <https://example.org/scen/twin#>
    SELECT $this ?value WHERE {
      { $this twin:picksAt ?pt } UNION { $this twin:placesAt ?pt }
      $this twin:depictedBy ?base ;
            shell:submodels/sm:submodelElements ?p .
      ?p ref:idShort "MaxReach" ; prop:value ?reach .
      ?base x3do:translation ?bL . ?bL rdf:first ?bx ; rdf:rest/rdf:rest/rdf:first ?bz .
      ?pt x3do:translation ?pL . ?pL rdf:first ?px ; rdf:rest/rdf:rest/rdf:first ?pz .
      BIND(((?px - ?bx) * (?px - ?bx) + (?pz - ?bz) * (?pz - ?bz)) AS ?d2)
      BIND(?pt AS ?value)
      FILTER(?d2 > ?reach * ?reach)
    }\"\"\" ] .

twin:S_F2 a sh:NodeShape ; sh:targetClass sosa:Observation ;
  sh:severity sh:Violation ;
  sh:message "The result's unit does not measure the observed property (QUDT quantity kind mismatch)." ;
  sh:sparql [ sh:select \"\"\"
    PREFIX sosa: <http://www.w3.org/ns/sosa/>
    PREFIX qudt: <http://qudt.org/schema/qudt/>
    SELECT $this ?value WHERE {
      $this sosa:observedProperty ?qk ; sosa:hasResult ?r . ?r qudt:unit ?value .
      FILTER NOT EXISTS { ?value qudt:hasQuantityKind ?qk }
    }\"\"\" ] .
"""

# ============================================================ HOSPITAL
PX = {"fhir": "http://hl7.org/fhir/", "bot": "https://w3id.org/bot#", "p": "https://example.org/scen/hospital/record#",
      "sc": "https://example.org/scen/hospital/scene.ttl#"}
PDEV = [
    # id, label, scene DEF, status, next service due (extension), sterile?, location
    ("ORTable", "Operating table", "ORTable", "active", "2027-03-01", True, "OR4"),
    ("Anesthesia1", "Anaesthesia workstation", "AnesthesiaMachine", "active", "2026-11-10", False, "OR4"),   # past due on 16 Nov 2026
    ("Light1", "Surgical light 1", "SurgicalLight1", "active", "2027-01-15", True, "OR4"),
    ("Light2", "Surgical light 2", "SurgicalLight2", "active", "2027-01-15", True, "OR4"),
    ("CArm1", "Mobile C-arm", "CArm", "active", "2027-02-01", False, "OR4"),
    ("InstrumentTable1", "Instrument table", "InstrumentTable", "active", "2027-06-01", True, "OR4"),
    ("SupplyCart1", "Supply cart", "SupplyCart", "active", "2027-06-01", False, "OR4"),
    ("Sink1", "Scrub sink", "ScrubSink", "active", "2027-06-01", False, "OR4"),
    ("Defib1", "Defibrillator", None, "active", "2027-04-01", False, "Storage2"),
]
PNEED = ["ORTable", "Anesthesia1", "Light1", "CArm1", "Defib1"]   # devices today's procedure lists as used


def hospital_domain(fault=False):
    t = prefixes(PX)
    t += ("p:Hospital a bot:Building ; bot:hasStorey p:Floor2 .\np:Floor2 a bot:Storey ; bot:hasSpace p:OR4, p:Storage2, p:Corridor2 .\n"
          "p:OR4 a bot:Space, fhir:Location ; rdfs:label \"Operating Room 4\" ; twin:depictedBy sc:OR4Room ; bot:adjacentZone p:Corridor2 .\n"
          "p:Corridor2 a bot:Space ; rdfs:label \"Corridor, floor 2\" ; bot:adjacentZone p:Storage2 .\n"
          "p:Storage2 a bot:Space, fhir:Location ; rdfs:label \"Equipment storage 2\" .\n"
          "p:SterileField a bot:Zone ; rdfs:label \"Sterile field\" ; twin:depictedBy sc:SterileField ; twin:radius 1.6 .\n"
          "p:OR4 bot:containsZone p:SterileField .\n")
    for did, label, d, st, due, sterile, loc in PDEV:
        if fault and did == "CArm1":
            loc = "Storage2"            # planted fault: the record says the C-arm is in storage; the scene draws it in OR 4
        t += (f"p:{did} a fhir:Device, bot:Element ; rdfs:label {json.dumps(label)} ; fhir:status [ fhir:v \"{st}\" ] ;"
              f" twin:nextServiceDue \"{due}\"^^xsd:date ; twin:sterile {str(sterile).lower()} ; fhir:location [ fhir:l p:{loc} ]"
              + (f" ; twin:depictedBy sc:{d}" if d else "") + " .\n")
        t += f"p:{loc} bot:containsElement p:{did} .\n"
    t += ("p:Proc1 a fhir:Procedure ; rdfs:label \"Hip arthroplasty, 16 Nov 2026 13:00\" ; fhir:status [ fhir:v \"preparation\" ] ; fhir:location [ fhir:l p:OR4 ] ;"
          " fhir:occurrence [ fhir:v \"2026-11-16T13:00:00-05:00\"^^xsd:dateTime ] ; fhir:used "
          + ", ".join(f"[ fhir:concept [ fhir:text \"{x}\" ] ; fhir:reference [ fhir:l p:{x} ] ]" for x in PNEED) + " .\n")
    t += "p:today twin:date \"2026-11-16\"^^xsd:date .\n"
    return t


PQ = [
    {"id": "P1", "title": "Is everything today's procedure needs in OR 4 and in service?",
     "why": "FHIR lists what the procedure uses and where each device is; the scene shows the room. Present and in service lights green; a device listed but elsewhere is named, so someone fetches it before the patient arrives.",
     "lights": "o",
     "sparql": """SELECT ?device ?where ?o ?ready WHERE {
  p:Proc1 fhir:used/fhir:reference/fhir:l ?d ; fhir:location/fhir:l ?room .
  ?d rdfs:label ?device ; fhir:location/fhir:l ?loc ; fhir:status/fhir:v ?st ; twin:nextServiceDue ?due .
  ?loc rdfs:label ?where . p:today twin:date ?today .
  OPTIONAL { ?d twin:depictedBy ?o }
  BIND(?loc = ?room && ?st = "active" && ?due >= ?today AS ?ready)
} ORDER BY ?ready ?device"""},
    {"id": "P2", "title": "Which devices are past their service date?",
     "why": "Core FHIR Device has no service-due field, so the record carries it as an extension, the usual practice. Joined to the scene, the overdue machine turns red where it stands.",
     "lights": "o",
     "sparql": """SELECT ?o ?device ?due WHERE {
  ?d a fhir:Device ; rdfs:label ?device ; twin:nextServiceDue ?due ; twin:depictedBy ?o .
  p:today twin:date ?today . FILTER(?due < ?today)
}"""},
    {"id": "P3", "title": "How do I bring the defibrillator from storage?",
     "why": "BOT knows which spaces touch; the scene knows where OR 4 is. The adjacency path is the route a porter walks.",
     "lights": None,
     "sparql": """SELECT ?from ?via ?to WHERE {
  p:Storage2 rdfs:label ?from . p:OR4 rdfs:label ?to .
  ?mid bot:adjacentZone p:Storage2 ; rdfs:label ?via . p:OR4 bot:adjacentZone ?mid .
}"""},
]
PINF = {"title": "Roll devices up to the floor and the building",
        "why": "BOT says the building has storeys, storeys have spaces, spaces contain elements. A rule rolls every device up, so a facilities view of floor 2 lights the same machines a clinician sees in OR 4.",
        "sparql": """SELECT ?o ?device WHERE {
  p:Floor2 bot:hasSpace ?s . ?s bot:containsElement ?d . ?d rdfs:label ?device ; twin:depictedBy ?o .
}""",
        "n3": "{ ?st bot:hasSpace ?s . ?s bot:containsElement ?e } => { ?st bot:containsElement ?e } ."}

PSHAPES = prefixes(PX) + """
twin:S_P1 a sh:NodeShape ; sh:targetClass fhir:Device ;
  sh:severity sh:Violation ;
  sh:message "A non-sterile device stands inside the sterile field (distance from the field's centre is below its radius)." ;
  sh:sparql [ sh:select \"\"\"
    PREFIX twin: <https://example.org/scen/twin#>
    PREFIX x3do: <https://www.web3d.org/specifications/X3dOntology4.1#>
    PREFIX rdf: <http://www.w3.org/1999/02/22-rdf-syntax-ns#>
    PREFIX p: <https://example.org/scen/hospital/record#>
    SELECT $this ?value WHERE {
      $this twin:sterile false ; twin:depictedBy ?o .
      p:SterileField twin:depictedBy ?z ; twin:radius ?r .
      ?o x3do:translation ?oL . ?oL rdf:first ?ox ; rdf:rest/rdf:rest/rdf:first ?oz .
      ?z x3do:translation ?zL . ?zL rdf:first ?zx ; rdf:rest/rdf:rest/rdf:first ?zz .
      BIND(((?ox - ?zx) * (?ox - ?zx) + (?oz - ?zz) * (?oz - ?zz)) AS ?value)
      FILTER(?value < ?r * ?r)
    }\"\"\" ] .

twin:S_P2 a sh:NodeShape ; sh:targetClass fhir:Device ;
  sh:severity sh:Violation ;
  sh:message "The record places this device in another room, but the scene draws it inside OR 4." ;
  sh:sparql [ sh:select \"\"\"
    PREFIX twin: <https://example.org/scen/twin#>
    PREFIX fhir: <http://hl7.org/fhir/>
    PREFIX p: <https://example.org/scen/hospital/record#>
    SELECT $this ?value WHERE {
      $this twin:depictedBy ?o ; fhir:location/fhir:l ?value .
      FILTER(?value != p:OR4)
    }\"\"\" ] .
"""

SCEN = {
    "heritage": {"title": "Heritage: the Osiris Shaft as a living record", "pfx": HX, "domain": heritage_domain, "q": HQ, "inf": HINF, "shapes": HSHAPES,
                 "fault_in": "both", "fault_text": "Shaft A is drawn 12 m tall against the documented 9.62 m, and the Hall of Records has lost its attribution."},
    "factory": {"title": "Factory: Cell 7, the line that checks its own layout", "pfx": FX, "domain": factory_domain, "q": FQ, "inf": FINF, "shapes": FSHAPES,
                "fault_in": "both", "fault_text": "Station 3 is moved 0.98 m outward, past the robot's 2.1 m reach, and a vibration reading is recorded in degrees Celsius."},
    "hospital": {"title": "Hospital: Operating Room 4, ready or not", "pfx": PX, "domain": hospital_domain, "q": PQ, "inf": PINF, "shapes": PSHAPES,
                 "fault_in": "both", "fault_text": "A non-sterile supply cart is pushed inside the sterile field, and the C-arm's record says it is still in storage."},
}

if __name__ == "__main__":
    for name, s in SCEN.items():
        d = DATA / name; d.mkdir(parents=True, exist_ok=True)
        (d / "domain.ttl").write_text(s["domain"](False))
        (d / "domain-fault.ttl").write_text(s["domain"](True))
        (d / "shapes.ttl").write_text(s["shapes"])
        pre = sparql_prefixes(s["pfx"])
        qs = [dict(q, sparql=q["sparql"]) for q in s["q"]]
        (d / "questions.json").write_text(json.dumps({"title": s["title"], "prefixes": pre, "questions": qs, "inference": s["inf"],
                                                      "fault_text": s["fault_text"]}, indent=1))
    print("ok")
