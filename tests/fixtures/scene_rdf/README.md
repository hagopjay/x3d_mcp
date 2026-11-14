# scene_to_rdf fixtures

X3D scenes and the Turtle the Web3D Consortium's `X3dToTurtle.xslt` writes for them. The goldens
(`*.golden.ttl`) were made by `evaluation/make_scene_rdf_goldens.py`: lxml applies the X3D DTD named
by the document's version (attribute defaults such as `containerField`), then Saxon-HE (saxonche) runs
the stylesheet. `manifest.json` records the sha256 of the stylesheet, the three DTDs and every file,
and the saxonche and lxml versions. `tests/test_semantic_web_tools.py` checks that the server's own
converter (`src/x3d_utils/scene_rdf.py`, no Saxon) gives a graph isomorphic to each golden.

| File | Source | X3D | Exercises |
|---|---|---|---|
| Altar.x3d | X3D Example Archives, X3dForAdvancedModeling/SanCarlosCathedral (Michele Foti) | 3.3 | IndexedFaceSet geometry, 19 USE copies |
| HelloWorld.x3d | X3D for Web Authors, Chapter01TechnicalOverview (Don Brutzman) | 3.3 | Shapes, ImageTexture url, DEF/USE |
| HelloX3dAuthorsAnimationChain.x3d | X3D for Web Authors, Chapter07EventAnimationInterpolation (Don Brutzman) | 3.3 | TimeSensor, an interpolator, ROUTE chain |
| MaterialModulator.x3d | X3D for Web Authors, Chapter14Prototypes (Don Brutzman) | 3.3 | ProtoDeclare / ProtoBody / IS, Script, ROUTE |
| MetadataNodeExamplesX3D4.x3d | X3D for Web Authors, Chapter15Metadata (Don Brutzman) | 4.0 | Metadata nodes, nested MetadataSet |
| LabScene41.x3d | written for these tests | 4.1 | PhysicalMaterial, Metadata on a Transform, USE, ROUTE |
| SwitchOrder.x3d | written for these tests | 4.0 | Switch children order (not visible in RDF), no identifier meta |
| faulted/HelloWorldFaulted.x3d | HelloWorld.x3d with six planted faults | 3.3 | one SHACL result per fault (see below) |
| faulted/HelloWorldBoxChild.x3d | HelloWorld.x3d with a Box in a children field | 3.3 | children-not-child-node |
| faulted/LabScene41Faulted.x3d | LabScene41.x3d with three planted faults | 4.1 | colour range, SFColor arity, transparency range |

Planted faults in `faulted/HelloWorldFaulted.x3d`: the Viewpoint is removed (no-viewpoint, a warning),
the Sphere is removed from its Shape (shape-no-geometry, a warning), `diffuseColor='0.1 1.5 1'`
(colour-out-of-range), a Box with its default `containerField='geometry'` is placed in a Transform
(field-not-in-domain), `<Material USE='ImageCloudlessEarth'/>` names an ImageTexture
(use-type-mismatch), and a ROUTE names `fromNode='Ghost'`, which is not defined
(route-missing-from-node).

The five X3D Example Archives scenes are used under the Web3D Consortium Open-Source License for
Models and Software (https://www.web3d.org/x3d/content/examples/license.html); the golden Turtle is
the output of `X3dToTurtle.xslt`, under the same licence. The notice is in `LICENSE.txt`, and it
applies to every file in this directory.

Regenerate (development only; needs `pip install saxonche`, a copy of `X3dToTurtle.xslt` and a
directory with `x3d-3.3.dtd`, `x3d-4.0.dtd` and `x3d-4.1.dtd` from https://www.web3d.org/specifications/):

    python evaluation/make_scene_rdf_goldens.py path/to/X3dToTurtle.xslt path/to/dtds
