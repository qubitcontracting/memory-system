# Cognee Ollama Setup — WORKING (2026-04-13)

## Status: FULLY WORKING with qwen2.5:32b + nomic-embed-text on vader

## Fixes Required

### 1. Adapter patch (code change)
File: `cognee/infrastructure/llm/structured_output_framework/litellm_instructor/llm/ollama/adapter.py`

In `__init__`, replace the model/endpoint setup:
```python
# Strip ollama/ prefix — OpenAI client sends model name directly to Ollama
self.model = model.removeprefix("ollama/") if model else model
# Ensure endpoint includes /v1 for OpenAI-compatible API
self.endpoint = endpoint.rstrip("/")
if not self.endpoint.endswith("/v1"):
    self.endpoint += "/v1"
```

### 2. Full config (Python)
```python
import os
os.environ['COGNEE_SKIP_CONNECTION_TEST'] = 'true'
os.environ['OLLAMA_API_BASE'] = 'http://192.168.0.126:11434'

cognee.config.set_llm_config({
    'llm_provider': 'ollama',
    'llm_model': 'ollama/qwen2.5:32b',
    'llm_endpoint': 'http://192.168.0.126:11434',
    'llm_api_key': 'ollama',
})

cognee.config.set_embedding_config({
    'embedding_provider': 'ollama',
    'embedding_model': 'nomic-embed-text',
    'embedding_endpoint': 'http://192.168.0.126:11434/api/embed',
    'embedding_api_key': 'ollama',
    'embedding_dimensions': 768,
    'huggingface_tokenizer': 'nomic-ai/nomic-embed-text-v1',
})
```

### 3. Dependencies
```bash
pip install cognee transformers
```

## Models needed on Ollama
- `qwen2.5:32b` — LLM for entity extraction (structured output via instructor)
- `nomic-embed-text` — embeddings (768 dimensions)
- `qwen2.5-coder:7b` — also works for LLM but may fail on complex schemas

## Test Results
- "What port does Deal Finder run on?" → `Deal Finder runs on port 8200`
- "What depends on Ollama?" → `spool processor`
- "What IP is Vader?" → `192.168.0.126`

## Patched file
`~/memory-benchmark/cognee-ollama-patch.py`

## Apply patch
```bash
COGNEE_ADAPTER="$(python3 -c 'import cognee; import os; print(os.path.dirname(cognee.__file__))')/infrastructure/llm/structured_output_framework/litellm_instructor/llm/ollama/adapter.py"
cp ~/memory-benchmark/cognee-ollama-patch.py "$COGNEE_ADAPTER"
```
