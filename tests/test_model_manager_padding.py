from unittest.mock import MagicMock, patch

from src.inference.model_manager import ModelManager


def test_decoder_only_batches_are_left_padded(monkeypatch):
    monkeypatch.setattr(ModelManager, "_instance", None)
    manager = ModelManager()
    tokenizer = MagicMock(pad_token=None, eos_token="<eos>")
    model = MagicMock()
    monkeypatch.setattr(manager, "_warmup", lambda: None)

    with patch("src.inference.model_manager.AutoTokenizer.from_pretrained", return_value=tokenizer), patch(
        "src.inference.model_manager.AutoModelForCausalLM.from_pretrained", return_value=model
    ):
        manager.load_models("fixture-model", compile_model=False)

    assert tokenizer.pad_token == "<eos>"
    assert tokenizer.padding_side == "left"
