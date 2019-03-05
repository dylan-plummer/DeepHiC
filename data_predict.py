import os, sys
import time
import argparse
import multiprocessing
import numpy as np
import torch
from torch.utils.data import TensorDataset, DataLoader
from tqdm import tqdm
from scipy.sparse import coo_matrix

import models.deephic as deephic
from models.hicplus import ConvNet

from utils.io import spreadM, together

from all_parser import *

def dataloader(data, batch_size=64):
    inputs = torch.tensor(data['data'], dtype=torch.float)
    inds = torch.tensor(data['inds'], dtype=torch.long)
    dataset = TensorDataset(inputs, inds)
    loader = DataLoader(dataset, batch_size=batch_size, shuffle=False)
    return loader

def data_info(data):
    indices = data['inds']
    compacts = data['compacts'][()]
    sizes = data['sizes'][()]
    return indices, compacts, sizes

def rebuild(data, indices):
    """Rebuild hic matrices from hicplus data, return a dict of matrices."""
    # cause of chunk=40 and stride=28 division, corp is needed
    div_dhic = data['data']
    div_hhic = data['target']
    hics = together(div_hhic, indices, corp=6, tag='HiC[orig]')
    down_hics = together(div_dhic, indices, corp=6, tag='HiC[down]')
    return hics, down_hics

get_digit = lambda x: int(''.join(list(filter(str.isdigit, x))))
def filename_parser(filename):
    info_str = filename.split('.')[0].split('_')[2:-1]
    chunk = get_digit(info_str[0])
    stride = get_digit(info_str[1])
    bound = get_digit(info_str[2])
    scale = 1 if info_str[3] == 'nonpool' else get_digit(info_str[3])
    return chunk, stride, bound, scale

def hicplus_predictor(hicplus_loader, device):
    hicplus = ConvNet(40, 28).to(device)
    hicplus.load_state_dict(torch.load('save/pytorch_model_12000.pytorch'))
    result_data_plus = []
    result_inds_plus = []
    hicplus.eval()
    with torch.no_grad():
        for batch in tqdm(hicplus_loader, desc='HiCPlus Predicting: '):
            imgs, inds = batch
            imgs = imgs.to(device)
            out = hicplus(imgs)
            result_data_plus.append(out.to('cpu').numpy())
            result_inds_plus.append(inds.numpy())
    result_data_plus = np.concatenate(result_data_plus, axis=0)
    result_inds_plus = np.concatenate(result_inds_plus, axis=0)
    plus_hics = together(result_data_plus, result_inds_plus, tag='HiC[plus]')
    return plus_hics
    
def deephic_predictor(srgan_loader, ckpt_file, scale, res_num, device):
    deepmodel = deephic.Generator(scale_factor=scale, in_channel=1, resblock_num=res_num).to(device)
    if not os.path.isfile(ckpt_file):
        ckpt_file = f'save/{ckpt_file}'
    deepmodel.load_state_dict(torch.load(ckpt_file))
    print(f'Loading DeepHiC checkpoint file from "{ckpt_file}"')
    result_data = []
    result_inds = []
    deepmodel.eval()
    with torch.no_grad():
        for batch in tqdm(srgan_loader, desc='DeepHiC Predicting: '):
            lr, inds = batch
            lr = lr.to(device)
            out = deepmodel(lr)
            result_data.append(out.to('cpu').numpy())
            result_inds.append(inds.numpy())
    result_data = np.concatenate(result_data, axis=0)
    result_inds = np.concatenate(result_inds, axis=0)
    deep_hics = together(result_data, result_inds, tag='HiC[deep]')
    return deep_hics

def save_data(hic, down_hic, plus_hic, deep_hic, compact, size, file):
    hic = spreadM(hic, compact, size)
    downhic = spreadM(down_hic, compact, size)
    plushic = spreadM(plus_hic, compact, size)
    deephic = spreadM(deep_hic, compact, size, convert_int=False, verbose=True)
    np.savez_compressed(file, hic=hic, downhic=downhic, hicplus=plushic, deephic=deephic, compact=compact)
    print('Saving file:', file)

if __name__ == '__main__':
    args = data_predict_parser().parse_args(sys.argv[1:])
    cell_line = args.cell_line
    low_res = args.low_res
    ckpt_file = args.checkpoint
    res_num = args.resblock
    cuda = args.cuda
    print('WARNING: Predict process needs large memory, thus ensure that your machine have ~150G memory.')
    if multiprocessing.cpu_count() > 23:
        pool_num = 23
    else:
        exit()

    in_dir = os.path.join('data/processed', cell_line)
    out_dir = os.path.join('data/predict', cell_line)
    mkdir(out_dir)

    files = [f for f in os.listdir(in_dir) if f.find(low_res) >= 0]
    deephic_file = [f for f in files if f.find('deephic') >= 0][0]
    hicplus_file = [f for f in files if f.find('hicplus') >= 0][0]

    chunk, stride, bound, scale = filename_parser(deephic_file)

    device = torch.device(f'cuda:{cuda}' if (torch.cuda.is_available() and cuda>-1 and cuda<torch.cuda.device_count()) else 'cpu')
    print(f'Using device: {device}')
    
    start = time.time()
    deephic_data = np.load(os.path.join(in_dir, deephic_file))
    hicplus_data = np.load(os.path.join(in_dir, hicplus_file))
    print(f'Loading data[DeepHiC]: {deephic_file}')
    print(f'Loading data[HiCPlus]: {hicplus_file}')
    deephic_loader = dataloader(deephic_data)
    hicplus_loader = dataloader(hicplus_data)
    
    indices, compacts, sizes = data_info(hicplus_data)
    hics, down_hics = rebuild(hicplus_data, indices) # rebuild matrices by hicplus data
    plus_hics = hicplus_predictor(hicplus_loader, device)
    deep_hics = deephic_predictor(deephic_loader, ckpt_file, scale, res_num, device)
    
    def save_data_n(key):
        file = os.path.join(out_dir, f'predict_chr{key}_{low_res}.npz')
        save_data(hics[key], down_hics[key], plus_hics[key], deep_hics[key], compacts[key], sizes[key], file)

    pool = multiprocessing.Pool(processes=pool_num)
    print(f'Start a multiprocess pool with process_num = {pool_num} for saving predicted data')
    for key in compacts.keys():
        pool.apply_async(save_data_n, (key,))
    pool.close()
    pool.join()
    print(f'All data saved. Running cost is {(time.time()-start)/60:.1f} min.')