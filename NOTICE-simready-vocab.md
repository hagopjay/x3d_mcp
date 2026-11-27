# Notice: SimReady label vocabularies

`src/validation/simready_vocab.py` holds eight closed lists of label names used by the X3D SimReady profile checks
(`src/validation/simready_checks.py`). It is generated, not written by hand: `python -I tests/build_simready_vocab.py PATH/TO/simready-foundation`.

| What | Source | Licence |
|---|---|---|
| The eight lists as packaged (file layout, canonical spellings, display names) | [NVIDIA/simready-foundation](https://github.com/NVIDIA/simready-foundation) at commit `c3bd1d8ff0ce3968c9099c2b059cb404595a48a4`, core tier: `rules_and_reqs/dataset_taxonomies/taxonomies/*.json` and `rules_and_reqs/physics_bodies/object_affordances/validation.py` | Apache-2.0, Copyright (c) 2026 NVIDIA CORPORATION & AFFILIATES |
| `ade20k`, 150 class names | [ADE20K](https://ade20k.csail.mit.edu/), MIT CSAIL | names only |
| `cityscapes`, 35 class names | [Cityscapes](https://www.cityscapes-dataset.com/) | names only |
| `coco`, 80 class names | [COCO](https://cocodataset.org/) | names only |
| `imagenet_1k`, 998 distinct class names | [ImageNet](https://www.image-net.org/) ILSVRC2012 | names only |
| `pascal_voc`, 20 class names | [PASCAL VOC](http://host.robots.ox.ac.uk/pascal/VOC/) | names only |
| `sunrgbd`, 10 class names | [SUN RGB-D](https://rgbd.cs.princeton.edu/) | names only |
| `affordance` (9), `affordance_state` (12) | SimReady Foundation object affordances capability | Apache-2.0 |

Only the class names are kept: no images, no annotations, no identifiers beyond the name. They are used for one thing,
checking that a label written in an X3D file is a member of the vocabulary it claims.

Changes from the source: the JSON files are reduced to the list of names plus a table of non-canonical spellings, and
written as one Python module so the validator needs no data file at run time.

**Takedown.** If you hold rights in one of these lists and want it removed, open an issue on this repository. The list is
taken out at once, and the check that reads it then stays silent for that vocabulary.
