#!/usr/bin/env python

# wujian@2020

import pathlib
import argparse

import torch as th
import numpy as np

from aps.loader import WaveReader, write_wav
from aps.utils import get_logger
from aps.eval import Computer

logger = get_logger(__name__)


class Separator(Computer):
    """
    Decoder wrapper
    """
    def __init__(self, cpt_dir, device_id=-1):
        super(Separator, self).__init__(cpt_dir,
                                        device_id=device_id,
                                        task="enh")
        logger.info(f"Load checkpoint from {cpt_dir}: epoch {self.epoch}")

    def run(self, src, chunk_len=-1, chunk_hop=-1):
        """
        Args:
            src (Array): (C) x S
        """
        if chunk_hop <= 0 and chunk_len > 0:
            chunk_hop = chunk_len
        N = src.shape[-1]
        src = th.from_numpy(src).to(self.device)
        if chunk_len == -1:
            return self.nnet.infer(src)
        else:
            chunks = []
            # now only for enhancement task
            for t in range(0, N, chunk_hop):
                pad = N - t - chunk_len
                if pad <= 0:
                    c = src[..., t:t + chunk_len]
                else:
                    c = th.nn.functional.pad(src[..., t:], (0, pad, 0, 0))
                s = self.nnet.infer(c)
                if pad > 0:
                    chunks.append(s[..., :-pad])
                else:
                    chunks.append(s)
            sep = th.zeros_like(src)
            for i, c in enumerate(chunks):
                beg = i * chunk_hop
                if i == len(chunks) - 1:
                    sep[beg:] = c
                else:
                    sep[beg:beg + chunk_hop] = c
            return sep


def run(args):
    sep_dir = pathlib.Path(args.sep_dir)
    separator = Separator(args.checkpoint, device_id=args.device_id)
    mix_reader = WaveReader(args.wav_scp, sr=args.sr, channel=args.channel)

    for key, mix in mix_reader:
        logger.info(f"Processing utterance {key}...")
        norm = np.max(np.abs(mix))
        sep = separator.run(mix)
        if isinstance(sep, th.Tensor):
            sep = sep.cpu().numpy()
            sep = sep * norm / np.max(np.abs(sep))
            write_wav(sep_dir / f"{key}.wav", sep, sr=args.sr)
        else:
            sep = [s.cpu().numpy() for s in sep]
            for i, s in enumerate(sep):
                s = s * norm / np.max(np.abs(s))
                write_wav(sep_dir / f"spk{i + 1}/{key}.wav", s, sr=args.sr)
    logger.info(f"Processed {len(mix_reader)} utterances done!")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Command to do speech separation",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("wav_scp",
                        type=str,
                        help="Mixture & Noisy input audio scripts")
    parser.add_argument("sep_dir",
                        type=str,
                        help="Directory to dump enhanced/separated output")
    parser.add_argument("--checkpoint",
                        type=str,
                        required=True,
                        help="Checkpoint of the separation/enhancement model")
    parser.add_argument("--device-id",
                        type=int,
                        default=-1,
                        help="GPU-id to offload model to, "
                        "-1 means running on CPU")
    parser.add_argument("--chunk-len",
                        type=int,
                        default=-1,
                        help="Chunk length for inference, "
                        "-1 means the whole utterance")
    parser.add_argument("--chunk-hop",
                        type=int,
                        default=-1,
                        help="Chunk hop size for inference")
    parser.add_argument("--sr",
                        type=int,
                        default=16000,
                        help="Sample rate of the source audio")
    parser.add_argument("--channel",
                        type=int,
                        default=-1,
                        help="Channel index for source audio")
    args = parser.parse_args()
    run(args)