#!/usr/bin/env bash

# wujian@2020

set -eu

nj=20
cmd="utils/run.pl"
dict=""
space=""
nbest=1
channel=-1
max_len=100
beam_size=16
normalized=true
lm=""
lm_weight=0

echo "$0 $@"

. ./utils/parse_options.sh || exit 1

[ $# -ne 4 ] && echo "Script format error: $0 <mdl-name> <exp-id> <tst-scp> <dec-dir>" && exit 1

mdl_id=$1
exp_id=$2

tst_scp=$3
dec_dir=$4

exp_dir=exp/$mdl_id/$exp_id
log_dir=$dec_dir/log && mkdir -p $log_dir

[ ! -f $tst_scp ] && echo "$0: missing test wave script: $tst_scp" && exit 0
[ ! -d $exp_dir ] && echo "$0: missing experiment directory: $exp_dir" && exit 0

wav_sp_scp=""
for n in $(seq $nj); do wav_sp_scp="$wav_sp_scp $log_dir/wav.$n.scp"; done

./utils/split_scp.pl $tst_scp $wav_sp_scp || exit 1

python=$(which python)
$cmd JOB=1:$nj $log_dir/decode.JOB.log \
    $python bin/decode.py \
    $log_dir/wav.JOB.scp \
    $log_dir/beam${beam_size}.JOB.decode \
    --beam-size $beam_size \
    --checkpoint $exp_dir \
    --device-id -1 \
    --channel $channel \
    --dict "$dict" \
    --lm "$lm" \
    --lm-weight $lm_weight \
    --space "$space" \
    --nbest $nbest \
    --dump-nbest $log_dir/beam${beam_size}.JOB.${nbest}best \
    --max-len $max_len \
    --normalized $normalized \
    --vectorized true

cat $log_dir/beam${beam_size}.*.decode | \
    sort -k1 > $dec_dir/beam${beam_size}.decode
cat $log_dir/beam${beam_size}.*.${nbest}best | \
    sort -k1 > $dec_dir/beam${beam_size}.${nbest}best

echo "$0 $@: Done"