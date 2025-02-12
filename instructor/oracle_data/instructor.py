# -*- coding: utf-8 -*-
# @Author       : William
# @Project      : TextGAN-william
# @FileName     : instructor.py
# @Time         : Created at 2019-04-25
# @Blog         : http://zhiweil.ml/
# @Description  :
# Copyrights (C) 2018. All Rights Reserved.

import numpy as np
import os
import torch
import torch.nn as nn
import wandb

import config as cfg
from metrics.bleu import BLEU
from metrics.clas_acc import ACC
from metrics.ioc import IOC
from metrics.gpt_nll import GPTNLL
from metrics.nll import NLL
from metrics.ppl import PPL
from metrics.dummy import Dummy
from models.Oracle import Oracle
from utils.data_loader import GenDataIter
from utils.data_utils import create_multi_oracle
from utils.helpers import Signal, create_logger, create_oracle, get_fixed_temperature
from utils.text_process import write_tensor, tensor_to_tokens


class BasicInstructor:
    def __init__(self, opt):
        self.log = create_logger(
            __name__,
            silent=False,
            to_disk=True,
            log_file=cfg.log_filename
            if cfg.if_test
            else [cfg.log_filename, cfg.save_root + "log.txt"],
        )
        self.sig = Signal(cfg.signal_file)
        self.opt = opt

        # oracle, generator, discriminator
        self.oracle = Oracle(
            32, 32, cfg.vocab_size, cfg.max_seq_len, cfg.padding_idx, gpu=cfg.CUDA
        )
        self.oracle_list = [
            Oracle(
                32, 32, cfg.vocab_size, cfg.max_seq_len, cfg.padding_idx, gpu=cfg.CUDA
            )
            for _ in range(cfg.k_label)
        ]

        self.dis = None
        self.clas = None

        self.show_config()
        self.check_oracle()  # Create Oracle models if not exist
        # DataLoader
        self.oracle_samples = torch.load(
            cfg.oracle_samples_path.format(cfg.samples_num)
        )
        self.oracle_samples_list = [
            torch.load(cfg.multi_oracle_samples_path.format(i, cfg.samples_num))
            for i in range(cfg.k_label)
        ]

        self.oracle_data = GenDataIter(self.oracle_samples)
        self.oracle_data_list = [
            GenDataIter(self.oracle_samples_list[i]) for i in range(cfg.k_label)
        ]

        # Criterion
        self.mle_criterion = nn.NLLLoss()
        self.dis_criterion = nn.CrossEntropyLoss()

        # Metrics
        # nll_oracle, less-better, changes in range -0.1 - 0.6, moderate weight
        self.nll_oracle = NLL(
            "NLL_oracle", weight=-3, if_use=cfg.use_nll_oracle, gpu=cfg.CUDA
        )
        # nll-gen, less-better, changes in range 1.5 - 3 will have smaller wight (not in use)
        self.nll_gen = NLL("NLL_gen", weight=0, if_use=cfg.use_nll_gen, gpu=cfg.CUDA)
        # nll-div, more-better, changes in range 0.5 - 1.5 will have smaller wight (not in use)
        self.nll_div = NLL("NLL_div", weight=0, if_use=cfg.use_nll_div, gpu=cfg.CUDA)
        # self-bleu, less-better, changes in range 0.7 - 0.9, will have relatively high weight
        self.self_bleu = BLEU("Self-BLEU", weight=-3, gram=3, if_use=cfg.use_self_bleu)
        # IOC, less-better, changes in range 0.8 - 2.0, smaller weight
        self.ioc = IOC(weight=-0.3, if_use=cfg.use_ioc, real_text=self.oracle_data)
        # dummy, add constant value to overall score
        self.dummy = Dummy(weight=1, value=5, if_use=True)
        self.all_metrics = [
            self.nll_oracle,
            self.nll_gen,
            self.nll_div,
            self.self_bleu,
            self.ioc,
            self.dummy,
        ]

    def _run(self):
        print("Nothing to run in Basic Instructor!")
        pass

    def _test(self):
        pass

    def init_model(self):
        if cfg.oracle_pretrain:
            if not os.path.exists(cfg.oracle_state_dict_path):
                create_oracle()
            self.oracle.load_state_dict(
                torch.load(
                    cfg.oracle_state_dict_path,
                    map_location="cuda:{}".format(cfg.device),
                )
            )

        if cfg.dis_pretrain:
            self.log.info(
                "Load pretrained discriminator: {}".format(cfg.pretrained_dis_path)
            )
            self.dis.load_state_dict(
                torch.load(
                    cfg.pretrained_dis_path, map_location="cuda:{}".format(cfg.device)
                )
            )
        if cfg.gen_pretrain:
            self.log.info(
                "Load MLE pretrained generator gen: {}".format(cfg.pretrained_gen_path)
            )
            self.gen.load_state_dict(
                torch.load(
                    cfg.pretrained_gen_path, map_location="cuda:{}".format(cfg.device)
                )
            )

        if cfg.CUDA:
            self.oracle = self.oracle.cuda()
            self.gen = self.gen.cuda()
            self.dis = self.dis.cuda()

    def train_gen_epoch(self, model, data_loader, criterion, optimizer):
        total_loss = 0
        for i, data in enumerate(data_loader):
            inp, target = data["input"], data["target"]
            if cfg.CUDA:
                inp, target = inp.cuda(), target.cuda()

            hidden = model.init_hidden(data_loader.batch_size)
            pred = model.forward(inp, hidden)
            loss = criterion(pred, target.view(-1))
            self.optimize(optimizer, loss, model)
            total_loss += loss.item()
        return total_loss / len(data_loader)

    def train_dis_epoch(self, model, data_loader, criterion, optimizer):
        total_loss = 0
        total_acc = 0
        total_num = 0
        for i, data in enumerate(data_loader):
            inp, target = data["input"], data["target"]
            if cfg.CUDA:
                inp, target = inp.cuda(), target.cuda()

            pred = model.forward(inp)
            loss = criterion(pred, target)
            self.optimize(optimizer, loss, model)

            total_loss += loss.item()
            total_acc += torch.sum((pred.argmax(dim=-1) == target)).item()
            total_num += inp.size(0)

        total_loss /= len(data_loader)
        total_acc /= total_num
        return total_loss, total_acc

    @staticmethod
    def eval_dis(model, data_loader, criterion):
        total_loss = 0
        total_acc = 0
        total_num = 0
        with torch.no_grad():
            for i, data in enumerate(data_loader):
                inp, target = data["input"], data["target"]
                if cfg.CUDA:
                    inp, target = inp.cuda(), target.cuda()

                pred = model.forward(inp)
                loss = criterion(pred, target)
                total_loss += loss.item()
                total_acc += torch.sum((pred.argmax(dim=-1) == target)).item()
                total_num += inp.size(0)
            total_loss /= len(data_loader)
            total_acc /= total_num
        return total_loss, total_acc

    @staticmethod
    def optimize_multi(opts, losses):
        for i, (opt, loss) in enumerate(zip(opts, losses)):
            opt.zero_grad()
            loss.backward(retain_graph=True if i < len(opts) - 1 else False)
            opt.step()

    @staticmethod
    def optimize(opt, loss, model=None, retain_graph=False):
        opt.zero_grad()
        loss.backward(retain_graph=retain_graph)
        if model is not None:
            torch.nn.utils.clip_grad_norm_(model.parameters(), cfg.clip_norm)
        opt.step()

    def show_config(self):
        """Show parser parameters settings"""
        self.log.info(100 * "=")
        self.log.info("> training arguments:")
        for arg in vars(self.opt):
            self.log.info(">>> {0}: {1}".format(arg, getattr(self.opt, arg)))
        self.log.info(100 * "=")

    def sample_for_metrics(self):
        eval_samples = self.gen.sample(cfg.samples_num, 4 * cfg.batch_size)
        gen_data = GenDataIter(eval_samples)
        gen_tokens = tensor_to_tokens(eval_samples)
        gen_tokens_s = tensor_to_tokens(
            self.gen.sample(cfg.small_sample_num, 4 * cfg.batch_size)
        )
        return gen_data, gen_tokens, gen_tokens_s

    def sample_for_metrics_with_label(self, label_i):
        eval_samples = self.gen.sample(
            cfg.samples_num, 4 * cfg.batch_size, label_i=label_i
        )
        gen_data = GenDataIter(eval_samples)
        gen_tokens = tensor_to_tokens(eval_samples)
        gen_tokens_s = tensor_to_tokens(
            self.gen.sample(cfg.small_sample_num, 8 * cfg.batch_size, label_i=label_i)
        )
        return gen_data, gen_tokens, gen_tokens_s

    def cal_metrics(self, fmt_str=False):
        """
        Calculate metrics
        :param fmt_str: if return format string for logging
        """
        with torch.no_grad():
            # Prepare data for evaluation
            gen_data, gen_tokens, gen_tokens_s = self.sample_for_metrics()

            # Reset metrics
            self.nll_oracle.reset(self.oracle, gen_data.loader)
            self.nll_gen.reset(self.gen, self.oracle_data.loader)
            self.nll_div.reset(self.gen, gen_data.loader)
            self.self_bleu.reset(test_text=gen_tokens_s, real_text=gen_tokens)
            self.ioc.reset(test_text=gen_tokens)

        metrics = {metric.name: metric.get_score() for metric in self.all_metrics}
        metrics.update(
            {
                "Overal_score": sum(
                    metric.weight * metric.get_score() for metric in self.all_metrics
                )
            }
        )
        wandb.log(metrics)

        if fmt_str:
            return "\n" + "\n".join(
                [f"{name} = {score}" for name, score in metrics.items()]
            )
        return [metric.get_score() for metric in self.all_metrics]

    def cal_metrics_with_label(self, label_i, fmt_str=False):
        assert type(label_i) == int, "missing label"
        with torch.no_grad():
            # Prepare data for evaluation
            gen_data, gen_tokens, gen_tokens_s = self.sample_for_metrics_with_label()

            # Reset metrics
            self.nll_oracle.reset(self.oracle_list[label_i], gen_data.loader, label_i)
            self.nll_gen.reset(self.gen, self.oracle_data_list[label_i].loader, label_i)
            self.nll_div.reset(self.gen, gen_data.loader, label_i)
            self.self_bleu.reset(test_text=gen_tokens_s, real_text=gen_tokens)
            self.ioc.reset(test_text=gen_tokens)
            self.nll_oracle.reset(test_text=gen_tokens)

        metrics = {
            f"label {label_i}_{metric.name}": metric.get_score()
            for metric in self.all_metrics
        }
        metrics.update(
            {
                f"label {label_i} Overal_score": sum(
                    metric.weight * metric.get_score() for metric in self.all_metrics
                )
            }
        )
        wandb.log(metrics)

        if fmt_str:
            return "\n" + "\n".join(
                [f"{name} = {score}" for name, score in metrics.items()]
            )
        return metrics

    def comb_metrics(self, fmt_str=False):
        all_scores = [
            self.cal_metrics_with_label(label_i) for label_i in range(cfg.k_label)
        ]

        if fmt_str:
            return ", ".join(
                [
                    f"{name} = {[scores[name] for scores in all_scores]}"
                    for name in all_scores[0]
                ]
            )
        return [scores.values() for scores in all_scores]

    def _save(self, phase, epoch):
        """Save model state dict and generator's samples"""
        if phase != "ADV":
            torch.save(
                self.gen.state_dict(),
                cfg.save_model_root + "gen_{}_{:05d}.pt".format(phase, epoch),
            )
        save_sample_path = cfg.save_samples_root + "samples_{}_{:05d}.txt".format(
            phase, epoch
        )
        samples = self.gen.sample(cfg.batch_size, cfg.batch_size)
        write_tensor(save_sample_path, samples)

    def update_temperature(self, i, N):
        self.gen.temperature.data = torch.Tensor(
            [get_fixed_temperature(cfg.temperature, i, N, cfg.temp_adpt)]
        )
        if cfg.CUDA:
            self.gen.temperature.data = self.gen.temperature.data.cuda()

    def check_oracle(self):
        if not cfg.oracle_pretrain:
            create_oracle()
            create_multi_oracle(cfg.k_label)

        # General text generation Oracle model
        if (
            not os.path.exists(cfg.oracle_samples_path.format(cfg.samples_num))
            or not cfg.oracle_pretrain
        ):
            create_oracle()

        # Category text generation Oracle models
        for i in range(cfg.k_label):
            if not os.path.exists(
                cfg.multi_oracle_samples_path.format(i, cfg.samples_num)
            ):
                create_multi_oracle(cfg.k_label)
                break

        # Load Oracle state dict
        self.oracle.load_state_dict(
            torch.load(
                cfg.oracle_state_dict_path, map_location="cuda:{}".format(cfg.device)
            )
        )
        for i in range(cfg.k_label):
            oracle_path = cfg.multi_oracle_state_dict_path.format(i)
            self.oracle_list[i].load_state_dict(
                torch.load(oracle_path, map_location="cuda:{}".format(cfg.device))
            )
