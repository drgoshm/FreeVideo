# Copyright 2026 Qwen-Image Team, The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Qwen3-VL prompt conditioning, ported from the Qwen-Image 2.1 pipeline.

``encode_prompt`` returns the public (embeddings, attention_mask) pair.
``encode_prompt_with_image_mask`` additionally returns the interleaved image
slots required by the 2.1 DiT. No models are moved between devices here.
The wrapper and underlying Qwen3-VL model are used sequentially: the temporary
RMSNorm hook, like the reference implementation, is not thread safe.
"""

import torch
from PIL import Image


class QwenTextEncoder:
    def __init__(self, model, processor):
        self.model = model
        self.processor = processor
        self.sys_prompt = "Comprehend and analyze the provided prompt."
        self.prompt_template_t2i = (
            f"<|im_start|>system\n{self.sys_prompt}<|im_end|>\n"
            "<|im_start|>user\n{}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
        self.prompt_template_ti2i = (
            f"<|im_start|>system\n{self.sys_prompt}<|im_end|>\n"
            "<|im_start|>user\n<image1><|vision_start|><|image_pad|><|vision_end|>{}<|im_end|>\n"
            "<|im_start|>assistant\n"
        )
        sys_message = [{"role": "system", "content": [{"type": "text", "text": self.sys_prompt}]}]
        sys_tokens = processor.apply_chat_template(sys_message, tokenize=True, return_dict=False)
        self._drop_idx = len(sys_tokens[0])
        self._img_token_id = processor.tokenizer.encode("<|image_pad|>")[0]

    def prepare_images(self, images):
        """Materialize the lazy BF16 vision tower on CPU before placement.enter."""
        if images and hasattr(self.model, "load_vision"):
            self.model.load_vision()

    @torch.no_grad()
    def encode_prompt(self, prompt, images=None):
        embeds, mask, _ = self.encode_prompt_with_image_mask(prompt, images)
        return embeds, mask

    @torch.no_grad()
    def encode_prompt_with_image_mask(self, prompt, images=None, *, device=None):
        if device is None:
            device = next(self.model.parameters()).device
        prompt = [prompt] if isinstance(prompt, str) else prompt
        if not prompt or not all(isinstance(p, str) for p in prompt):
            raise ValueError("prompt must be a string or a nonempty list of strings")
        prompt = [" " if not p else p for p in prompt]
        if images is not None and not images:
            images = None
        is_t2i = images is None
        if is_t2i:
            prompts = [self.prompt_template_t2i.format(t) for t in prompt]
        else:
            self.prepare_images(images)
            prompts, condition_pil_list = [], []
            for t in prompt:
                replace = "<image1><|vision_start|><|image_pad|><|vision_end|>"
                for i in range(2, len(images) + 1):
                    replace += f" <image{i}><|vision_start|><|image_pad|><|vision_end|>"
                template = self.prompt_template_ti2i.replace(
                    "<image1><|vision_start|><|image_pad|><|vision_end|>", replace
                )
                prompts.append(template.format(t))
            for _ in prompt:
                for img in images:
                    if not isinstance(img, Image.Image):
                        img = Image.fromarray(img)
                    if img.mode == "RGBA":
                        white = Image.new("RGB", img.size, (255, 255, 255))
                        white.paste(img, mask=img.getchannel("A"))
                        img = white
                    condition_pil_list.append(img)

        processor_kwargs = dict(text=prompts, padding=True, padding_side="left", return_tensors="pt")
        if not is_t2i:
            processor_kwargs["images"] = condition_pil_list
        model_inputs = self.processor(**processor_kwargs).to(device)
        forward_kwargs = dict(
            input_ids=model_inputs.input_ids,
            attention_mask=model_inputs.attention_mask,
            output_hidden_states=True,
        )
        if not is_t2i and hasattr(model_inputs, "pixel_values"):
            forward_kwargs.update(pixel_values=model_inputs.pixel_values, image_grid_thw=model_inputs.image_grid_thw)
        if hasattr(model_inputs, "mm_token_type_ids"):
            forward_kwargs["mm_token_type_ids"] = model_inputs.mm_token_type_ids

        # Transformers 5.15 ties hidden_states[-1] to the normalized output.
        # Return the norm's input for this call, exactly as the reference does.
        text_model = getattr(self.model.model, "language_model", self.model.model)
        handle = text_model.norm.register_forward_hook(lambda module, args, output: args[0])
        try:
            outputs = self.model(**forward_kwargs)
        finally:
            handle.remove()
        hidden_states = outputs.hidden_states[-1]
        bool_mask = model_inputs.attention_mask.bool()
        valid_lengths = bool_mask.sum(dim=1)
        selected = hidden_states[bool_mask]
        split_hidden_states = list(torch.split(selected, valid_lengths.tolist(), dim=0))
        split_hidden_states = [e[self._drop_idx :] for e in split_hidden_states]
        image_pad_mask = [
            (sample_ids[sample_mask.bool()] == self._img_token_id)[self._drop_idx :]
            for sample_ids, sample_mask in zip(model_inputs.input_ids, model_inputs.attention_mask)
        ]
        attn_mask_list = [torch.ones(e.size(0), dtype=torch.long, device=e.device) for e in split_hidden_states]
        max_seq_len = max(e.size(0) for e in split_hidden_states)
        prompt_embeds = torch.stack(
            [torch.cat([u, u.new_zeros(max_seq_len - u.size(0), u.size(1))]) for u in split_hidden_states]
        )
        encoder_attention_mask = torch.stack(
            [torch.cat([u, u.new_zeros(max_seq_len - u.size(0))]) for u in attn_mask_list]
        )
        image_pad_mask = torch.stack([torch.cat([u, u.new_zeros(max_seq_len - u.size(0))]) for u in image_pad_mask])
        if encoder_attention_mask.all():
            encoder_attention_mask = None
        return prompt_embeds, encoder_attention_mask, image_pad_mask
