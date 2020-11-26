#!/usr/bin/env python

# Copyright 2019 Jian Wu
# License: Apache 2.0 (http://www.apache.org/licenses/LICENSE-2.0)

import random

import torch as th
import torch.nn as nn
import torch.nn.functional as F

from typing import List, Dict, Optional, Tuple, Union
from aps.const import NEG_INF


def trace_back_hypos(point: th.Tensor,
                     back_point: List[th.Tensor],
                     hist_token: List[th.Tensor],
                     score: th.Tensor,
                     sos: int = 1,
                     eos: int = 2) -> List[Dict]:
    """
    Trace back the decoding transcription sequence from the current time point
    Args:
        point (Tensor): starting point
        back_point (list[Tensor]): father point at each step
        hist_token (list[Tensor]): beam token at each step
        score (Tensor): decoding score
    """
    trans = []
    score = score.tolist()
    for ptr, tok in zip(back_point[::-1], hist_token[::-1]):
        trans.append(tok[point].tolist())
        point = ptr[point]
    hypos = []
    trans = trans[::-1]
    for i, s in enumerate(score):
        token = [t[i] for t in trans]
        hypos.append({"score": s, "trans": [sos] + token + [eos]})
    return hypos


class OneHotEmbedding(nn.Module):
    """
    Onehot encode
    """

    def __init__(self, vocab_size: int):
        super(OneHotEmbedding, self).__init__()
        self.vocab_size = vocab_size

    def extra_repr(self):
        return f"vocab_size={self.vocab_size}"

    def forward(self, x: th.Tensor) -> th.Tensor:
        """
        args:
            x: ...
        return
            e: ... x V
        """
        S = list(x.shape) + [self.vocab_size]
        # ... x V
        H = th.zeros(S, dtype=th.float32, device=x.device)
        # set one
        H = H.scatter(-1, x[..., None], 1)
        return H


class VanillaRNNDecoder(nn.Module):
    """
    PyTorch's RNN decoder
    """

    def __init__(self,
                 enc_proj: int,
                 vocab_size: int,
                 dec_rnn: str = "lstm",
                 rnn_layers: int = 3,
                 rnn_hidden: int = 512,
                 rnn_dropout: float = 0.0,
                 input_feeding: bool = False,
                 vocab_embeded: bool = True) -> None:
        super(VanillaRNNDecoder, self).__init__()
        RNN = dec_rnn.upper()
        supported_rnn = {"RNN": nn.RNN, "GRU": nn.GRU, "LSTM": nn.LSTM}
        if RNN not in supported_rnn:
            raise RuntimeError(f"unknown RNN type: {RNN}")
        if vocab_embeded:
            self.vocab_embed = nn.Embedding(vocab_size, rnn_hidden)
            input_size = enc_proj + rnn_hidden
        else:
            self.vocab_embed = OneHotEmbedding(vocab_size)
            input_size = enc_proj + vocab_size
        self.decoder = supported_rnn[RNN](input_size,
                                          rnn_hidden,
                                          rnn_layers,
                                          batch_first=True,
                                          dropout=rnn_dropout,
                                          bidirectional=False)
        self.proj = nn.Linear(rnn_hidden + enc_proj, enc_proj)
        self.pred = nn.Linear(enc_proj, vocab_size)
        self.input_feeding = input_feeding
        self.vocab_size = vocab_size

    def _step_decoder(
        self,
        emb_pre: th.Tensor,
        att_ctx: th.Tensor,
        dec_hid: Union[th.Tensor, Tuple[th.Tensor, th.Tensor], None] = None
    ) -> Tuple[th.Tensor, Union[th.Tensor, Tuple[th.Tensor, th.Tensor]]]:
        """
        Args
            emb_pre: N x D_emb
            att_ctx: N x D_enc
        """
        # N x 1 x (D_emb+D_enc)
        dec_in = th.cat([emb_pre, att_ctx], dim=-1).unsqueeze(1)
        # N x 1 x (D_emb+D_enc) => N x 1 x D_dec
        dec_out, hx = self.decoder(dec_in, hx=dec_hid)
        # N x 1 x D_dec => N x D_dec
        return dec_out.squeeze(1), hx

    def _step(
        self,
        att_net: nn.Module,
        out_pre: th.Tensor,
        enc_out: th.Tensor,
        att_ctx: th.Tensor,
        dec_hid: Union[th.Tensor, Tuple[th.Tensor, th.Tensor], None] = None,
        att_ali: Optional[th.Tensor] = None,
        enc_len: Optional[th.Tensor] = None,
        proj: Optional[th.Tensor] = None
    ) -> Tuple[th.Tensor, th.Tensor, Union[th.Tensor, Tuple[
            th.Tensor, th.Tensor]], th.Tensor, th.Tensor]:
        """
        Make a prediction step
        """
        # N x D_emb or N x V
        emb_pre = self.vocab_embed(out_pre)
        # dec_out: N x D_dec
        dec_out, dec_hid = self._step_decoder(
            emb_pre, proj if self.input_feeding else att_ctx, dec_hid=dec_hid)
        # att_ali: N x Ti, att_ctx: N x D_enc
        att_ali, att_ctx = att_net(enc_out, enc_len, dec_out, att_ali)
        # proj: N x D_enc
        proj = self.proj(th.cat([dec_out, att_ctx], dim=-1))
        # pred: N x V
        pred = self.pred(F.relu(proj))
        return att_ali, att_ctx, dec_hid, proj, pred

    def forward(self,
                att_net: nn.Module,
                enc_pad: th.Tensor,
                enc_len: Optional[th.Tensor],
                tgt_pad: th.Tensor,
                sos: int = -1,
                schedule_sampling: float = 0) -> Tuple[th.Tensor, th.Tensor]:
        """
        Args
            enc_pad: N x Ti x D_enc
            enc_len: N or None
            tgt_pad: N x To
            schedule_sampling:
                1: using prediction
                0: using ground truth
        Return
            outs: N x To x V
            alis: N x To x T
        """
        N, _, D_enc = enc_pad.shape
        outs = []  # collect prediction
        att_ali = None  # attention alignments
        dec_hid = None
        dev = enc_pad.device
        # zero init context
        att_ctx = th.zeros([N, D_enc], device=dev)
        proj = th.zeros([N, D_enc], device=dev)
        alis = []  # collect alignments
        # step by step
        #   0   1   2   3   ... T
        # SOS   t0  t1  t2  ... t{T-1}
        #  t0   t1  t2  t3  ... EOS
        for t in range(tgt_pad.shape[-1] + 1):
            # using output at previous time step
            # out: N
            if t and random.random() < schedule_sampling:
                out_pre = th.argmax(outs[-1].detach(), dim=1)
            else:
                if t == 0:
                    out_pre = th.tensor([sos] * N, dtype=th.int64, device=dev)
                else:
                    out_pre = tgt_pad[:, t - 1]
            # step forward
            att_ali, att_ctx, dec_hid, proj, pred = self._step(att_net,
                                                               out_pre,
                                                               enc_pad,
                                                               att_ctx,
                                                               dec_hid=dec_hid,
                                                               att_ali=att_ali,
                                                               enc_len=enc_len,
                                                               proj=proj)
            outs.append(pred)
            alis.append(att_ali)
        # N x To x V
        outs = th.stack(outs, dim=1)
        # N x To x Ti
        alis = th.stack(alis, dim=1)
        return outs, alis

    def beam_search(self,
                    att_net: nn.Module,
                    enc_out: th.Tensor,
                    beam: int = 8,
                    nbest: int = 1,
                    max_len: int = -1,
                    sos: int = -1,
                    eos: int = -1,
                    normalized: bool = True) -> List[Dict]:
        """
        Beam search algothrim (intuitive but not efficient)
        Args
            enc_out: 1 x T x F
        """
        if sos < 0 or eos < 0:
            raise RuntimeError(f"Invalid SOS/EOS ID: {sos:d}/{eos:d}")
        if max_len <= 0:
            raise RuntimeError(f"Invalid max_len: {max_len:d}")
        N, _, D_enc = enc_out.shape
        if N != 1:
            raise RuntimeError(
                f"Got batch size {N:d}, now only support one utterance")
        dev = enc_out.device
        att_ctx = th.zeros([N, D_enc], device=dev)
        proj = th.zeros([N, D_enc], device=dev)

        def init_node():
            return {
                "proj": proj,
                "score": 0.0,
                "trans": [sos],
                "att_ali": None,
                "att_ctx": att_ctx,
                "dec_hid": None
            }

        alive = [init_node()]
        hypos = []
        nbest = min(beam, nbest)
        if beam > self.vocab_size:
            raise RuntimeError(f"Beam size({beam}) > vocabulary size")
        # step by step
        for t in range(max_len):
            beams = []
            for n in alive:
                # [x], out is different
                out = th.tensor([n["trans"][-1]], dtype=th.int64, device=dev)
                # step forward
                att_ali, att_ctx, dec_hid, proj, pred = self._step(
                    att_net,
                    out,
                    enc_out,
                    n["att_ctx"],
                    dec_hid=n["dec_hid"],
                    att_ali=n["att_ali"],
                    proj=n["proj"])
                # compute prob: V, nagetive
                prob = F.log_softmax(pred, dim=1).squeeze(0)
                # beam
                topk_score, topk_index = th.topk(prob, beam)
                # new node
                next_node_templ = {
                    "att_ali": att_ali,
                    "att_ctx": att_ctx,
                    "dec_hid": dec_hid,
                    "score": n["score"],
                    "proj": proj
                }
                for score, index in zip(topk_score, topk_index):
                    # copy
                    new_node = next_node_templ.copy()
                    # add score
                    new_node["score"] += score.item()
                    # add trans
                    new_node["trans"] = n["trans"].copy()
                    new_node["trans"].append(index.item())
                    beams.append(new_node)
            # clip beam
            beams = sorted(beams, key=lambda n: n["score"], reverse=True)[:beam]

            # add finished ones
            hypos.extend([n for n in beams if n["trans"][-1] == eos])
            # keep unfinished ones
            alive = [n for n in beams if n["trans"][-1] != eos]

            if not len(alive):
                break

            if len(hypos) >= beam:
                break

            if t == max_len - 1:
                for n in alive:
                    n["trans"].append(eos)
                    hypos.append(n)

        # choose nbest
        if normalized:
            nbest_hypos = sorted(hypos,
                                 key=lambda n: n["score"] /
                                 (len(n["trans"]) - 1),
                                 reverse=True)
        else:
            nbest_hypos = sorted(hypos, key=lambda n: n["score"], reverse=True)
        return [{
            "score": n["score"],
            "trans": n["trans"]
        } for n in nbest_hypos[:nbest]]

    def beam_search_vectorized(self,
                               att_net: nn.Module,
                               enc_out: th.Tensor,
                               lm: Optional[nn.Module] = None,
                               lm_weight: float = 0,
                               beam: int = 8,
                               nbest: int = 1,
                               max_len: int = -1,
                               sos: int = -1,
                               eos: int = -1,
                               normalized: bool = True) -> List[Dict]:
        """
        Vectorized beam search algothrim
        Args
            enc_out: 1 x T x F
        """
        if sos < 0 or eos < 0:
            raise RuntimeError(f"Invalid SOS/EOS ID: {sos:d}/{eos:d}")
        if max_len <= 0:
            raise RuntimeError(f"Invalid max_len: {max_len:d}")
        N, _, D_enc = enc_out.shape
        if N != 1:
            raise RuntimeError(
                f"Got batch size {N:d}, now only support one utterance")

        nbest = min(beam, nbest)
        if beam > self.vocab_size:
            raise RuntimeError(f"Beam size({beam}) > vocabulary size")

        dev = enc_out.device
        att_ali = None
        dec_hid = None
        # N x T x F => N*beam x T x F
        enc_out = th.repeat_interleave(enc_out, beam, 0)
        att_ctx = th.zeros([N * beam, D_enc], device=dev)
        proj = th.zeros([N * beam, D_enc], device=dev)

        accu_score = th.zeros(beam, device=dev)
        hist_token = []
        back_point = []
        lm_state = None

        hypos = []
        # step by step
        for t in range(max_len):
            # beam
            if t:
                out = hist_token[-1]
                point = back_point[-1]
            else:
                out = th.tensor([sos] * (beam * N), dtype=th.int64, device=dev)
                point = th.arange(0, beam, dtype=th.int64, device=dev)

            # swap order
            if dec_hid is not None:
                if isinstance(dec_hid, tuple):
                    # shape: num_layers * num_directions, batch, hidden_size
                    h, c = dec_hid
                    dec_hid = (h[:, point], c[:, point])
                else:
                    dec_hid = dec_hid[:, point]
            if att_ali is not None:
                att_ali = att_ali[point]

            # step forward
            att_ali, att_ctx, dec_hid, proj, pred = self._step(att_net,
                                                               out,
                                                               enc_out,
                                                               att_ctx[point],
                                                               dec_hid=dec_hid,
                                                               att_ali=att_ali,
                                                               proj=proj[point])
            # compute prob: beam x V, nagetive
            prob = F.log_softmax(pred, dim=-1)

            if lm:
                if lm_state is not None:
                    if isinstance(lm_state, tuple):
                        # shape: num_layers * num_directions, batch, hidden_size
                        h, c = lm_state
                        lm_state = (h[:, point], c[:, point])
                    else:
                        lm_state = lm_state[:, point]
                lm_prob, lm_state = lm(out[..., None], lm_state)
                # beam x V
                prob += F.log_softmax(lm_prob[:, 0], dim=-1) * lm_weight

            # local pruning: beam x beam
            topk_score, topk_token = th.topk(prob, beam, dim=-1)
            if t == 0:
                # beam
                accu_score += topk_score[0]
                token = topk_token[0]
            else:
                # beam x beam = beam x 1 + beam x beam
                accu_score = accu_score[..., None] + topk_score
                # beam*beam => beam
                accu_score, topk_index = th.topk(accu_score.view(-1),
                                                 beam,
                                                 dim=-1)
                # point to father's node
                point = topk_index // beam

                # beam*beam
                topk_token = topk_token.view(-1)
                token = topk_token[topk_index]

            # continue flags
            end_eos = (token == eos).tolist()

            # process eos nodes
            if sum(end_eos):
                idx = [
                    i for i, end_with_eos in enumerate(end_eos) if end_with_eos
                ]
                idx = th.tensor(idx, dtype=th.int64, device=dev)
                hyp_full = trace_back_hypos(point[idx],
                                            back_point,
                                            hist_token,
                                            accu_score[idx],
                                            sos=sos,
                                            eos=eos)
                accu_score[idx] = NEG_INF
                hypos += hyp_full

            if len(hypos) >= beam:
                break

            # add best token
            hist_token.append(token)
            back_point.append(point)

            # process non-eos nodes at the final step
            if t == max_len - 1:
                end_wo_eos = (token != eos).tolist()
                if sum(end_wo_eos):
                    idx = [i for i, go_on in enumerate(end_wo_eos) if go_on]
                    idx = th.tensor(idx, dtype=th.int64, device=dev)
                    hyp_partial = trace_back_hypos(idx,
                                                   back_point,
                                                   hist_token,
                                                   accu_score[idx],
                                                   sos=sos,
                                                   eos=eos)
                    hypos += hyp_partial

        if normalized:
            nbest_hypos = sorted(hypos,
                                 key=lambda n: n["score"] /
                                 (len(n["trans"]) - 1),
                                 reverse=True)
        else:
            nbest_hypos = sorted(hypos, key=lambda n: n["score"], reverse=True)
        return nbest_hypos[:nbest]

    def beam_search_batch(self,
                          att_net: nn.Module,
                          enc_out: th.Tensor,
                          enc_len: th.Tensor,
                          beam: int = 8,
                          nbest: int = 1,
                          max_len: int = -1,
                          sos: int = -1,
                          eos: int = -1,
                          normalized: bool = True) -> List[Dict]:
        """
        Batch level vectorized beam search algothrim (NOTE: not stable!)
        Args
            enc_out: N x T x F
            enc_len: N
        """
        if sos < 0 or eos < 0:
            raise RuntimeError(f"Invalid SOS/EOS ID: {sos:d}/{eos:d}")
        if max_len <= 0:
            raise RuntimeError(f"Invalid max_len: {max_len:d}")
        N, _, D_enc = enc_out.shape

        def _trace_back_hypos(uttid,
                              point,
                              back_point,
                              hist_token,
                              score,
                              sos=1,
                              eos=2):
            """
            Trace back from current time point
            """
            trans = []
            score = score.item()
            for ptr, tok in zip(back_point[::-1], hist_token[::-1]):
                trans.append(tok[uttid, point].item())
                point = ptr[uttid, point]
            return {"score": score, "trans": [sos] + trans[::-1] + [eos]}

        nbest = min(beam, nbest)
        if beam > self.vocab_size:
            raise RuntimeError(f"Beam size({beam}) > vocabulary size")

        dev = enc_out.device
        att_ali = None
        dec_hid = None
        # N x T x F => N*beam x T x F
        enc_out = th.repeat_interleave(enc_out, beam, 0)
        enc_len = th.repeat_interleave(enc_len, beam, 0)
        att_ctx = th.zeros([N * beam, D_enc], device=dev)
        proj = th.zeros([N * beam, D_enc], device=dev)

        accu_score = th.zeros(N, beam, device=dev)
        hist_token = []
        back_point = []
        step_point = th.arange(0, beam * N, beam, device=dev, dtype=th.int64)
        # for each utterance
        hypos = [[] for _ in range(N)]
        stop_batch = [False] * N
        # step by step
        for t in range(max_len):
            # N*beam
            if t:
                out = hist_token[-1].view(-1)
                # N x beam
                point = back_point[-1] + step_point[..., None]
                point = point.view(-1)
            else:
                out = th.tensor([sos] * (beam * N), dtype=th.int64, device=dev)
                point = th.tensor(list(range(beam)) * N,
                                  dtype=th.int64,
                                  device=dev)

            # swap order
            if dec_hid is not None:
                if isinstance(dec_hid, tuple):
                    # shape: num_layers * num_directions, batch, hidden_size
                    h, c = dec_hid
                    dec_hid = (h[:, point], c[:, point])
                else:
                    dec_hid = dec_hid[:, point]
            if att_ali is not None:
                att_ali = att_ali[point]

            # step forward
            att_ali, att_ctx, dec_hid, proj, pred = self._step(att_net,
                                                               out,
                                                               enc_out,
                                                               att_ctx[point],
                                                               enc_len=enc_len,
                                                               dec_hid=dec_hid,
                                                               att_ali=att_ali,
                                                               proj=proj[point])
            # compute prob: N*beam x V, nagetive
            prob = F.log_softmax(pred, dim=-1)
            # local pruning: N*beam x beam
            topk_score, topk_token = th.topk(prob, beam, dim=-1)
            if t == 0:
                # N x beam
                accu_score += topk_score[::beam]
                token = topk_token[::beam]
                point = point.view(N, -1)
            else:
                # N*beam x beam = N*beam x 1 + N*beam x beam
                accu_score = accu_score.view(-1, 1) + topk_score
                # N x beam*beam => N x beam
                accu_score, topk_index = th.topk(accu_score.view(N, -1),
                                                 beam,
                                                 dim=-1)
                # point to father's node
                # N x beam
                point = topk_index // beam

                # N x beam*beam
                topk_token = topk_token.view(N, -1)
                token = th.gather(topk_token, -1, topk_index)

            # continue flags, N x beam
            not_end = (token != eos).tolist()

            # process eos nodes
            for u in range(N):
                # skip utterance u
                if sum(not_end[u]) == 0 or len(hypos[u]) >= beam:
                    stop_batch[u] = True
                else:
                    for i, go_on in enumerate(not_end[u]):
                        if not go_on:
                            hyp = _trace_back_hypos(u,
                                                    point[u, i],
                                                    back_point,
                                                    hist_token,
                                                    accu_score[u, i],
                                                    sos=sos,
                                                    eos=eos)
                            accu_score[u, i] = NEG_INF
                            hypos[u].append(hyp)

            # all True, break search
            if sum(stop_batch) == N:
                break

            # add best token
            hist_token.append(token.clone())
            back_point.append(point)

            # process non-eos nodes at the final step
            if t == max_len - 1:
                for u in range(N):
                    # skip utterance u
                    if stop_batch[u]:
                        continue
                    for i, go_on in enumerate(not_end[u]):
                        if go_on:
                            hyp = _trace_back_hypos(u,
                                                    i,
                                                    back_point,
                                                    hist_token,
                                                    accu_score[u, i],
                                                    sos=sos,
                                                    eos=eos)
                            hypos[u].append(hyp)

        nbest_hypos = []
        for utt_bypos in hypos:
            if normalized:
                hypos = sorted(utt_bypos,
                               key=lambda n: n["score"] / (len(n["trans"]) - 1),
                               reverse=True)
            else:
                hypos = sorted(utt_bypos,
                               key=lambda n: n["score"],
                               reverse=True)
            nbest_hypos.append(hypos[:nbest])
        return nbest_hypos
