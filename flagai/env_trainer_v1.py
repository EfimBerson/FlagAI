# Copyright © 2022 BAAI. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License")
# Arguments for training
try:
    import deepspeed.utils
    import deepspeed
except:
    pass
try:
    from flagai import mpu
except Exception:
    pass

try:
    import bmtrain as bmt
except:
    pass

try:
    import wandb
except ImportError:
    wandb = None

import torch
import argparse
import os
import random
import math
import numpy as np
import torch.distributed as dist
from flagai.logger import log_dist
from torch.utils.tensorboard import SummaryWriter
from flagai.utils import load_checkpoint, save_checkpoint, load_optim, load_rng
from flagai.schedulers import AnnealingLR
from flagai.optimizers import get_optimizer, get_optimizer_param_groups
from flagai.fp16 import FP16_Module
from flagai.utils import Timers
from flagai.launch import launch_dist
from torch.nn.parallel import DistributedDataParallel as DDP
from flagai.fp16 import DynamicLossScaler

# TODO
# torch.autograd.set_detect_anomaly(True)

"""
The Trainer class, to easily train a pytorh model on a new task.
"""
def save_best(best_score, eval_dict):
    return best_score if best_score < eval_dict['loss'] else eval_dict['loss']

def get_args_list(env_args):
    not_need_to_launch_args = ["not_call_launch", "local_rank", "master_port", "master_ip", "hostfile", "num_gpus", "num_nodes", "node_rank"]
    args_list = []
    args = dir(env_args)
    for arg in args:
        if not arg.startswith("__") and not arg.startswith("_") and arg not in not_need_to_launch_args:
            args_list.append(f"--{arg}")
            args_list.append(str(getattr(env_args, arg)))

    print(f"args list is {args_list}")
    return args_list

class EnvTrainer():
    def __init__(self,
                 env_args,
    ):
        self.timers = Timers()
        self.env_type = env_args.env_type
        if self.env_type not in set(
            ["deepspeed", 'pytorch', 'pytorchDDP', 'deepspeed+mpu', 'bmtrain']):
            raise Exception("Not supported env_type!!!!")
        os.environ["ENV_TYPE"] = env_args.env_type
        self.experiment_name = env_args.experiment_name
        self.model_name = env_args.model_name
        self.batch_size = env_args.batch_size
        self.gradient_accumulation_steps = env_args.gradient_accumulation_steps
        self.lr = env_args.lr
        self.warmup_start_lr = env_args.warmup_start_lr
        self.weight_decay = env_args.weight_decay
        self.eps = env_args.eps
        self.epochs = env_args.epochs
        self.clip_grad = env_args.clip_grad
        self.seed = env_args.seed
        self.fp16 = env_args.fp16
        self.warm_up = env_args.warm_up
        self.warm_up_iters = env_args.warm_up_iters
        self.skip_iters = env_args.skip_iters
        self.adam_beta1 = env_args.adam_beta1
        self.adam_beta2 = env_args.adam_beta2

        self.log_interval = env_args.log_interval
        self.eval_interval = env_args.eval_interval

        # model checkpointing
        self.save_dir = env_args.save_dir
        self.save_interval = env_args.save_interval
        self.save_optim = env_args.save_optim
        self.save_rng = env_args.save_rng
        self.save_best = save_best
        self.load_dir = env_args.load_dir
        self.load_type = env_args.load_type
        self.load_optim = env_args.load_optim
        self.load_rng = env_args.load_rng
        self.tb_writer = None
        if env_args.tensorboard:
            self.tb_writer = SummaryWriter(
                os.path.join(env_args.tensorboard_dir, env_args.experiment_name))

        # distribute settings
        self.pytorch_device = env_args.pytorch_device
        self.checkpoint_activations = env_args.checkpoint_activations
        self.deepspeed_activation_checkpointing = env_args.deepspeed_activation_checkpointing
        self.num_checkpoints = env_args.num_checkpoints
        self.env_type = env_args.env_type
        self.not_call_launch = env_args.not_call_launch
        self.deepspeed_config = env_args.deepspeed_config
        self.model_parallel_size = env_args.model_parallel_size
        self.num_nodes = env_args.num_nodes
        self.num_gpus = env_args.num_gpus
        self.master_ip = env_args.master_ip
        self.master_port = env_args.master_port
        self.hostfile = env_args.hostfile
        self.training_script = env_args.training_script

        # wandb
        self.wandb = env_args.wandb
        self.wandb_dir = env_args.wandb_dir
        self.wandb_key = env_args.wandb_key

        # if model already_fp16, OPT 1.3B
        self.already_fp16 = env_args.already_fp16

        self.resume_dataset = env_args.resume_dataset
        self.shuffle_dataset = env_args.shuffle_dataset

        # bmt
        self.bmt_cpu_offload = env_args.bmt_cpu_offload
        self.bmt_lr_decay_style = env_args.bmt_lr_decay_style
        self.bmt_loss_scale = env_args.bmt_loss_scale
        self.bmt_loss_scale_steps = env_args.bmt_loss_scale_steps

        # lora
        self.adapter_save = env_args.lora

        self.warmup_start_lr = env_args.warmup_start_lr

        if self.env_type != 'pytorch':
            training_paras = get_args_list(env_args)
            self.rank = int(os.environ.get('RANK', 0))
            self.world_size = int(os.environ.get('WORLD_SIZE', 1))
            self.local_rank = env_args.local_rank
            log_dist("not_call_launch: {}".format(self.not_call_launch))
            # Implement for AutoLaunch
            # >>> python train.py # will call get_dist_args()
            # `--not_call_launch` is default 'False'
            # So, if `env_type` is `pytorch`, the `Trainer` will not call lanch_dist()
            # Otherwise, the lanch_dist() is called to launch 'train.py' with `--not_call_launch`
            if not self.not_call_launch:
                launch_dist(launcher='distributed_deepspeed' if 'deepspeed'
                            in self.env_type else 'distributed_torch',
                            num_nodes=self.num_nodes,
                            gpus_per_node=self.num_gpus,
                            master_addr=self.master_ip,
                            master_port=self.master_port,
                            hostfile=self.hostfile,
                            training_script=self.training_script,
                            training_paras=training_paras)
                os._exit(1)
            self.initialize_distributed()

    def set_seed(self, seed=1234):
        """Set random seed for reproducability."""
        if seed is not None and seed > 0:
            random.seed(seed)
            np.random.seed(seed)
            torch.manual_seed(seed)
            if self.env_type == 'deepspeed+mpu':
                mpu.model_parallel_cuda_manual_seed(seed)

    def initialize_distributed(self):
        """Initialize torch.distributed."""
        if self.env_type == 'pytorch':
            log_dist('No need to initialize')
            return
        if self.env_type in ['deepspeed', 'deepspeed+mpu', 'pytorchDDP', 'bmtrain']:
            torch.backends.cudnn.enabled = False
            # Manually set the device ids.
            device = self.rank % torch.cuda.device_count()
            if self.local_rank is not None:
                device = self.local_rank
            torch.cuda.set_device(device)
            # Call the init process
            init_method = 'tcp://'
            self.master_ip = os.getenv('MASTER_ADDR', 'localhost')
            self.master_port = os.getenv('MASTER_PORT', '6000')

            init_method += self.master_ip + ':' + self.master_port
            log_dist(
                "init method {}, rank {}, device {}, local_rank {}.".format(
                    init_method, self.rank, device, self.local_rank))
            if self.env_type == 'bmtrain':
                # self.get_env_args()
                bmt.init_distributed(
                    seed=self.seed,
                    init_method=init_method)
            else:
                torch.distributed.init_process_group(
                    backend='nccl',  # gloo
                    world_size=self.world_size,
                    rank=self.rank,
                    init_method=init_method)
        # Set the model-parallel / data-parallel communicators.
        if self.env_type == 'deepspeed+mpu':
            os.environ["MODEL_PARALLEL_SIZE"] = str(self.model_parallel_size)
            try:
                mpu.initialize_model_parallel(self.model_parallel_size)
                if 'deepspeed' in self.env_type and self.deepspeed_activation_checkpointing:
                    deepspeed.checkpointing.configure(
                        mpu,
                        deepspeed_config=self.deepspeed_config,
                        num_checkpoints=self.num_checkpoints)
                    mpu.checkpoint = deepspeed.checkpointing.checkpoint
                    mpu.get_cuda_rng_tracker = deepspeed.checkpointing.get_cuda_rng_tracker
                    mpu.model_parallel_cuda_manual_seed = deepspeed.checkpointing.model_parallel_cuda_manual_seed
            except Exception as e:
                log_dist(e)
                log_dist("No mpu is installed! No model parallel is used")
            log_dist("initialize eviroments succesed")

        #if self.env_type != 'bmtrain':
        self.set_seed(self.seed)

        # wandb
        if self.wandb and wandb is not None and self.rank == 0:
            wandb.login(key=self.wandb_key)
            wandb.init(project=self.experiment_name, dir=self.wandb_dir)

    def get_dataloader(self, dataset, collate_fn, shuffle=False, rank_split=False, drop_last=False):
        """ initilize the dataloader"""
        if dataset is None:
            return None
        if self.env_type == 'pytorch':
            return torch.utils.data.DataLoader(dataset,
                                               batch_size=self.batch_size,
                                               collate_fn=collate_fn,
                                               num_workers=4,
                                               prefetch_factor=4,
                                               pin_memory=True,
                                               drop_last=drop_last,
                                               shuffle=shuffle)
        else:
            if self.env_type == 'deepspeed+mpu':
                rank = mpu.get_model_parallel_src_rank()
                data_rank = mpu.get_data_parallel_rank()
                log_dist("*"*80)
                log_dist(f"local rank {self.rank} src rank  {rank} data rank {data_rank}")
                log_dist("*"*80)
                sampler = torch.utils.data.distributed.DistributedSampler(
                    dataset,
                    num_replicas=self.world_size//self.model_parallel_size,
                    rank=data_rank,
                    shuffle=shuffle)
            elif self.env_type == 'bmtrain':
                # TODO
                if rank_split:
                    return torch.utils.data.DataLoader(dataset,
                                                       batch_size=self.batch_size,
                                                       collate_fn=collate_fn,
                                                       num_workers=4,
                                                       prefetch_factor=4,
                                                       pin_memory=True,
                                                       drop_last=drop_last,
                                                       shuffle=shuffle)
                else:
                    num_replicas = self.world_size
                    rank = self.rank
                    sampler = torch.utils.data.distributed.DistributedSampler(
                        dataset,
                        num_replicas=num_replicas,
                        rank=rank,
                        shuffle=shuffle)
            else:
                num_replicas = self.world_size
                rank = self.rank
                sampler = torch.utils.data.distributed.DistributedSampler(
                    dataset, rank=rank, shuffle=shuffle)
            return torch.utils.data.DataLoader(dataset,
                                               batch_size=self.batch_size,
                                               sampler=sampler,
                                               num_workers=4,
                                               drop_last=drop_last,
                                               pin_memory=False,
                                               prefetch_factor=4,
                                               collate_fn=collate_fn)

    def pre_train(self, model=None, find_unused_parameters=True):
        # TODO
        self.model = model

        if self.load_dir:
            log_dist("loading checkpoints form {}".format(self.load_dir))
            self.sd = load_checkpoint(self.model,
                                      load_dir=self.load_dir,
                                      load_type=self.load_type)
        # Turn on training mode which enables dropout.
        self.model.train()

        if self.fp16 and self.env_type == 'pytorchDDP':
            log_dist(
                "Warning: The pytorchDDP plus FP16 may not working togather!!!"
            )

        # TODO
        if self.fp16 and not self.already_fp16:
            self.model.half()
        if self.checkpoint_activations:
            self.model.config[
                'checkpoint_activations'] = self.checkpoint_activations

        if self.env_type == 'pytorchDDP':
            self.model.to(torch.device('cuda', self.local_rank))
            self.model = DDP(self.model,
                             device_ids=[self.local_rank],
                             find_unused_parameters=find_unused_parameters)

        elif self.env_type == 'pytorch':
            self.model.to(self.pytorch_device)
        elif self.env_type == 'bmtrain':
            # print('*'*20, 'self.model', model, __file__)
            self.model = bmt.BMTrainModelWrapper(self.model)
            if hasattr(self.model, "pre_train_hook"):
                self.model.pre_train_hook()
            # print('*'*20, 'BMTrainModelWrapper model', self.model, __file__)
        else:
            self.model.cuda(torch.device('cuda', self.local_rank))
        
        # TODO
        if self.fp16 and self.env_type != 'bmtrain':
            self.model = FP16_Module(self.model)

    def do_train(self,
                 optimizer=None,
                 lr_scheduler=None,
                 train_dataset=None,
                 valid_dataset=None,
                 metric_methods=[],
                 collate_fn=None,
                 find_unused_parameters=True,
                 rank_split=False,
                 tokenizer=None):
        # TODO
        self.tokenizer = tokenizer

        if not isinstance(train_dataset, torch.utils.data.DataLoader):
            train_dataloader = self.get_dataloader(train_dataset, collate_fn,
                                                   self.shuffle_dataset, rank_split=rank_split,
                                                   drop_last=True)
        else:
            train_dataloader = train_dataset

        if not isinstance(valid_dataset, torch.utils.data.DataLoader):
            valid_dataloader = self.get_dataloader(valid_dataset, collate_fn,
                                                   False, drop_last=True)
        else:
            valid_dataloader = valid_dataset

        param_groups = get_optimizer_param_groups(self.model)

        if hasattr(param_groups[0], 'params'):
            # for T5 Model
            param_groups = param_groups[0]['params']

        self.optimizer = optimizer
        if self.optimizer is None and 'deepspeed' not in self.env_type and self.epochs > 0:
            if self.env_type == 'bmtrain':
                if self.fp16:
                    if self.bmt_cpu_offload:
                        self.optimizer = bmt.optim.AdamOffloadOptimizer(param_groups, 
                                                                        weight_decay=self.weight_decay,
                                                                        betas=(self.adam_beta1, self.adam_beta2),
                                                                        lr=self.lr,
                                                                        eps=self.eps)
                    else:
                        self.optimizer = bmt.optim.AdamOptimizer(param_groups, 
                                                                 weight_decay=self.weight_decay,
                                                                 betas=(self.adam_beta1, self.adam_beta2),
                                                                 lr=self.lr,
                                                                 eps=self.eps)
                else:
                    self.optimizer = get_optimizer(
                        param_groups=param_groups,
                        lr=self.lr,
                        weight_decay=self.weight_decay,
                        adam_beta1=self.adam_beta1,
                        adam_beta2=self.adam_beta2,
                        cpu_optimizer=False,
                        cpu_torch_adam=False,
                        fp16=self.fp16,
                        optimizer='adam')  # if not self.fp16 else 'adafactor')
            else:
                self.optimizer = get_optimizer(
                    param_groups=param_groups,
                    lr=self.lr,
                    adam_beta1=self.adam_beta1,
                    adam_beta2=self.adam_beta2,
                    weight_decay=self.weight_decay,
                    cpu_optimizer=False,
                    cpu_torch_adam=False,
                    fp16=self.fp16,
                    optimizer='adam')  # if not self.fp16 else 'adafactor')

        if  self.env_type == 'bmtrain':
            bmt.synchronize()

        self.model.train()

        if 'deepspeed' in self.env_type:
            # initialize the deepspeed
            model, self.optimizer, _, lr_scheduler = deepspeed.initialize(
                model=self.model,
                # if huggingface t5: param_groups[0]['params']
                model_parameters=param_groups,
                optimizer=self.optimizer,
                lr_scheduler=lr_scheduler,
                mpu=mpu if self.env_type == 'deepspeed+mpu' else None,
                config=self.deepspeed_config,
                dist_init_required=True)
            self.model = model

        self.total_iter = int(self.epochs * len(train_dataloader))
        if lr_scheduler == None and self.optimizer != None and (self.warm_up > 0 or self.warm_up_iters > 0) and 'deepspeed' not in self.env_type and self.epochs > 0:
            num_iters = self.total_iter
            if self.warm_up_iters > 0:
                warmup_iter = self.warm_up_iters
            else:
                warmup_iter = int(self.warm_up * self.total_iter)

            if self.env_type == 'bmtrain':
                ## lr_scheduler.step with optim_manager.step
                ## lr_scheduler = bmt.lr_scheduler.Noam(
                ## lr_scheduler = bmt.lr_scheduler.Cosine(
                if self.bmt_lr_decay_style == 'linear':
                    lr_scheduler = bmt.lr_scheduler.Linear(
                        self.optimizer,
                        start_lr=self.lr, 
                        warmup_iter=warmup_iter,
                        end_iter=num_iters)
                else:
                    from flagai.schedulers import Cosine10PP
                    lr_scheduler = Cosine10PP(
                        self.optimizer,
                        start_lr=self.lr, 
                        warmup_iter=warmup_iter,
                        end_iter=num_iters,
                        warmup_start_lr=self.warmup_start_lr)
            else:
                lr_scheduler = AnnealingLR(
                    self.optimizer,
                    start_lr=self.lr,
                    warmup_iter=int(self.warm_up * self.epochs *
                                    len(train_dataloader)),
                    decay_style='linear',
                    num_iters=self.epochs * len(train_dataloader))

        if self.load_optim:
            load_optim(self.optimizer, lr_scheduler, self.sd)
        if self.load_rng:
            load_rng(self.sd)

        ## Needed global optim_manager
        if self.env_type == 'bmtrain':
            if self.fp16:
                loss_scale = self.bmt_loss_scale
                loss_scale_steps = self.bmt_loss_scale_steps
            else:
                loss_scale = None
            optim_manager = bmt.optim.OptimManager(loss_scale=loss_scale,
                                                   loss_scale_steps=loss_scale_steps)
            optim_manager.add_optimizer(self.optimizer, lr_scheduler)

        # Tracking loss.
        total_lm_loss = 0.0
        total_grad_norm = 0.0
        self.iteration = 0
        self.accumulate_count = 0
        best_iteration = 0
        best_loss = float('inf')
        # For each remaining epoch
        self.timers('interval time').start()
        # self.eval_metrics = eval_metrics
        # self.do_eval = valid_dataset!=None
        self.metric_methods = metric_methods
        best_score = float('inf')
        if len(self.metric_methods) > 0:
            best_score = -best_score

        '''
        # Temporary Usage
        save_checkpoint(self.iteration+1,
                        best_iteration+1,
                        self.model,
                        self.optimizer,
                        lr_scheduler,
                        save_optim=self.save_optim,
                        save_dir=self.save_dir,
                        save_rng=self.save_rng,
                        iteration_in_epoch=0)
        import sys
        sys.exit(0)
        '''
        print("save directory is: ", self.save_dir)
        in_first_epoch = True
        for epoch in range(self.epochs):
            if self.env_type != 'pytorch':
                set_epoch = getattr(train_dataloader.sampler, "set_epoch", None)
                if set_epoch is not None:
                    train_dataloader.sampler.set_epoch(epoch + self.world_size)

            # For all the batches in the dataset.
            for iteration_, batch in enumerate(train_dataloader):

                # skip batches when resume_dataset=True
                iteration_in_epoch = 0
                if in_first_epoch and self.resume_dataset and 'iteration_in_epoch' in self.sd:
                    iteration_in_epoch = self.sd['iteration_in_epoch']
                    if iteration_ <= iteration_in_epoch:
                        if iteration_%1000==0:
                            log_dist(f"Resume skip iteration={iteration_+1}", [0])
                        self.iteration += 1
                        continue
                elif in_first_epoch and iteration_ < self.skip_iters:
                    if iteration_%1000==0:
                        log_dist(f"Resume skip iteration={iteration_+1}", [0])
                    self.iteration += 1
                    continue

                if 'input_ids' in batch and iteration_ % 1000 == 0:
                    log_dist("Batch Input_ids Size %s"%str(batch['input_ids'].size()), [0])

                # Train for one step.
                if 'pytorch' != self.env_type:
                    batch = {
                        x: batch[x].to(torch.device('cuda', self.local_rank))
                        for x in batch if x not in ['uid', 'meta', 'mode']
                    }
                elif 'pytorch' == self.env_type:
                    batch = {
                        x: batch[x].to(torch.device(self.pytorch_device))
                        for x in batch if x not in ['uid', 'meta', 'mode']
                    }

                cached = None
                if self.env_type == 'pytorchDDP':
                    lm_loss, _ = self.train_step_pytorchDDP(
                        batch, self.model, self.optimizer, lr_scheduler)
                    dist.barrier()

                elif self.env_type == 'pytorch':
                    lm_loss, _ = self.train_step_pytorch(
                        batch, self.model, self.optimizer, lr_scheduler)
                
                elif self.env_type == 'bmtrain':
                    lm_loss, cached = self.train_step_bmtrain(
                        batch, self.model, optim_manager)

                else:
                    lm_loss, _ = self.train_step_deepspeed(batch,
                                                           self.model,
                                                           self.optimizer,
                                                           lr_scheduler,
                                                           single_step=True)
                    dist.barrier()

                if lm_loss is not None:
                    if not isinstance(lm_loss, float):
                        total_lm_loss += lm_loss.data.detach().item()
                    else:
                        total_lm_loss += lm_loss

                if 'bmtrain' in self.env_type and cached is not None and 'grad_norm' in cached:
                    total_grad_norm += cached['grad_norm']

                # Logging.
                if (self.iteration + 1) % self.log_interval == 0:
                    if self.optimizer is not None:
                        learning_rate = self.optimizer.param_groups[0]['lr']
                    else:
                        learning_rate = self.model.optimizer.param_groups[0]['lr']
                    if self.env_type == 'bmtrain':
                        avg_lm_loss = total_lm_loss / self.log_interval
                    else:
                        avg_lm_loss = total_lm_loss / self.log_interval
                    elapsed_time = self.timers('interval time').elapsed()

                    # TODO
                    #avg_lm_loss *= self.gradient_accumulation_steps
                    avg_grad_norm = total_grad_norm / self.log_interval
                    self.report_iteration_metrics(
                        self.optimizer, learning_rate, avg_lm_loss,
                        elapsed_time * 1000.0 / self.log_interval,
                        self.iteration + 1,
                        self.epochs * len(train_dataloader),
                        optimizer_manager=optim_manager if self.env_type == 'bmtrain' else None,
                        grad_norm=avg_grad_norm)

                    if self.tb_writer:
                        self.tb_writer.add_scalar('train/loss', avg_lm_loss,
                                                  self.iteration + 1)
                        self.tb_writer.add_scalar('lr', learning_rate,
                                                  self.iteration + 1)
                    total_lm_loss = 0.0
                    total_grad_norm = 0.0

                # Evaluation #todo add train_args
                if self.eval_interval and (
                        self.iteration + 1
                ) % self.eval_interval == 0 and valid_dataloader is not None:
                    self.timers.log(['forward', 'backward', 'optimizer'],
                                    normalizer=self.eval_interval)
                    prefix = 'epoch {}'.format(epoch)
                    eval_dict = self.evaluate_and_print_results(
                        prefix=prefix,
                        data_loader=valid_dataloader,
                        model=self.model,
                        forward_step_func=self.forward_step,
                        verbose=False)
                    if eval_dict is not None:
                        eval_loss = eval_dict.get("loss", 0.0)
                        if self.tb_writer:
                            self.tb_writer.add_scalar('eval/loss', eval_loss,
                                                      self.iteration + 1)
                        for i in range(len(self.metric_methods)):
                            name = self.metric_methods[i][0]
                            score = eval_dict.get(name, 0)
                            if self.tb_writer:
                                self.tb_writer.add_scalar(
                                    'eval_metrics/%s' % (name), score,
                                    self.iteration + 1)

                        # wandb
                        if self.wandb and wandb is not None and self.rank == 0:
                            metrics = dict()
                            if "loss" in eval_dict:
                                metrics['dev loss'] = eval_dict.get("loss", 0.0)
                            if "perplexity" in eval_dict:
                                metrics['dev perplexity'] = eval_dict.get("perplexity", 0.0)
                            if len(metrics) > 0:
                                wandb.log(metrics, step=self.iteration+1)
                                
                        if self.save_best is not None and self.save_best(best_score, eval_dict) != best_score:
                            best_score = self.save_best(best_score, eval_dict)
                            log_dist("saving best model with score {:.4f}".format(best_score))
                            if self.adapter_save:
                                self.model.save_pretrained(save_directory=self.save_dir)
                            else:
                                save_checkpoint(self.iteration+1,
                                                best_iteration+1,
                                                self.model,
                                                self.optimizer,
                                                lr_scheduler,
                                                save_optim=self.save_optim,
                                                save_dir=self.save_dir,
                                                save_rng=self.save_rng,
                                                iteration_in_epoch=iteration_)
                if self.save_dir and (self.iteration + 1) % self.save_interval == 0 and \
                        self.iteration != best_iteration:
                    if self.adapter_save:
                        self.model.save_pretrained(save_directory=self.save_dir)
                    else:
                        save_checkpoint(self.iteration+1,
                                        best_iteration+1,
                                        self.model,
                                        self.optimizer,
                                        lr_scheduler,
                                        save_optim=self.save_optim,
                                        save_dir=self.save_dir,
                                        save_rng=self.save_rng,
                                        iteration_in_epoch=iteration_)
                self.iteration += 1

            # at the end of each epoch.
            in_first_epoch = False

            # Checkpointing at the end of each epoch.
            # self.iteration-1 as the exact iteration
            if self.save_dir and (self.iteration-1) != best_iteration:
                self.model.save_pretrained(save_directory=self.save_dir)
                if self.adapter_save:
                    self.model.save_pretrained(save_directory=self.save_dir)
                else:
                    save_checkpoint(self.iteration+1,
                                    best_iteration+1,
                                    self.model,
                                    self.optimizer,
                                    lr_scheduler,
                                    save_optim=self.save_optim,
                                    save_dir=self.save_dir,
                                    save_rng=self.save_rng,
                                    iteration_in_epoch=iteration_)

        # Evaluation #todo add train_args
        if ((self.epochs == 0) or (self.eval_interval and
                                   (self.iteration ) % self.eval_interval != 0)
            ) and valid_dataloader is not None:
            prefix = 'final evaluate'
            self.evaluate_and_print_results(
                prefix=prefix,
                data_loader=valid_dataloader,
                model=self.model,
                forward_step_func=self.forward_step,
                verbose=False)

        # wandb
        if self.wandb and wandb is not None and self.rank == 0:
            wandb.finish()

    def train_step_pytorch(self,
                           data,
                           model,
                           optimizer,
                           lr_scheduler,
                           mems=None):
        """Single training step."""
        # Forward model for one step.
        self.timers('forward').start()
        step_output = self.forward_step(data, model, mems)
        self.timers('forward').stop()
        # accumulate gradients
        lm_loss = step_output['loss']
        lm_loss /= self.gradient_accumulation_steps
        reduced_loss = lm_loss.detach().clone().view(1)
        # skip the iter while loss has NAN
        if not DynamicLossScaler._has_inf_or_nan(reduced_loss):
            # Calculate gradients, reduce across processes, and clip.
            self.timers('backward').start()
            if self.fp16 and hasattr(optimizer, 'backward'):
                optimizer.backward(lm_loss,
                                   update_master_grads=False,
                                   retain_graph=True)
            else:
                lm_loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), self.clip_grad)
            self.timers('backward').stop()

            # Update parameters.
            self.timers('optimizer').start()
            if (self.accumulate_count +
                    1) % self.gradient_accumulation_steps == 0:
                if self.fp16:
                    # optimizer.update_master_grads()
                    optimizer.step()
                    optimizer.zero_grad()
                else:
                    optimizer.step()
                    # optimizer.zero_grad()
                self.accumulate_count = 0
            else:
                self.accumulate_count += 1
            if lr_scheduler:
                lr_scheduler.step()
            self.timers('optimizer').stop()

        else:
            log_dist("Found NaN loss, skip backward", [0])
            del lm_loss, reduced_loss
            mems = None
            reduced_loss = None
        return reduced_loss, mems

    def train_step_pytorchDDP(self,
                              data,
                              model,
                              optimizer,
                              lr_scheduler,
                              mems=None):
        """Single training step."""

        from contextlib import nullcontext
        if self.fp16:
            no_sync = model.module.no_sync
        else:
            no_sync = model.no_sync

        mycontext = no_sync if (
            self.accumulate_count +
            1) != self.gradient_accumulation_steps else nullcontext

        with mycontext():
            # Forward model for one step.
            self.timers('forward').start()
            step_output = self.forward_step(data, model, mems)
            self.timers('forward').stop()

            # accumulate gradients
            lm_loss = step_output['loss']
            lm_loss = lm_loss / self.gradient_accumulation_steps
            # reduce sum of losses
            reduced_loss = lm_loss.detach().clone().view(1)
            # dist.all_reduce(reduced_loss.data)
            # reduced_loss.data = reduced_loss.data / self.world_size

            # skip the iter while loss has NAN
            if not DynamicLossScaler._has_inf_or_nan(reduced_loss):
                # Calculate gradients, reduce across processes, and clip.
                self.timers('backward').start()

                if self.fp16 and hasattr(optimizer, 'backward'):
                    log_dist("The optimizer has backward function")
                    optimizer.backward(lm_loss,
                                       update_master_grads=False,
                                       retain_graph=True)
                else:
                    lm_loss.backward()

                grad_norm = torch.nn.utils.clip_grad_norm_(model.module.parameters(),
                                               self.clip_grad)
                self.timers('backward').stop()

                # Update parameters.
                self.timers('optimizer').start()
                if (self.accumulate_count +
                        1) % self.gradient_accumulation_steps == 0:
                    if self.fp16:
                        optimizer.update_master_grads()
                        optimizer.step()
                        optimizer.zero_grad()
                    else:
                        optimizer.step()
                        # model.zero_grad()

                    self.accumulate_count = 0
                else:
                    self.accumulate_count += 1
                if lr_scheduler:
                    lr_scheduler.step()
                self.timers('optimizer').stop()
                dist.barrier()

            else:
                log_dist("Found NaN loss, skip backward", [0])
                del lm_loss, reduced_loss
                mems = None
                reduced_loss = None
        return reduced_loss, mems

    def train_step_deepspeed(self,
                             data,
                             model,
                             optimizer,
                             lr_scheduler,
                             mems=None,
                             single_step=False):
        """Single training step."""

        # Forward model for one step.
        if (self.accumulate_count + 1) % self.gradient_accumulation_steps == 0:
            model.set_gradient_accumulation_boundary(True)
        else:
            model.set_gradient_accumulation_boundary(False)
        self.timers('forward').start()
        step_output = self.forward_step(data, model, mems)
        self.timers('forward').stop()
        lm_loss = step_output['loss']
        reduced_loss = lm_loss.detach().clone().view(1)

        if self.env_type == 'deepspeed+mpu':
            torch.distributed.all_reduce(reduced_loss.data,
                                         group=mpu.get_data_parallel_group())
        elif self.env_type == 'deepspeed':
            torch.distributed.all_reduce(reduced_loss.data)
        if 'deepspeed' in self.env_type:
            reduced_loss.data = reduced_loss.data / \
                (self.world_size / self.model_parallel_size)
        if not DynamicLossScaler._has_inf_or_nan(reduced_loss):
            # Calculate gradients, reduce across processes, and clip.
            self.timers('backward').start()
            model.backward(lm_loss)
            self.timers('backward').stop()
            # Update parameters.
            self.timers('optimizer').start()
            model.step()
            if lr_scheduler:
                lr_scheduler.step()
            self.timers('optimizer').stop()
            if (self.accumulate_count +
                    1) % self.gradient_accumulation_steps == 0:
                self.accumulate_count = 0
            else:
                self.accumulate_count += 1
            dist.barrier()
        else:
            log_dist("Found NaN loss, skip backward", [0])
            del lm_loss, reduced_loss
            mems = []
            reduced_loss = None
        return reduced_loss, mems
    
    def train_step_bmtrain(self,
                           data,
                           model,
                           optim_manager,
                           mems=None,
                           single_step=False):
        """Single training step."""

        # Forward model for one step.
        self.timers('forward').start()
        model_output = self.forward_step(data, model, mems)
        self.timers('forward').stop()

        # accumulate gradients
        logits = model_output['logits']
        loss = model_output['loss']

        lm_loss = bmt.sum_loss(loss)
        #lm_loss /= self.gradient_accumulation_steps
        reduced_loss = lm_loss.detach().clone().view(1)

        # skip the iter while loss has NAN
        if not DynamicLossScaler._has_inf_or_nan(reduced_loss):
            # Calculate gradients, reduce across processes, and clip.
            self.timers('backward').start()
            optim_manager.backward(loss)

            grad_norm = optim_manager.clip_grad_norm(optim_manager.optimizers[0].param_groups, max_norm=self.clip_grad)
            self.timers('backward').stop()

            # Update parameters.
            self.timers('optimizer').start()
            if (self.accumulate_count +
                    1) % self.gradient_accumulation_steps == 0:
                optim_manager.step()
                optim_manager.zero_grad()
                self.accumulate_count = 0
            else:
                # Need update lr_scheduler
                for lr_scheduler in optim_manager.lr_schedulers:
                    if lr_scheduler is not None:
                        lr_scheduler.step()
                self.accumulate_count += 1

            self.timers('optimizer').stop()

            # cached results
            mems = dict()
            mems['grad_norm'] = grad_norm.data.detach().float()

        else:
            log_dist("Found NaN loss, skip backward", [0])
            del lm_loss, reduced_loss
            mems = None
            reduced_loss = None
            lm_loss = None

        return lm_loss, mems

    def forward_step(self, data, model, mems=None):
        """Simple forward step. """
        data['mems'] = mems
        model_output = model(**data)
        logits = model_output['logits']
        loss = model_output['loss']
        hidden_states = None
        if 'hidden_states' in model_output:
            hidden_states = model_output['hidden_states']
        elif 'encoder_hidden_states' in model_output:
            hidden_states = model_output['encoder_hidden_states']

        return {
            'loss': loss,
            'hidden_states': hidden_states,
            'logits': logits.contiguous().float()
        }

    def backward_step(self, optimizer, model, lm_loss):
        """Backward step."""

        # Total loss.
        loss = lm_loss
        # Backward pass.
        # if self.train_args.deepspeed:
        if 'deepspeed' in self.env_type:
            model.backward(loss)
        else:
            # optimizer.zero_grad()
            if hasattr(optimizer, 'backward'):
                optimizer.backward(loss, update_master_grads=False)
            else:
                loss.backward()
                if self.env_type == 'pytorchDDP':
                    optimizer.step()

        # if self.train_args.deepspeed or self.train_args.DDP_impl == 'torch':
        self.timers('allreduce').reset()
        if self.env_type == 'pytorch':
            torch.nn.utils.clip_grad_norm_(model.parameters(), self.clip_grad)
        return lm_loss

    def _gather_all(self, input_):

        # Bypass the function if we are using only 1 GPU.
        if torch.distributed.get_world_size() == 1:
            return input_
        # Size and dimension.
        last_dim = input_.dim() - 1
        rank = torch.distributed.get_rank()
        world_size = torch.distributed.get_world_size()

        tensor_list = [
            torch.empty_like(input_, device=input_.device)
            for _ in range(world_size)
        ]
        tensor_list[rank] = input_

        torch.distributed.all_gather(tensor_list, input_)

        # Note: torch.cat already creates a contiguous tensor.
        if last_dim >= 0:
            output = torch.cat(tensor_list, dim=0).contiguous()
        else:
            output = torch.mean(torch.FloatTensor(tensor_list))

        return output

    def _gather_all_mpu(self, input_):
        group = mpu.get_model_parallel_group()

        # Bypass the function if we are using only 1 GPU.
        if torch.distributed.get_world_size(group=group) == 1:
            return input_
        # Size and dimension.
        last_dim = input_.dim() - 1
        rank = torch.distributed.get_rank(group=group)
        world_size = torch.distributed.get_world_size(group=group)

        tensor_list = [
            torch.empty_like(input_, device=input_.device)
            for _ in range(world_size)
        ]
        tensor_list[rank] = input_
        torch.distributed.all_gather(tensor_list, input_, group=group)

        # Note: torch.cat already creates a contiguous tensor.
        output = torch.cat(tensor_list, dim=last_dim).contiguous()

        return output

    def evaluate(self,
                 data_loader=None,
                 model=None,
                 forward_step_func=None,
                 verbose=False):
        """Evaluation."""

        # Turn off checkpoint_activations
        tmp_checkpoint_activations = None
        tmp_model = model
        while hasattr(tmp_model, 'module'):
            tmp_model = tmp_model.module
        # Turn on evaluation mode which disables dropout.
        tmp_model.eval()

        # TODO
        from collections.abc import Iterable
        if hasattr(tmp_model, 'config') and \
           isinstance(tmp_model.config, Iterable) and \
           'checkpoint_activations' in tmp_model.config:
            tmp_checkpoint_activations = tmp_model.config[
                'checkpoint_activations']
            tmp_model.config['checkpoint_activations'] = False

        mems = None
        metrics = [0. for _ in range(len(self.metric_methods))]

        with torch.no_grad():
            assert data_loader is not None, "val loader is not None."
            all_logits = []
            all_labels = []
            all_losses = []
            for data_iterator in data_loader:
                # Forward evaluation.

                meta = data_iterator.get('meta', None)

                if 'deepspeed' in self.env_type or 'DDP' in self.env_type:
                    data_iterator = {
                        x: data_iterator[x].to(
                            torch.device('cuda', self.local_rank))
                        for x in data_iterator
                        if x not in ['uid', 'meta', 'mode']
                    }
                elif torch.cuda.is_available():

                    data_iterator = {
                        x:
                        data_iterator[x].to(torch.device(self.pytorch_device))
                        for x in data_iterator
                        if x not in ['uid', 'meta', 'mode']
                    }
                step_output = forward_step_func(data_iterator, model, mems)
                '''when contiguous memory optimizations are enabled, the buffers
                allocated by the optimizations are deallocated during backward pass
                in the absence of backward pass the buffers should be reset after each
                forward pass'''
                if 'deepspeed' in self.env_type and self.deepspeed_activation_checkpointing:
                    deepspeed.checkpointing.reset()
                logits = step_output['logits']
                lm_loss = step_output['loss']

                if 'labels' in data_iterator:
                    labels = data_iterator['labels']
                else:
                    labels = data_iterator['target_ids']
                if len(self.metric_methods) != 0:
                    if {metric_tuple[0] for metric_tuple in self.metric_methods} & {"rouge", "bleu"}:
                        batch_preds = torch.argmax(logits.detach(), dim=-1).cpu()
                        batch_labels = labels.detach().cpu()
                        all_logits.extend(batch_preds)
                        all_labels.extend(batch_labels)
                    else:
                        all_logits.append(logits)
                        all_labels.append(labels)
                all_losses.append(lm_loss.view(1))

            all_losses = torch.cat(all_losses, dim=0)
            if len(self.metric_methods) != 0:
                all_logits = torch.cat(all_logits, dim=0)
                all_labels = torch.cat(all_labels, dim=0)

            if self.env_type == 'pytorchDDP' or self.env_type == 'deepspeed':
                if len(self.metric_methods) != 0:
                    all_logits = self._gather_all(all_logits)
                    all_labels = self._gather_all(all_labels)
                all_losses = self._gather_all(all_losses)

            elif self.env_type == 'deepspeed+mpu':
                if len(self.metric_methods) != 0:
                    all_logits = self._gather_all_mpu(all_logits)
                    all_labels = self._gather_all_mpu(all_labels)
                all_losses = self._gather_all_mpu(all_losses)

            all_ppls = []
            if all_losses.device != torch.device('cpu'):
                all_ppls = torch.exp(all_losses)
                all_losses = all_losses.mean().cpu().detach().numpy()
                all_ppls = all_ppls.mean().cpu().detach().numpy()

            for i in range(len(self.metric_methods)):
                eval_method = self.metric_methods[i][1]
                metrics[i] += eval_method(all_logits, all_labels, meta=meta)

        # TODO
        if self.tokenizer is not None:
            test_data = [
                "Hollym Gate railway station",
                "In mathematics, the Haagerup property"
            ]
            from flagai.model.predictor.predictor import Predictor
            predictor = Predictor(model, self.tokenizer)
            for text in test_data:
                log_dist(text, [0])
                output_text = predictor.predict_generate_beamsearch(
                    text,
                    out_max_length=128,
                    beam_size=3)
                log_dist(output_text, [0])
    
        # Move model back to the train mode.

        # model.train()
        tmp_model.train()
        # recover the settings for checkpoint_activations
        if hasattr(tmp_model,
                   'config') and 'checkpoint_activations' in tmp_model.config:
            tmp_model.config[
                'checkpoint_activations'] = tmp_checkpoint_activations
        metric_dct = {}
        for i in range(len(self.metric_methods)):
            metric_name = self.metric_methods[i][0]
            metric_dct.update({metric_name: metrics[i]})
        metric_dct.update({"loss": all_losses})
        metric_dct.update({"perplexity": all_ppls})
        return metric_dct

    def report_iteration_metrics(self, optimizer, lr, loss, elapsed_time, step,
                                 total_step, optimizer_manager=None, grad_norm=0.0):
        log_string = ' iteration {:8d}/{:8d} |'.format(step, total_step)
        log_string += ' elapsed time per iteration (ms): {:.1f} |'.format(
            elapsed_time)
        log_string += ' learning rate {:.3E} |'.format(lr)
        log_string += ' loss {:.6E} |'.format(loss)
        perplexity = math.exp(loss)
        log_string += ' perplexity {:.6E} |'.format(perplexity)

        loss_scale = 0.0
        # when bmtrain, optimizer is optimizer_manager
        if self.fp16 and 'bmtrain' in self.env_type:
            loss_scale = optimizer_manager.loss_scale
        elif self.fp16:
            loss_scale = optimizer.cur_scale if 'deepspeed' in self.env_type \
                else hasattr(optimizer, 'loss_scale') and optimizer.loss_scale
        log_string += ' loss scale {:.1f} |'.format(loss_scale)

        log_string += ' grad norm {:.6f} |'.format(grad_norm)

        log_string += ' gradient_accumulation {}/{}'.format(self.accumulate_count, self.gradient_accumulation_steps)

        log_dist(log_string, [0])

        # wandb
        if self.wandb and wandb is not None and self.rank == 0:
            metrics = dict()
            metrics['total_step'] = total_step
            metrics['elapsed_time'] = elapsed_time
            metrics['learning rate'] = lr
            metrics['loss'] = loss
            metrics['loss_scale'] = loss_scale
            metrics['perplexity'] = perplexity
            metrics['grad_norm'] = grad_norm
            try:
                # billion per step
                tokens_num = step * self.world_size * self.batch_size * 2048. / 1000 / 1000 / 1000
                metrics['tokens_num'] = tokens_num
            except:
                pass

            wandb.log(metrics, step=step)

    def report_evaluate_metrics(self, prefix, loss, ppl, gpt_loss, bert_loss,
                                sent_loss, multi_loss, step):
        string = ' validation loss at {}'.format(prefix)
        string += ' | LM loss: {:.6E}'.format(loss)
        string += ' | LM PPL: {:.6E}'.format(ppl)
        length = len(string) + 1
        log_dist('-' * 100, [self.rank])
        log_dist('-' * length, [self.rank])
        log_dist(string, [self.rank])
        log_dist('-' * length, [self.rank])

    def evaluate_and_print_results(
        self,
        prefix=None,
        forward_step_func=None,
        data_loader=None,
        model=None,
        verbose=False,
    ):
        """Helper function to evaluate and dump results on screen."""
        eval_dict = self.evaluate(forward_step_func=forward_step_func,
                                  data_loader=data_loader,
                                  model=model,
                                  verbose=verbose)
        if eval_dict.get("loss", None) is not None:
            string = ' validation loss at {} | {:.4f}, '.format(
                prefix, eval_dict["loss"])
        if eval_dict.get("perplexity", None) is not None:
            string = ' validation perplexity at {} | {:.4f}, '.format(
                prefix, eval_dict["perplexity"])
        # with open("results.txt", "a") as myfile:
        #     myfile.write(string)
        if self.metric_methods is None:
            return eval_dict

        for i in range(len(self.metric_methods)):
            name = self.metric_methods[i][0]
            string += ", {} {:.3f}".format(name, eval_dict[name])
        # string = ' validation loss at {} | {:.4f},  Acc {:.2f}'.format(
        #     prefix, eval_dict["loss"], eval_dict["metrics"])
        length = len(string) + 1
        log_dist('-' * length, [self.rank])
        log_dist(string, [self.rank])
        log_dist('-' * length, [self.rank])
        return eval_dict

