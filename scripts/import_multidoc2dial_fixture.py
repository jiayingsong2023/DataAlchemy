"""Build deterministic PDF/RAG suites from the pinned public MultiDoc2Dial archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import textwrap
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from harness.evaluation import validate_suite_manifest
from harness.product_loop import parse_document

SOURCE_URL = "https://doc2dial.github.io/multidoc2dial/file/multidoc2dial.zip"
SOURCE_SHA256 = "f0c034c249663d7b3cb08b19cf2cc2c3d101372485be982621d4711931a1ce00"
DATASET_REVISION = "1108a969d076f04c7367f0c2427d1c5d6d6bdaa0"
LICENSE = "Apache-2.0"
SPLIT_SIZES = {"train": 20, "validation": 8, "evaluation_holdout": 12}
DOC_MEMBER = "multidoc2dial/multidoc2dial_doc.json"
DIAL_MEMBER = "multidoc2dial/multidoc2dial_dial_train.json"


def _sha256(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _ascii(value: str) -> bool:
    try:
        value.encode("ascii")
    except UnicodeEncodeError:
        return False
    return bool(value.strip())


def _pdf(pages: list[list[str]]) -> bytes:
    """Write the small text-only PDF fixture without adding a PDF-generation dependency."""
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"",  # filled after page object numbers are known
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    page_refs = []
    for lines in pages:
        page_number = len(objects) + 1
        content_number = page_number + 1
        page_refs.append(f"{page_number} 0 R")
        escaped = [
            line.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
            for line in lines
        ]
        commands = ["BT /F1 8 Tf 45 755 Td 10 TL"]
        commands.extend(f"({line}) Tj T*" for line in escaped)
        commands.append("ET")
        stream = "\n".join(commands).encode("ascii")
        objects.append(
            (
                f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
                f"/Resources << /Font << /F1 3 0 R >> >> /Contents {content_number} 0 R >>"
            ).encode("ascii")
        )
        objects.append(
            b"<< /Length "
            + str(len(stream)).encode()
            + b" >>\nstream\n"
            + stream
            + b"\nendstream"
        )
    objects[1] = f"<< /Type /Pages /Count {len(pages)} /Kids [{' '.join(page_refs)}] >>".encode()

    body = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, value in enumerate(objects, 1):
        offsets.append(len(body))
        body.extend(f"{number} 0 obj\n".encode() + value + b"\nendobj\n")
    xref = len(body)
    body.extend(f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode())
    body.extend(b"".join(f"{offset:010} 00000 n \n".encode() for offset in offsets[1:]))
    body.extend(
        f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    )
    return bytes(body)


def _candidates(archive: zipfile.ZipFile) -> list[dict[str, str]]:
    with archive.open(DOC_MEMBER) as stream:
        by_domain = json.load(stream)["doc_data"]
    documents = {
        doc_id: document for domain in by_domain.values() for doc_id, document in domain.items()
    }
    with archive.open(DIAL_MEMBER) as stream:
        dialogues = json.load(stream)["dial_data"]

    candidates: list[dict[str, str]] = []
    seen_docs: set[str] = set()
    for domain in sorted(dialogues):
        for dialogue in sorted(dialogues[domain], key=lambda item: item["dial_id"]):
            turns = dialogue["turns"]
            for index, turn in enumerate(turns[:-1]):
                response = turns[index + 1]
                refs = response.get("references", [])
                doc_ids = {ref.get("doc_id") for ref in refs}
                if (
                    turn.get("role") != "user"
                    or response.get("role") != "agent"
                    or len(doc_ids) != 1
                ):
                    continue
                doc_id = next(iter(doc_ids))
                document = documents.get(doc_id)
                if (
                    not document
                    or doc_id in seen_docs
                    or response.get("da") == "respond_no_solution"
                ):
                    continue
                spans = document["spans"]
                evidence = [spans[str(ref["id_sp"])]["text_sp"] for ref in refs]
                answer = " ".join(evidence).strip().replace("\n", " ")
                answer = " ".join(answer.split())
                utterance = " ".join(str(turn.get("utterance", "")).split())
                history = " ".join(
                    f"{item['role'].title()}: {' '.join(str(item['utterance']).split())}"
                    for item in turns[max(0, index - 4) : index]
                )
                question = (
                    f"Conversation: {history} Current user: {utterance}"
                    if history
                    else utterance
                )
                title = " ".join(str(document.get("title", doc_id)).split())
                values = (doc_id, title, question, answer)
                if not (
                    8 <= len(answer) <= 65
                    and len(re.findall(r"[A-Za-z]{2,}", answer)) >= 4
                    and all(_ascii(value) for value in values)
                ):
                    continue
                candidates.append(
                    {
                        "dialogue_id": dialogue["dial_id"],
                        "turn_id": str(turn["turn_id"]),
                        "domain": domain,
                        "doc_id": doc_id,
                        "title": title,
                        "question": question,
                        "answer": answer,
                    }
                )
                seen_docs.add(doc_id)
    return candidates


def _page(candidate: dict[str, str]) -> list[str]:
    header = [
        candidate["title"],
        f"Domain: {candidate['domain']}",
        f"Document ID: {candidate['doc_id']}",
        "",
    ]
    evidence = textwrap.wrap(f"Grounded evidence: {candidate['answer']}", width=92)
    return header + evidence


def build_fixture(source_zip: Path, output_dir: Path) -> dict[str, Any]:
    """Verify, select, materialize, and re-read the complete public fixture."""
    archive_body = source_zip.read_bytes()
    if _sha256(archive_body) != SOURCE_SHA256:
        raise ValueError("multidoc2dial_source_hash_mismatch")
    with zipfile.ZipFile(source_zip) as archive:
        candidates = _candidates(archive)
    required = sum(SPLIT_SIZES.values())
    if len(candidates) < required:
        raise ValueError("multidoc2dial_candidates_insufficient")

    output_dir.mkdir(parents=True, exist_ok=True)
    cursor = 0
    suites = []
    selected_doc_ids: dict[str, list[str]] = {}
    for split, size in SPLIT_SIZES.items():
        selected = candidates[cursor : cursor + size]
        cursor += size
        pdf_path = output_dir / f"{split}.pdf"
        pdf_body = _pdf([_page(item) for item in selected])
        pdf_path.write_bytes(pdf_body)
        source_path = str(pdf_path.resolve())
        try:
            source_path = pdf_path.resolve().relative_to(Path.cwd().resolve()).as_posix()
        except ValueError:
            pass
        suite = {
            "version": f"multidoc2dial-{DATASET_REVISION[:12]}-{split}-v1",
            "policy_version": "public-rag-fixture-v1",
            "source": {
                "path": source_path,
                "sha256": _sha256(pdf_body),
                "pages": size,
                "license": LICENSE,
                "dataset_revision": DATASET_REVISION,
            },
            "cases": [
                {
                    "case_id": f"multidoc2dial-{split}-{number:03d}",
                    "query": item["question"],
                    "split": split,
                    "expected_status": "grounded",
                    "required_substrings": [item["answer"]],
                    "required_pages": [number],
                    "dataset_lineage": {
                        key: item[key]
                        for key in ("dialogue_id", "turn_id", "domain", "doc_id")
                    },
                }
                for number, item in enumerate(selected, 1)
            ],
        }
        suite = validate_suite_manifest(suite)
        suite_path = output_dir / f"suite-{split}.json"
        suite_path.write_text(
            json.dumps(suite, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        parsed = parse_document(pdf_body, pdf_path.name)
        if len(parsed) != size:
            raise RuntimeError("multidoc2dial_pdf_page_count_mismatch")
        for case in suite["cases"]:
            page = parsed[case["required_pages"][0] - 1]["text"]
            if case["required_substrings"][0] not in page:
                raise RuntimeError("multidoc2dial_answer_not_in_pdf")
        suites.append(
            {"split": split, "path": str(suite_path), "sha256": _sha256(suite_path.read_bytes())}
        )
        selected_doc_ids[split] = [item["doc_id"] for item in selected]

    all_doc_ids = [doc_id for values in selected_doc_ids.values() for doc_id in values]
    if len(set(all_doc_ids)) != required:
        raise RuntimeError("multidoc2dial_split_leakage")
    manifest = {
        "schema_version": "public_rag_fixture.v1",
        "dataset": "IBM/MultiDoc2Dial",
        "dataset_revision": DATASET_REVISION,
        "source_url": SOURCE_URL,
        "source_sha256": SOURCE_SHA256,
        "license": LICENSE,
        "license_evidence": (
            "https://huggingface.co/datasets/IBM/multidoc2dial/blob/"
            f"{DATASET_REVISION}/README.md"
        ),
        "selection": {"source_split": "train", "counts": SPLIT_SIZES, "document_isolated": True},
        "suites": suites,
    }
    (output_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-zip", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path("data/public/multidoc2dial-v1"))
    args = parser.parse_args()
    source_zip = args.source_zip
    if source_zip is None:
        source_zip = args.output_dir / "source" / "multidoc2dial.zip"
        source_zip.parent.mkdir(parents=True, exist_ok=True)
        if not source_zip.exists():
            request = urllib.request.Request(SOURCE_URL, headers={"User-Agent": "DataAlchemy/1"})
            with urllib.request.urlopen(request, timeout=60) as response:
                source_zip.write_bytes(response.read())
    manifest = build_fixture(source_zip, args.output_dir)
    print(json.dumps(manifest, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
