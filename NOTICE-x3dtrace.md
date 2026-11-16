# NOTICE for x3dtrace (vendored copy)

`src/x3d_utils/x3dtrace.py` is a copy of `src/x3dtrace.py` from the x3d.ML repository (hagopjay/x3d.ML), commit 305fd601a678fc75f4a9196e6e1497615fd7f0ab (the head of pull request 13 there). Do not edit it here: change it in x3d.ML and copy it.
It is standard library only. The copy's integrity is checked by `tests/test_x3dtrace_vendored.py`, which reads the line below and the conformance rows in `tests/fixtures/trace_conformance.json` (the same file as in x3d.ML).

x3dtrace.py sha256 dcba70d739007c0dce2095d91346a8321400aed36cf1d1fe519c6fac23554462

The notice that travels with x3dtrace follows.

x3dtrace is an independent implementation. It was written from a behavioural description of GAIK's `source_tracing` (a written
algorithm spec and the published example vectors); no GAIK source file or function body was copied into it. Because the
verdict table, the report field names and the default policy values follow GAIK, the MIT notice below is reproduced in full.

## Ideas and vocabulary we took, with credit

- **Method.** The quote-and-verify method (the writer copies a short exact quote beside each value, and plain code then
  FINDs the quote in the source, COMPAREs the value with the quote and LOCATEs it) is described in Umair Ali Khan,
  "How I Added Traceability in Structured Data Extraction with AI", AI Advances (Medium), November 2026.
- **GAIK toolkit.** The verdict names (`verified`, `check`, `reworded`, `unchecked`), the item field names (`field`,
  `value`, `quote`, `document`, `page`, `cited_page`, `status`, `value_status`, `verdict`, `rects`, `value_rects`, `span`,
  `occurrences`, `record`), the report shape (`summary`, `documents`, `sources`) and the example value/quote pairs in
  `tests/fixtures/trace_conformance.json` that carry `"origin": "gaik-test"` or `"gaik-readme"` come from the
  `source_tracing` component of the GAIK toolkit, https://github.com/GAIK-project/gaik-toolkit (reference commit
  394a94b, 16 November 2026).

GAIK is published under the MIT licence. Its notice, which applies to the example pairs and the names above, reads:

    MIT License

    Copyright (c) 2026 GAIK - GenAI for knowledge mgt

    Permission is hereby granted, free of charge, to any person obtaining a copy
    of this software and associated documentation files (the "Software"), to deal
    in the Software without restriction, including without limitation the rights
    to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
    copies of the Software, and to permit persons to whom the Software is
    furnished to do so, subject to the following conditions:

    The above copyright notice and this permission notice shall be included in all
    copies or substantial portions of the Software.

    THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
    IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
    FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
    AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
    LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
    OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
    SOFTWARE.

## Where x3dtrace deliberately differs from GAIK (and why)

GAIK's matcher is tuned for invoices and purchase orders. A scene is a different kind of source, so x3dtrace changes
these points (each has a test in `tests/test_x3dtrace.py`):

| Point | GAIK `source_tracing` | x3dtrace |
|---|---|---|
| Sign, decimal point, exponent | dropped by the tokeniser (`-1` equals `1`, `0.5` equals `0 5`, `1e-5` reads as 1 and 5) | kept in number tokens |
| Vectors (SFVec3f, SFColor, SFRotation) | strings read as dates or digit runs, lists unchecked | compared per component, in order |
| Identifiers (DEF, USE) | lowercased, a wrong lowercase word is `unchecked` | case-sensitive, a wrong identifier is `check` |
| Unicode | `str.lower()` only | NFC, casefold in prose, soft hyphen removed |
| Entities (`&amp;`) | read as the word `amp` | decoded in scene text |
| Units | a converted value is `check` | a conversion is accepted only when the quote itself carries the unit (`90 degrees`, `50 %`, `2 ft`, `#ff0000`) and the item records the conversion |
| Repeated quote | boxed at the first occurrence | prefix, suffix, node path or line choose the occurrence (W3C TextQuoteSelector style) |
| Coverage | share of values that have a citation | also `verified_coverage`, the share whose quote was verified |
| Position | PDF page boxes (needs PyMuPDF) | character span, line and column, node path, DEF path and world-space box; PDF positions are imported from a GAIK report |

Nothing here was sent upstream.
