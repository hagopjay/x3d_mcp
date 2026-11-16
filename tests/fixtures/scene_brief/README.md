# scene_brief fixtures

`injection_scene.x3d` is a scene whose author text is hostile on purpose: instruction-like sentences in a comment before the root element, in `meta`,
`WorldInfo`, a `Viewpoint` description, a `MetadataString`, a `Text` string, an `Anchor` description and url, a DEF name, and a Script's CDATA source, plus a numeric
field that holds text (`Box size`) and a ROUTE whose target does not exist.

Every sentence in it is inert test data. Nothing in this repository acts on it, and the host in its url (`attacker.example`) is a reserved name that resolves nowhere.
The tests in `tests/test_scene_brief.py` check the contrary: that no string of 12 or more characters, or containing whitespace, from this file appears anywhere in a
default `scene_brief` answer, at any budget.

The file was written for this repository (it is not a Web3D Consortium example) and carries no third-party licence.
