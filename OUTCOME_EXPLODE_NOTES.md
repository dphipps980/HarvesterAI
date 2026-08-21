# Outcome exploding in the wide export — findings and plan

`_explode_outcome_cols()` used to turn the outcome-specifier and `xPO_/xSO_GMeansTable`
JSON columns into flat per-outcome columns in `Human_Wide`. It is **removed from the
export path** (2026-08-21) because it produced wrong control-group numbers and a very
large number of columns. The specifier and means-table columns now come out as raw JSON,
which is lossless — everything below is what has to be true before it goes back in.

The last version of the function is in git: `git show d517ad3:app.py` (`_explode_outcome_cols`,
plus `_unique_label`). `_explode_group_cols` and `_explode_cortable_cols` are untouched and
still run — the IG/CG name and N columns they produce were checked and are correct.

## What was wrong

1. **Every CG column was a copy of the IG column.** The value lookup walked
   `omeans["data"]` and took the first group it found, for both IG and CG, with a TODO
   admitting the group role was never cross-referenced. Checked on the real export
   (`O_PO0_*__Pre_Means`, YEAH WP1, Main Extraction): 61 papers with data, IG_M equal to
   CG_M in all 61, different in none. **Control-group means were never exported.**

2. **A whole block could be skipped.** Column detection classified a column from
   `dropna().iloc[0]` — the first non-empty value only. `POutcomeSpecifier4` (diet) starts
   with a paper whose answer is `[]`, so `_is_outcomespec` said no and the entire block was
   left unexploded, silently. Same fragility in the group and cortable exploders.

3. **Stale outcome ids inflated the sheet.** Column ids came from the union of keys in the
   means JSON, which keeps entries for outcomes later deleted from the specifier.
   `xPO4_GMeansTable` carries ids 0–25 while no paper lists more than 14 diet outcomes, so
   ~12 outcomes' worth of columns were empty by construction.

4. **Names could not be parsed.** `O_{label}{outcome_id}_...` concatenated the two, so
   `O_PO30_IG_M__Pre_Means` reads as either PO3/outcome 0 or PO/outcome 30.

## What the data actually looks like (YEAH WP1, run "Main Extraction")

- 129 papers have means data, and **all 129 have a GroupSpecifier** — cross-referencing
  roles is safe and loses nothing. Role combinations: 77 intervention+control,
  27 intervention-only, 21 with one or more `other` arms, 1 with a blank role.
- `other` arms (3rd/4th study arms, 21 papers) are dropped by both exploders — IG/CG only.
  Decide whether they need OG1/OG2 columns before reinstating.
- Outcomes per paper, by block (max): PO 8, PO2 16, PO3 8, PO4 14, PO5 5, PO6 11,
  SO 8, SO2 17. Wide format sizes every row to the maximum, so one 17-outcome paper gives
  every row 17 outcome slots.
- Means columns are `ids × 4 (IG/CG × M/SD) × timepoints` per block: 1,724 columns for the
  full project, 1,180 in a filtered export. Timepoints run Pre/Post/FU/FU1/FU2.

## Plan for putting it back

1. Have `_explode_group_cols` return its per-paper `{group_id: group}` map and pass it in,
   so IG takes the `intervention` arm and CG the `control` arm by id. Leave a cell blank
   when the paper has no arm in that role (27 papers are intervention-only). Fall back to
   entry order only when a paper has no group roles at all.
2. Detect specifier/means columns by scanning the column for any matching value, not
   `iloc[0]`.
3. Restrict outcome ids per paper to the ids in that paper's specifier answer, then union.
4. Name columns `O_{block}_o{id}_{IG|CG}_{M|SD}__{timepoint}_{question}`.
5. Consider capping or grouping the means block — 1,700 columns is hard to use even when
   correct. A separate long-format "means" sheet (paper, run, block, outcome, arm,
   timepoint, M, SD) may be the better shape.

Block labels (`PO`, `PO2`…`SO2`) come from the extractor qtypes and are still used for
column naming — see `_outcome_block` and `_wide_col_keys` in `app.py`.
