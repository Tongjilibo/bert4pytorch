from bert4torch.models.glm import GLM2
from bert4torch.models.transformer import PreTrainedModelForDecoder
from .visual import EVA2CLIPModel
from bert4torch.snippets import DottableDict
import torch
from typing import Optional, List


def is_empty(images_list: Optional[List[List[torch.Tensor]]]):
    if images_list is None or len(images_list) == 0:
        return True
    for image_list in images_list:
        if image_list is not None:
            return False
    return True


class GLM4V(PreTrainedModelForDecoder):
    passed_kwargs = PreTrainedModelForDecoder.passed_kwargs | {"images"}
    def __init__(self, **config):
        super().__init__(**config)
        self.config = DottableDict(config)
        self.vision = EVA2CLIPModel(self.config)
        self.llm = GLM2(**config)

    def load_variable(self, *args, **kwargs):
        return self.llm.load_variable(*args, **kwargs)
    
    def load_trans_ckpt(self, state_dict):
        return self.llm.load_trans_ckpt(state_dict, prefix='llm.')

    def save_trans_ckpt(self):
        vision_state_dict = self.vision.state_dict()
        key_list = list(vision_state_dict.keys())
        for k in key_list:
            vision_state_dict[f'transformer.vision.{k}'] = vision_state_dict.pop(k)
            
        llm_state_dict = self.llm.save_trans_ckpt()
        return {**vision_state_dict, **llm_state_dict}
    
    def variable_mapping(self):
        mapping = self.llm.variable_mapping()
        new_mapping = dict()
        for model_key, ckpt_key in mapping.items():
            new_mapping[f'llm.{model_key}'] = ckpt_key
        
        for model_key, _ in self.vision.named_parameters():
            new_mapping[f'vision.{model_key}'] = f'transformer.vision.{model_key}'
        return new_mapping
    
    def load_embeddings(self, embeddings):
        return super().load_embeddings(embeddings)

    def forward(self, *inputs, images=None, position_ids=None, **model_kwargs):
        inputs = self.args_segmentate(inputs, **model_kwargs)
        input_ids = inputs[0]

        if not is_empty(images):  # multi-modality
            image_size: int = self.config.vision_config['image_size']
            patch_size: int = self.config.vision_config['patch_size']
            num_patches = (image_size // patch_size // 2) ** 2
            assert len(input_ids) == len(images), f"{len(input_ids)} {len(images)}"
            inputs_embeds = self.llm.embeddings(input_ids)

            images = images.to(dtype=inputs_embeds.dtype)
            images_features = self.vision(images)

            if position_ids is None:
                position_ids = self.get_position_ids(input_ids, device=inputs_embeds.device)
            new_input_embeds, new_position_ids = [], []

            for i in range(len(input_ids)):
                input_id = input_ids[i].tolist()
                boi_token_pos, eoi_token_pos = input_id.index(self.config.boi_token_id), input_id.index(self.config.eoi_token_id)
                assert eoi_token_pos - boi_token_pos == 2
                new_input_embeds.append(torch.cat(
                    (inputs_embeds[i, :boi_token_pos], images_features[i].to(inputs_embeds.device),
                        inputs_embeds[i, eoi_token_pos + 1:])))
                new_position_ids.append(torch.cat(
                    (position_ids[i, :boi_token_pos + 1], position_ids[i, boi_token_pos + 1].repeat(num_patches),
                        position_ids[i, eoi_token_pos:])
                ))
            inputs_embeds = torch.stack(new_input_embeds, dim=0)
            position_ids = torch.stack(new_position_ids, dim=0)
        else:
            inputs_embeds = input_ids

        return self.llm(input_ids=inputs_embeds, position_ids=position_ids, **model_kwargs)
    
    def prepare_inputs_for_generation(self,
        input_ids,
        past_key_values=None,
        attention_mask=None,
        step=None,
        input_seqlen=None,
        output_ids=None,
        position_ids=None,
        images=None,
        **kwargs,
    ):
        if attention_mask is not None:
            image_size: int = self.config.vision_config['image_size']
            patch_size: int = self.config.vision_config['patch_size']
            num_patches = (image_size // patch_size // 2) ** 2
            new_attention_masks = []

            # if not image, use this default id
            eoi_token_pos = 6
            boi_token_pos = 4

            for i in range(len(input_ids)):
                input_id = input_ids[i].tolist()
                if not is_empty(images):
                    boi_token_pos, eoi_token_pos = input_id.index(self.config.boi_token_id), input_id.index(self.config.eoi_token_id)
                assert eoi_token_pos - boi_token_pos == 2
                new_attention_masks.append(torch.cat(
                    (attention_mask[i, :boi_token_pos + 1], attention_mask.new_ones(num_patches),
                     attention_mask[i, eoi_token_pos:])
                ))
            attention_mask = torch.stack(new_attention_masks, dim=0)
        
        if step > 0:
            if past_key_values is not None:
                position_ids = position_ids[..., -1:]
                input_ids = input_ids[:, -1:]
        
        kwargs.update(
            {
                # "input_ids": input_ids,
                "images": images,
                "past_key_values": past_key_values,
                "attention_mask": attention_mask
            }
        )
        return kwargs

    def get_position_ids(self, input_ids, device):
        batch_size, seq_length = input_ids.shape
        position_ids = torch.arange(seq_length, dtype=torch.long, device=device).unsqueeze(0).repeat(batch_size, 1)
        return position_ids