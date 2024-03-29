import os
from os.path import join as ospj
import time
import datetime
from munch import Munch
import logging
import sys
import numpy as np

import torchvision
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.cuda.amp import GradScaler, autocast
from torch.utils.tensorboard import SummaryWriter
from tqdm import tqdm
from utils import accuracy, CheckpointIO, MultiDimAverageMeter

from models.build_models import build_model, num_classes, last_dim
from data_aug.data_loader import get_original_loader, get_val_loader, InputFetcher


class ERMSolver(nn.Module):
    def __init__(self, args):
        super().__init__()
        self.args = args
        self.device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
        self.num_classes = num_classes[args.data]
        self.attr_dims = [self.num_classes, self.num_classes]

        self.nets = build_model(args)
        # below setattrs are to make networks be children of Solver, e.g., for self.to(self.device)
        for name, module in self.nets.items():
            setattr(self, name, module)

        self.optims = Munch() # Used in pretraining
        for net in self.nets.keys():
            if args.optimizer == 'Adam':
                self.optims[net] = torch.optim.Adam(
                    self.nets[net].parameters(),
                    args.lr_ERM,
                    weight_decay=args.weight_decay
                )
            elif args.optimizer == 'SGD':
                self.optims[net] = torch.optim.SGD(
                    self.nets[net].parameters(),
                    args.lr_ERM,
                    momentum=0.9,
                    weight_decay=args.weight_decay
                )

        self.ckptios = [
            CheckpointIO(ospj(args.checkpoint_dir, '{:06d}_{}_nets.ckpt'), **self.nets),
        ]
        logging.basicConfig(filename=os.path.join(args.log_dir, 'training.log'),
                            level=logging.INFO)

        # BUILD LOADERS
        self.loaders = Munch(train=get_original_loader(args, simclr_aug=False))

        if self.args.lambda_upweight != 1 and self.args.oversample_pth is not None:
            pth = self.args.oversample_pth
            wrong_label = torch.load(pth)
            upweight = torch.ones_like(wrong_label)
            if self.args.finetune:
                indices = np.load(ospj(self.args.checkpoint_dir, f'subset_indices_{self.args.finetune_ratio}.npy'))
                for ind, _ in enumerate(upweight):
                    if ind not in indices:
                        upweight[ind] = 0

            print(f'Number of wrong/total samples: {wrong_label.sum()}/{upweight.sum()}. Finetuning: {self.args.finetune}')

            upweight[wrong_label == 1] = self.args.lambda_upweight
            upweight_loader = get_original_loader(self.args, sampling_weight=upweight, simclr_aug=False)
            self.loaders = Munch(train=upweight_loader)

        if args.finetune:
            self.loaders.train_finetune = get_original_loader(args, simclr_aug=False, finetune=True, finetune_ratio=args.finetune_ratio)

        if args.data != 'imagenet':
            self.loaders.val = get_val_loader(args, split='valid')
            self.loaders.test = get_val_loader(args, split='test')
        else:
            self.loaders.val = Munch(
                biased=get_val_loader(args, 'biased'),
                unbiased=get_val_loader(args, 'unbiased'),
                ImageNetA=get_val_loader(args, 'ImageNet-A')
            )
            self.loaders.test = get_val_loader(args, 'biased')

        self.scheduler = Munch()
        milestones = [int(args.ERM_epochs/3), int(args.ERM_epochs/3)]
        self.scheduler.classifier = torch.optim.lr_scheduler.MultiStepLR(
            self.optims[net], milestones=milestones, gamma=args.lr_decay_gamma
        )

        self.writer = SummaryWriter(args.log_dir)
        self.criterion = torch.nn.CrossEntropyLoss()
        self.gce = GeneralizedCELoss()
        self.normalize = nn.BatchNorm1d(last_dim[args.arch], affine=False)

        self.to(self.device)

    def _reset_grad(self):
        def _recursive_reset(optims_dict):
            for _, optim in optims_dict.items():
                if isinstance(optim, dict):
                    _recursive_reset(optim)
                else:
                    optim.zero_grad()
        return _recursive_reset(self.optims)

    def _save_checkpoint(self, step, token):
        for ckptio in self.ckptios:
            ckptio.save(step, token)

    def _load_checkpoint(self, step, token, which=None, return_fname=False):
        for ckptio in self.ckptios:
            ckptio.load(step, token, which, return_fname)

    def validation(self, fetcher):
        self.nets.encoder.eval()
        self.nets.classifier.eval()

        attrwise_acc_meter = MultiDimAverageMeter(self.attr_dims)

        total_correct, total_num = 0, 0

        for images, labels, bias, _ in tqdm(fetcher):
            label = labels.to(self.device)
            images = images.to(self.device)
            bias = bias.to(self.device)

            with torch.no_grad():
                aux = self.nets.encoder(images, simclr=False, penultimate=True)
                features_penul = aux['penultimate']
                logit = self.nets.classifier(features_penul)
                pred = logit.data.max(1, keepdim=True)[1].squeeze(1)
                correct = (pred == label).long()

                total_correct += correct.sum()
                total_num += correct.shape[0]

            attr = torch.cat((labels.view(-1,1).to(self.device), bias.view(-1,1).to(self.device)), dim=1)
            attrwise_acc_meter.add(correct.cpu(), attr.cpu())

        print(attrwise_acc_meter.cum.view(self.attr_dims[0], -1))
        print(attrwise_acc_meter.cnt.view(self.attr_dims[0], -1))

        total_acc = total_correct / float(total_num)
        accs = attrwise_acc_meter.get_mean()

        self.nets.encoder.train()
        self.nets.classifier.train()
        return total_acc, accs

    def report_validation(self, valid_attrwise_acc, valid_acc, step=0):
        eye_tsr = torch.eye(self.attr_dims[0]).long()
        valid_acc_align = valid_attrwise_acc[eye_tsr == 1].mean().item()
        valid_acc_conflict = valid_attrwise_acc[eye_tsr == 0].mean().item()

        all_acc = dict()
        for acc, key in zip([valid_acc, valid_acc_align, valid_acc_conflict],
                            ['Acc/total', 'Acc/align', 'Acc/conflict']):
            all_acc[key] = acc
            self.writer.add_scalar(key, acc, global_step=step)
        log = f"(Validation) Iteration [{step}], "
        log += ' '.join(['%s: [%.4f]' % (key, value) for key, value in all_acc.items()])
        logging.info(log)
        print(log)

    def validate_imagenet(self, val_loader, num_classes=9, num_clusters=9,
                          num_cluster_repeat=3, key=None):
        self.nets.encoder.eval()
        self.nets.classifier.eval()

        total = 0
        f_correct = 0
        num_correct = [np.zeros([num_classes, num_clusters]) for _ in range(num_cluster_repeat)]
        num_instance = [np.zeros([num_classes, num_clusters]) for _ in range(num_cluster_repeat)]

        with torch.no_grad():
            for images, labels, bias_labels, index in val_loader:

                images, labels = images.to(self.device), labels.to(self.device)
                for bias_label in bias_labels:
                    bias_label.to(self.device)

                aux = self.nets.encoder(images, simclr=False, penultimate=True)
                features_penul = aux['penultimate']
                output = self.nets.classifier(features_penul)

                batch_size = labels.size(0)
                total += batch_size

                if key == 'unbiased':
                    num_correct, num_instance = self.imagenet_unbiased_accuracy(
                        output.data, labels, bias_labels,
                        num_correct, num_instance, num_cluster_repeat)
                else:
                    f_correct += self.n_correct(output, labels)

        self.nets.encoder.train()
        self.nets.classifier.train()

        if key == 'unbiased':
            result = {'num_correct': np.array(num_correct),
                      'num_instance': np.array(num_instance)}
            np.save(ospj(self.args.log_dir, 'unbiased_acc_array.npy'), result)
            for k in range(num_cluster_repeat):
                x, y = [], []
                _num_correct, _num_instance = num_correct[k].flatten(), num_instance[k].flatten()
                for i in range(_num_correct.shape[0]):
                    __num_correct, __num_instance = _num_correct[i], _num_instance[i]
                    if __num_instance >= 10:
                        x.append(__num_instance)
                        y.append(__num_correct / __num_instance)
                f_correct += sum(y) / len(x)

            return f_correct / num_cluster_repeat
        else:
            return f_correct / total

    def imagenet_unbiased_accuracy(self, outputs, labels, cluster_labels,
                                   num_correct, num_instance,
                                   num_cluster_repeat=3):
        for j in range(num_cluster_repeat):
            for i in range(outputs.size(0)):
                output = outputs[i]
                label = labels[i]
                cluster_label = cluster_labels[j][i]

                _, pred = output.topk(1, 0, largest=True, sorted=True)
                correct = pred.eq(label).view(-1).float()

                num_correct[j][label][cluster_label] += correct.item()
                num_instance[j][label][cluster_label] += 1

        return num_correct, num_instance

    def n_correct(self, pred, labels):
        _, predicted = torch.max(pred.data, 1)
        n_correct = (predicted == labels).sum().item()
        return n_correct

    def off_diagonal(self, x):
        # return a flattened view of the off-diagonal elements of a square matrix
        n, m = x.shape
        assert n == m
        return x.flatten()[:-1].view(n - 1, n + 1)[:, 1:].flatten()

    def train(self):
        scaler = GradScaler(enabled=self.args.fp16_precision)
        imagenet_stats = {'biased': [], 'unbiased': [], 'ImageNetA': []}

        n_iter = 0
        logging.info(f"Start ERM training for {self.args.ERM_epochs} epochs.")

        if self.args.finetune:
            loader = self.loaders.train_finetune
        else:
            loader = self.loaders.train

        i = 0
        for epoch_counter in range(self.args.ERM_epochs):
            for images, labels, _, _ in tqdm(loader):
                images = images.to(self.args.device)
                labels = labels.to(self.args.device)

                #for ind, img in enumerate(images):
                #    torchvision.utils.save_image(img, f'recon/{i}iter_{labels[0]}_{labels[1]}.png', normalize=True)

                aux = self.nets.encoder(images, simclr=False, penultimate=True)
                features_penul = aux['penultimate']
                c = self.normalize(features_penul).T @ self.normalize(features_penul)
                c.div_(self.args.batch_size)
                loss_offdiag = self.off_diagonal(c).pow_(2).sum() / features_penul.size(1) ** 2

                logits = self.nets.classifier(features_penul)
                loss_ce = self.criterion(logits, labels)
                loss = loss_ce - self.args.lambda_offdiag * loss_offdiag
                #loss = self.gce(logits, labels).mean()

                self.optims.classifier.zero_grad()
                self.optims.encoder.zero_grad()

                scaler.scale(loss).backward()

                scaler.step(self.optims.classifier)
                scaler.step(self.optims.encoder)
                scaler.update()

                if n_iter % self.args.log_every_n_steps == 0:
                    top1 = accuracy(logits, labels, topk=(1, ))
                    self.writer.add_scalar('loss', loss, global_step=n_iter)
                    self.writer.add_scalar('loss/ce', loss_ce, global_step=n_iter)
                    self.writer.add_scalar('loss/rank_reg', loss_offdiag, global_step=n_iter)
                    self.writer.add_scalar('acc/top1', top1[0], global_step=n_iter)
                    self.writer.add_scalar('learning_rate', self.scheduler.classifier.get_lr()[0], global_step=n_iter)

                n_iter += 1

                #self.scheduler.classifier.step()

            msg = f"Epoch: {epoch_counter}\tLR: {self.scheduler.classifier.get_lr()[0]}\tLoss: {loss}\tTop1 accuracy: {top1[0]}"
            logging.info(msg)
            print(msg)

            if self.args.data != 'imagenet':
                total_acc, valid_attrwise_acc = self.validation(self.loaders.val)
                self.report_validation(valid_attrwise_acc, total_acc, n_iter+1)
                msg = f"Iter: {n_iter+1}\tLoss: {loss}\tAccuracy: {total_acc}"
                logging.info(msg)
                print(msg)
            else:
                msg = f"Iter: {n_iter+1}\t"
                for key, val_loader in self.loaders.val.items():
                    val_acc = self.validate_imagenet(val_loader, key=key)
                    imagenet_stats[key].append(val_acc)
                    msg += f"{key}: {val_acc}\t"
                logging.info(msg)
                print(msg)

            if self.args.oversample_pth is None:
                self.save_score_idx(loader=get_original_loader(self.args, simclr_aug=False), epoch=epoch_counter)


        logging.info("Training has finished.")
        # save model checkpoints
        self._save_checkpoint(step=epoch_counter+1, token='biased_ERM')

        logging.info(f"Model checkpoint and metadata has been saved at {self.args.log_dir}.")

    def save_score_idx(self, loader, epoch=None):
        self.nets.encoder.eval()
        self.nets.classifier.eval()
        dataset = get_original_loader(self.args, return_dataset=True, simclr_aug=False)
        num_data = len(dataset)

        iterator = enumerate(loader)
        score_idx = torch.zeros(num_data).to(self.device)
        wrong_idx = torch.zeros(num_data).to(self.device)
        debias_idx = torch.zeros(num_data).to(self.device)
        total_num = 0

        for _, (images, labels, bias_labels, idx) in iterator:
            idx = idx.to(self.device)
            labels = labels.to(self.device)
            bias_labels = bias_labels.to(self.device)
            images= images.to(self.device)

            with torch.no_grad():
                aux = self.nets.encoder(images, freeze=True, penultimate=True)
                features_penul = aux['penultimate']
                logits = self.nets.classifier(features_penul)

                # bias score
                bias_prob = nn.Softmax()(logits)[torch.arange(logits.size(0)), labels]
                bias_score = 1 - bias_prob

                # wrong
                pred = logits.data.max(1, keepdim=True)[1].squeeze(1)
                wrong = (pred != labels).long()

                # true label
                debiased = (labels != bias_labels).float()

                for i, v in enumerate(idx):
                    score_idx[v] = bias_score[i]
                    debias_idx[v] = debiased[i]
                    wrong_idx[v] = wrong[i]

            total_num += labels.shape[0]

        if not self.args.finetune: assert total_num == len(score_idx)
        print(f'Average bias score: {score_idx.mean()}')

        self.nets.encoder.train()
        self.nets.classifier.train()
        score_idx_path = lambda x: ospj(self.args.checkpoint_dir, f'score_idx{x}.pth')
        wrong_idx_path = lambda x: ospj(self.args.checkpoint_dir, f'wrong_idx{x}.pth')
        debias_idx_path = lambda x: ospj(self.args.checkpoint_dir, f'debias_idx{x}.pth')

        if self.args.data == 'stl10mnist':
            score_idx_path = score_idx_path(f'_{self.args.bias_ratio}')
            wrong_idx_path = wrong_idx_path(f'_{self.args.bias_ratio}')
            debias_idx_path = debias_idx_path(f'_{self.args.bias_ratio}')
        elif epoch is not None:
            score_idx_path = score_idx_path(epoch)
            wrong_idx_path = wrong_idx_path(epoch)
            debias_idx_path = debias_idx_path(epoch)

        torch.save(score_idx, score_idx_path)
        torch.save(wrong_idx, wrong_idx_path)
        torch.save(debias_idx, debias_idx_path)
        print(f'Saved bias score in {score_idx_path}')
        self.pseudo_label_precision_recall(wrong_idx, debias_idx)

    def pseudo_label_precision_recall(self, wrong_label, debias_label):
        if self.args.data == 'celebA':
            debias_label = 1 - debias_label

        print(torch.sum(wrong_label))
        print(torch.sum(debias_label))

        spur_precision = torch.sum(
                (wrong_label == 1) & (debias_label == 1)
            ) / torch.sum(wrong_label)
        premsg = f"Spurious precision: {spur_precision}"
        print(premsg)
        logging.info(premsg)

        spur_recall = torch.sum(
                (wrong_label == 1) & (debias_label == 1)
            ) / torch.sum(debias_label)
        recmsg = f"Spurious recall: {spur_recall}"
        print(recmsg)
        logging.info(recmsg)

    def evaluate(self):
        fetcher_val = self.loaders.test
        self._load_checkpoint(self.args.ERM_epochs, 'biased_ERM')
        total_acc, valid_attrwise_acc = self.validation(fetcher_val)
        self.report_validation(valid_attrwise_acc, total_acc, 0)


class GeneralizedCELoss(nn.Module):

    def __init__(self, q=0.7):
        super(GeneralizedCELoss, self).__init__()
        self.q = q

    def forward(self, logits, targets):
        p = F.softmax(logits, dim=1)
        if np.isnan(p.mean().item()):
            raise NameError('GCE_p')
        Yg = torch.gather(p, 1, torch.unsqueeze(targets, 1))
        # modify gradient of cross entropy
        loss_weight = (Yg.squeeze().detach()**self.q)*self.q
        if np.isnan(Yg.mean().item()):
            raise NameError('GCE_Yg')

        loss = F.cross_entropy(logits, targets, reduction='none') * loss_weight

        return loss
