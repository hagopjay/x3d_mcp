# Documentation

An index of what is in `docs/`. Edit freely: every page here is plain Markdown,
and nothing is generated from it.

## The X3D MCP server

| Page | What it covers |
|---|---|
| [Model Context Protocol for Machine-Aided X3D Authoring (PDF)](Model_Context_Protocol__MCP__for_Machine_Aided_X3D_Authoring.pdf) | The Web3D 2026 paper: motivation, design, evaluation |
| [x3d-standard-overview.md](x3d-standard-overview.md) | X3D in one page: versions, profiles, encodings |
| [x3d-file-structure.md](x3d-file-structure.md) | What a valid X3D file looks like, head to Scene |
| [x3d-node-reference.md](x3d-node-reference.md) | Nodes the server generates and checks |
| [x3d-validation-strategy.md](x3d-validation-strategy.md) | The validation layers: XML, XSD, semantic |
| [x3d-examples.md](x3d-examples.md) | Worked example scenes |
| [x3d-tools-and-references.md](x3d-tools-and-references.md) | Browsers, converters, specifications |
| [ecosystem-research.md](ecosystem-research.md) | The wider ecosystem the server sits in |
| [transfer-layer.md](transfer-layer.md) | Moving large scenes in and out without the model: the blob store, `/blob` routes, `blob_id` and `return_mode`, settings, security |

## The Accountability Playground

A web page where a model writes X3D, this repository's validator says exactly
what is wrong, the model repairs, and a browser checks that the result is
visible. Code in [`playground/`](../playground/).

| Page | Read it when |
|---|---|
| [playground/GUIDE.md](playground/GUIDE.md) | You have a pass link and want to know what the page shows |
| [playground/ADMIN.md](playground/ADMIN.md) | You run the playground: passes, the access list, limits, spend, the run log |
| [playground/ARCHITECTURE.md](playground/ARCHITECTURE.md) | You want to change it, deploy it, or check a claim it makes |
