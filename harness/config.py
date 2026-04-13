"""Benchmark configuration — paths, URLs, system configs."""

from pathlib import Path

HOME = Path.home()
BENCHMARK_DIR = HOME / "memory-benchmark"
CORPUS_DIR = BENCHMARK_DIR / "corpus"
TEXT_DIR = CORPUS_DIR / "text"
SANDBOXES_DIR = BENCHMARK_DIR / "sandboxes"
RESULTS_DIR = BENCHMARK_DIR / "results"
HARNESS_DIR = BENCHMARK_DIR / "harness"

# Ollama (vader)
OLLAMA_URL = "http://192.168.0.126:11434/api/generate"
OLLAMA_MODEL = "qwen2.5-coder:7b"
OLLAMA_TIMEOUT = 90

# Production Chroma (for claude-mem old baseline)
PROD_CHROMA_PATH = str(HOME / ".claude-mem" / "chroma")
PROD_COLLECTION = "claude_memories"

# Sandbox paths
CLAUDE_MEM_FIXED_CHROMA = str(SANDBOXES_DIR / "claude-mem" / "chroma")
CLAUDE_MEM_FIXED_COLLECTION = "benchmark"

MEMPALACE_SANDBOX = SANDBOXES_DIR / "mempalace"
BASIC_MEMORY_SANDBOX = SANDBOXES_DIR / "basic-memory"
