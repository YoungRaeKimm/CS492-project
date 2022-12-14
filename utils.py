import torch
from torch.utils.data import DataLoader, Dataset
import torchvision.transforms as transforms
from continuum import ClassIncremental
from continuum.datasets import CIFAR100, TinyImageNet200, ImageNet100
import random
import numpy as np
import termcolor
import os

'''random seed'''
import random
seed = 1234
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)


def toRed(content):
    return termcolor.colored(content,"red",attrs=["bold"])

def toGreen(content):
    return termcolor.colored(content,"green",attrs=["bold"])

def toBlue(content):
    return termcolor.colored(content,"blue",attrs=["bold"])

'''return dataset transforms according to the dataset'''
def get_transforms(dataset, test=False):
    
    if dataset == 'cifar100':
        transform = [
                transforms.RandomCrop(32, padding=4),
                transforms.RandomHorizontalFlip(),
                transforms.ToTensor(),
                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]

        transform_test = [
                transforms.ToTensor(),
                transforms.Normalize((0.5, 0.5, 0.5), (0.5, 0.5, 0.5)),
        ]
    else:
        transform = [
            transforms.RandomResizedCrop(224),
            transforms.RandomHorizontalFlip(),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225])
        ]

        transform_test = [
            transforms.Resize(256),
            transforms.CenterCrop(224),
            transforms.ToTensor(),
            transforms.Normalize(mean=[0.485, 0.456, 0.406],
                                std=[0.229, 0.224, 0.225])
        ]
        
    if test == False:
        return transform
    else:
        return transform_test

'''Using incremental dataset library continuum'''
def IncrementalDataLoader(dataset_name, data_path, train, n_split, task_id, batch_size, transform):
    '''random seed'''
    seed = 1234
    random.seed(seed)
    np.random.seed(seed)
    torch.manual_seed(seed)
    torch.cuda.manual_seed_all(seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False
        
    if task_id >= n_split:
        print(f'task id {task_id} > n_split {n_split}')
        return False

    dataset_name = dataset_name.lower()
    n_classes = 100
    if dataset_name == 'cifar100':
        dataset = CIFAR100(data_path, train=train)
    elif dataset_name == 'tinyimagenet200':
        dataset = TinyImageNet200(data_path, train=train)
        n_classes = 200
    elif dataset_name == 'imagenet100':
        dataset = ImageNet100(data_path, train=train)
    else:
        print('invalid dataset : ', dataset_name)
        return False

    scenario = ClassIncremental(dataset, increment=n_classes//n_split, transformations=transform)
    loader = DataLoader(scenario[task_id], batch_size = batch_size, shuffle=True, drop_last=True)
    return loader

'''
Equation (8) in paper
Store the examplars considering this confidence score value.
'''
def confidence_score(z, c):
    B = z.shape[0]
    score = torch.zeros(B)
    for i in range(B):
        score[i] = torch.exp(z[i, c[i]]) / (torch.exp(z[i, :])).sum()
    return score

'''
Memory Buffer
It stores data(x), label(y), task(t), logit value(z) from previous task.
k is the size per label.
The size of memory is fixed, therefore have to update the stored values
considering the size per class.
In other words, if new class is newly registered to memory buffer,
then remove before examplars and insert new impressive class data.
'''
class MemoryDataset(Dataset):
    def __init__(self, x, y, t, z, k):
        self.x = x
        self.y = y
        self.t = t
        self.z = z
        self.k = k
        self.size = len(self.x)
    
    def __len__(self):
        return self.size
    
    def __getitem__(self, idx):
        return self.x[idx], self.y[idx], self.t[idx], self.z[idx]

    def remove_examplars(self, new_k):
        new_x = torch.zeros_like(self.x)
        new_y = torch.zeros_like(self.y)
        new_t = torch.zeros_like(self.t)
        new_z = torch.zeros_like(self.z)

        for i, start in enumerate(range(0, self.size, self.k)):
            if start+new_k > self.size:
                break
            new_x[new_k*i:new_k*(i+1)] = self.x[start:start+new_k]
            new_y[new_k*i:new_k*(i+1)] = self.y[start:start+new_k]
            new_t[new_k*i:new_k*(i+1)] = self.t[start:start+new_k]
            new_z[new_k*i:new_k*(i+1)] = self.z[start:start+new_k]

        self.x = new_x
        self.y = new_y
        self.t = new_t.type(torch.LongTensor)
        self.z = new_z
        self.k = new_k
    
    def update_memory(self, label, new_x, new_y, new_t, new_z):
        self.x[label*self.k:(label+1)*self.k,...] = new_x
        self.y[label*self.k:(label+1)*self.k] = new_y
        self.t[label*self.k:(label+1)*self.k] = new_t
        self.z[label*self.k:(label+1)*self.k,...] = new_z