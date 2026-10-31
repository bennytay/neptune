"""The benchmark harness: the same Claude, three kinds of memory, one fixed question set.

- **Arms** (``arms``): ``files`` gives Claude the site folder with list / read / grep / Python /
  view-image tools; ``context`` puts every file in the prompt, oldest feed lines dropped first
  when the site is bigger than the window; ``neptune`` gives it the ``neptune_site_*`` tools.
  The same system frame, model, effort and tool-call budget for all three.
- **Questions** (``questions``): the gold questions of Plant 7, Harbor and the public UR3 site,
  each with the facts its answer must hold. Nothing here imports the arms' answers.
- **Scoring** (``score``): a judge model marks each expected fact stated, missing or
  contradicted, and whether the answer gave a value where the truth is unknown, stale or
  pending (a made-up value); citations are checked against the raw files without a model.
- **Results** are one JSON line per question and arm with the full transcript and the judge's
  verdicts, so a recorded run is summarised again without calling a model.

``python -m neptune_context.bench run|summary`` (``make bench``).
"""
