#!/usr/bin/env bash
root="/home/ravishankar/personal_work_troja/vinparser"
cd ~/personal_work_troja/vinparser
git checkout eval_codeswitch
git pull
echo "$root/venv/bin/python $root/Runner.py --parse --code_switch \
--use_cuda --train $root/data/codeswitch/en-hi/en-hi-train-append.conllu \
--dev $root/data/codeswitch/en-hi/en-hi-dev.conllu --test $root/data/codeswitch/en-hi/en-hi-test.conllu \
--lm $root/data/codeswitch/en-hi/lm_triples.txt \
--config $root/config.ini" > parse.sh
if [ ! -d "results" ]; then mkdir results; fi
qsub -e "$root/results/err" -o "$root/results/$1" -q gpu.q@dll[256] -l gpu=1,gpu_cc_min3.5=1,gpu_ram=2G parse.sh
