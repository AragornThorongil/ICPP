# -*- coding: utf-8 -*-

import argparse
import os
import random
import sys
import time
from collections import OrderedDict
from multiprocessing.managers import BaseManager

import torch
import torch.distributed.deprecated as dist
from cjltest.models import MnistCNN, AlexNetForCIFAR
from cjltest.utils_data import get_data_transform
from cjltest.utils_model import test_model
from torch.multiprocessing import Process as TorchProcess
from torch.multiprocessing import Queue
from torch.utils.data import DataLoader
from torchvision import datasets, models, transforms
import ResNetOnCifar10

parser = argparse.ArgumentParser()
# Information of the cluster
parser.add_argument('--ps-ip', type=str, default='127.0.0.1')
parser.add_argument('--ps-port', type=str, default='29500')
parser.add_argument('--this-rank', type=int, default=0)
parser.add_argument('--workers-num', type=int, default=2)

# Model and Dataset
parser.add_argument('--data-dir', type=str, default='~/dataset')
parser.add_argument('--model', type=str, default='MnistCNN')

# Hyper-parameters
parser.add_argument('--timeout', type=float, default=10000000.0)
parser.add_argument('--epochs', type=int, default=1)
parser.add_argument('--train-bsz', type=int, default=200)
parser.add_argument('--stale-threshold', type=int, default=1000)

args = parser.parse_args()

# SSP
def run(model, test_data, queue, param_q, stop_signal, train_pics):
    if args.model == 'MnistCNN':
        criterion = torch.nn.NLLLoss()
    else:
        criterion = torch.nn.NLLLoss()

    # Transform tensor to numpy
    tmp = map(lambda item: (item[0], item[1].numpy()), model.state_dict().items())
    _tmp = OrderedDict(tmp)
    workers = [v+1 for v in range(args.workers_num)]
    for _ in workers:
        param_q.put(_tmp)
    print('Model Sent Finished!')

    print('Begin!')

    epoch_train_loss = 0
    iteration_in_epoch = 0
    data_size_epoch = 0   # len(train_data), one epoch
    epoch_count = 0
    staleness_sum_suqare_epoch = 0
    staleness_sum_epoch = 0

    staleness = 0 # the global clock
    learner_staleness = {l: 0 for l in workers}
    s_time = time.time()
    epoch_time = s_time

    # In SSP, the fast workers have to wait the slowest worker a given duration
    # The fast worker exceeding the duration will be pushed into the queue to wait
    stale_stack = []

    trainloss_file = './trainloss' + args.model + '.txt'
    staleness_file = './staleness' + args.model + ".txt"

    if(os.path.isfile(trainloss_file)):
        os.remove(trainloss_file)
    if(os.path.isfile(staleness_file)):
        os.remove(staleness_file)
    f_trainloss = open(trainloss_file, 'a')
    f_staleness = open(staleness_file, 'a')
    global_clock = 0
    while True:
        if not queue.empty():
            it_start_time = time.time()

            tmp_dict = queue.get()
            rank_src = list(tmp_dict.keys())[0]
            isWorkerEnd = tmp_dict[rank_src][2]
            if isWorkerEnd:
                print("Worker {} has completed all its data computation!".format(rank_src))
                learner_staleness.pop(rank_src)
                if (len(learner_staleness) == 0):
                    f_trainloss.close()
                    f_staleness.close()
                    stop_signal.put(1)
                    print('Epoch is done: {}'.format(epoch_count))
                    break
                continue

            stale = int(staleness - learner_staleness[rank_src])
            staleness_sum_epoch += stale
            # staleness_sum_suqare_epoch += stale**2
            staleness_sum_suqare_epoch += 0.0
            staleness += 1
            learner_staleness[rank_src] = staleness
            stale_stack.append(rank_src)

            # recv gradients of the worker and update current model
            for idx, param in enumerate(model.parameters()):
                tmp_tensor = torch.zeros_like(param.data)
                dist.recv(tensor=tmp_tensor, src=rank_src)
                param.data -= tmp_tensor/(stale+1)

            global_clock += 1
            iteration_loss = tmp_dict[rank_src][0]
            batch_size = tmp_dict[rank_src][1]

            iteration_in_epoch += 1
            epoch_train_loss += iteration_loss
            data_size_epoch += batch_size


            # judge if the staleness exceed the staleness threshold in SSP
            outOfStale = False
            for stale_each_worker in learner_staleness:
                if (stale_each_worker not in stale_stack) & \
                    (staleness - learner_staleness[stale_each_worker] > args.stale_threshold):
                    outOfStale = True
                    break
            if not outOfStale:
                for i in range(len(stale_stack)):
                    rank_wait = stale_stack.pop()
                    # update the value of staleness in the corresponding learner
                    learner_staleness[rank_wait] = staleness
                    for idx, param in enumerate(model.parameters()):
                        dist.send(tensor=param.data, dst=rank_wait)
            else:
                continue


            it_end_time = time.time()
            f_staleness.write(str(epoch_count) +
                        "\t" + str(rank_src) +
                        "\t" + str(batch_size) +
                        "\t" + str(stale) +
                        "\t" + str(it_end_time - it_start_time) +
                        "\t" + str(global_clock) +
                        '\n')
            f_staleness.flush()

            # once reach an epoch, count the average train loss
            if(data_size_epoch >= train_pics):
                e_epoch_time = time.time()
                #variance of stale
                diversity_stale = (staleness_sum_suqare_epoch/iteration_in_epoch)\
                                 - (staleness_sum_epoch/iteration_in_epoch)**2
                staleness_sum_suqare_epoch = 0
                staleness_sum_epoch = 0
                #test_loss, test_acc = test_model(dist.get_rank(), model, test_data, criterion=criterion)
                test_acc = 0.0
                test_acc = 0.0
                # rank, trainloss, variance of stalness, time in one epoch, time till now
                f_trainloss.write(str(args.this_rank) +
                                  "\t" + str(epoch_train_loss/float(iteration_in_epoch)) +
                                  "\t" + str(diversity_stale) +
                                  "\t" + str(e_epoch_time - epoch_time) +
                                  "\t" + str(e_epoch_time - s_time) +
                                  "\t" + str(epoch_count) +
                                  "\t" + str(test_acc) +
                                  "\t" + str(global_clock) +
                                  '\n')
                f_trainloss.flush()
                iteration_in_epoch = 0
                epoch_count += 1
                epoch_train_loss = 0
                data_size_epoch = 0
                epoch_time = e_epoch_time

            # The training stop
            if(epoch_count >= args.epochs):
                f_trainloss.close()
                f_staleness.close()
                stop_signal.put(1)
                print('Epoch is done: {}'.format(epoch_count))
                break

        e_time = time.time()
        if (e_time - s_time) >= float(args.timeout):
            f_trainloss.close()
            f_staleness.close()
            stop_signal.put(1)
            print('Time up: {}, Stop Now!'.format(e_time - s_time))
            break


def init_processes(rank, size, model, test_data, queue, param_q, stop_signal, train_pics, fn, backend='tcp'):
    os.environ['MASTER_ADDR'] = args.ps_ip
    os.environ['MASTER_PORT'] = args.ps_port
    dist.init_process_group(backend, rank=rank, world_size=size)
    fn(model, test_data, queue, param_q, stop_signal, train_pics)


if __name__ == "__main__":
    manual_seed = 1
    random.seed(manual_seed)
    torch.manual_seed(manual_seed)

    if args.model == 'MnistCNN':
        model = MnistCNN()
        train_t, test_t = get_data_transform('mnist')
        test_dataset = datasets.MNIST(args.data_dir, train=False, download=False,
                                      transform=test_t)
        train_pics = 60000
    elif args.model == 'AlexNet':
        model = AlexNetForCIFAR()
        train_t, test_t = get_data_transform('cifar')
        test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                        transform=test_t)
        train_pics = 50000
    elif args.model == 'LROnMnist':
        model = ResNetOnCifar10.LROnMnist()
        train_transform, test_transform = get_data_transform('mnist')
        test_dataset = datasets.MNIST(args.data_dir, train=False, download=False,
                                      transform=test_transform)
        train_pics = 60000
    elif args.model == 'LROnCifar10':
        model = ResNetOnCifar10.LROnCifar10()
        train_transform, test_transform = get_data_transform('cifar')
        test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                      transform=test_transform)
        train_pics = 50000
    elif args.model == 'ResNet18OnCifar10':
        model = ResNetOnCifar10.ResNet18()

        train_transform, test_transform = get_data_transform('cifar')
        test_dataset = datasets.CIFAR10(args.data_dir, train=False, download=False,
                                        transform=test_transform)
        train_pics = 50000
    elif args.model == 'ResNet34':
        model = models.resnet34(pretrained=False)

        test_transform, train_transform = transforms.Compose([
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                 std=[0.229, 0.224, 0.225])
        ])
        test_dataset = datasets.ImageFolder(args.data_dir, train=False, download=False,
                                        transform=test_transform)
        train_pics = 121187
    else:
        print('Model must be {} or {}!'.format('MnistCNN', 'AlexNet'))
        sys.exit(-1)

    test_data = DataLoader(test_dataset, batch_size=100, shuffle=True)

    world_size = args.workers_num + 1
    this_rank = args.this_rank

    queue = Queue()
    param = Queue()
    stop_or_not = Queue()


    class MyManager(BaseManager):
        pass


    MyManager.register('get_queue', callable=lambda: queue)
    MyManager.register('get_param', callable=lambda: param)
    MyManager.register('get_stop_signal', callable=lambda: stop_or_not)
    manager = MyManager(address=(args.ps_ip, 5000), authkey=b'queue')
    manager.start()

    q = manager.get_queue()  # Queue receiving the models
    param_q = manager.get_param()  # Queue receiving the initial models
    stop_signal = manager.get_stop_signal()  # Queue receiving the stop signal

    p = TorchProcess(target=init_processes, args=(this_rank, world_size, model,test_data,
                                                  q, param_q, stop_signal, train_pics, run))
    p.start()
    p.join()
    manager.shutdown()
