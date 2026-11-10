# ChemClaw MOF Handwritten Records Suite - English Edition

This is a fully English-language localization of `chemclaw_mof_handwritten_records_suite.zip`, supplied by the user. It contains the same three UiO-66 samples and the same six dated record pages, P01-P06.

## Contents

- `images/`: six English simulated handwritten notebook pages, in chronological order. All visible record text, annotations and diagram labels are English. Images were localized with the built-in imagegen tool, with manual visual checks.
- `ChemClaw_MOF_Handwritten_Lab_Notebook_P01-P06_EN.pdf`: the same six English images assembled into an image-only PDF, suitable for visual/OCR evaluation.
- `chemclaw_mof_lab_records_benchmark_en.md`: translated background, complete textual records, evaluation questions, ground truth and standardized reference table. **Evaluator-only: this file contains answers.**
- `inputs/records_P01-P06_en.md`: complete translated records only, excluding the background, questions and answer key.
- `inputs/pages/`: one translated text record per page, for staged or page-by-page testing.
- `evaluation_prompts_en.json`: four English evaluation prompts, with IDs L1, L2, H1 and H2.
- `image_generation_prompts.json`: the English prompts used to localize the notebook images, including the final FT-IR and PXRD annotation corrections.
- `manifest.json`: page mapping, file hashes and validation results.

## Running an evaluation

For visual evaluation, supply only the six files in `images/` or the six-page PDF, then ask one of the English evaluation prompts. For text-only evaluation, supply `inputs/records_P01-P06_en.md` instead. The files in `inputs/pages/` support chronological ingestion without revealing later pages.

Keep the benchmark guide and other evaluator files outside the system's record input. The source notebook itself already contains some conversions and explanatory annotations; these are intentionally retained as source content.

## What is preserved

- Sample IDs `Zr-BDC-01`, `Zr-BDC-02`, `Zr-BDC-03` and batch IDs `EXP-1012-A`, `EXP-1012-B`, `EXP-1013-C`.
- Dates, measurements, synthesis and activation steps, yields, characterization results and performance values.
- Crossed-out `200 mg` corrected to `233.2 mg`, and crossed-out `2.0 mL` corrected to `2.30 mL`.
- The implicit reference "same as the previous batch," with actual weights recorded separately for sample 02.
- Mixed raw units: sample 03 charges in g, water in mL/µL, sample 02 uptake in cm³/g STP, and low pressure in kPa/bar.
- The activation rework sequence, uncertainty about the ZrO₂ impurity, and the distinction between a planned operation and its later recorded completion.

The full text is translated from the supplied Markdown records. The image text is reflowed and sometimes abbreviated to remain readable on six handwritten pages. The scientific interpretations and the source's description of the 1.85 nm defect-cage feature are retained as statements from this synthetic benchmark. Small drawn plots are illustrative schematics; use the written numerical values for evaluation.

## Localization and provenance

Language: English. Creation date: 2026-10-28 (Asia/Shanghai). Source: the user-supplied original ZIP, rather than other revised benchmarks in the workspace. The original archive is unchanged.

The English image pages are newly localized synthetic notebook images. They are not scans of independently performed experiments. The PDF contains those exact image pages; the separate Markdown files provide the complete readable transcripts.

