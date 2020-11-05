# Copyright 2020 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import torch as th
import torch.nn as nn
import torch.nn.functional as tf

from itertools import permutations
from typing import List, Any, Callable, Optional
from aps.const import IGNORE_ID


def ce_objf(outs: th.Tensor, tgts: th.Tensor) -> th.Tensor:
    """
    Cross entropy loss function
    Args:
        outs (Tensor): N x T x V
        tgts (Tensor): N x T
    Return
        loss (Tensor): (1)
    """
    _, _, V = outs.shape
    # N(To+1) x V
    outs = outs.view(-1, V)
    # N(To+1)
    tgts = tgts.view(-1)
    ce_loss = tf.cross_entropy(outs,
                               tgts,
                               ignore_index=IGNORE_ID,
                               reduction="mean")
    return ce_loss


def ls_objf(outs: th.Tensor,
            tgts: th.Tensor,
            lsm_factor: float = 0.1) -> th.Tensor:
    """
    Label smooth loss function (using KL)
    Args:
        outs (Tensor): N x T x V
        tgts (Tensor): N x T
    Return
        loss (Tensor): (1)
    """
    _, _, V = outs.shape
    # NT x V
    outs = outs.view(-1, V)
    # NT
    tgts = tgts.view(-1)
    mask = (tgts != IGNORE_ID)
    # M x V
    outs = th.masked_select(outs, mask.unsqueeze(-1)).view(-1, V)
    # M
    tgts = th.masked_select(tgts, mask)
    # M x V
    dist = outs.new_full(outs.size(), lsm_factor / V)
    dist = dist.scatter_(1, tgts.unsqueeze(-1), 1 - lsm_factor)
    # KL distance
    loss = tf.kl_div(tf.log_softmax(outs, -1), dist, reduction="batchmean")
    return loss


def multiple_objf(inp: List[Any],
                  ref: List[Any],
                  objf: Callable,
                  weight: Optional[List[float]] = None,
                  transform: Optional[Callable] = None,
                  batchmean: bool = False) -> th.Tensor:
    """
    Compute summary of multiple loss functions
    Args:
        inp (list(Object)): estimated list
        ref (list(Object)): reference list
        objf (function): function to compute single pair loss (per mini-batch)
    Return:
        loss (Tensor): N (per mini-batch) if batchmean == False
    """
    if len(inp) != len(ref):
        raise ValueError("Size mismatch between #inp and " +
                         f"#ref: {len(inp)} vs {len(ref)}")
    num_tasks = len(inp)
    if weight == None:
        weight = [1 / num_tasks] * num_tasks

    if len(weight) != len(inp):
        raise RuntimeError(
            f"Missing weight ({len(weight)}) for {len(inp)} tasks")
    if transform:
        inp = [transform(i) for i in inp]
        ref = [transform(r) for r in ref]

    loss = [objf(o, r) for o, r in zip(inp, ref)]
    # NOTE: summary not average
    loss = sum([s * l for s, l in zip(weight, loss)])
    if batchmean:
        loss = th.mean(loss)
    return loss


def permu_invarint_objf(inp: List[Any],
                        ref: List[Any],
                        objf: Callable,
                        transform: Optional[Callable] = None,
                        batchmean: bool = False,
                        return_permutation: bool = False) -> th.Tensor:
    """
    Compute permutation-invariant loss
    Args:
        inp (list(Object)): estimated list
        ref (list(Object)): reference list
        objf (function): function to compute single pair loss (per mini-batch)
    Return:
        loss (Tensor): N (per mini-batch) if batchmean == False
    """
    num_spks = len(inp)
    if num_spks != len(ref):
        raise ValueError("Size mismatch between #inp and " +
                         f"#ref: {num_spks} vs {len(ref)}")

    def permu_objf(permu, out, ref):
        """
        Return tensor (P x N) for each permutation and mini-batch
        """
        return sum([objf(out[s], ref[t]) for s, t in enumerate(permu)
                   ]) / len(permu)

    if transform:
        inp = [transform(i) for i in inp]
        ref = [transform(r) for r in ref]

    loss_mat = th.stack(
        [permu_objf(p, inp, ref) for p in permutations(range(num_spks))])

    # if we want to maximize the objective, i.e, snr, remember to add negative flag to the objf
    loss, index = th.min(loss_mat, dim=0)
    if batchmean:
        loss = th.mean(loss)
    if return_permutation:
        return loss, index
    else:
        return loss


class MultiObjfComputer(nn.Module):
    """
    A class to compute summary of multiple objective functions
    """

    def __init__(self):
        super(MultiObjfComputer, self).__init__()

    def forward(self,
                inp: List[Any],
                ref: List[Any],
                objf: Callable,
                weight: Optional[List[float]] = None,
                transform: Optional[Callable] = None,
                batchmean: bool = False) -> th.Tensor:
        return multiple_objf(inp,
                             ref,
                             objf,
                             weight=weight,
                             transform=transform,
                             batchmean=batchmean)


class PermuInvarintObjfComputer(nn.Module):
    """
    A class to compute permutation-invariant objective function
    """

    def __init__(self):
        super(PermuInvarintObjfComputer, self).__init__()

    def forward(self,
                inp: List[Any],
                ref: List[Any],
                objf: Callable,
                transform: Optional[Callable] = None,
                batchmean: bool = False,
                return_permutation: bool = False) -> th.Tensor:
        return permu_invarint_objf(inp,
                                   ref,
                                   objf,
                                   transform=transform,
                                   return_permutation=return_permutation,
                                   batchmean=batchmean)