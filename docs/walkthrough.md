# DataAlchemy AI-Native Enhancement: Walkthrough

This walkthrough demonstrates the new "AI-Native" capabilities added to DataAlchemy, transforming it into a full-scale enterprise data factory.

---

## 1. Distributed Semantic Deduplication (MinHash LSH)

**Problem**: Multiple copies of the same email, log tailing, or database backups create "data pollution".
**Solution**: Use MinHash LSH to detect near-duplicates (e.g., 90% similarity).

### How it works:
1. Data is ingested from sources (Jira, Confluence, Documents).
2. Spark Job joins all data into a unified DataFrame.
3. `MinHashDedup` module identifies similar documents using Jaccard Distance.
4. Only the unique representative of each cluster is kept.

**Usage**: Integrated into `SparkEngine`. 
- **Config**: `threshold=0.9` (Default)
- **Module**: `src/etl/dedup/minhash_dedup.py`

---

## 2. Multi-Format SFT Synthesis (Alpaca & ShareGPT)

**Problem**: Different training frameworks (LLaMA-Factory, Axolotl) require different data formats.
**Solution**: `SFTGenerator` now supports multiple formats and multi-turn generation.

### Code Sample:
```python
from synthesis.sft_generator import SFTGenerator

# 1. Generate Alpaca-format data for LLaMA-Factory
alpaca_gen = SFTGenerator(output_format="alpaca", mode="single")
alpaca_gen.process_corpus("s3://data-alchemy/unique/")

# 2. Generate ShareGPT-format data for multi-turn dialogues
multi_gen = SFTGenerator(output_format="sharegpt", mode="multi")
multi_gen.process_corpus("s3://data-alchemy/unique/")
```

**Integration**: Automatically generates `dataset_info.json` in the output directory.

---

## 3. Hierarchical Semantic Chunking for RAG

**Problem**: Fixed-size windowing cuts sentences in half and loses header context.
**Solution**: Use `MarkdownChunker` or `RecursiveChunker`.

### Features:
- **`MarkdownChunker`**: Identifies `# H1` through `#### H4` and attaches them to every chunk's metadata.
- **`RecursiveChunker`**: Attempts to split on paragraph (`\n\n`), then line (`\n`), then sentence (`。 `) before cutting characters.

**Usage**:
```python
from rag.vector_store import VectorStore
from rag.chunkers.markdown import MarkdownChunker

store = VectorStore()
chunker = MarkdownChunker(max_chunk_size=1000)

# Documents are chunked and header metadata is preserved during ingestion
store.add_documents(documents, chunker=chunker)
```

---

## 4. Advanced PII Masking (Microsoft Presidio)

**Problem**: Regex can't identify person names or addresses reliably.
**Solution**: NER-based pass using Microsoft Presidio.

### Masking Flow:
1. **Pass 1 (Regex)**: Fast removal of API Keys, IP addresses, and Emails.
2. **Pass 2 (NER)**: Deep scan for `PERSON`, `LOCATION`, and `PHONE_NUMBER`.

**Usage**: 
- Automatically invoked by `advanced_sanitize()` in `src/etl/sanitizers.py`.
- **Requirements**: `pip install presidio-analyzer presidio-anonymizer spacy` + `python -m spacy download en_core_web_lg`.

---

## 5. Verification Checklist

- [x] **Deduplication**: Successfully removed 90% similar items in Spark cluster logs.
- [x] **SFT Output**: Generated `sft_train.jsonl` follows Alpaca/ShareGPT schemas.
- [x] **LLaMA-Factory**: `dataset_info.json` correctly updated.
- [x] **RAG Persistence**: Vector store now includes hierarchical header metadata in SQLite.
- [x] **Privacy**: Sensitive names now appear as `<PERSON>` in the cleaned corpus.
