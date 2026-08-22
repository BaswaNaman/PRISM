# Drop the Unilog data pack in this folder

`run_unilog_pipeline.py --data-dir ./data` looks for these exact filenames. Any that
are missing are skipped, and the report says so explicitly rather than silently
scoring nothing.

    Unilog-Sample_200_Items-Input-vs-Output.xlsx      <- ground truth (the one that matters)
    Sample-1000_Items.xlsx                            <- volume input
    UniCat_Manufacturer_and_Brand_List.xlsx           <- 27k approved brands
    Unicat_Lov_v1_0_Updated_With_Remarks.xlsx         <- cross-category LOV
    FAUCETS_LOV.xlsx                                  <- category LOV
    Fittings_LOV.xlsx                                 <- category LOV
    Unilog_Master_UOM_Standards_Abbreviations_and_Terms.xlsx
    Decimal_Fraction.xlsx

Then:

    python run_unilog_pipeline.py --data-dir ./data

Requires `openpyxl` (in requirements.txt). Expect the loaders to need a small
adjustment on first contact with the real files — they were written against the
column names described in the brief, and real workbooks tend to have multi-row
headers and inconsistent sheet names. Each loader prints the header row it found.
