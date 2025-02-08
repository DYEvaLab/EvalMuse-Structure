# -*- coding: utf-8 -*-
"""RAHF_train.ipynb

Automatically generated by Colab.

Original file is located at
    https://colab.research.google.com/drive/1n8Bug-l4fVCAXA7kLDJmhbKkN52jIsXm
"""
import os
import time
import torch
from model.model_final import RAHF
from torch.utils.data import DataLoader
from torch.optim.lr_scheduler import LambdaLR, CosineAnnealingLR
import torch.nn as nn
import logging
import argparse
import torch.distributed as dist
from dataset import RAHFDataset
from utils import print_log_iter, eval_in_training, save_in_training, final_save

def train(args):
  try:
    local_rank = int(os.environ['LOCAL_RANK'])
  except:
    local_rank = dist.get_rank()  
  torch.cuda.set_device(local_rank)
  gpu = f'cuda:{local_rank}'
  print(f'GPU: {gpu}')
  torch.cuda.empty_cache()

  save_path = f'{args.bytenas_path}/experiments/{args.experiment_name}'
  if not os.path.exists(save_path):
    os.makedirs(save_path)
  logging.basicConfig(filename=f'{save_path}/{args.experiment_name}.log', level=logging.INFO, format='%(asctime)s - %(message)s')
  logger = logging.getLogger()
  datapath = args.data_path
  print('datapath', datapath)
  print('bytenas path', args.bytenas_path)
  pretrained_processor_path = 'altclip_processor'
  pretrained_model_path = 'altclip_model'
  
  dist.init_process_group(backend='nccl', init_method='env://')
  args.rank = dist.get_rank()

  print(f'Using {torch.cuda.device_count()} GPUs')
  print(f'Freeze the pretrained componenets? {args.warmup}. Preparing model...')
  model = RAHF(pretrained_model_path=pretrained_model_path,freeze=args.warmup)
  model.cuda(gpu)
  if len(args.load_checkpoint) > 0:
    load_checkpoint = f'{args.bytenas_path}/experiments/{args.load_checkpoint}'
    print(f'Load checkpoint {load_checkpoint}')
    checkpoint = torch.load(f'{load_checkpoint}', map_location='cpu')
    model.load_state_dict(checkpoint['model'])
  else:
    print('Train from scratch')
  model = nn.SyncBatchNorm.convert_sync_batchnorm(model)                  
  # model = nn.parallel.DistributedDataParallel(model, device_ids=[gpu], broadcast_buffers=False, find_unused_parameters=True)    
  model = nn.parallel.DistributedDataParallel(model, device_ids=[gpu], find_unused_parameters=False)
  optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr)
  # lr_lambda = lambda step: min((step+1) / 500.0, 1.0)
  # lr_lambda = lambda step: min(1.0/math.sqrt(step+1), 1.0)
  # lr_lambda = lambda step: 1.0
  # scheduler = CyclicLR(optimizer, base_lr=1e-5, max_lr=1e-3, step_size_up=400, cycle_momentum=False)
  # scheduler = LambdaLR(optimizer, lr_lambda=lr_lambda)
  scheduler = CosineAnnealingLR(optimizer, T_max=args.iters, eta_min=args.min_lr, last_epoch=-1)
  if len(args.load_checkpoint) > 0:
    optimizer.load_state_dict(checkpoint['optimizer'])
    scheduler.load_state_dict(checkpoint['scheduler'])

  criterion = torch.nn.MSELoss().to(gpu)
  def criterion_heatmap(output_heatmap, target_heatmap, weighted_loss, criterion):
    if weighted_loss:
      mse_heatmap = torch.nn.MSELoss(reduction='none').to(gpu)
      loss_heatmap = mse_heatmap(output_heatmap, target_heatmap)
      loss_weights_heatmap = target_heatmap + 1.0 / 255.0 # loss weight related to pixel value, prevent from output all 0
      weighted_loss = (loss_heatmap * loss_weights_heatmap).sum(dim=(-2, -1)) / loss_weights_heatmap.sum(dim=(-2, -1))
      weighted_loss = weighted_loss.sum() / weighted_loss.shape[0]
      return weighted_loss
    else:
      return criterion(output_heatmap, target_heatmap)
  if args.weighted_loss:
    print('Use weighted MSE loss')
  else:
    print('Use normal MSE loss')

  def train_loop(model, train_dataloader, val_dataloader, iter_counter, epoch_counter, end_iter, accumulate_step):
    print(f"iter:{iter_counter}, epoch:{epoch_counter}, end:{end_iter}, accumlate:{accumulate_step}")
    while True:
      model.train()
      print(f'Epoch {epoch_counter}')
      train_dataloader.sampler.set_epoch(epoch_counter)
      iter_loss = [[], [], [], []]
      for batch_id, (inputs, targets) in enumerate(train_dataloader):
          inputs = inputs.to(gpu)
          inputs_pixel_values, inputs_ids_im, inputs_ids_mis = inputs['pixel_values'].squeeze(1), inputs['input_ids'][:, 0, :], inputs['input_ids'][:, 1, :]
          outputs_im = model(inputs_pixel_values, inputs_ids_im, need_score=True)  # implausibility
          # implausibility heatmap
          output_heatmap, target_heatmap = outputs_im[0].to(gpu), targets['artifact_map'].float().to(gpu)
          loss_im = criterion_heatmap(output_heatmap, target_heatmap, args.weighted_loss, criterion)

          # implausibility score 
          output_score, target_score = outputs_im[1].to(gpu), targets['artifact_score'].float().to(gpu)

          loss_score = criterion(output_score, target_score)
          # implausibility loss 
          iter_loss[0].append(loss_im.item())
          iter_loss[1].append(loss_score.item())     
          loss_im = loss_im + loss_score
          loss_im.backward()

          iter_loss[2].append(0)
          iter_loss[3].append(0)
          
          if (batch_id + 1) % accumulate_step == 0 or batch_id == len(train_dataloader):
            optimizer.step()
            optimizer.zero_grad()
            scheduler.step()
            iter_counter += 1
            dist.barrier()
            print_log_iter(optimizer, iter_counter, iter_loss, logger)
            iter_loss = [[], [], [], []]
            if iter_counter % args.val_iter == 0:
              eval_in_training(model, val_dataloader, gpu, criterion, iter_counter, logger)
            if iter_counter % args.save_iter == 0:
              save_in_training(model, optimizer, scheduler, iter_counter, save_path)
            if iter_counter >= end_iter:
              return iter_counter, epoch_counter
              
      epoch_counter += 1

  print('Preparing dataloader...')
  train_dataset = RAHFDataset(datapath, 'train', pretrained_processor_path, finetune=False, img_len=448)
  train_sampler = torch.utils.data.distributed.DistributedSampler(train_dataset)
  train_dataloader = DataLoader(dataset=train_dataset, 
                                batch_size=args.batch_size, 
                                shuffle=False, 
                                num_workers=8,
                                pin_memory=True,
                                sampler=train_sampler)

  val_dataset = RAHFDataset(datapath, 'test', pretrained_processor_path, img_len=448)
  val_sampler = torch.utils.data.distributed.DistributedSampler(val_dataset)
  val_dataloader = DataLoader(dataset=val_dataset,
                              batch_size=args.batch_size,
                              # batch_size=1,   # to get finetune performance
                              shuffle=False,
                              pin_memory=True, 
                              num_workers=8,
                              sampler=val_sampler)
  
  train_dataloader1 = DataLoader(dataset=train_dataset, 
                                batch_size=1, 
                                shuffle=False, 
                                num_workers=8,
                                pin_memory=True,
                                sampler=train_sampler)

  dist.barrier()
  print('Training...')
  iter_counter = 0
  epoch_counter = 0
  start_time = time.time()
  torch.autograd.set_detect_anomaly(True)
  iter_counter, epoch_counter = train_loop(model, train_dataloader, val_dataloader, iter_counter, epoch_counter, args.iters//2, args.accumulate_step)
  dist.barrier()
  model.module.image_encoder.unfreeze()
  model.module.text_encoder.unfreeze()
  print('Unfreeze image encoder and text encoder after 1000 iterations.')
  del train_dataloader
  iter_counter, epoch_counter = train_loop(model, train_dataloader1, val_dataloader, iter_counter, epoch_counter, args.iters, 32)
  dist.barrier()
  final_save(model, optimizer, scheduler, start_time, save_path)
  dist.destroy_process_group()

def main():
  parser = argparse.ArgumentParser()
  # Training settings
  # parser.add_argument('-gpu_n', default=4, type=int, help="how many gpu")
  # parser.add_argument('-g', '--gpuid', default=0, type=int, help="which gpu to use")
  parser.add_argument("--local-rank", default=0, type=int, help='rank in current node')                                   
  # Experiment settings
  parser.add_argument("--experiment_name", required=True, type=str, help="name of this experiment")
  parser.add_argument("--load_checkpoint", default='', type=str, help="the name of the checkpoint to be loaded")
  parser.add_argument("--bytenas_path", type=str, default='xxx', help="path of bytenas")  # 存放实验相关内容
  parser.add_argument("--data_path", type=str, default='xxx', help="path of data")       # 训练/测试数据存放路径
  parser.add_argument('--iters', required=True, type=int, metavar='N', help='number of total iterations to run')
  parser.add_argument('--batch_size', default=4, type=int, metavar='N', help='the batchsize for each gpu')
  parser.add_argument('--accumulate_step', default=16, type=int, metavar='N', help='accumulate_step * batch_size = actual batch size')
  parser.add_argument('--lr', default=1e-3, type=float, help='base learning rate')
  parser.add_argument('--min_lr', default=0.0, type=float, help='min learning rate')
  parser.add_argument('--val_iter', default=25, type=int, metavar='N', help='number of iterations to run validation')
  parser.add_argument('--save_iter', default=200, type=int, metavar='N', help='number of iterations to save')
  parser.add_argument('--warmup', action='store_true', help='whether to freeze the pretrained components')
  parser.add_argument('--weighted_loss', action='store_true', help='weighted loss for heatmap prevent output all 0')
  # parser.add_argument('--loss_weights', default=[1.0, 1.0, 0.5, 0.5], help='loss weight for: implausibility heatmap & score, misalignment heatmap & score')   
  args = parser.parse_args()
  train(args)

if __name__ == '__main__':
  main()