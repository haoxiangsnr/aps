#!/usr/bin/env python

# wujian@2019

import yaml
import codecs
import random
import pprint
import pathlib
import argparse

import torch as th
import numpy as np

from aps.utils import StrToBoolAction, set_seed
from aps.trainer.ddp import Trainer

from aps.loader import support_loader
from aps.transform import support_transform
from aps.task import support_task
from aps.asr import support_nnet

blank_sym = "<blank>"
constrained_conf_keys = [
    "nnet", "nnet_conf", "task", "task_conf", "data_conf", "trainer_conf",
    "asr_transform", "enh_transform"
]


def load_conf(yaml_conf, dict_path):
    """
    Load yaml configurations
    """
    # load configurations
    with open(yaml_conf, "r") as f:
        conf = yaml.full_load(f)
    # create task_conf if None
    if "task_conf" not in conf:
        conf["task_conf"] = {}

    nnet_conf = conf["nnet_conf"]
    # add dictionary info
    with codecs.open(dict_path, encoding="utf-8") as f:
        vocab = {}
        for line in f:
            unit, idx = line.split()
            vocab[unit] = int(idx)

    if "<sos>" not in vocab or "<eos>" not in vocab:
        raise ValueError(f"Missing <sos>/<eos> in {args.dict}")
    nnet_conf["vocab_size"] = len(vocab)

    for key in conf.keys():
        if key not in constrained_conf_keys:
            raise ValueError(f"Invalid configuration item: {key}")
    print("Arguments in yaml:\n{}".format(pprint.pformat(conf)), flush=True)
    trainer_conf = conf["trainer_conf"]
    use_ctc = "ctc_regularization" in trainer_conf and trainer_conf[
        "ctc_regularization"] > 0
    is_transducer = conf["task"] == "transducer"
    if not is_transducer:
        nnet_conf["sos"] = vocab["<sos>"]
        nnet_conf["eos"] = vocab["<eos>"]
    # for CTC/RNNT
    if use_ctc or is_transducer:
        if blank_sym not in vocab:
            raise RuntimeError(
                f"Missing {blank_sym} in dictionary for CTC/RNNT training")
        conf["task_conf"]["blank"] = vocab[blank_sym]
        if is_transducer:
            nnet_conf["blank"] = vocab[blank_sym]
        else:
            nnet_conf["ctc"] = use_ctc
    return conf


def run(args):
    # set random seed
    seed = set_seed(args.seed)
    if seed is not None:
        print(f"Set random seed as {seed}")

    conf = load_conf(args.conf, args.dict)
    data_conf = conf["data_conf"]
    trn_loader = support_loader(**data_conf["train"],
                                train=True,
                                fmt=data_conf["fmt"],
                                batch_size=args.batch_size,
                                num_workers=args.num_workers,
                                **data_conf["loader"])
    dev_loader = support_loader(**data_conf["valid"],
                                train=False,
                                fmt=data_conf["fmt"],
                                batch_size=args.batch_size,
                                num_workers=args.num_workers,
                                **data_conf["loader"])

    asr_cls = support_nnet(conf["nnet"])
    asr_transform = None
    enh_transform = None
    if "asr_transform" in conf:
        asr_transform = support_transform("asr")(**conf["asr_transform"])
    if "enh_transform" in conf:
        enh_transform = support_transform("enh")(**conf["enh_transform"])

    if enh_transform:
        nnet = asr_cls(enh_transform=enh_transform,
                       asr_transform=asr_transform,
                       **conf["nnet_conf"])
    elif asr_transform:
        nnet = asr_cls(asr_transform=asr_transform, **conf["nnet_conf"])
    else:
        nnet = asr_cls(**conf["nnet_conf"])

    task = support_task(conf["task"], nnet, **conf["task_conf"])

    trainer = Trainer(task,
                      device_ids=args.device_id,
                      checkpoint=args.checkpoint,
                      resume=args.resume,
                      init=args.init,
                      save_interval=args.save_interval,
                      prog_interval=args.prog_interval,
                      tensorboard=args.tensorboard,
                      **conf["trainer_conf"])

    if args.eval_interval > 0:
        trainer.run_batch_per_epoch(trn_loader,
                                    dev_loader,
                                    num_epochs=args.epochs,
                                    eval_interval=args.eval_interval)
    else:
        trainer.run(trn_loader, dev_loader, num_epochs=args.epochs)

    # dump configurations
    with open(f"{args.checkpoint}/train.yaml", "w") as f:
        yaml.dump(conf, f)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description=
        "Command to start ASR model training, configured by yaml files",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("--conf",
                        type=str,
                        required=True,
                        help="Yaml configuration file for training")
    parser.add_argument("--dict",
                        type=str,
                        required=True,
                        help="Dictionary file")
    parser.add_argument("--device-id",
                        type=str,
                        default="0",
                        help="Training on which GPU device")
    parser.add_argument("--epochs",
                        type=int,
                        default=50,
                        help="Number of training epoches")
    parser.add_argument("--checkpoint",
                        type=str,
                        required=True,
                        help="Directory to save models")
    parser.add_argument("--resume",
                        type=str,
                        default="",
                        help="Exist model to resume training from")
    parser.add_argument("--init",
                        type=str,
                        default="",
                        help="Exist model to initialize model training")
    parser.add_argument("--batch-size",
                        type=int,
                        default=32,
                        help="Number of utterances in each batch")
    parser.add_argument("--eval-interval",
                        type=int,
                        default=-1,
                        help="Number of batches trained per epoch "
                        "(for larger training dataset)")
    parser.add_argument("--save-interval",
                        type=int,
                        default=-1,
                        help="Interval to save the checkpoint")
    parser.add_argument("--prog-interval",
                        type=int,
                        default=100,
                        help="Interval to report the progress of the training")
    parser.add_argument("--num-workers",
                        type=int,
                        default=4,
                        help="Number of workers used in script data loader")
    parser.add_argument("--tensorboard",
                        action=StrToBoolAction,
                        default="false",
                        help="Flags to use the tensorboad")
    parser.add_argument("--seed",
                        type=str,
                        default="777",
                        help="Random seed used for random package")
    args = parser.parse_args()
    print("Arguments in args:\n{}".format(pprint.pformat(vars(args))),
          flush=True)
    run(args)
