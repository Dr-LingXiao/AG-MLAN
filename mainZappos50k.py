import argparse
import os
import sys
import cv2
import json
import time
import shutil
import logging
import numpy as np
from PIL import Image
Image.MAX_IMAGE_PIXELS = None
from visdom import Visdom
import matplotlib.cm as cm
from torch.autograd import Variable
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim
from torchvision import transforms
import torch.backends.cudnn as cudnn
from triplet_image_loader import TripletImageLoader as TripletImageLoader1
import resnet
from metric import APScorer
from model2 import get_model
from image_loader import TripletImageLoader, ImageLoader, MetaLoader
torch.set_num_threads(4)
# Command Line Argument Parser
parser = argparse.ArgumentParser(description='Attribute-Specific Embedding Network')
parser.add_argument('--batch-size', type=int, default=16, metavar='N',
                    help='input batch size for training (default: 16)')
parser.add_argument('--epochs', type=int, default=50, metavar='N',
                    help='number of epochs to train (default: 50)')
parser.add_argument('--start_epoch', type=int, default=1, metavar='N',
                    help='number of start epoch (default: 1)')
parser.add_argument('--lr', type=float, default=0.0001, metavar='LR',
                    help='learning rate (default: 1e-4)')
parser.add_argument('--seed', type=int, default=1, metavar='S',
                    help='random seed (default: 1)')
parser.add_argument('--no-cuda', action='store_true', default=False,
                    help='disable CUDA training')
parser.add_argument('--log-interval', type=int, default=400, metavar='N',
                    help='how many batches to wait before logging training status')
parser.add_argument('--margin', type=float, default=0.2, metavar='M',
                    help='margin for triplet loss (default: 0.3)')
parser.add_argument('--resume', default=None, type=str,
                    help='path to latest checkpoint (default: none)')
parser.add_argument('--name', default='AG_MLAN', type=str,
                    help='name of experiment')
parser.add_argument('--num_triplets', type=int, default=200000, metavar='N',
                    help='how many unique training triplets (default: 100000)')
parser.add_argument('--dim_embed', type=int, default=1024, metavar='N',
                    help='dimensions of embedding (default: 1024)')
parser.add_argument('--test', dest='test', action='store_true',
                    help='inference on test set')
parser.add_argument('--visdom', dest='visdom', action='store_true',
                    help='Use visdom to track and plot')
parser.add_argument('--visdom_port', type=int, default=4655, metavar='N',
                    help='visdom port')
parser.add_argument('--data_path', default="data", type=str,
                    help='path to data directory')
parser.add_argument('--dataset', default="Zappos50k", type=str,
                    help='name of dataset')
parser.add_argument('--model', default="AG_MLAN", type=str,
                    help='model to load')
parser.add_argument('--step_size', type=int, default=3, metavar='N',
                    help='learning rate decay step size')
parser.add_argument('--decay_rate', type=float, default=0.9, metavar='N',
                    help='learning rate decay rate')
parser.add_argument('--conditions', nargs='*', type=int,
                    help='Set of similarity notions')
parser.set_defaults(test=False)
parser.set_defaults(visdom=False)


def train(train_loader, tnet, criterion, optimizer, epoch,awl):

    losses = AverageMeter()
    accs = AverageMeter()

    # switch to train mode
    tnet.train()
    for batch_idx, (data1, data2, data3, c) in enumerate(train_loader):
        if args.cuda:
            data1, data2, data3, c = data1.cuda(), data2.cuda(), data3.cuda(), c.cuda()
  
        # compute similarity
        sim_a, sim_b = tnet(data1, data2, data3, c)

        # -1 means, sim_a should be smaller than sim_b
        target = torch.FloatTensor(sim_a.size()).fill_(-1)
        if args.cuda:
            target = target.cuda()
        
        loss_triplet = criterion(sim_a, sim_b, target)
        loss = loss_triplet

        # measure accuracy and record loss
        acc = accuracy(sim_a, sim_b)
        losses.update(loss.data.item(), data1.size(0))
        accs.update(acc, data1.size(0))

        # compute gradient and do optimizer step
        optimizer.zero_grad()
        loss.backward()
        optimizer.step()

        if batch_idx % args.log_interval == 0:
            logger.info('w Epoch: {} [{}/{}]\t'
                  'Loss: {:.4f} ({:.4f}) \t'
                  'Acc: {:.2f}% ({:.2f}%)'.format(
                epoch, batch_idx * len(data1), len(train_loader.dataset),
                losses.val, losses.avg, 
                100. * accs.val, 100. * accs.avg))

    # log avg values to visdom
    if args.visdom:
        plotter.plot('acc', 'train', epoch, accs.avg)
        plotter.plot('loss', 'loss', epoch, losses.avg)

def test(test_loader, tnet, criterion, epoch):
    losses = AverageMeter()
    accs = AverageMeter()
    # accs_cs = {}
    # for condition in conditions:
    #     accs_cs[condition] = AverageMeter()

    # switch to evaluation mode
    tnet.eval()
    for batch_idx, (data1, data2, data3, c) in enumerate(test_loader):
        # print(batch_idx)
        if args.cuda:
            data1, data2, data3, c = data1.cuda(), data2.cuda(), data3.cuda(), c.cuda()
        data1, data2, data3, c = Variable(data1), Variable(data2), Variable(data3), Variable(c)
        c_test = c

        # compute output
        dista, distb = tnet(data1, data2, data3, c)
        target = torch.FloatTensor(dista.size()).fill_(-1)
        if args.cuda:
            target = target.cuda()

        test_loss = criterion(dista, distb, target).data.item()

        # measure accuracy and record loss
        acc = accuracy(dista, distb)
        accs.update(acc, data1.size(0))
        # for condition in conditions:
        #     accs_cs[condition].update(accuracy_id(dista, distb, c_test, condition), data1.size(0))
        losses.update(test_loss, data1.size(0))

        # for condition in conditions:
    #    print('sim ' + str(condition) + ': ' + str(accs_cs[condition].avg))

    print('\nTest set: Average loss: {:.4f}, Accuracy: {:.2f}%\n'.format(
        losses.avg, 100. * accs.avg))
    return accs.avg



def save_checkpoint(state, is_best, filename='checkpoint.pth.tar'):
    """Saves checkpoint to disk"""
    directory = "runs_Zappos50k/%s/"%(args.name)
    if not os.path.exists(directory):
        os.makedirs(directory)
    filename = directory + filename
    torch.save(state, filename)
    if is_best:
        shutil.copyfile(filename, 'runs_Zappos50k/%s/'%(args.name) + 'model_best.pth.tar')


class VisdomLinePlotter(object):
    # Plots to Visdom
    def __init__(self, env_name='main'):
        self.viz = Visdom(port=args.visdom_port)
        self.env = env_name
        self.plots = {}

    # plot curve graph
    def plot(self, var_name, split_name, x, y):
        if var_name not in self.plots:
            self.plots[var_name] = self.viz.line(X=np.array([x,x]), Y=np.array([y,y]), env=self.env, opts=dict(
                legend=[split_name],
                title=var_name,
                xlabel='Epochs',
                ylabel=var_name
            ))
        else:
            self.viz.line(X=np.array([x]), Y=np.array([y]), env=self.env, win=self.plots[var_name], name=split_name, update='append')

    # plot attention map
    def plot_attention(self, imgs, heatmaps, tasks, alpha=0.5):
        global meta

        for i in range(len(tasks)):
            heatmap = heatmaps[i]
            heatmap = cv2.resize(heatmap, (224,224), interpolation=cv2.INTER_CUBIC)
            heatmap = np.maximum(heatmap, 0)
            heatmap /= np.max(heatmap)
            heatmap_marked = np.uint8(cm.gist_rainbow(heatmap)[..., :3] * 255)
            heatmap_marked = cv2.cvtColor(heatmap_marked, cv2.COLOR_BGR2RGB)
            heatmap_marked = np.uint8(imgs[i] * alpha + heatmap_marked * (1. - alpha))
            heatmap_marked = heatmap_marked.transpose([2,0,1])

            win_name = 'img %d - %s'%(i,meta.data['ATTRIBUTES'][tasks[i]])
            if win_name not in self.plots:
                self.plots[win_name] = self.viz.image(
                    heatmap_marked,
                    env=self.env,
                    opts=dict(
                        title=win_name
                    )
                )
                self.plots[win_name+'heatmap'] = self.viz.heatmap(
                    heatmap,
                    env=self.env,
                    opts=dict(
                        title=win_name
                    )
                )
            else:
                self.viz.image(
                    heatmap_marked,
                    env=self.env,
                    win =self.plots[win_name],
                    opts=dict(
                        title=win_name
                    )
                )
                self.viz.heatmap(
                    heatmap,
                    env=self.env,
                    win=self.plots[win_name+'heatmap'],
                    opts=dict(
                        title=win_name
                    )
                )


class AverageMeter(object):
    """Computes and stores the average and current value"""
    def __init__(self):
        self.reset()

    def reset(self):
        self.val = 0
        self.avg = 0
        self.sum = 0
        self.count = 0

    def update(self, val, n=1):
        self.val = val
        self.sum += val * n
        self.count += n
        self.avg = self.sum / self.count


def accuracy(sim_a, sim_b):
    # triplet prediction acc
    margin = 0
    pred = (sim_b - sim_a - margin).cpu().data
    return float((pred > 0).sum())/float(sim_a.size()[0])


def mean_average_precision(cand_set, queries, c_gdtruth, q_gdtruth):
    '''
    calculate mAP of a conditional set. Samples in candidate and query set are of the same condition.
        cand_set: 
            type:   nparray
            shape:  c x feature dimension
        queries:
            type:   nparray
            shape:  q x feature dimension
        c_gdtruth:
            type:   nparray
            shape:  c
        q_gdtruth:
            type:   nparray
            shape:  q
    '''
 
    scorer = APScorer(cand_set.shape[0])

    # similarity matrix
    simmat = np.matmul(queries, cand_set.T)

    ap_sum = 0
    for q in range(simmat.shape[0]):
        sim = simmat[q]
        index = np.argsort(-sim)
        sorted_labels = []
        for i in range(index.shape[0]):
            if c_gdtruth[index[i]] == q_gdtruth[q]:
                sorted_labels.append(1)
            else:
                sorted_labels.append(0)
        
        ap = scorer.score(sorted_labels)
        ap_sum += ap

    mAP = ap_sum / simmat.shape[0]

    return mAP


def set_logger():
    logger = logging.getLogger(__name__)
    logger.setLevel(level=logging.INFO)

    formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')

    logfile = args.model+'.log' if not args.test else args.model+'_test.log'
    file_handler = logging.FileHandler(logfile, 'w')
    file_handler.setLevel(logging.INFO)
    file_handler.setFormatter(formatter)

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)

    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)

    return logger


def main():

    global args
    args = parser.parse_args()

    global logger
    logger = set_logger()

    args.cuda = not args.no_cuda and torch.cuda.is_available()
    torch.manual_seed(args.seed)
    global conditions
    if args.conditions is not None:
        conditions = args.conditions
    else:
        conditions = [0, 1, 2, 3]
    if args.cuda:
        torch.cuda.manual_seed(args.seed)

    if args.visdom:
        global plotter
        plotter = VisdomLinePlotter(env_name=args.name)

    # global meta
    # meta = MetaLoader(args.data_path, args.dataset)

    # global attributes
    # attributes = [i for i in range(len(meta.data['ATTRIBUTES']))]

    # backbone = resnet()
    criterion1 = torch.nn.BCEWithLogitsLoss()
    awl = resnet.AutomaticWeightedLoss(2)
    backbone = resnet.resnet50(len(conditions))
    enet = get_model(args.model)(backbone, n_attributes=len(conditions), embedding_size=args.dim_embed)
    tnet = get_model('Tripletnet')(enet, criterion1,len(conditions))
    if args.cuda:
        tnet.cuda()
        awl.cuda()

    criterion = torch.nn.MarginRankingLoss(margin = args.margin)
    n_parameters = sum([p.data.nelement() for p in tnet.parameters()])
    logger.info('  + Number of params: {}'.format(n_parameters))

    # optionally resume from a checkpoint
    if args.resume:
        if os.path.isfile(args.resume):
            logger.info("=> loading checkpoint '{}'".format(args.resume))
            checkpoint = torch.load(args.resume)
            args.start_epoch = checkpoint['epoch'] + 1
            mAP = checkpoint['prec']
            tnet.load_state_dict(checkpoint['state_dict'])
            logger.info("=> loaded checkpoint '{}' (epoch {} mAP on validation set {})"
                    .format(args.resume, checkpoint['epoch'], mAP))
        else:
            logger.info("=> no checkpoint found at '{}'".format(args.resume))

    cudnn.benchmark = True

    kwargs = {'num_workers': 4, 'pin_memory': True} if args.cuda else {}
    normalize = transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                     std=[0.229, 0.224, 0.225])

    if args.test:
        test_loader = torch.utils.data.DataLoader(
            TripletImageLoader1('data', 'ut-zap50k-images', 'filenames.json',
                               conditions, 'test', n_triplets=160000,
                               transform=transforms.Compose([
                                   transforms.Resize(224),
                                   transforms.CenterCrop(224),
                                   transforms.ToTensor(),
                                   normalize,
                               ])),
            batch_size=args.batch_size, shuffle=False, **kwargs)
        test_mAP = test(test_loader, tnet, criterion, 1)
        sys.exit()

    parameters = filter(lambda p: p.requires_grad, tnet.parameters())
    optimizer = optim.Adam(parameters, lr=args.lr)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=args.step_size, gamma=args.decay_rate)
    train_loader = torch.utils.data.DataLoader(
        TripletImageLoader1('data', 'ut-zap50k-images', 'filenames.json',
                           conditions, 'train', n_triplets=args.num_triplets,
                           transform=transforms.Compose([
                               transforms.Resize(224),
                               transforms.CenterCrop(224),
                               transforms.RandomHorizontalFlip(),
                               transforms.ToTensor(),
                               normalize,
                           ])),
        batch_size=args.batch_size, shuffle=True, **kwargs)

    val_loader = torch.utils.data.DataLoader(
        TripletImageLoader1('data', 'ut-zap50k-images', 'filenames.json',
                           conditions, 'val', n_triplets=80000,
                           transform=transforms.Compose([
                               transforms.Resize(224),
                               transforms.CenterCrop(224),
                               transforms.ToTensor(),
                               normalize,
                           ])),
        batch_size=args.batch_size, shuffle=False, **kwargs)
    logger.info("Begin training on {} dataset.".format(args.dataset))

    best_mAP = 0
    start = time.time()
    for epoch in range(args.start_epoch, args.epochs + 1):
        # train for one epoch
        train(train_loader, tnet, criterion, optimizer, epoch, awl)
        # train_loader.dataset.refresh()
        # evaluate on validation set
        mAP = test(val_loader, tnet, criterion, epoch)

        # remember best meanAP and save checkpoint
        is_best = mAP > best_mAP
        best_mAP = max(mAP, best_mAP)
        save_checkpoint({
            'epoch': epoch,
            'state_dict': tnet.state_dict(),
            'prec': mAP,
        }, is_best)

        # update learning rate
        scheduler.step()
        for param in optimizer.param_groups:
            logger.info('lr:{}'.format(param['lr']))
            break

    end = time.time()
    duration = int(end - start)
    minutes = (duration // 60) % 60
    hours = duration // 3600
    logger.info('training time {}h {}min'.format(hours, minutes))


if __name__ == '__main__':
    main()