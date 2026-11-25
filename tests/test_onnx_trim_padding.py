"""The Hugging Face pipeline pads every window of a long text to the longest one; the model skips the padding.

These use a stand-in model whose logits record which positions it computed, so they check the slicing and the
restoring of the pipeline's expected shape, not ONNX Runtime itself.
"""

import pytest

pytest.importorskip("optimum.onnxruntime")
torch = pytest.importorskip("torch")

from pii_shield.onnx_nlp_engine import _trim_padding, _trim_padding_enabled  # noqa: E402

LABELS = 3


class _Model:
    """Logits are the position index plus one, so the zero padding is easy to tell from computed rows."""

    def __init__(self):
        self.widths = []
        self.forward = self._forward

    def _forward(self, input_ids=None, attention_mask=None, token_type_ids=None, **kwargs):
        self.widths.append(input_ids.shape[-1])
        positions = torch.arange(1, input_ids.shape[-1] + 1, dtype=torch.float32)
        logits = positions[None, :, None].expand(input_ids.shape[0], -1, LABELS).clone()
        if kwargs.get("return_dict") is False:
            return (logits,)
        return {"logits": logits}


def _inputs(real: int, width: int, batch: int = 1):
    ids = torch.ones((batch, width), dtype=torch.long)
    mask = torch.zeros((batch, width), dtype=torch.long)
    mask[:, :real] = 1
    return {"input_ids": ids, "attention_mask": mask, "token_type_ids": torch.zeros_like(ids)}


def test_padding_is_cut_off_and_the_logits_keep_the_pipelines_width():
    model = _Model()
    _trim_padding(model)
    out = model.forward(**_inputs(real=5, width=8))
    assert model.widths == [5]
    logits = out["logits"]
    assert logits.shape == (1, 8, LABELS)
    assert logits[0, :5, 0].tolist() == [1, 2, 3, 4, 5] and logits[0, 5:].abs().sum() == 0


def test_a_full_window_is_left_alone():
    model = _Model()
    _trim_padding(model)
    model.forward(**_inputs(real=8, width=8))
    assert model.widths == [8]


def test_a_batch_is_cut_to_its_longest_row():
    model = _Model()
    _trim_padding(model)
    inputs = _inputs(real=4, width=8, batch=2)
    inputs["attention_mask"][1, :6] = 1
    out = model.forward(**inputs)
    assert model.widths == [6] and out["logits"].shape == (2, 8, LABELS)


def test_padding_that_is_not_a_suffix_is_not_touched():
    model = _Model()
    _trim_padding(model)
    inputs = _inputs(real=5, width=8)
    inputs["attention_mask"][0, 2] = 0
    model.forward(**inputs)
    assert model.widths == [8]


def test_tuple_outputs_and_missing_masks_pass_through():
    model = _Model()
    _trim_padding(model)
    assert isinstance(model.forward(**_inputs(real=3, width=8), return_dict=False), tuple)
    model.forward(input_ids=torch.ones((1, 8), dtype=torch.long))
    assert model.widths == [8, 8]


def test_the_switch(monkeypatch):
    monkeypatch.delenv("ONNX_TRIM_PADDING", raising=False)
    assert _trim_padding_enabled() is True
    monkeypatch.setenv("ONNX_TRIM_PADDING", "false")
    assert _trim_padding_enabled() is False
