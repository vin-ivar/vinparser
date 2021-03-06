#!/usr/bin/env bash
root="/home/ravishankar/personal_work_troja/vinparser"
if [[ -f "$root/results/err" ]]; then echo "error file exists: removing.. "; rm "$root/results/err"; fi
if [[ -f "$root/results/$1" ]]; then echo "$1 exists: removing.. "; rm "$root/results/$1"; fi
cd ~/personal_work_troja/vinparser
git checkout master
git pull
echo "$root/venv/bin/python $root/Runner.py --tag --parse --use_cuda --save $root/models/en_baseline_2.pt \
--train $root/data/en-ud-train.conllu.sem \
--dev $root/data/en-ud-dev.conllu.sem --test $root/data/en-ud-test.conllu.sem \
--embed $root/data/embeds/glove.6B.100d.txt --config $root/config.ini" > parse.sh
if [ ! -d "results" ]; then mkdir results; fi
qsub -e "$root/results/err" -o "$root/results/$1" -q gpu.q@dll[256] -l gpu=1,gpu_cc_min3.5=1,gpu_ram=4G parse.sh
