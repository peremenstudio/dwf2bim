---
name: next-milestone
description: Continue building the product from its specification - pick the next unfinished milestone in docs/PRODUCT_SPEC.md and carry it through. Use when asked to continue, to go on with the plan or the spec, or "מה הלאה".
---

# Carry the next milestone

1. Read `docs/PRODUCT_SPEC.md` in full, then `docs/BUILD_PROTOCOL.md`. The roadmap table in the spec says what is done.
2. Check section 6, "Open questions". If the milestone depends on one that has no answer written there, ask the person first, and write the answer into the spec.
3. Take the baseline before changing anything: rebuild every corpus project into a scratch folder and keep each `quality.json` (see the fix-model skill for the corpus and the commands).
4. Build the milestone in small steps. Each step follows the fix-model skill: scope stated, unit test with made-up numbers, corpus rebuilt, verdicts compared.
5. A milestone is done when its "Done when" column holds on every corpus project. Then:
   - move its rules into `docs/BUILD_PROTOCOL.md`;
   - mark the row done in the spec, with the date;
   - update section 4 of the spec with fresh measurements.
6. Review the models with the review-model skill. Every fault the checks missed becomes a new check, or a new row in the roadmap.
7. Report in Hebrew: what was built, the numbers before and after per project, what the person must run in Revit, and what comes next.

Do not start a second milestone in the same pass without telling the person the first is done and what it changed.
