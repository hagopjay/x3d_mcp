# ROUTE order, colour range and H-Anim name checks

Three semantic checks, and one change to how `validate_xml` reports a libxml2 failure.
Measured with libxml2 2.14.6 (lxml 6.1.3) against the bundled X3D 4.0 XSD. "Before" is the
validator at 56809ca (fork main 535fdac with the integration branch merged); "after" is this change.

## What each check catches

| Check | Level | What it catches | Fix it names |
|---|---|---|---|
| `route-before-def` | error | A ROUTE that comes before the DEF (or IMPORT AS) it names. The XSD accepts it; X_ITE drops the ROUTE, so the animation never runs. The X3D Schematron calls this an error too. | Move the ROUTE to the end of its Scene or ProtoBody (exact hint, op `move`). |
| `route-out-of-scope` | error | A ROUTE inside a ProtoBody naming a DEF outside it, or the reverse. | Says which scope holds the name. |
| `colour-out-of-range` | error | A colour component outside 0..1 on SFColor, MFColor, SFColorRGBA and MFColorRGBA attributes, `field@value` and ProtoInstance `fieldValue`, including the ones the XSD lets through (below). | Clamp to 0..1, or divide by 255 when the values look like 0..255 (exact hint). |
| `hanim-joint-name`, `hanim-segment-name` | warning (unknown segment names: info) | On HAnim 2.0/2.1 humanoids (no version means 2.0 in X3D 4): a 1.x alias such as `l_wrist`, a prefixed standard name such as `hanim_l_knee`, or a case or underscore variant such as `HumanoidRoot`; one summary per humanoid for other names. The 146 joint and 146 segment names and their aliases come from the bundled XSD. | Rename to the standard name, so motions and tools that bind joints by name find it. |

The IMPORT change also removes a false error: a ROUTE to an `IMPORT AS` name was reported as an unknown node.

## The colours the XSD lets through

- `0.5 0.5 1.5 ...` passes the MFColor pattern: at a tuple boundary the regexp reads `1.5` as `1.` and `.5`.
- MFColor and MFColorRGBA patterns allow a leading minus sign.
- An exponent passes: `0.5e1` is 5.
- Past about the twelfth colour of a list, an out-of-range value makes libxml2's pattern engine give up
  after about 1.7 s with "Internal error in XML Schema validation", and validation stops there. Before
  this change `validate_xml` reported that as the 10 MB start-tag limit ("not checkable at this size")
  on files of a few KB. The same happens with bad tokens deep in MFVec3f, MFVec2f, MFFloat and MFRotation.

`listvalues.py` reads numeric values in linear time, the way the XSD patterns mean them. When the engine
gives up, `validate_xml` finds the element named in the error log, reports the bad value in words
("value 101 of 120 is '1.0059 1.0511 0.9': '1.0059' is outside 0..1"), swaps in a placeholder and runs
the XSD again (up to 20 times), so the rest of the document is still checked. If it cannot find the
value, the verdict is `xsd-engine-failure`, unchecked, never a size limit.

## Measured (no model)

Corpus: 2,857 X3D XML scenes under 1 MB. The XSD verdict is unchanged on every one (1,593 invalid,
1,264 valid), no exceptions, median added time 1 ms.

Planted faults, one per copy of a scene that passes the old gate (XSD valid, no semantic errors):

| Class | n | Before | After |
|---|---|---|---|
| ROUTE moved to the start of the Scene | 200 | 0 | 200 |
| Colour 1.25 at entry 13 or later of an MF list | 30 | 17 flagged, all as "not checkable", 0 with the cause | 30, with the cause |
| `1.5` in the first colour of an MFColor (re-split) | 200 | 177 | 200 |
| Negative first component | 200 | 0 | 200 |
| SFColor `1.5 0 0` on a Material | 200 | 200 | 200 |
| HAnimJoint renamed to its 1.x alias | 59 | 0 | 59 |
| Control (unchanged) | 200 | 0 invalid | 0 invalid |

8 of the 200 controls get a new H-Anim warning: their humanoids already use 1.x names such as
`r_wrist` and `l_ankle` while declaring HAnim 2.0, so the warning is real.

On the 901 corpus scenes that pass the old gate: 3 have a ROUTE before its DEF, 1 a ROUTE out of
scope, 38 HAnim joint-name warnings. Spot checks of the ROUTE errors were all real.
