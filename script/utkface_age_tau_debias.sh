#!/bin/bash

seed=(500 600 700 800)

for s in ${seed[@]}; do
    CUDA_VISIBLE_DEVICES=$1 python run.py --data UTKFace --bias_attr age --target_attr gender --mode_CL SimCLR \
        --seed $s --lambda_offdiag 0. --simclr_epochs 100 --linear_iters 3000 \
        --exp_name "tau0.07" \
        --data_dir /home/pky/research_new/dataset --temperature 0.07

    CUDA_VISIBLE_DEVICES=$1 python run.py --data UTKFace --bias_attr age --target_attr gender --mode_CL SimCLR \
        --seed $s --lambda_offdiag 0. --simclr_epochs 100 --linear_iters 3000 \
        --exp_name "tau5." \
        --data_dir /home/pky/research_new/dataset --temperature 5.

    CUDA_VISIBLE_DEVICES=$1 python run.py --data UTKFace --bias_attr age --target_attr gender --mode_CL SimCLR \
        --seed $s --lambda_offdiag 0. --simclr_epochs 100 --linear_iters 3000 \
        --mode oversample --lambda_upweight 6 --exp_name "tau0.07" \
        --data_dir /home/pky/research_new/dataset \
        --oversample_pth "expr/checkpoint/UTKFace_tau5._age_SimCLR_lambda_0.0_seed_$s/wrong_idx.pth"
done


#CUDA_VISIBLE_DEVICES=$1 python run.py --data celebA --target_attr makeup \
#    --lambda_offdiag 0. --batch_size 128 --simclr_epochs 20 --linear_iters 5000 \
#    --data_dir /home/pky/research_new/dataset \
#    --seed $2 \
#    --mode oversample --lambda_list 0. 0.01 0.02 0.03 0.04 0.05 --cutoff 0.68 --lambda_upweight 8
