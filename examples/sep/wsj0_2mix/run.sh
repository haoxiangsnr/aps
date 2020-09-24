#!/usr/bin/env bash

# wujian@2019

set -eu

stage=1
dataset="wsj0_2mix"
exp="1a"
gpu=0
seed=777
epochs=100
tensorboard=false
batch_size=8
num_workers=4
eval_interval=-1
save_interval=-1
prog_interval=100

. ./utils/parse_options.sh || exit 1;

[ $# -ne 1 ] && echo "Script format error: $0 <wsj0-2mix-dir>" && exit 1

data_dir=$1

prepare_scp () {
  find $2 -name "*.$1" | awk -F '/' '{printf("%s\t%s\n", $NF, $0)}' | sed "s:.$1::"
}

if [ $stage -le 1 ]; then
  for x in "tr" "tt" "cv"; do [ ! -d $data_dir/$x ] && echo "$data_dir/$x not exists, exit ..." && exit 1; done
  data_dir=$(cd $data_dir && pwd)
  mkdir -p data/$dataset/{tr,cv,tt}
  for dir in tr cv tt; do
    # make mix.scp
    prepare_scp wav $data_dir/$dir/mix > data/$dataset/$dir/mix.scp
    # make spk{1,2}.scp
    prepare_scp wav $data_dir/$dir/s1 > data/$dataset/$dir/spk1.scp
    prepare_scp wav $data_dir/$dir/s2 > data/$dataset/$dir/spk2.scp
  done
  echo "$0: Prepare data done under data/$dataset"
fi

if [ $stage -le 2 ]; then
  ./scripts/train_ss.sh \
    --gpu $gpu --seed $seed \
    --epochs $epochs --batch-size $batch_size \
    --num-workers $num_workers \
    --eval-interval $eval_interval \
    --save-interval $save_interval \
    --prog-interval $prog_interval \
    --tensorboard $tensorboard \
    $dataset $exp
  echo "$0: Train model done under exp/$dataset/$exp"
fi

if [ $stage -le 3 ]; then
  # generate separation audio under exp/$dataset/$exp/bss
  ./bin/eval_bss \
    --checkpoint exp/$dataset/$exp \
    --sr 8000 \
    --device-id $gpu \
    data/$dataset/tt/mix.scp \
    exp/$dataset/$exp/bss
  # remix
  mkdir -p exp/$dataset/$exp/bss/spk{1,2}
  prepare_scp wav exp/$dataset/$exp/bss | awk -v \
    dir=exp/$dataset/$exp/bss/spk1 '{printf("sox %s %s/%s.wav remix 1\n", $2, dir, $1)}' | bash
  prepare_scp wav exp/$dataset/$exp/bss | awk -v \
    dir=exp/$dataset/$exp/bss/spk2 '{printf("sox %s %s/%s.wav remix 2\n", $2, dir, $1)}' | bash
  # compute si-snr
  prepare_scp wav exp/$dataset/$exp/bss/spk1 > exp/$dataset/$exp/bss/spk1.scp
  prepare_scp wav exp/$dataset/$exp/bss/spk2 > exp/$dataset/$exp/bss/spk2.scp
  ./bin/compute_sisnr.py data/$dataset/tt/spk1.scp,data/$dataset/tt/spk2.scp \
    exp/$dataset/$exp/bss/spk1.scp,exp/$dataset/$exp/bss/spk2.scp
fi
