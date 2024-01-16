import os
import tempfile
import itertools

from text_generation_server.utils.tokens import batch_top_tokens
import torch

from dataclasses import dataclass
from opentelemetry import trace
from transformers import AutoTokenizer, AutoModelForCausalLM, PreTrainedTokenizerBase, AutoConfig
from typing import Optional, Tuple, List, Type, Dict
from habana_frameworks.torch.hpu import wrap_in_hpu_graph
import habana_frameworks.torch as htorch
from contextlib import nullcontext
from optimum.habana.utils import HabanaProfile

from optimum.habana.transformers.generation import MODELS_OPTIMIZED_WITH_STATIC_SHAPES
from optimum.habana.checkpoint_utils import (
    get_repo_root,
    model_on_meta,
    write_checkpoints_json,
)

from text_generation_server.models import Model
from text_generation_server.models.types import (
    Batch,
    PrefillTokens,
    Generation,
    GeneratedText,
    TopTokens,
)
from text_generation_server.pb import generate_pb2
from text_generation_server.utils import HeterogeneousNextTokenChooser, StoppingCriteria, Sampling
from loguru import logger

tracer = trace.get_tracer(__name__)

BATCH_BUCKET_SIZE = int(os.environ.get('BATCH_BUCKET_SIZE', 8))
PREFILL_BATCH_BUCKET_SIZE = int(os.environ.get('PREFILL_BATCH_BUCKET_SIZE', 4))
TRACE_FILENAME = os.environ.get('TRACE_FILENAME')

def trace(txt):
    if TRACE_FILENAME is not None:
        print(txt, flush=True, file=open(TRACE_FILENAME, 'a'))


def round_up(number, k):
    return (number + k - 1) // k * k


def batch_alloc(new_bs, tensor):
    return tensor.new_empty((new_bs,) + tensor.shape[1:])


def to_tensors(indices, device):
    def convert(idx):
        return torch.tensor(idx, device=device)
    return [[(convert(dst), convert(src)) for dst, src in batch_ind] for batch_ind in indices]


def move_data(dst_tensor, chunk_size, indices, src_tensors):
    batch_dim = 0
    bs = dst_tensor.size(batch_dim)
    assert bs % chunk_size == 0, 'Batch dim must be divisible by chunk size!'
    result = dst_tensor
    if chunk_size > 1:
        dst_tensor = dst_tensor.view(bs // chunk_size, chunk_size, *dst_tensor.shape[1:])
    htorch.core.mark_step()
    for ind, src_t in zip(indices, src_tensors):
        if chunk_size > 1:
            src_t = src_t.view(bs // chunk_size, chunk_size, *src_t.shape[1:])
        for dst_idx, src_idx in ind:
            src_data = torch.index_select(src_t, batch_dim, src_idx)
            dst_tensor.index_copy_(batch_dim, dst_idx, src_data)
            htorch.core.mark_step()
    return result


def shift(tensor, dim, offset):
    shape = tensor.shape
    elements = shape[dim]
    if offset == 0 or abs(offset) > elements:
        return tensor
    htorch.core.mark_step()
    indices = torch.arange(0, elements, dtype=torch.int32, device=tensor.device)
    offset = torch.tensor(offset, dtype=torch.int32, device=tensor.device)
    indices = torch.clamp(indices - offset, 0, elements - 1)
    target_shape = [1,] * len(tensor.shape)
    target_shape[dim] = elements
    indices = indices.view(target_shape).expand(shape)
    result = torch.gather(tensor, dim, indices)
    htorch.core.mark_step()
    return result


def shift_all(srcs, dim, offsets):
    return [shift(src, dim, offset) for src, offset in zip(srcs, offsets)]


@dataclass
class CausalLMRequest:
    idx: int
    data: generate_pb2.Request
    input_length: int
    prefix_offset: int
    read_offset: int
    stopping_criteria: StoppingCriteria

    all_input_ids: torch.Tensor

    @classmethod
    def from_pb(cls, idx: int, data: generate_pb2.Request, tokenizer: PreTrainedTokenizerBase):
        return cls(
            idx=idx,
            data=data,
            input_length=None,
            prefix_offset=None,
            read_offset=None,
            stopping_criteria=StoppingCriteria.from_pb(data.stopping_parameters, tokenizer),
            all_input_ids=None,)

    def update_idx(self, new_idx):
        prev = self.idx
        self.idx = new_idx
        return (new_idx, prev)

@dataclass
class CausalLMBatch(Batch):
    batch_id: int
    requests: List[CausalLMRequest]

    # Decoder values
    input_ids: torch.Tensor
    attention_mask: torch.Tensor
    position_ids: torch.Tensor
    past_key_values: Optional[List[Tuple]]

    # Generation helpers
    next_token_chooser: HeterogeneousNextTokenChooser
    top_n_tokens: List[int]
    top_n_tokens_tensor: torch.Tensor

    # Maximum number of tokens this batch will grow to
    max_tokens: int

    input_length: int
    right_padding: int

    def to_pb(self) -> generate_pb2.CachedBatch:
        return generate_pb2.CachedBatch(
            id=self.batch_id,
            request_ids=[r.data.id for r in self.requests],
            size=len(self),
            max_tokens=self.max_tokens,
        )

    @classmethod
    def recombine(cls, batches: List["CausalLMBatch"], req_ids: List[List[int]], is_optimized_for_gaudi: bool = False) -> "CausalLMBatch":
        new_bs = round_up(sum([len(reqs) for reqs in req_ids]), BATCH_BUCKET_SIZE)
        batch_id = batches[0].batch_id
        device = batches[0].input_ids.device

        # TODO: for now use consecutive indices. This could be optimized to reuse existing batch memory and only overwrite
        # indices that are no longer used instead of allocating new memory
        free_indices = itertools.count(0)
        to_tensors = lambda ind: (torch.tensor(ind[0], device=device), torch.tensor(ind[1], device=device))
        requests = [[req for req in batch.requests if req.data.id in ids] for batch, ids in zip(batches, req_ids)]
        indices = [[to_tensors(req.update_idx(next(free_indices))) for req in batch_reqs] for batch_reqs in requests]
        requests = list(itertools.chain(*requests))

        # TODO: Add support for changing max seq len, i.e. due to output length bucketing
        # FIXME: max_seq_len for non optimized code
        max_input_length = max(req.input_length for req in requests)
        offsets = [(max_input_length - b.input_length) for b in batches]
        trace(f'RECOMBINE: bs:{new_bs} requests: {len(requests)} offsets: {offsets}')

        max_seq_len = batches[0].attention_mask.size(1)
        input_length = max(r.input_length for r in requests)
        right_padding = max_seq_len - input_length
        max_tokens = len(requests) * max_seq_len

        chunk_size = batches[0].past_key_values[0][0].size(0) // batches[0].input_ids.size(0)
        num_layers = len(batches[0].past_key_values)
        past_key_values_type = type(batches[0].past_key_values)

        seq_dim = 1
        if batches[0].past_key_values[0][0].size(-1) != batches[0].past_key_values[0][1].size(-1):
            # Case for Bloom
            key_dim = -1
        else:
            key_dim = -2
        value_dim = -2

        for b in batches:
            b.past_key_values = list(b.past_key_values)

        src = [b.input_ids for b in batches]
        for b in batches:
            del b.input_ids
        src = shift_all(src, seq_dim, offsets)
        input_ids = batch_alloc(new_bs, src[0])
        input_ids = move_data(input_ids, 1, indices, src)

        src = [b.attention_mask for b in batches]
        for b in batches:
            del b.attention_mask
        src = shift_all(src, seq_dim, offsets)
        attention_mask = batch_alloc(new_bs, src[0])
        attention_mask = move_data(attention_mask, 1, indices, src)

        src = [b.position_ids for b in batches]
        for b in batches:
            del b.position_ids
        src = shift_all(src, seq_dim, offsets)
        position_ids = batch_alloc(new_bs, src[0])
        position_ids = move_data(position_ids, 1, indices, src)

        past_key_values = []
        for layer_num in range(num_layers):
            src = [b.past_key_values[layer_num][0] for b in batches]
            src = shift_all(src, key_dim, offsets)
            updated_key = batch_alloc(new_bs * chunk_size, src[0])
            updated_key = move_data(updated_key, chunk_size, indices, src)

            src = [b.past_key_values[layer_num][1] for b in batches]
            src = shift_all(src, value_dim, offsets)
            updated_value = batch_alloc(new_bs * chunk_size, src[0])
            updated_value = move_data(updated_value, chunk_size, indices, src)

            past_key_values.append((updated_key, updated_value))
            for b in batches:
                b.past_key_values[layer_num] = None

        past_key_values = past_key_values_type(past_key_values)

        top_n_tokens = [r.data.top_n_tokens for r in requests]
        top_n_tokens_tensor = torch.tensor(top_n_tokens, device=device, dtype=torch.int64)
        next_token_chooser = HeterogeneousNextTokenChooser.from_pb(
            [r.data.parameters for r in requests],
            batches[0].next_token_chooser.device,
            batches[0].next_token_chooser.dtype
        )

        htorch.core.mark_step()

        return cls(
            batch_id=batch_id,
            requests=requests,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=past_key_values,
            next_token_chooser=next_token_chooser,
            top_n_tokens=top_n_tokens,
            top_n_tokens_tensor=top_n_tokens_tensor,
            max_tokens=max_tokens,
            input_length=input_length,
            right_padding=right_padding
        )


    @classmethod
    def from_pb(
        cls,
        pb: generate_pb2.Batch,
        tokenizer: PreTrainedTokenizerBase,
        dtype: torch.dtype,
        device: torch.device,
        is_optimized_for_gaudi: bool = False,
    ) -> "CausalLMBatch":
        trace(f'NEW BATCH: ({len(pb.requests)}){[req.id for req in pb.requests]}')
        requests = [CausalLMRequest.from_pb(idx, req, tokenizer) for idx, req in enumerate(pb.requests)]

        max_input_length = max(r.data.truncate for r in requests)
        max_new_tokens = max(r.stopping_criteria.max_new_tokens for r in requests)

        # TODO: Add support for sparse batches
        top_n_tokens = [r.top_n_tokens for r in pb.requests]
        top_n_tokens_tensor = torch.tensor(top_n_tokens, device=device, dtype=torch.int64)
        next_token_chooser = HeterogeneousNextTokenChooser.from_pb([r.parameters for r in pb.requests], dtype, device)

        # TODO: this should be set to rust side `max_total_tokens`,
        # (see https://github.com/huggingface/text-generation-inference/blob/main/launcher/src/main.rs#L177)
        # but TGI does not offer an API to expose this variable to python, as this variable
        # is handled by the client but it appears the model is initialized by the server.
        # An alternative could be to initialize the buffers during warmup.
        # Dummy
        max_total_tokens = int(os.getenv("MAX_TOTAL_TOKENS", "0"))
        logger.info("MAX_TOTAL_TOKENS = {}".format(max_total_tokens))

        # TODO: by tokenizing all inputs at once we loose information on actual input lengths
        # this means that we cannot shift inputs to the left after a long input sequence
        # was filtered out
        new_bs = round_up(len(requests), PREFILL_BATCH_BUCKET_SIZE)
        dummy_inputs = ["?"] * (new_bs - len(requests))
        tokenized_inputs = tokenizer(
            [r.data.inputs for r in requests] + dummy_inputs,
            return_tensors="pt",
            padding="max_length",
            return_token_type_ids=False,
            truncation=True,
            max_length=max_input_length,
        )

        input_len = tokenized_inputs["input_ids"].shape[1]
        extra_padding = 0
        if is_optimized_for_gaudi and max_total_tokens > 0:
            extra_padding = max(extra_padding, max_total_tokens - max_input_length - max_new_tokens)

        for r in requests:
            r.input_length = input_len
            r.prefix_offset = input_len - 5
            r.read_offset = input_len

        #max_tokens = new_bs * max_total_tokens
        max_tokens = len(requests) * max_total_tokens

        input_ids = tokenized_inputs["input_ids"]
        attention_mask = tokenized_inputs["attention_mask"]

        if is_optimized_for_gaudi:
            input_ids = torch.nn.functional.pad(
                input_ids, (0, max_new_tokens + extra_padding), value=tokenizer.pad_token_id
            )
            attention_mask = torch.nn.functional.pad(
                attention_mask, (0, max_new_tokens + extra_padding), value=0)
            all_input_ids = input_ids.T.split(1, dim=1)
        else:
            all_input_ids = input_ids.clone().T.split(1, dim=1)

        for r in requests:
            r.all_input_ids = all_input_ids[r.idx]

        input_ids = input_ids.to(device)
        attention_mask = attention_mask.to(device)
        position_ids = attention_mask.long().cumsum(-1) - 1
        position_ids.masked_fill_(attention_mask == 0, 1)

        htorch.core.mark_step()

        return cls(
            batch_id=pb.id,
            requests=requests,
            input_ids=input_ids,
            attention_mask=attention_mask,
            position_ids=position_ids,
            past_key_values=None,
            next_token_chooser=next_token_chooser,
            top_n_tokens=top_n_tokens,
            top_n_tokens_tensor=top_n_tokens_tensor,
            max_tokens=max_tokens,
            input_length=max_input_length,
            right_padding=max_new_tokens + extra_padding if is_optimized_for_gaudi else 0
        )

    @tracer.start_as_current_span("filter")
    def filter(self, request_ids: List[int], is_optimized_for_gaudi: bool = False) -> Optional["CausalLMBatch"]:
        trace("FILTER")
        return self.__class__.recombine([self], [request_ids], is_optimized_for_gaudi)

    @classmethod
    @tracer.start_as_current_span("concatenate")
    def concatenate(cls, batches: List["CausalLMBatch"], is_optimized_for_gaudi: bool = False) -> "CausalLMBatch":
        trace('CONCAT')
        return cls.recombine(batches, [[req.data.id for req in b.requests] for b in batches], is_optimized_for_gaudi)

    def __len__(self):
        return len(self.requests)


class CausalLM(Model):
    def __init__(
        self,
        model_id: str,
        revision: Optional[str] = None,
        dtype: Optional[torch.dtype] = None,
        trust_remote_code: Optional[bool] = False,
    ):
        logger.info("======== init CausalLM ========")
        
        device = torch.device("hpu")

        dtype = torch.bfloat16 if dtype is None else dtype

        from optimum.habana.transformers.modeling_utils import adapt_transformers_to_gaudi

        adapt_transformers_to_gaudi()

        logger.info("======== setup tokenizer ========")
        tokenizer = AutoTokenizer.from_pretrained(
            model_id,
            revision=revision,
            padding_side="left",
            truncation_side="left",
            trust_remote_code=trust_remote_code,
        )

        model_kwargs = {
            "revision": revision,
            "trust_remote_code": trust_remote_code,
            # "low_cpu_mem_usage": True,
        }

        world_size = int(os.getenv("WORLD_SIZE", "1"))
        rank = int(os.getenv("RANK"), 0)
        self.enable_hpu_graph = os.getenv("ENABLE_HPU_GRAPH", "true").lower() == "true"
        self.limit_hpu_graph = os.getenv("LIMIT_HPU_GRAPH", "false").lower() == "true"

        if world_size > 1:
            logger.info("======== using deepspeed ========")
            import habana_frameworks.torch.hpu as torch_hpu

            # Get world size, rank and local rank
            from habana_frameworks.torch.distributed.hccl import initialize_distributed_hpu

            world_size, rank, local_rank = initialize_distributed_hpu()
            import deepspeed

            # Initialize process(es) for DeepSpeed
            deepspeed.init_distributed(dist_backend="hccl")
            logger.info(
                "DeepSpeed is enabled. world_size {} rank {} local_rank {}".format(world_size, rank, local_rank)
            )
            logger.info("======== setup config ========")
            config = AutoConfig.from_pretrained(model_id, **model_kwargs)
            load_to_meta = model_on_meta(config)

            if load_to_meta:
                # Construct model with fake meta tensors, later will be replaced on devices during ds-inference ckpt load
                with deepspeed.OnDevice(dtype=dtype, device="meta"):
                    model = AutoModelForCausalLM.from_config(config, torch_dtype=dtype)
            else:
                get_repo_root(model_id, local_rank=os.getenv("LOCAL_RANK"))
                # TODO: revisit placement on CPU when auto-injection is possible
                logger.info("======== setup model ========")
                with deepspeed.OnDevice(dtype=dtype, device="cpu"):
                    model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=dtype, **model_kwargs)
                # model = AutoModelForCausalLM.from_pretrained(model_id, torch_dtype=dtype, device_map="cpu", **model_kwargs)
                
            model = model.eval()

            # Initialize the model
            ds_inference_kwargs = {"dtype": dtype}
            ds_inference_kwargs["tensor_parallel"] = {"tp_size": world_size}
            ds_inference_kwargs["enable_cuda_graph"] = self.enable_hpu_graph

            if load_to_meta:
                # model loaded to meta is managed differently
                checkpoints_json = tempfile.NamedTemporaryFile(suffix=".json", mode="+w")
                write_checkpoints_json(model_id, local_rank, checkpoints_json)
                ds_inference_kwargs["checkpoint"] = checkpoints_json.name
            model = deepspeed.init_inference(model, **ds_inference_kwargs)
            model = model.module
        else:
            get_repo_root(model_id)
            model = AutoModelForCausalLM.from_pretrained(
                model_id,
                revision=revision,
                torch_dtype=dtype,
                trust_remote_code=trust_remote_code,
            )
            model = model.eval().to(device)
            #wrap in hpu_graph only if self.enable_hpu_graph is set
            if self.enable_hpu_graph:
                model = wrap_in_hpu_graph(model, disable_tensor_cache=True)

        if model.config.model_type in MODELS_OPTIMIZED_WITH_STATIC_SHAPES:
            self.is_optimized_for_gaudi = True
        else:
            self.is_optimized_for_gaudi = False

        if tokenizer.pad_token_id is None:
            if model.config.pad_token_id is not None:
                tokenizer.pad_token_id = model.config.pad_token_id
            elif model.config.eos_token_id is not None:
                tokenizer.pad_token_id = model.config.eos_token_id
            elif model.generation_config.pad_token_id is not None:
                tokenizer.pad_token_id = model.generation_config.pad_token_id
            elif model.generation_config.eos_token_id is not None:
                tokenizer.pad_token_id = model.generation_config.eos_token_id
            elif tokenizer.eos_token_id is not None:
                tokenizer.pad_token_id = tokenizer.eos_token_id
            else:
                tokenizer.add_special_tokens({"pad_token": "[PAD]"})

        kwargs = {
            "use_cache": True,
            "return_dict": True,
        }

        if model.config.model_type == "llama":
            kwargs["attn_softmax_bf16"] = True
            kwargs["trim_logits"] = True

        super(CausalLM, self).__init__(
            model=model,
            tokenizer=tokenizer,
            requires_padding=True,
            dtype=dtype,
            device=device,
            rank=rank,
            kwargs=kwargs,
        )
        self.profiling_warmup_steps = int(os.getenv("PROF_WARMUPSTEP", "0"))
        self.profiling_steps = int(os.getenv("PROF_STEP", "5"))
        output_dir = os.getenv("PROF_PATH", "/tmp/hpu_profile")
        self.hb_profer = HabanaProfile(
            warmup=self.profiling_warmup_steps, active=self.profiling_steps, output_dir=output_dir
        )
        if self.profiling_warmup_steps > 0:
            self.hb_profer_started = True
            self.hb_profer.start()
        else:
            self.hb_profer = None
            self.hb_profer_started = False
        self.step = 0

    @property
    def batch_type(self) -> Type[CausalLMBatch]:
        return CausalLMBatch

    def decode(self, generated_ids: List[int]) -> str:
        return self.tokenizer.decode(generated_ids, skip_special_tokens=True, clean_up_tokenization_spaces=False)

    def forward(
        self,
        input_ids,
        attention_mask,
        position_ids,
        token_idx: Optional = None,
        past_key_values: Optional = None,
        bypass_hpu_graph: Optional = None,
    ) -> Tuple[torch.Tensor, List[Tuple[torch.Tensor, torch.Tensor]]]:
        # Model Forward
        kwargs = {
            "input_ids": input_ids,
            "attention_mask": attention_mask,
            "past_key_values": past_key_values,
        }

        if self.is_optimized_for_gaudi:
            kwargs["token_idx"] = token_idx

        if self.has_position_ids:
            kwargs["position_ids"] = position_ids

        if bypass_hpu_graph != None:
            kwargs["bypass_hpu_graphs"] = bypass_hpu_graph

        kwargs.update(self.kwargs)
        outputs = self.model.forward(**kwargs)
        return outputs.logits, outputs.past_key_values

    @tracer.start_as_current_span("generate_token")
    def generate_token(self, batch: CausalLMBatch) -> Tuple[List[Generation], Optional[CausalLMBatch]]:
        trace(f'GENERATE ({len(batch.requests)}){[r.data.id for r in batch.requests]}, {batch.input_ids.shape}')
        self.step = self.step + 1
        if self.hb_profer_started == True and self.step > self.profiling_warmup_steps + self.profiling_steps:
            self.hb_profer.stop()
            self.hb_profer_started = False

        if self.is_optimized_for_gaudi:
            token_idx = torch.tensor(batch.attention_mask.shape[-1] - batch.right_padding).to(self.device)
            attention_mask = batch.attention_mask
        else:
            token_idx = None
            # slice the attention mask to the correct shape
            # TODO fix me!
            attention_mask = batch.attention_mask[:, : -batch.padding_right_offset]
        prefill = batch.past_key_values is None
        if batch.past_key_values:
            if token_idx is not None:
                input_ids = torch.index_select(batch.input_ids, 1, token_idx - 1)
        else:
            input_ids = batch.input_ids

        logits, past = self.forward(
            input_ids,
            attention_mask,
            batch.position_ids,
            token_idx,
            batch.past_key_values,
            bypass_hpu_graph = prefill and self.limit_hpu_graph if self.enable_hpu_graph else None
        )

        # Results
        generations: List[Generation] = []
        stopped = True

        # Select next token
        input_length = batch.input_length
        if self.is_optimized_for_gaudi and logits.shape[-2] > 1:
            next_token_ids, next_token_logprobs, logprobs = batch.next_token_chooser(
                batch.input_ids[:, :token_idx], logits[:, input_length - 1 : input_length, :].squeeze(-2)
            )
        else:
            next_token_ids, next_token_logprobs, logprobs = batch.next_token_chooser(
                batch.input_ids[:, :token_idx], logits.squeeze(-2)
            )

        batch_top_token_ids, batch_top_token_logprobs = batch_top_tokens(
            batch.top_n_tokens,
            batch.top_n_tokens_tensor,
            logprobs,
        )

        next_token_logprobs = next_token_logprobs.tolist()
        next_token_ids_cpu = next_token_ids.cpu()
        htorch.core.mark_step()

        for req in batch.requests:
            i = req.idx
            request = req.data
            input_length = req.input_length
            prefix_offset = req.prefix_offset
            read_offset = req.read_offset
            do_sample = batch.next_token_chooser.do_sample[i]
            seed = batch.next_token_chooser.seeds[i]
            stopping_criteria = req.stopping_criteria
            all_input_ids = req.all_input_ids
            top_n_tokens = batch.top_n_tokens[i]
            next_token_id = next_token_ids_cpu[i]
            next_token_logprob = next_token_logprobs[i]
            top_token_ids = batch_top_token_ids[i]
            top_token_logprobs = batch_top_token_logprobs[i]

            # Append next token to all tokens
            if self.is_optimized_for_gaudi:
                all_input_ids[input_length] = next_token_id
            else:
                all_input_ids = torch.cat([all_input_ids, next_token_id])
            new_input_length = input_length + 1

            # Generated token
            next_token_text, prefix_offset, read_offset = self.decode_token(
                all_input_ids[0:new_input_length, 0], prefix_offset, read_offset
            )

            # Evaluate stopping criteria
            stop, reason = stopping_criteria(
                next_token_id,
                next_token_text,
            )

            if not stop:
                stopped = False

            # Shard generations
            # All generations will be appended in the rust sharded client
            if i % self.world_size == self.rank:
                if stop:
                    # Decode generated tokens
                    output_text = self.decode(
                        all_input_ids[new_input_length - stopping_criteria.current_tokens : new_input_length, 0]
                    )
                    generated_text = GeneratedText(
                        output_text,
                        stopping_criteria.current_tokens,
                        reason,
                        seed if do_sample else None,
                    )
                else:
                    generated_text = None

                # Prefill
                if stopping_criteria.current_tokens == 1 and request.prefill_logprobs:
                    # Remove generated token to only have prefill and add nan for first prompt token
                    prefill_logprobs = [float("nan")] + next_token_logprobs
                    prefill_token_ids = all_input_ids[0 : new_input_length - 1]
                    prefill_texts = self.tokenizer.batch_decode(
                        prefill_token_ids,
                        clean_up_tokenization_spaces=False,
                        skip_special_tokens=False,
                    )
                    prefill_tokens = PrefillTokens(prefill_token_ids, prefill_logprobs, prefill_texts)
                else:
                    prefill_tokens = None

                if top_n_tokens > 0:
                    toptoken_texts = self.tokenizer.batch_decode(
                        top_token_ids,
                        clean_up_tokenization_spaces=False,
                        skip_special_tokens=False,
                    )
                    special_toptokens = [token_id in self.all_special_ids for token_id in top_token_ids]
                    top_tokens = TopTokens(
                        top_token_ids,
                        top_token_logprobs,
                        toptoken_texts,
                        special_toptokens,
                    )
                else:
                    top_tokens = None

                generation = Generation(
                    request.id,
                    prefill_tokens,
                    next_token_id,
                    next_token_logprob,
                    next_token_text,
                    next_token_id in self.all_special_ids,
                    generated_text,
                    top_tokens,
                )

                generations.append(generation)

            req.all_input_ids = all_input_ids
            req.input_length = new_input_length
            req.prefix_offset = prefix_offset
            req.read_offset = read_offset
            htorch.core.mark_step()

        if token_idx is None:
            batch.input_ids[:, 0] = next_token_ids[:, 0]
        else:
            batch.input_ids.index_copy_(1, token_idx.cpu(), next_token_ids.unsqueeze(1))

        # We finished all generations in the batch; there is no next batch
        if stopped:
            if self.hb_profer_started == True:
                self.hb_profer.step()
            return generations, None

        # Slice unused values from prefill, use it to store next token
        if token_idx is None:
            batch.input_ids = batch.input_ids[:, :1]

        # Update attention_mask as we added a new token to input_ids
        if self.is_optimized_for_gaudi:
            batch.attention_mask.index_fill_(1, token_idx, 1)
        else:
            batch.attention_mask[:, -batch.padding_right_offset] = 1

        # Adjust lengths
        batch.input_length += 1
        if batch.right_padding > 0:
            batch.right_padding -= 1

        # Update position_ids
        if prefill:
            batch.position_ids = batch.position_ids[:, token_idx - 1 : token_idx] + 1
        else:
            batch.position_ids += 1
        # Update past key values
        batch.past_key_values = past
        if self.hb_profer_started == True:
            self.hb_profer.step()
        htorch.core.mark_step()

        return generations, batch
