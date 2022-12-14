#!/bin/bash

timestamp=$(date +%s)

                        # --resume True \
                        # --resume_task 1 \
                        # --resume_time 20221201_1356 \
py3clean ./
CUDA_VISIBLE_DEVICES=2 python3 -B ../run.py \
                        --ILtype task \
                        --dataset cifar100 \
                        --split 10 \
                        --alpha 1. \
                        --beta 1. \
                        --gamma 0.5 \
                        --memory_size 1000 \
                        --rt 10. \
                        --num_head 4 \
                        --hidden_dim 512 \
                        # -everytest False \
                        # > ../ckpt/logs/$(date -d "today" +"%Y%m%d_%H%M").txt