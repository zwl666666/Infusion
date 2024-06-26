from inspect import isfunction
import math
import torch
import torch.nn.functional as F
from torch import nn, einsum
from einops import rearrange, repeat
from typing import Optional, Any

from ldm.modules.diffusionmodules.util import checkpoint
from infusion.roe import ROELinear
import numpy as np
from PIL import Image
try:
    import xformers
    import xformers.ops

    XFORMERS_IS_AVAILBLE = True
except:
    XFORMERS_IS_AVAILBLE = False
#XFORMERS_IS_AVAILBLE = False
# CrossAttn precision handling
import os

_ATTN_PRECISION = os.environ.get("ATTN_PRECISION", "fp32")


def exists(val):
    return val is not None


def uniq(arr):
    return {el: True for el in arr}.keys()


def default(val, d):
    if exists(val):
        return val
    return d() if isfunction(d) else d


def max_neg_value(t):
    return -torch.finfo(t.dtype).max


def init_(tensor):
    dim = tensor.shape[-1]
    std = 1 / math.sqrt(dim)
    tensor.uniform_(-std, std)
    return tensor


# feedforward
class GEGLU(nn.Module):
    def __init__(self, dim_in, dim_out):
        super().__init__()
        self.proj = nn.Linear(dim_in, dim_out * 2)

    def forward(self, x):
        x, gate = self.proj(x).chunk(2, dim=-1)
        return x * F.gelu(gate)


class FeedForward(nn.Module):
    def __init__(self, dim, dim_out=None, mult=4, glu=False, dropout=0.):
        super().__init__()
        inner_dim = int(dim * mult)
        dim_out = default(dim_out, dim)
        project_in = nn.Sequential(
            nn.Linear(dim, inner_dim),
            nn.GELU()
        ) if not glu else GEGLU(dim, inner_dim)

        self.net = nn.Sequential(
            project_in,
            nn.Dropout(dropout),
            nn.Linear(inner_dim, dim_out)
        )

    def forward(self, x):
        return self.net(x)


def zero_module(module):
    """
    Zero out the parameters of a module and return it.
    """
    for p in module.parameters():
        p.detach().zero_()
    return module


def Normalize(in_channels):
    return torch.nn.GroupNorm(num_groups=32, num_channels=in_channels, eps=1e-6, affine=True)


class CrossAttention(nn.Module):
    def __init__(self, query_dim, context_dim=None, heads=8, dim_head=64, dropout=0.):
        super().__init__()
        inner_dim = dim_head * heads

        self.scale = dim_head ** -0.5
        self.heads = heads

        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)
        if context_dim is None:
            self.to_k = nn.Linear(query_dim, inner_dim, bias=False)
            self.to_v = nn.Linear(query_dim, inner_dim, bias=False)
        else:
            self.to_k = ROELinear(in_features=context_dim, out_features=inner_dim, bias=False, lock=True)
            self.to_v = ROELinear(in_features=context_dim, out_features=inner_dim, bias=False)

        self.to_out = nn.Sequential(
            nn.Linear(inner_dim, query_dim),
            nn.Dropout(dropout)
        )

    def forward(self, x, context=None, mask=None, target_input=None, C_inv=None, beta=0.75, tau=0.1, **kwargs):
        h = self.heads

        q = self.to_q(x)
        if context is None:
            k = self.to_k(x)
            v = self.to_v(x)
        else:
            context_super = kwargs.pop('context_super', None)
            k = self.to_k(context, target_input=target_input, C_inv=C_inv, beta=beta, tau=tau,
                          input_super=context_super, **kwargs)
            v = self.to_v(context, target_input=target_input, C_inv=C_inv, beta=beta, tau=tau, **kwargs)

        q, k, v = map(lambda t: rearrange(t, 'b n (h d) -> (b h) n d', h=h), (q, k, v))

        # force cast to fp32 to avoid overflowing
        if _ATTN_PRECISION == "fp32":
            with torch.autocast(enabled=False, device_type='cuda'):
                q, k = q.float(), k.float()
                sim = einsum('b i d, b j d -> b i j', q, k) * self.scale
        else:
            sim = einsum('b i d, b j d -> b i j', q, k) * self.scale

        del q, k

        if exists(mask):
            mask = rearrange(mask, 'b ... -> b (...)')
            max_neg_value = -torch.finfo(sim.dtype).max
            mask = repeat(mask, 'b j -> (b h) () j', h=h)
            sim.masked_fill_(~mask, max_neg_value)

        # attention, what we cannot get enough of
        sim = sim.softmax(dim=-1)
        '''
        #print attention map
        sim_norm = sim[32:].mean(dim=0)
        if sim_norm.shape[1]==77:
            n = int(np.sqrt(sim_norm.shape[0]))
            first_5_tensors = sim_norm[:, :5]
            for i in range(5):
                tensor = first_5_tensors[:,i]
                tensor = (tensor-min(tensor))/max(tensor)
                # 假设你想要保存为 8-bit 灰度图片，如果张量的范围在 [0, 1] 之间，乘以 255
                img_data = (tensor * 255).cpu().numpy().astype(np.uint8)
                img_data_reshaped = img_data.reshape((n, n))
                img = Image.fromarray(img_data_reshaped, mode='L')
                img.save(f'attention/attention_{i + 1}.png')
        '''

        out = einsum('b i j, b j d -> b i d', sim, v)
        out = rearrange(out, '(b h) n d -> b n (h d)', h=h)
        return self.to_out(out)


class MemoryEfficientCrossAttention(nn.Module):
    # https://github.com/MatthieuTPHR/diffusers/blob/d80b531ff8060ec1ea982b65a1b8df70f73aa67c/src/diffusers/models/attention.py#L223
    def __init__(self, query_dim, context_dim=None, heads=8, dim_head=64, dropout=0.0):
        super().__init__()
        print(f"Setting up {self.__class__.__name__}. Query dim is {query_dim}, context_dim is {context_dim} and using "
              f"{heads} heads.")
        inner_dim = dim_head * heads

        self.heads = heads
        self.dim_head = dim_head

        self.to_q = nn.Linear(query_dim, inner_dim, bias=False)
        if context_dim is None:
            self.to_k = nn.Linear(query_dim, inner_dim, bias=False)
            self.to_v = nn.Linear(query_dim, inner_dim, bias=False)
        else:
            self.to_k = ROELinear(in_features=context_dim, out_features=inner_dim, bias=False, lock=True)
            self.to_k = ROELinear(in_features=context_dim, out_features=inner_dim, bias=False,is_k=True)
            self.to_v = ROELinear(in_features=context_dim, out_features=inner_dim, bias=False)

        self.to_out = nn.Sequential(nn.Linear(inner_dim, query_dim), nn.Dropout(dropout))
        self.attention_op: Optional[Any] = None

    def forward(self, x, context=None,concept_token_idx=None, mask=None, target_input=None, C_inv=None, beta=0.75, tau=0.1, **kwargs):
        q = self.to_q(x)
        if context is None:
            k = self.to_k(x)
            v = self.to_v(x)
        else:
            context_super = kwargs.pop('context_super', None)
            k = self.to_k(context,concept_token_idx, target_input=target_input, C_inv=C_inv, beta=beta, tau=tau,
                          input_super=context_super, **kwargs)
            v = self.to_v(context,concept_token_idx, target_input=target_input, C_inv=C_inv, beta=beta, tau=tau, **kwargs)

        q1,q2 = torch.chunk(q, 2, dim=0)
        k1,k2 = torch.chunk(k, 2, dim=0)
        v1,v2 = torch.chunk(v, 2, dim=0)
        b, _, _ = q.shape
        q, k, v = map(
            lambda t: t.unsqueeze(3)
            .reshape(b, t.shape[1], self.heads, self.dim_head)
            .permute(0, 2, 1, 3)
            .reshape(b * self.heads, t.shape[1], self.dim_head)
            .contiguous(),
            (q, k, v),
        )

        # actually compute the attention, what we cannot get enough of
        if context is not None:
            b1, _, _ = q1.shape
            q1, k1, v1, q2, k2, v2 = map(
            lambda t: t.unsqueeze(3)
            .reshape(b1, t.shape[1], self.heads, self.dim_head)
            .permute(0, 2, 1, 3)
            .reshape(b1 * self.heads, t.shape[1], self.dim_head)
            .contiguous(),
            (q1, k1, v1, q2, k2, v2),
            )
            out1 = xformers.ops.memory_efficient_attention(q1, k1, v1, attn_bias=None, op=self.attention_op)
            out2 = xformers.ops.memory_efficient_attention(q1, k2, v2, attn_bias=None, op=self.attention_op)
            out = torch.cat((out1,out2),dim=0)
        else:
            out = xformers.ops.memory_efficient_attention(q, k, v, attn_bias=None, op=self.attention_op)
        if exists(mask):
            raise NotImplementedError
        out = (
            out.unsqueeze(0)
            .reshape(b, self.heads, out.shape[1], self.dim_head)
            .permute(0, 2, 1, 3)
            .reshape(b, out.shape[1], self.heads * self.dim_head)
        )
        return self.to_out(out)

class BasicTransformerBlock(nn.Module):
    ATTENTION_MODES = {
        "softmax": CrossAttention,  # vanilla attention
        "softmax-xformers": MemoryEfficientCrossAttention
    }

    def __init__(self, dim, n_heads, d_head, dropout=0., context_dim=None, gated_ff=True, checkpoint=True,
                 disable_self_attn=False):
        super().__init__()
        attn_mode = "softmax-xformers" if XFORMERS_IS_AVAILBLE else "softmax"
        assert attn_mode in self.ATTENTION_MODES
        attn_cls = self.ATTENTION_MODES[attn_mode]
        self.disable_self_attn = disable_self_attn
        self.attn1 = attn_cls(query_dim=dim, heads=n_heads, dim_head=d_head, dropout=dropout,
                              context_dim=context_dim if self.disable_self_attn else None)  # is a self-attention if not self.disable_self_attn
        self.ff = FeedForward(dim, dropout=dropout, glu=gated_ff)
        self.attn2 = attn_cls(query_dim=dim, context_dim=context_dim,
                              heads=n_heads, dim_head=d_head, dropout=dropout)  # is self-attn if context is none
        self.norm1 = nn.LayerNorm(dim)
        self.norm2 = nn.LayerNorm(dim)
        self.norm3 = nn.LayerNorm(dim)
        self.checkpoint = checkpoint

    def forward(self, x, context=None,concept_token_idx=None, target_input=None, C_inv=None, beta=0.75, tau=0.1, **kwargs):
        if self.checkpoint and self.training:
            return checkpoint(self._forward, (x, context,concept_token_idx, target_input, C_inv, beta, tau),
                          self.parameters(), self.checkpoint)
        else:
            return self._forward(
                x, context=context,concept_token_idx=concept_token_idx, target_input=target_input, C_inv=C_inv, beta=beta, tau=tau, **kwargs)

    def _forward(self, x, context=None,concept_token_idx=None, target_input=None, C_inv=None, beta=0.75, tau=0.1, **kwargs):
        x = self.attn1(self.norm1(x), context=context if self.disable_self_attn else None,
                       concept_token_idx=concept_token_idx if self.disable_self_attn else None,
                       target_input=target_input, C_inv=C_inv, beta=beta, tau=tau, **kwargs) + x
        x = self.attn2(self.norm2(x), context=context,concept_token_idx=concept_token_idx, target_input=target_input, C_inv=C_inv,
                       beta=beta, tau=tau, **kwargs) + x
        x = self.ff(self.norm3(x)) + x
        return x


class SpatialTransformer(nn.Module):
    """
    Transformer block for image-like data.
    First, project the input (aka embedding)
    and reshape to b, t, d.
    Then apply standard transformer action.
    Finally, reshape to image
    NEW: use_linear for more efficiency instead of the 1x1 convs
    """

    def __init__(self, in_channels, n_heads, d_head,
                 depth=1, dropout=0., context_dim=None,
                 disable_self_attn=False, use_linear=False,
                 use_checkpoint=True):
        super().__init__()
        if exists(context_dim) and not isinstance(context_dim, list):
            context_dim = [context_dim]
        self.in_channels = in_channels
        inner_dim = n_heads * d_head
        self.norm = Normalize(in_channels)
        if not use_linear:
            self.proj_in = nn.Conv2d(in_channels,
                                     inner_dim,
                                     kernel_size=1,
                                     stride=1,
                                     padding=0)
        else:
            self.proj_in = nn.Linear(in_channels, inner_dim)

        self.transformer_blocks = nn.ModuleList(
            [BasicTransformerBlock(inner_dim, n_heads, d_head, dropout=dropout, context_dim=context_dim[d],
                                   disable_self_attn=disable_self_attn, checkpoint=use_checkpoint)
             for d in range(depth)]
        )
        if not use_linear:
            self.proj_out = zero_module(nn.Conv2d(inner_dim,
                                                  in_channels,
                                                  kernel_size=1,
                                                  stride=1,
                                                  padding=0))
        else:
            self.proj_out = zero_module(nn.Linear(in_channels, inner_dim))
        self.use_linear = use_linear

    def forward(self, x, context=None,concept_token_idx=None, **kwargs):
        # note: if no context is given, cross-attention defaults to self-attention
        if not isinstance(context, list):
            context = [context]
        if not isinstance(concept_token_idx, list):
            concept_token_idx = [concept_token_idx]
        b, c, h, w = x.shape
        x_in = x
        x = self.norm(x)
        if not self.use_linear:
            x = self.proj_in(x)
        x = rearrange(x, 'b c h w -> b (h w) c').contiguous()
        if self.use_linear:
            x = self.proj_in(x)
        for i, block in enumerate(self.transformer_blocks):
            x = block(x, context=context[i],concept_token_idx=concept_token_idx[i], **kwargs)
        if self.use_linear:
            x = self.proj_out(x)
        x = rearrange(x, 'b (h w) c -> b c h w', h=h, w=w).contiguous()
        if not self.use_linear:
            x = self.proj_out(x)
        return x + x_in
