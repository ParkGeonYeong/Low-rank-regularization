#!/bin/bash

seed=(1004 1003 1002 1001)


for s in ${seed[@]}; do
    echo "seed: $s"
    CUDA_VISIBLE_DEVICES=$1 python run.py --data celebA --target_attr makeup --bias_attr gender --mode_CL SimCLR \
        --lambda_offdiag 0. --batch_size 128 --simclr_epochs 20 --linear_iters 5000 \
        --data_dir /home/pky/research_new/dataset \
        --seed $s --finetune --lr_clf 0.0001 --lr_simclr 0.0001

    #CUDA_VISIBLE_DEVICES=$1 python run.py --data celebA --target_attr makeup --bias_attr gender --mode_CL SimCLR \
    #    --lambda_offdiag 0.01 --batch_size 128 --simclr_epochs 20 --linear_iters 5000 \
    #    --data_dir /home/pky/research_new/dataset \
    #    --seed $s --finetune --lr_clf 0.0001 --lr_simclr 0.0001

    #CUDA_VISIBLE_DEVICES=$1 python run.py --data celebA --target_attr makeup --bias_attr gender --mode_CL SimCLR \
    #    --lambda_offdiag 0. --batch_size 128 --simclr_epochs 20 --linear_iters 5000 \
    #    --data_dir /home/pky/research_new/dataset \
    #    --seed $s --finetune \
    #    --mode oversample --lambda_upweight 8 \
    #    --oversample_pth "expr/checkpoint/celebA_makeup_SimCLR_lambda_0.01_seed_$s/wrong_idx.pth" \
    #    --lr_clf 0.0001 --lr_simclr 0.0001 \
    #    --optimizer SGD --wd 0.1
done


#CUDA_VISIBLE_DEVICES=$1 python run.py --data celebA --target_attr makeup \
#    --lambda_offdiag 0. --batch_size 128 --simclr_epochs 20 --linear_iters 5000 \
#    --data_dir /home/pky/research_new/dataset \
#    --seed $2 \
#    --mode oversample --lambda_list 0. 0.01 0.02 0.03 0.04 0.05 --cutoff 0.68 --lambda_upweight 8
