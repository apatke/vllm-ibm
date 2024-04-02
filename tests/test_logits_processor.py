import random
from typing import Tuple
from unittest.mock import patch

import pytest
import torch

from vllm.model_executor.layers.logits_processor import LogitsProcessor
from vllm.model_executor.utils import set_random_seed
from vllm.sequence import SamplingParams, SequenceData, SequenceGroupMetadata
from vllm.tgis_utils.logits_processors import LengthPenaltyWarper
from vllm.worker.model_runner import ModelRunner


class MockLogitsProcessor(LogitsProcessor):

    def __init__(self, vocab_size: int, scale: float,
                 fake_logits: torch.Tensor):
        super().__init__(vocab_size=vocab_size, scale=scale)
        self.fake_logits = fake_logits.clone()

    def forward(self, *args, **kwargs):
        with patch(
                "vllm.model_executor.layers.logits_processor._prune_hidden_states",
                lambda x, y: x
        ), patch(
                "vllm.model_executor.layers.logits_processor.LogitsProcessor._get_logits",
                lambda *args, **kwargs: self.fake_logits):
            return super().forward(*args, **kwargs)


def _prepare_test(
    batch_size: int
) -> Tuple[torch.Tensor, torch.Tensor, MockLogitsProcessor, ModelRunner]:
    vocab_size = 32000
    input_tensor = torch.rand((batch_size, 1024), dtype=torch.float16)
    fake_logits = torch.full((batch_size, vocab_size),
                             1e-2,
                             dtype=input_tensor.dtype)
    logits_processor = MockLogitsProcessor(32000, 0.5, fake_logits)
    model_runner = ModelRunner(None, None, None, None, None)
    return input_tensor, fake_logits, logits_processor, model_runner


RANDOM_SEEDS = list(range(128))
CUDA_DEVICES = [
    f"cuda:{i}" for i in range(1 if torch.cuda.device_count() == 1 else 2)
]


@pytest.mark.parametrize("seed", RANDOM_SEEDS)
@pytest.mark.parametrize("device", CUDA_DEVICES)
def test_logits_processors(seed: int, device: str):
    set_random_seed(seed)
    torch.set_default_device(device)
    batch_size = random.randint(1, 256)
    input_tensor, fake_logits, logits_processor, model_runner = _prepare_test(
        batch_size)

    # This sample logits processor gives infinite score to the i-th token,
    # where i is the length of the input sequence.
    # We therefore expect the output token sequence to be [0, 1, 2, ...]
    def pick_ith(token_ids, logits):
        logits[len(token_ids)] = float("inf")
        return logits

    seq_group_metadata_list = []
    prompt_lens = []
    for i in range(batch_size):
        seq_group_metadata_list.append(
            SequenceGroupMetadata(
                request_id=f"test_{i}",
                is_prompt=True,
                seq_data={0: SequenceData([1, 2, 3])},
                sampling_params=SamplingParams(temperature=0,
                                               logits_processors=[pick_ith]),
                block_tables={0: [1]},
            ))
        prompt_lens.append(seq_group_metadata_list[-1].seq_data[0].get_len())

    sampling_metadata = model_runner._prepare_sample(seq_group_metadata_list,
                                                     prompt_lens,
                                                     subquery_lens=prompt_lens)
    logits_processor_output = logits_processor(
        embedding=None,
        hidden_states=input_tensor,
        sampling_metadata=sampling_metadata)

    assert torch.isinf(logits_processor_output[:, 0]).all()

    fake_logits *= logits_processor.scale
    assert torch.allclose(logits_processor_output[:, 1], fake_logits[:, 1],
                          1e-4)

    del model_runner


@pytest.mark.parametrize("seed", RANDOM_SEEDS)
@pytest.mark.parametrize("device", CUDA_DEVICES)
def test_exponential_decay_length_penalty(seed: int, device: str):
    set_random_seed(seed)
    torch.set_default_device(device)
    batch_size = random.randint(1, 256)
    input_tensor, fake_logits, logits_processor, model_runner = _prepare_test(
        batch_size)
    logits_processor.scale = 1.0

    eos_token_id = 100
    lenpen = LengthPenaltyWarper([2, 2.0], eos_token_id)

    seq_group_metadata_list = []
    prompt_lens = []
    for i in range(batch_size):
        seq_group_metadata_list.append(
            SequenceGroupMetadata(
                request_id=f"test_{i}",
                is_prompt=True,
                # Output length 4 exceeds penalty start index by 2
                seq_data={0: SequenceData([1, 2, 3], [1, 2, 3, 4])},
                sampling_params=SamplingParams(temperature=0,
                                               logits_processors=[lenpen]),
                block_tables={0: [1]},
            ))
        prompt_lens.append(seq_group_metadata_list[-1].seq_data[0].get_len())

    sampling_metadata = model_runner._prepare_sample(seq_group_metadata_list,
                                                     prompt_lens,
                                                     subquery_lens=prompt_lens)
    logits_processor_output = logits_processor(
        embedding=None,
        hidden_states=input_tensor,
        sampling_metadata=sampling_metadata)

    assert torch.allclose(logits_processor_output[:, eos_token_id],
                          fake_logits[:, eos_token_id] * 4.0, 1e-4)
    assert torch.allclose(logits_processor_output[:, :eos_token_id],
                          fake_logits[:, :eos_token_id], 1e-4)
    assert torch.allclose(logits_processor_output[:, eos_token_id + 1:],
                          fake_logits[:, eos_token_id + 1:], 1e-4)

    del model_runner