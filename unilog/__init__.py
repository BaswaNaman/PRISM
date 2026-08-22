"""
Unilog / UniHack compliance layer.
==================================

PRISM's original engine answers "what does this document say about this product?".
The UniHack brief asks a different question: "rewrite this catalogue row so it
conforms to Unilog's published content standard." This package implements that
second job, and is deliberately decoupled from the FastAPI app so it can be run
head-less against the supplied spreadsheets.

Modules
-------
fractions_util : decimal <-> fraction inch conversion (exact 64ths, per
                 Decimal_Fraction.xlsx). Complete and data-independent.
uom            : normalise unit strings to Unilog's approved abbreviation and
                 enforce the "space between number and unit" house rule.
descriptions   : build the five required description formats (Invoice, Mobile,
                 Product Title / Short, Long Description) with char-limit checks.
brands         : canonicalise messy manufacturer/brand strings against the
                 approved UniCat list; filters "-- Unbranded --" placeholders.
lov            : load the List Of Values and validate that every generated
                 attribute value is inside the controlled vocabulary.
dedup          : detect duplicate catalogue rows by normalised MPN + brand.
evaluate       : score generated output against the 200-item ground truth
                 (field-level accuracy, char-limit compliance, % values in LOV).

Data dependency
---------------
`fractions_util`, `uom` and `descriptions` are fully functional as shipped.
`brands`, `lov` and `evaluate` need the client spreadsheets; each exposes a
`load_*` function that reads the workbook when present and degrades to a clearly
reported "no data" state when it is absent, so nothing silently fabricates.
Drop the workbooks into ./data/ and they activate automatically.
"""

__all__ = [
    "fractions_util",
    "uom",
    "descriptions",
    "brands",
    "lov",
    "dedup",
    "evaluate",
]
