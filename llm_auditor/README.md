# CoMAS LLM Auditor

This package audits completed CoMAS experiments without entering the runtime
decision or mitigation path. It reads `metadata.json`, `summary.json`, and
`timeline.ndjson`; it never publishes to ETCD, elects an executor, calls
FlowBlocker, or installs OpenFlow rules.

The deterministic mode is the source of truth:

```bash
python3 -m llm_auditor experiments/results/<run> --mode audit
```

The explanation mode gives the deterministic verdict to the LLM and asks it
only for a grounded explanation:

```bash
python3 -m llm_auditor experiments/results/<run> \
  --mode explain \
  --model qwen3.5:9b \
  --ollama-url http://127.0.0.1:12434
```

The evaluation mode hides the deterministic verdict and measures whether the
LLM independently reaches the same classification. It is experimental and
must not control mitigation:

```bash
python3 -m llm_auditor experiments/results/<run> \
  --mode evaluate \
  --model qwen3.5:9b \
  --ollama-url http://127.0.0.1:12434
```

Both LLM modes request an Ollama JSON Schema response and use temperature 0,
seed 42, a 4096-token context, and `keep_alive=0` by default. The generated
JSON records the inference parameters and Ollama timing counters.

## Synthetic protocol campaign

The protocol campaign isolates six declared fixtures: `AGREED` as claim
winner, `WAITING_PROPOSALS`, `AGREED` as authorized non-winner,
`CORROBORATED`, `VETOED`, and benign `NORMAL`. It evaluates each categorical
field independently and preserves the Ollama response, timing, parameters,
expected oracle, and exact comparison:

```bash
python3 -m llm_auditor.protocol_campaign \
  --model qwen3.5:9b \
  --ollama-url http://127.0.0.1:12434 \
  --seeds 42 \
  --output llm_protocol_campaign.json
```

After the single-seed pilot, seed stability can be checked with:

```bash
python3 -m llm_auditor.protocol_campaign \
  --model qwen3.5:9b \
  --ollama-url http://127.0.0.1:12434 \
  --seeds 1,7,42,2026,9999 \
  --output llm_protocol_campaign_seeds.json
```

These inputs are explicitly marked as `synthetic_protocol_fixture`. They test
whether the LLM applies the documented protocol semantics; they are not new
network experiments and provide no evidence about detection accuracy,
coordination scalability, or mitigation effectiveness. Repetitions over the
same fixtures are not independent network observations, so the output reports
descriptive exact-match rates and does not attach a confidence interval.
