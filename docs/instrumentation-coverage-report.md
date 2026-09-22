# Dropzone instrumentation coverage report

This report is the MET-210 baseline and final result for the deterministic
offline matrix scenario. It runs real `ReadableSpan` objects through
`MetergraphGenAIExporter` and the capture runtime. It does not make provider
requests or require credentials.

Run it with:

```bash
python/.venv/bin/python -m pytest \
  python/tests/integrations/instrumentation/test_coverage_matrix.py -q
```

## Baseline before MET-210

The matrix contract existed, and direct provider, LiteLLM, Phoenix, and
Langfuse paths had focused tests. The remaining project-target paths did not
have one repeatable pass/fail scenario or one report covering unsupported
behavior. Bedrock, Azure, and LangSmith were therefore recorded as coverage
gaps even where a local dialect test already existed.

## Final matrix result

| Group | Scenarios | Result | Evidence |
| --- | ---: | --- | --- |
| OpenAI Python and TypeScript | 2 | PASS | exporter capture fixture |
| Anthropic Python and TypeScript | 2 | PASS | exporter capture fixture |
| Google Gemini Python and TypeScript | 2 | PASS | exporter capture fixture |
| Vercel AI Gateway and AI SDK | 2 | PASS | exporter capture fixture |
| OpenRouter | 1 | PASS | exporter capture fixture |
| LiteLLM legacy and current OTel shapes | 2 | PASS | exporter capture fixture |
| Amazon Bedrock | 1 | PASS | provider alias and exporter fixture |
| Azure OpenAI | 1 | PASS | provider alias and error fixture |
| OpenInference / Phoenix | 1 | PASS | flattened-message fixture |
| Langfuse | 1 | PASS | generation observation fixture |
| LangSmith | 1 | PASS | LLM run and span-kind fixture |
| Supported scenarios | 16 | PASS | `test_supported_matrix_paths_capture_required_fields` |

## Unsupported and degraded paths

| Path | Expected result | Result |
| --- | --- | --- |
| Ordinary non-GenAI span | `not-genai`, export succeeds | PASS |
| GenAI span without model | `no-model`, export succeeds | PASS |
| OpenInference chain | `ineligible-kind`, no row | PASS |
| Langfuse non-generation observation | `ineligible-kind`, no row | PASS |
| LangSmith chain | `ineligible-kind`, no row | PASS |
| Malformed optional content | capture metadata, `parse-degraded` | PASS |
| Azure provider error | error row preserves provider attribution | PASS |

The remaining intentional gaps are live provider qualification for Bedrock,
Azure, and LiteLLM credentials, plus upstream-version drift for those packages.
Those checks belong in the separate upstream and provider integration jobs and
are not hidden by this offline report.
