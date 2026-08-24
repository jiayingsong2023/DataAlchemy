import hashlib
import json
import zipfile

import pytest
from pypdf import PdfReader

from scripts import import_multidoc2dial_fixture as importer


def test_build_fixture_verifies_source_and_isolates_splits(tmp_path, monkeypatch):
    documents = {}
    dialogues = []
    for number in range(3):
        doc_id = f"doc-{number}"
        answer = f"Grounded evidence answer {number}"
        documents[doc_id] = {
            "title": f"Document {number}",
            "doc_text": answer,
            "spans": {"1": {"text_sp": answer}},
        }
        dialogues.append(
            {
                "dial_id": f"dialogue-{number}",
                "turns": [
                    {
                        "role": "user",
                        "turn_id": 1,
                        "utterance": f"Question {number}?",
                        "references": [],
                    },
                    {
                        "role": "agent",
                        "turn_id": 2,
                        "utterance": answer,
                        "da": "respond_solution",
                        "references": [{"doc_id": doc_id, "id_sp": "1"}],
                    },
                ],
            }
        )

    source = tmp_path / "source.zip"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr(importer.DOC_MEMBER, json.dumps({"doc_data": {"domain": documents}}))
        archive.writestr(importer.DIAL_MEMBER, json.dumps({"dial_data": {"domain": dialogues}}))
    monkeypatch.setattr(importer, "SOURCE_SHA256", hashlib.sha256(source.read_bytes()).hexdigest())
    monkeypatch.setattr(
        importer,
        "SPLIT_SIZES",
        {"train": 1, "validation": 1, "evaluation_holdout": 1},
    )

    output = tmp_path / "fixture"
    manifest = importer.build_fixture(source, output)

    assert manifest["selection"]["document_isolated"] is True
    lineages = []
    for suite_descriptor in manifest["suites"]:
        suite = json.loads((output / f"suite-{suite_descriptor['split']}.json").read_text())
        assert suite["cases"][0]["split"] == suite_descriptor["split"]
        lineages.append(suite["cases"][0]["dataset_lineage"]["doc_id"])
        page = PdfReader(output / f"{suite_descriptor['split']}.pdf").pages[0]
        assert suite["cases"][0]["required_substrings"][0] in (page.extract_text() or "")
    assert len(set(lineages)) == 3

    source.write_bytes(b"tampered")
    with pytest.raises(ValueError, match="multidoc2dial_source_hash_mismatch"):
        importer.build_fixture(source, output)
