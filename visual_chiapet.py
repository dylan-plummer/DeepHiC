import os, sys
import time
import argparse
import multiprocessing
import random
import numpy as np
import pandas as pd
import matplotlib.pyplot as plt

from sklearn.metrics import auc
from all_parser import *

def infoparser(info):
    import re
    # 'chr1:121484520..121485419-chr10:42530561..42531081,2'
    info = info.split(',')[0]
    # 'chr1:121484520..121485419-chr10:42530561..42531081'
    p = re.split("-|\.\.|\:", info)
    return p

def getonechr(infopd, chrn):
    chrn = f'chr{chrn}'
    return infopd[(infopd.chr1 == chrn) & (infopd.chr2 == chrn)]

def getbins(data, start, end, resolution):
    num = ''.join(filter(str.isdigit, start))
    bin_l = f'binLeft{num}'
    bin_r = f'binRight{num}'
    bias_l = f'biasLeft{num}'
    bias_r = f'biasRight{num}'
    data[bin_l] = data[start] // resolution
    data[bin_r] = data[end] // resolution
    data[bias_l] = resolution - data[start] % resolution
    data[bias_r] = data[end] % resolution

def read_chiapet_k562(raw_dir):
    # reading raw data
    cols = ['chr1', 'start', 'end', 'info', 'unk', 'dot', 'unk1', 'unk2', 'rgb', 'unk3', 'unk4', 'unk5']
    rawfile = os.path.join(raw_dir, 'ENCFF001THV.bed.gz')
    rawdata = pd.read_csv(rawfile, sep='\t', header=None, names=cols, compression='gzip')
    # parse the info column
    parsed = rawdata['info'].apply(infoparser)
    # build a new dataframe only have interested columns
    cols = ['chr1', 'start1', 'end1', 'chr2', 'start2', 'end2']
    types = {'chr1': str, 'start1': int, 'end1': int, 'chr2': str, 'start2': int, 'end2': int}
    result = pd.DataFrame(parsed.values.tolist(), index=parsed.index, columns=cols).astype(types)
    return result

def read_chiapet(cell_line, raw_dir, low_dist, up_dist, resolution=10_000):
    if cell_line == 'K562':
        rawdata = read_chiapet_k562(raw_dir)

    # convert to bins and drop useless columns
    getbins(rawdata, 'start1', 'end1', resolution)
    getbins(rawdata, 'start2', 'end2', resolution)
    cols = ['chr1', 'binLeft1', 'binRight1', 'biasLeft1', 'biasRight1', 
            'chr2', 'binLeft2', 'binRight2', 'biasLeft2', 'biasRight2']
    rawdata = rawdata[cols]
    # only consider intra loops
    intra = rawdata[rawdata.chr1 == rawdata.chr2]
    binLeft1_temp = (intra.binRight1.values + intra.binLeft1.values) // 2
    binLeft2_temp = (intra.binRight2.values + intra.binLeft2.values) // 2
    intra['binLeft1'] = binLeft1_temp
    intra['binLeft2'] = binLeft2_temp
    # only consider loops in interested distance interval
    intra = intra[(intra.binLeft2 - intra.binLeft1) > low_dist]
    intra = intra[(intra.binLeft2 - intra.binLeft1) < up_dist]
    # drop chrX
    # intra = intra[intra.chr1 != 'chrX'].reset_index(drop=True)
    return intra

def read_sigfile(file, resolution=10_000):
    if file.find('/hic.') >= 0: print(f'Reading file: {file}')
    sig = pd.read_csv(file, sep='\t', compression='gzip')
    sig.locus1 = (sig.locus1 - resolution//2) // resolution
    sig.locus2 = (sig.locus2 - resolution//2) // resolution
    sig.rename(columns={'locus1': 'bin1', 'locus2': 'bin2'}, inplace = True)
    return sig

def roc_curve(pos_data, neg_data):
    thres = np.linspace(0, 1, num=200)
    fpr = []
    tpr = []
    for t in thres:
        fpr.append(len(np.where(neg_data <= t)[0])/len(neg_data))
        tpr.append(len(np.where(pos_data <= t)[0])/len(pos_data))
    return fpr, tpr, thres

def get_posdata(truth, sig, chrn):
    single = getonechr(truth, chrn)
    # get all unique loops in ChIA-PET data
    loops = np.column_stack([single.binLeft1.values, single.binLeft2.values])
    loops = np.unique(loops, axis=0)
    gp = sig.groupby(['bin1', 'bin2'])
    # retrieve these loops in significance Dataframe
    results, indices = [], []
    for l in loops:
        results.extend(gp.get_group(tuple(l)).q_vals.values)
        indices.extend(gp.get_group(tuple(l)).index.values)
    pos_data = np.array(results)
    pos_index = set(indices)
    return pos_data, pos_index

def get_posneg(sigpath, truth, chrn, passNo=2, resolution=10_000):
    tags = ['hic', 'downhic', 'hicplus', 'deephic']
    posneg = dict.fromkeys(tags)
    for tag in tags:
        sigfile = os.path.join(sigpath, f'chr{chrn}/{tag}.pass{passNo}_spline.significant.gz')
        sig = read_sigfile(sigfile, resolution)
        pos_data, pos_index = get_posdata(truth, sig, chrn)
        if tag == 'hic':
            # get all negative indices
            all_index = set(sig[sig.contactType == 'intraInRange'].index.tolist())
            neg_index = all_index - pos_index
            # random sample negative indices
            neg_index = sorted(list(random.sample(neg_index, len(pos_index))))
            neg_data = sig.loc[neg_index].q_vals.values
            posneg['neg'] = neg_data
        posneg[tag] = pos_data
    return posneg

def plot_roc(posneg, file=None):
    fig = plt.figure(figsize=[12, 4])
    ax = fig.add_subplot(1, 2, 1)
    ax.hist([posneg['hic'], posneg['downhic'], posneg['hicplus'], posneg['deephic'], posneg['neg']], 
             label=['Experimental', 'Downsampled', 'HiCPlus', 'DeepHiC', 'Negative'], 
             color=['C2', 'C5', 'C0', 'C1', 'C4'], density=True)
    ax.set_xlabel('q value')
    ax.set_ylabel('density')
    ax.legend()
    ax = fig.add_subplot(1, 2, 2)
    fpr, tpr, _ = roc_curve(posneg['hic'], posneg['neg'])
    auc_score = auc(fpr, tpr)
    ax.plot(fpr, tpr, label=f'Experimental (AUC={auc_score:.3f})', color='C2')

    fpr, tpr, _ = roc_curve(posneg['downhic'], posneg['neg'])
    auc_score = auc(fpr, tpr)
    ax.plot(fpr, tpr, label=f'Downsampled (AUC={auc_score:.3f})', color='C5', linestyle='--')

    fpr, tpr, _ = roc_curve(posneg['hicplus'], posneg['neg'])
    auc_score = auc(fpr, tpr)
    ax.plot(fpr, tpr, label=f'HiCPlus (AUC={auc_score:.3f})', color='C0')

    fpr, tpr, _ = roc_curve(posneg['deephic'], posneg['neg'])
    auc_score = auc(fpr, tpr)
    ax.plot(fpr, tpr, label=f'DeepHiC (AUC={auc_score:.3f})', color='C1')
    ax.set_xlabel('False positive rate', fontsize='x-large')
    ax.set_ylabel('True positive rate', fontsize='x-large')
    ax.legend()
    if file is not None:
        print(f'Ploting to {file}.svg and .eps')
        fig.savefig(file+'.svg', format='svg')
        fig.savefig(file+'.eps', format='eps')
        plt.close(fig)

if __name__ == '__main__':
    args = visual_chiapet_parser().parse_args(sys.argv[1:])
    cell_line = args.cell_line
    low_res = args.low_res
    high_res = args.high_res # default is 10kb
    passNo = args.passNo
    low_dist = args.lowerbound
    up_dist = args.upperbound

    raw_dir = os.path.join('data/raw/ChIA-PET')
    truth = read_chiapet(cell_line, raw_dir, low_dist, up_dist, resolution=res_map[high_res])
    print(f'Got {len(truth)} pairs of interactions from CTCF ChIA-PET in {cell_line} within ({low_dist}, {up_dist}) distance.')

    pool_num = 23 if multiprocessing.cpu_count() > 23 else multiprocessing.cpu_count()

    out_dir = os.path.join(f'data/results/{cell_line}/{low_res}/visual/roc_analysis')
    mkdir(out_dir)
    data_dir = os.path.join(out_dir, 'data')
    mkdir(data_dir)

    sig_dir = os.path.join(f'data/results/{cell_line}/{low_res}/pfithic_output')
    chrns = list(range(1, 23)) + ['X']

    start = time.time()
    pool = multiprocessing.Pool(processes=pool_num)
    print(f'Start a multiprocess pool with processes = {pool_num} for analysis ChIA-PET')
    result = []
    for chrn in chrns:
        res = pool.apply_async(get_posneg, (sig_dir, truth, chrn,), {'passNo': passNo, 'resolution': res_map[high_res]})
        result.append(res)
    pool.close()
    pool.join()
    print(f'All process done.')
    posneg_dict = {n: r.get() for n, r in zip(chrns, result)}
    posneg_all = dict.fromkeys(posneg_dict[chrns[0]].keys())
    for chrn in chrns:
        posneg = posneg_dict[chrn]
        filename = os.path.join(out_dir, f'roc_analysis_chr{chrn}')
        plot_roc(posneg, file=filename)
        if chrn == chrns[0]:
            for key in posneg.keys():
                posneg_all[key] = posneg[key]
        else:
            for key in posneg.keys():
                posneg_all[key] = np.concatenate([posneg_all[key], posneg[key]])
        pg_pd = pd.DataFrame(posneg)
        pg_pd.to_csv(os.path.join(data_dir, f'posneg_scores_chr{chrn}.csv'), index=None, sep='\t')
    fig_file = os.path.join(out_dir, f'roc_analysis_allchr')
    plot_roc(posneg_all, file=fig_file)
    pg_pd_all = pd.DataFrame(posneg_all)
    pg_pd_all.to_csv(os.path.join(data_dir, f'allchr_posneg_scores.csv'), index=None, sep='\t')
    print(f'All pipeline done. Running cost is {(time.time()-start)/60:.1f} min')
