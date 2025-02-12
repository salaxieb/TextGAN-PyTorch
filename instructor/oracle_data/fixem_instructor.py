import os
import random
from itertools import chain

from pathlib import Path
import numpy as np
import torch
from torch.utils.data import Dataset, DataLoader
from tqdm import tqdm, trange


import config as cfg
from instructor.oracle_data.instructor import BasicInstructor
from instructor.real_data.fixem_instructor import (
    FixemGANInstructor as RealDataFixemGANInstructor,
)
from utils.gan_loss import GANLoss
from utils.text_process import text_file_iterator
from utils.data_loader import DataSupplier, GANDataset
from utils.nn_helpers import create_noise, number_of_parameters
from utils.helpers import create_oracle
from metrics.nll import NLL
from utils.create_embeddings import EmbeddingsTrainer, load_embedding
from models.Oracle import Oracle
from models.generators.FixemGAN_G import Generator
from models.discriminators.FixemGAN_D import Discriminator

# afterwards:
# check target real/fake to be right (Uniform or const)
# random data portion generator - data supplier sample from randomint


class FixemGANInstructor(BasicInstructor, RealDataFixemGANInstructor):
    def __init__(self, opt):
        self.oracle = Oracle(
            32, 32, cfg.vocab_size, cfg.max_seq_len, cfg.padding_idx, gpu=cfg.CUDA
        )
        if cfg.oracle_pretrain:
            if not os.path.exists(cfg.oracle_state_dict_path):
                create_oracle()
            self.oracle.load_state_dict(
                torch.load(
                    cfg.oracle_state_dict_path,
                    map_location="cuda:{}".format(cfg.device),
                )
            )

        if cfg.CUDA:
            self.oracle = self.oracle.cuda()

        super().__init__(opt)

    def build_embedding(self):
        # train embedding on available dataset or oracle
        self.log.info(f"Didn't find embeddings in {cfg.pretrain_embedding_path}")
        self.log.info("Will train new one, it may take a while...")
        sources = [cfg.oracle_samples_path.format(cfg.w2v_samples_num)]
        EmbeddingsTrainer(sources, cfg.pretrain_embedding_path).make_embeddings()
