import argparse
import time
import shutil
import os
import os.path as osp
import csv
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
from torch.optim.lr_scheduler import ReduceLROnPlateau, StepLR
from data_cnn60 import NTUDataLoaders, AverageMeter, make_dir, get_cases, get_num_classes
from sklearn.metrics import confusion_matrix
from collections import OrderedDict
import torch.nn.functional as F
from ReViSE import VisAE, AttAE, contractive_loss, MMDLoss, multi_class_hinge_loss
import pickle as pkl


parser = argparse.ArgumentParser(description='View adaptive')
parser.add_argument('--ss', type=int, help="No of unseen classes")
parser.add_argument('--st', type=str, help="Type of split")
parser.add_argument('--dataset', type=str, help="dataset path")
parser.add_argument('--wdir', type=str, help="directory to save weights path")
parser.add_argument('--le', type=str, help="language embedding model")
parser.add_argument('--ve', type=str, help="visual embedding model")
parser.add_argument('--phase', type=str, help="train or val")
parser.add_argument('--gpu', type=str, help="gpu device number")
parser.add_argument('--ntu', type=int, help="no of classes")
parser.add_argument('--temp', type=int, help="temperature")
parser.add_argument('--thresh', type=float, help="threshold")

args = parser.parse_args()

gpu = args.gpu
ss = args.ss
st = args.st
dataset_path = args.dataset
wdir = args.wdir
le = args.le
ve = args.ve
phase = args.phase
num_classes = args.ntu
temp = args.temp
thresh = args.thresh

# gpu = '0'
# ss = 10
# st = 'r'
# dataset_path = 'ntu_results/shift_10_r'
# # wdir = args.wdir
# le = 'bert'
# ve = 'shift'
# phase = 'train'
# num_classes = 120



os.environ["CUDA_VISIBLE_DEVICES"] = gpu
seed = 5
torch.manual_seed(seed)
torch.cuda.manual_seed(seed)
np.random.seed(seed)
device = torch.device("cuda")
print(torch.cuda.device_count())


zvisae_checkpoint =  wdir + '/' + le +'/main_gvisae_best.pth.tar'
zattae_checkpoint =  wdir + '/' + le +'/main_gattae_best.pth.tar'
criterion2 = nn.MSELoss().to(device)
att_input_size = 1024
att_intermediate_size = 512
att_hidden_size = 100
attae = VisAE(att_input_size, att_intermediate_size, att_hidden_size).to(device)
attae.load_state_dict(torch.load(zattae_checkpoint)['revise_state_dict'], strict=False)
attae_optimizer = optim.Adam(attae.parameters(),lr=1e-4, weight_decay = 0.001)
attae_scheduler = ReduceLROnPlateau(attae_optimizer, mode='max', factor=0.1, patience=15, cooldown=3, verbose=True)

vis_input_size = 256
vis_intermediate_size = 512
vis_hidden_size = 100
visae = AttAE(vis_input_size, vis_hidden_size).to(device)
visae.load_state_dict(torch.load(zvisae_checkpoint)['revise_state_dict'], strict=False)
visae_optimizer = optim.Adam(visae.parameters(), lr=1e-4)
visae_scheduler = ReduceLROnPlateau(visae_optimizer, mode='max', factor=0.1, patience=15, cooldown=3, verbose=True)
print("Loaded Revise Model")

ntu_loaders = NTUDataLoaders(dataset_path, 'max', 1)
train_loader = ntu_loaders.get_train_loader(1024, 8)
zsl_loader = ntu_loaders.get_val_loader(1024, 8)
val_loader = ntu_loaders.get_test_loader(1024, 8)
zsl_out_loader = ntu_loaders.get_val_out_loader(1024, 8)
val_out_loader = ntu_loaders.get_test_out_loader(1024, 8)
train_size = ntu_loaders.get_train_size()
zsl_size = ntu_loaders.get_val_size()
val_size = ntu_loaders.get_test_size()
print('Train on %d samples, validate on %d samples' % (train_size, zsl_size))

if phase == 'val':
    gzsl_inds = np.load('../resources/label_splits/'+st+'s'+str(num_classes - ss)+'.npy')
    unseen_inds = np.load('../resources/label_splits/'+st+'v'+str(ss)+'_0.npy')
    seen_inds = np.load('../resources/label_splits/'+st+'s'+str(num_classes - ss - ss)+'_0.npy')
else:
    gzsl_inds = np.arange(num_classes)
    unseen_inds = np.load('../resources/label_splits/'+st+'u'+str(ss)+'.npy')
    seen_inds = np.load('../resources/label_splits/'+st+'s'+str(num_classes - ss)+'.npy')

labels = np.load('../resources/labels.npy')
unseen_labels = labels[unseen_inds]
seen_labels = labels[seen_inds]

s2v_labels = torch.from_numpy(np.load('../resources/ntu' + str(num_classes) + '_'+ le + '_labels.npy')).view([num_classes, att_input_size])
s2v_labels = s2v_labels/torch.norm(s2v_labels, dim = 1).view([num_classes, 1]).repeat([1, att_input_size])

unseen_s2v_labels = s2v_labels[unseen_inds, :]
seen_s2v_labels = s2v_labels[seen_inds, :]


def accuracy(class_embedding, vis_trans_out, target, inds):
    inds = torch.from_numpy(inds).to(device)
    temp_vis = vis_trans_out.unsqueeze(1).expand(vis_trans_out.shape[0], class_embedding.shape[0], vis_trans_out.shape[1])
    temp_cemb = class_embedding.unsqueeze(0).expand(vis_trans_out.shape[0], class_embedding.shape[0], vis_trans_out.shape[1])
    preds = torch.argmax(torch.sum(temp_vis*temp_cemb, axis=2), -1)
    acc = torch.sum(inds[preds] == target).item()/(preds.shape[0])
    # print(torch.sum(inds[preds] == target).item())
    return acc, torch.sum(temp_vis*temp_cemb, axis=2)

def ce_accuracy(output, target):
    batch_size = target.size(0)
    _, pred = output.topk(1, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))
    correct = correct.view(-1).float().sum(0, keepdim=True)

    return correct.mul_(100.0 / batch_size)

def get_text_data(target, s2v_labels):
    return s2v_labels[target].view(target.shape[0], 1024)


def get_w2v_data(target_nouns, w2v_model, nouns):
    
    srt_inp = torch.zeros([target_nouns.shape[0], 1, target_nouns.shape[1]]).float()
    noun_inp = target_nouns.view(target_nouns.shape[0], 1, target_nouns.shape[1]).float()
    inp = torch.cat([srt_inp, noun_inp], 1)
    return inp

def ce_accuracy(output, target):
    batch_size = target.size(0)
    _, pred = output.topk(1, 1, True, True)
    pred = pred.t()
    correct = pred.eq(target.view(1, -1).expand_as(pred))
    correct = correct.view(-1).float().sum(0, keepdim=True)
    return correct.mul_(100.0 / batch_size)

def zsl_validate(val_loader, visae, attae, epoch):
    with torch.no_grad():
        losses = AverageMeter()
        acces = AverageMeter()
        ce_loss_vals = []
        ce_acc_vals = []
        visae.eval()
        attae.eval()
        scores = []
        class_embeddings = attae(unseen_s2v_labels.to(device).float())[2]
        for i, (emb, target) in enumerate(val_loader):
            emb = emb.to(device)
            vis_emb = torch.log(1 + emb)
        
            vis_hidden, vis_out, vis_trans_out = visae(vis_emb)
            vis_recons_loss = criterion2(vis_out, vis_emb)
            
            att_emb = get_text_data(target, s2v_labels.float()).to(device)
            att_hidden, att_out, att_trans_out = attae(att_emb)
            att_recons_loss = criterion2(att_out, att_emb)
            
            # mmd loss
            loss_mmd = MMDLoss(vis_hidden, att_hidden).to(device)
            
            
            # supervised binary prediction loss
            pred_loss = multi_class_hinge_loss(vis_trans_out, att_trans_out, target, class_embeddings)
            loss = pred_loss + vis_recons_loss + att_recons_loss + loss_mmd
            acc, score = accuracy(class_embeddings, vis_trans_out, target.to(device), unseen_inds)
            losses.update(loss.item(), emb.size(0))
            acces.update(acc, emb.size(0))
            scores.append(score)
            ce_loss_vals.append(loss.cpu().detach().numpy())
            ce_acc_vals.append(acc)
            if i % 20 == 0:
                print('ZSL Validation Epoch-{:<3d} {:3d}/{:3d} batches \t'
                      'loss {loss.val:.4f} ({loss.avg:.4f})\t'
                      'accu {acc.val:.3f} ({acc.avg:.3f})\t'.format(
                       epoch, i , len(val_loader), loss=losses, acc=acces))
                print('Vis Reconstruction Loss {:.4f}\t'
                      'Att Reconstruction Loss {:.4f}\t'
                      'MMD Loss {:.4f}\t'
                      'Supervised Binary Prediction Loss {:.4f}'.format(
                       vis_recons_loss.item(), att_recons_loss.item(), loss_mmd.item(), pred_loss.item()))
                
    return losses.avg, acces.avg, scores

def validate(train_loader, epoch, margin):

    losses = AverageMeter()
    acces = AverageMeter()
    ce_loss_vals = []
    ce_acc_vals = []
    scores = []
    for i, (inputs, target) in enumerate(train_loader):
        # (inputs, target) = next(iter(train_loader))
        emb = inputs.to(device)
        scores.append(emb)
    return scores

zsl_loss, zsl_acc, test_zs = zsl_validate(val_loader, visae, attae, 0)
test_seen = np.load(dataset_path + '/gtest_out.npy')

tars = []
for i, (_, target) in enumerate(val_loader):
    tars += list(target.numpy())

test_y = []
for i in tars:
    if i in unseen_inds:
        test_y.append(0)
    else:
        test_y.append(1)

test_zs = np.array([j.cpu().detach().numpy() for i in test_zs for j in i])

def temp_scale(seen_features, T):
    return np.array([np.exp(i)/np.sum(np.exp(i)) for i in (seen_features + 1e-12)/T])

prob_test_zs = np.array([np.exp(i)/np.sum(np.exp(i)) for i in test_zs])
prob_test_seen = temp_scale(test_seen, temp)
main_prob_test_seen = np.array([np.exp(i)/np.sum(np.exp(i)) for i in test_seen])
feat_test_zs = np.sort(prob_test_zs, 1)[:,::-1][:,:ss]
feat_test_seen = np.sort(prob_test_seen, 1)[:,::-1][:,:ss]

gating_test_x = np.concatenate([feat_test_zs, feat_test_seen], 1)
gating_test_y = test_y

with open(wdir + '/' + le + '/gating_model.pkl', 'rb') as f:
    gating_model = pkl.load(f)

prob_gate = gating_model.predict_proba(gating_test_x)
pred_test = 1 - prob_gate[:, 0]>thresh
a = prob_gate
b = np.zeros(prob_gate.shape[0])
p_gate_seen = prob_gate[:, 1]
prob_y_given_seen = main_prob_test_seen + (1/num_classes)*np.repeat((1 - p_gate_seen)[:, np.newaxis], num_classes, 1)

p_gate_unseen = prob_gate[:, 0]
prob_y_given_unseen = prob_test_zs + (1/ss)*np.repeat((1 - p_gate_unseen)[:, np.newaxis], ss, 1)

prob_seen = prob_y_given_seen*np.repeat(p_gate_seen[:, np.newaxis], num_classes, 1)
prob_unseen = prob_y_given_unseen*np.repeat(p_gate_unseen[:, np.newaxis], ss, 1)

final_preds = []

seen_count = 0
tot_seen = 0
unseen_count = 0
tot_unseen = 0
count = 0
for i in range(len(prob_seen)):
    if pred_test[i] == 1:
        pred = np.argmax(main_prob_test_seen[i, :])
    else:
        pred = unseen_inds[np.argmax(prob_test_zs[i, :])]
    
    if tars[i] in seen_inds:
        tot_seen += 1
        if pred == tars[i]:
            seen_count += 1
    else:
        if pred_test[i] == 0:
            count+=1
        tot_unseen += 1
        if pred == tars[i]:
            unseen_count += 1
#     final_preds.append(pred)

seen_acc = seen_count/tot_seen
unseen_acc = unseen_count/tot_unseen
h_mean = 2*seen_acc*unseen_acc/(seen_acc + unseen_acc)

print('seen accuracy', seen_acc)
print('unseen accuracy', unseen_acc)
print('harmonic mean', h_mean)