# Tests

No credentials, no network. Run them with:

```bash
cd bridge && ../.venv/bin/python -m pytest
```

| File | What it covers |
| --- | --- |
| `test_normalise.py` | Quantity parsing and item identity — what makes two spoken items the same thing |
| `test_engine.py` | The three-way merge: adds, deletes, ticks, echo prevention, dedupe, the safety guard |
| `test_service.py` | Failure handling, backoff, alerting, health reporting, threading |
| `test_anylist_client.py` | HTTP client behaviour against a stubbed sidecar |
| `test_alerts.py` | Alert suppression |
| `test_sidecar_contract.py` | The Python/Node seam, against the real Express server |
| `test_end_to_end.py` | Whole user stories, with only Google itself simulated |

The last two boot `anylist-api/test/serve-stub.js`, which runs the real server
with the `anylist` package swapped for an in-memory stub. They skip
automatically if `npm install` has not been run in `anylist-api/`.
