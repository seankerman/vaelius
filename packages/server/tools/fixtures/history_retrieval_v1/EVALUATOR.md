# H1 DEV observation grader

`tools/evaluate_history_dev.py` scores the public DEV contract only. It never
opens the private H confirmation or dispatches a model. A separate canonical
PostgreSQL runner must ingest the synthetic sources and produce observations;
this grader does not claim to run that pipeline or prove installed behavior.

Input JSON has `development_sha256` equal to `manifest.json`, plus optional
`source_to_curated`, `fixed_curated_retrieval` and `end_to_end` fields. Missing
layers are reported as unavailable. Extraction observations are a list of
normalized event records with `event_id`, `occurrence_id`, `source_order` and the
oracle field names in `FIELDS` in `evaluate_history_dev.py`. The runner assigns
fixture IDs **after** canonical extraction by exact source/evidence matching;
oracle IDs and answers must not be supplied to the model or production query.
The transport replay may appear only as `kind: transport_receipt`.

`fixed_curated_retrieval` has `input_layer: fixed_curated_records` and a
`questions` list. Each item has `question_id`, `mode`, `as_of`, `answerable`,
delivered `event_ids`, delivered `evidence_refs`, and for a denial the
`abstention_reason`. The long Oak history question also supplies `pages`, each
with `event_ids`, actual `serialized_bytes`, `has_more`, and `next_cursor`.
This layer must load the fixture's fixed curated records, not extracted records.

The grader reports exact 47 meaningful extraction events, 36 positive questions,
12 denial/abstention questions, citation precision and the Oak 320-byte page
invariant. `positive_structural_complete` checks event IDs, spans, mode and time;
it does not certify that free-form answer prose is complete. The latter requires
an independent facet review. A score from normalized observations is not proof
that the canonical PostgreSQL pipeline produced them. An end-to-end gate stays
unavailable until a separately verified source-to-delivery run is supplied.

Run from the AgentNetwork root:

```sh
python3 AgentHub/tools/evaluate_history_dev.py --observations /private/path/dev-observations.json --output /private/path/dev-score.json
PYTHONPATH=AgentHub:AgentClient:AgentHub/tests python3 -m unittest AgentHub/tests/test_history_dev_evaluator.py
```
