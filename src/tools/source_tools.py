"""Evidence tools: where in the source a finding lives, a guarded one-attribute repair, and what inference
changes in a SHACL verdict.

    source_witness_audit   byte-located witnesses (DEF/USE/ROUTE order and identity) bound to the source sha256
    patch_x3d_attribute    replace one attribute value on a unique DEF, guarded by the source sha256; every other
                           byte is kept, and the result is re-checked against the XSD and validate_semantic
    shacl_counterfactual   the same shapes on the asserted graph and after inference: which results disappear
                           (a violation hidden by entailment, not repaired), which appear, which stay

The ideas and the witness code come from the Semantic Twin Lab Evidence Edition (16 Nov 2026).
"""

import json
import time

from mcp.server.fastmcp import Context, FastMCP

from transfer.tooling import holds_input, resolve_input, run_heavy
from tools.semantic_web import _err, _scene
from validation.semantic import semantic_diagnostics
from validation.validate import validate_xml
from x3d_utils import semantic_web as sw
from x3d_utils import source_witness as srcw
from x3d_utils.ontology_guard import QueryTimeout

SAMPLE = 25
INLINE_MAX = 400_000


def _recheck(text: str) -> dict:
    """Post-edit gates actually run on the edited bytes: XSD and validate_semantic (errors and warnings)."""
    out = {}
    try:
        v = validate_xml(text)
        out["xsd_valid"] = bool(v.get("valid"))
        if not out["xsd_valid"]:
            out["xsd_errors"] = [str(e)[:200] for e in (v.get("errors") or [])][:5]
    except Exception as e:  # the gate failed to run: say so, never report it as passed
        out["xsd_valid"] = None
        out["xsd_error"] = f"XSD check did not run: {e}"
    try:
        diags = semantic_diagnostics(text)
        errs = [d for d in diags if d.level == "error"]
        warns = [d for d in diags if d.level == "warning"]
        out["semantic_errors"] = len(errs)
        out["semantic_warnings"] = len(warns)
        if errs:
            out["semantic_error_messages"] = [d.message[:200] for d in errs[:5]]
    except Exception as e:
        out["semantic_errors"] = None
        out["semantic_error"] = f"validate_semantic did not run: {e}"
    return out


def _signature(r: dict) -> tuple:
    return (r.get("check_id"), r.get("focus"), r.get("path"), r.get("value"), r.get("severity"))


def counterfactual(asserted: dict, derived: dict, sample: int = SAMPLE) -> dict:
    """Compare two SHACL reports of the same shapes on the same scene: asserted graph vs after inference."""
    a = {_signature(r): r for r in asserted["results"]}
    d = {_signature(r): r for r in derived["results"]}
    hidden = [a[k] for k in a if k not in d]
    added = [d[k] for k in d if k not in a]
    kept = [a[k] for k in a if k in d]

    def by_check(rows):
        out = {}
        for r in rows:
            out[r["check_id"]] = out.get(r["check_id"], 0) + 1
        return dict(sorted(out.items()))

    hidden_v = [r for r in hidden if r["severity"] == "Violation"]
    verdict = []
    if hidden_v:
        verdict.append(f"{len(hidden_v)} violation(s) found on the asserted graph disappear after inference. "
                       "The source was not changed, so these are hidden, not repaired.")
    if added:
        verdict.append(f"{len(added)} result(s) appear only after inference, on "
                       f"{len({r['focus'] for r in added})} focus node(s): the inferred types widened the shapes' "
                       "targets. Check them before treating them as faults in the scene.")
    if not hidden and not added:
        verdict.append("Inference changed no result for these shapes on this scene.")
    return {
        "asserted": {"conforms": asserted["conforms"], "counts": asserted["counts"],
                     "results": len(asserted["results"]), "distinct": len(a)},
        "derived": {"conforms": derived["conforms"], "counts": derived["counts"],
                    "results": len(derived["results"]), "distinct": len(d)},
        "hidden_by_inference": {"count": len(hidden), "violations": len(hidden_v), "by_check": by_check(hidden),
                                "sample": hidden[:sample]},
        "added_by_inference": {"count": len(added), "focus_nodes": len({r["focus"] for r in added}),
                               "by_check": by_check(added), "sample": added[:sample]},
        "unchanged": {"count": len(kept), "by_check": by_check(kept)},
        "verdict": " ".join(verdict),
    }


def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    @holds_input
    async def source_witness_audit(content: str = "", path: str = "", blob_id: str = "",
                                   ctx: Context = None) -> str:
        """Locate DEF/USE/ROUTE problems in the exact source bytes: each finding carries a byte span and line.

        Checks, within each Scene and ProtoBody scope: duplicate DEF, USE with no DEF, USE before its DEF,
        USE of a different node type, and ROUTE endpoints with no DEF or IMPORT. Every witness names the
        source sha256, so it stops pointing anywhere useful once the document changes. These are the
        order-dependent facts an RDF graph of the scene cannot hold (a forward USE and a backward USE
        convert to the same triples), so this complements shacl_validate rather than repeating it.
        No DTD or external entity is loaded; this is not an XSD check.

        Args:
            content / path / blob_id: The X3D (XML) document; exactly one.

        Returns JSON {ok, clean, source_sha256, source_bytes, elements, definitions, doctype, findings:
        [{severity, code, message, witness {start_byte, end_byte, line, column, element, def, xml_path},
        related_witness?}], limitations}. ok means the audit ran; clean means it found nothing.
        """
        t0 = time.perf_counter()
        try:
            text, _label = await resolve_input(content, path, blob_id, ctx)
        except ValueError as e:
            return _err(e)
        res = await run_heavy(srcw.audit_scene, text.encode("utf-8"), size=len(text))
        res["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(res, indent=1)

    @mcp.tool(structured_output=False)
    @holds_input
    async def patch_x3d_attribute(def_name: str, attribute: str, new_value: str, expected_sha256: str,
                                  content: str = "", path: str = "", blob_id: str = "",
                                  expected_value: str = "", recheck: bool = True, return_mode: str = "full",
                                  ctx: Context = None) -> str:
        """Change one existing attribute on one DEF'd node, keeping every other byte of the document.

        A reviewable repair: the edit is refused unless the document's sha256 equals expected_sha256 (no
        stale edits), the DEF occurs exactly once, the attribute already exists, and (when given) its
        current value equals expected_value. Quotes, layout, comments and a DOCTYPE stay as they were,
        unlike modify_x3d_node, which re-serialises the document. The edited bytes are then re-checked
        against the XSD and validate_semantic; RDF, SHACL and rendering are not re-run here.

        Args:
            def_name: DEF of the node to edit (must be unique in the document).
            attribute: An existing attribute (not DEF, USE or xmlns), e.g. "diffuseColor".
            new_value: The new value, as it would appear in the attribute (escaped for you).
            expected_sha256: sha256 of the document you read (source_witness_audit returns it).
            content / path / blob_id: The X3D (XML) document; exactly one.
            expected_value: The value you expect to replace; refused if the document differs.
            recheck: Run the XSD and validate_semantic on the result (default true).
            return_mode: "full" (default) includes the edited document; "summary" returns only the receipt.

        Returns JSON {ok, receipt {source_sha256, output_sha256, witness, attribute, before_value,
        after_value, replaced_byte_range, validation}, recheck {xsd_valid, semantic_errors,
        semantic_warnings}, document?}. On refusal {ok: false, code, error}.
        """
        t0 = time.perf_counter()
        if return_mode not in ("full", "summary"):
            return _err("return_mode must be 'full' or 'summary'")
        try:
            text, _label = await resolve_input(content, path, blob_id, ctx)
        except ValueError as e:
            return _err(e)
        try:
            output, receipt = await run_heavy(
                lambda: srcw.patch_attribute(text.encode("utf-8"), def_name=def_name, attribute=attribute,
                                             value=new_value, expected_sha256=(expected_sha256 or "").strip().lower(),
                                             expected_value=expected_value if expected_value != "" else None),
                size=len(text))
        except srcw.SourceError as e:
            return _err(str(e), code=e.code)
        new_text = output.decode("utf-8")
        out = {"ok": True, "receipt": receipt}
        if recheck:
            out["recheck"] = await run_heavy(_recheck, new_text, size=len(new_text))
            out["receipt"]["validation"] = ("well-formed XML; XSD and validate_semantic re-run (see recheck); "
                                            "RDF, SHACL and rendering not re-run")
        if return_mode == "full":
            if len(new_text) <= INLINE_MAX:
                out["document"] = new_text
            else:
                out["note"] = f"document is {len(new_text):,} characters; use return_mode='summary' and apply the receipt"
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(out, indent=1)

    @mcp.tool(structured_output=False)
    @holds_input
    async def shacl_counterfactual(content: str = "", path: str = "", blob_id: str = "", graph_id: str = "",
                                   shapes: str = "x3d-core", extra_shapes_ttl: str = "", inference: str = "rdfs",
                                   ontology_version: str = "4.1", data_ttl: str = "", sample: int = SAMPLE,
                                   ctx: Context = None) -> str:
        """Run the same SHACL shapes twice, on the asserted scene graph and after inference, and compare.

        The asserted graph is never changed. Results that disappear after inference were hidden by
        entailment, not fixed: with the X3D Ontology, hasChildren's range types any node placed in a
        children field as an X3DChildNode, so a Box placed there stops failing S7. Results that appear
        only after inference come from inferred types widening the shapes' targets (with RDFS, every
        rdfs:domain of a field applies at once). Read this before turning inference on in shacl_validate.

        Args:
            content / path / blob_id: The scene, converted first; or graph_id from this session.
            shapes / extra_shapes_ttl / ontology_version / data_ttl: as for shacl_validate.
            inference: The derived profile to compare with "none": "rdfs" (default), "owlrl" or "both".
            sample: Results listed per bucket (default 25).

        Returns JSON {asserted {conforms, counts, results, distinct}, derived {...}, hidden_by_inference
        {count, violations, by_check, sample}, added_by_inference {count, focus_nodes, by_check, sample},
        unchanged {count, by_check}, verdict, graph_id, instrument, ms}. results counts the engine's raw
        results; distinct and the buckets compare (check, focus, path, value, severity).
        """
        t0 = time.perf_counter()
        if inference == "none":
            return _err("inference must be 'rdfs', 'owlrl' or 'both': the comparison is always against 'none'")
        try:
            sample = max(0, min(int(sample), 200))
            sg, inp, cached = await _scene(ctx, content, path, blob_id, graph_id)
            size = sg.triples * 40 + len(data_ttl)
            asserted = await run_heavy(sw.shacl, sg, shapes, extra_shapes_ttl, "none", ontology_version, None,
                                       data_ttl, size=size)
            derived = await run_heavy(sw.shacl, sg, shapes, extra_shapes_ttl, inference, ontology_version, None,
                                      data_ttl, size=size)
        except QueryTimeout as e:
            return _err(str(e))
        except (ValueError, sw.SceneError) as e:
            return _err(e)
        except RuntimeError as e:
            return _err(f"SHACL error: {e}")
        out = {"ok": True, "inference": inference, **counterfactual(asserted, derived, sample),
               "graph_id": sg.graph_id, "graph_sha256": sg.sha256}
        if not cached:
            out["converted"] = {"triples": sg.triples, "warnings": sg.warnings, "input": inp}
        out["instrument"] = sw.instrument(asserted["ontology_version"], shapes if shapes in sw.SHAPES else "x3d-core")
        out["ms"] = round((time.perf_counter() - t0) * 1000, 1)
        return json.dumps(out, indent=1)
