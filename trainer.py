import time
import os
import torch
import torch.nn as nn
import torch.optim as optim
import torchvision.transforms as transforms
import pickle as pkl
import random
import numpy as np

from copy import deepcopy
from models.lvt import *
from utils import IncrementalDataLoader, confidence_score, MemoryDataset, toRed, toBlue, toGreen
    

'''random seed'''
seed = 1234
random.seed(seed)
np.random.seed(seed)
torch.manual_seed(seed)
torch.cuda.manual_seed_all(seed)
torch.backends.cudnn.deterministic = True
torch.backends.cudnn.benchmark = False
        
'''dataset transforms'''
transform = [
        transforms.RandomCrop(32, padding=4),
        transforms.RandomHorizontalFlip(),
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
]

transform_test = [
        transforms.ToTensor(),
        transforms.Normalize((0.4914, 0.4822, 0.4465), (0.2023, 0.1994, 0.2010)),
]

'''Recursively initialize the parameters'''
def init_xavier(submodule):
    if isinstance(submodule, torch.nn.Conv2d):
        torch.nn.init.xavier_uniform_(submodule.weight)
        if submodule.bias is not None:
            submodule.bias.data.fill_(0.01)
    elif isinstance(submodule, torch.nn.BatchNorm2d):
        submodule.weight.data.fill_(1.0)
        submodule.bias.data.zero_()
    elif isinstance(submodule, nn.Linear):
        torch.nn.init.xavier_uniform_(submodule.weight)
        if submodule.bias is not None:
            submodule.bias.data.fill_(0.01)
    elif isinstance(submodule, Attention):
        for sm in list(submodule.children()):
            init_xavier(sm)

'''
All training and testing functions are implemented in this class.
'''
class Trainer():
    def __init__(self, config):
        self.log_dir = config.log_dir
        self.dataset = config.dataset
        self.train_epoch = config.epoch
        self.batch_size = config.batch_size
        self.lr = config.lr
        self.split = config.split
        self.memory_size = config.memory_size
        self.ILtype = config.ILtype
        self.data_path = config.data_path
        self.scheduler = config.scheduler
        self.device = torch.device('cuda') if torch.cuda.is_available() else torch.device('cpu')
        self.act = nn.Softmax(dim=1)
        if self.dataset == 'tinyimagenet200':
            self.n_classes = 200
        else:
            self.n_classes = 100
        self.increment = int(self.n_classes//self.split)
        self.resume = config.resume
        self.resume_task = config.resume_task
        self.resume_time = config.resume_time
        self.cur_classes = self.increment
        
        # hyper parameter
        self.num_head = config.num_head             # number of heads in attention 
        self.hidden_dim = config.hidden_dim         # number of hidden dimension in attention
        self.bias = True                            # bias in Transformer or shrink module
        self.alpha = config.alpha                   # coefficient of L_r
        self.beta = config.beta                     # coefficient of L_d
        self.gamma = config.gamma                   # coefficient of L_a
        self.rt = config.rt                         # coefficient of L_At
        self.T = 2.                                 # softmax temperature, which is used in distillation loss
        
        
        '''
        If resume flag is True, then create the LVT and load the saved check point
        If it is False, then create the LVT and initialize the parameters.
        '''
        if config.resume or config.test:
            self.model = LVT(batch=self.batch_size, n_class=self.increment*self.resume_task, IL_type=self.ILtype, dim=512, num_heads=self.num_head, hidden_dim=self.hidden_dim, bias=self.bias, device=self.device).to(self.device)
            cur_dir = os.path.dirname(os.path.realpath(__file__))
            model_name = f'model_{self.resume_time}_task_{self.resume_task-1}.pt'
            self.model = torch.load(os.path.join(os.path.join(cur_dir, self.log_dir, "saved_models", model_name)), map_location=self.device)
            # import pdb; pdb.set_trace()
            self.prev_model = deepcopy(self.model).to(self.device)
            self.model.add_classes(self.increment)
        else:
            self.model = LVT(batch=self.batch_size, n_class=self.increment, IL_type=self.ILtype, dim=512, num_heads=self.num_head, hidden_dim=self.hidden_dim, bias=self.bias, device=self.device).to(self.device)
            self.prev_model = None
            self.model.apply(init_xavier)
        
        '''
        Since dimension of memory depends on the dimension of input image,
        Initialize them on train phase.
        '''
        self.memory = None
        self.optimizer = optim.SGD(self.model.parameters(), lr = self.lr)
        if self.scheduler:
            self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, self.train_epoch/10, 0.1)
            
        
        '''random seed'''
        seed = 1234
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        
    '''
    Save the model and memory according to the task number.
    '''
    def save(self, model, memory, task):
        model_time = time.strftime("%Y%m%d_%H%M")
        model_name = f"model_{model_time}_task_{task}.pt"
        memory_name = f"memory_{model_time}_task_{task}.pt"
        print(f'Model saved as {model_name}')
        print(f'Memory saved as {memory_name}')
        cur_dir = os.path.dirname(os.path.realpath(__file__))
        print(f'Path : {os.path.join(os.path.join(cur_dir, self.log_dir, "saved_models", model_name))}')
        torch.save(model, os.path.join(os.path.join(cur_dir, self.log_dir, "saved_models", model_name)))
        with open(os.path.join(os.path.join(cur_dir, self.log_dir, "saved_models", memory_name)), 'wb') as f:
            pkl.dump(memory, f)
    
    '''
    Core function.
    This function trains the model during whole tasks.
    '''
    def train(self):
        '''
        We use cross entropy loss for getting classification loss
        and KL divergence loss to distillate the knowledge of previous task model.
        '''
        cross_entropy = nn.CrossEntropyLoss()
        kl_divergence = nn.KLDivLoss(log_target=True, reduction='batchmean')

        self.model.train()
        '''
        Task starts.
        '''
        start_task = self.resume_task if self.resume is True else 0
        for task in range(start_task, self.split):
            data_loader = IncrementalDataLoader(self.dataset, self.data_path, True, self.split, task, self.batch_size, transform)
            # print(data_loader)
            # x : (B, 3, 32, 32) | y : (B,) | t : (B,)
            x = data_loader.dataset[0][0]
            K = self.memory_size // (self.increment * (task+1))

            '''
            Initialize memory buffer.
            If resume flag is True, load the saved memory.
            Else make memory.
            '''
            if self.memory is None:
                if self.resume:
                    cur_dir = os.path.dirname(os.path.realpath(__file__))
                    memory_name = f'memory_{self.resume_time}_task_{self.resume_task-1}.pt'
                    with open(os.path.join(os.path.join(cur_dir, self.log_dir, "saved_models", memory_name)), 'rb') as f:
                        self.memory = pkl.load(f)
                else:
                    self.memory = MemoryDataset(
                        torch.zeros(self.memory_size, *x.shape),
                        torch.zeros(self.memory_size),
                        torch.zeros(self.memory_size),
                        K
                    )
                # else:
            #     memory_loader = DataLoader(MemoryDataset, batch_size=self.batch_size, shuffle=True)

            
            '''
            In LVT paper, the authors said that the gradient values of key and bias of attention module 
            represents the importance the last task. (equation (2))
            The average value of gradient is calculated in here.
            '''
            if task > 0:
                prev_avg_K_grad = None
                prev_avg_bias_grad = None
                length = 0
                for x, y, _ in data_loader:
                    length += 1
                    x = x.to(device=self.device)
                    y = y.to(device=self.device)
                    shift_y = torch.full_like(y, task*self.increment)
                    y = y - shift_y

                    inj_logit = self.prev_model.forward_inj(self.prev_model.forward_backbone(x))
                    # cross_entropy(inj_logit, y).backward()
                    if prev_avg_K_grad is not None:
                        cross_entropy(inj_logit, y).backward()
                        prev_avg_K_grad += self.prev_model.get_K_grad()
                        prev_avg_bias_grad += self.prev_model.get_bias_grad()
                    else:
                        cross_entropy(inj_logit, y).backward()
                        prev_avg_K_grad = self.prev_model.get_K_grad()
                        prev_avg_bias_grad = self.prev_model.get_bias_grad()
                prev_avg_K_grad /= length
                prev_avg_bias_grad /= length
                K_w_prev = self.prev_model.get_K()
                K_bias_prev = self.prev_model.get_bias()


            '''
            Train one task during configured epoch.
            '''
            # train
            for epoch in range(self.train_epoch):
                # Train current Task
                correct, total = 0, 0
                for batch_idx, (x, y, t) in enumerate(data_loader):
                    x = x.to(device=self.device)
                    y = y.to(device=self.device)
                    shift_y = torch.full_like(y, task*self.increment)
                    y = y - shift_y

                    feature = self.model.forward_backbone(x)
                    inj_logit = self.model.forward_inj(feature)
                    acc_logit = self.model.forward_acc(feature)

                    # if task == 0:
                    #     acc_logit = torch.zeros_like(inj_logit).to(self.device)

                    # print(inj_logit)
                    '''
                    L_It and L_At is obtained by the new data.
                    L_It is cross entropy loss value between output of the
                    injection classifier and GT value.
                    L_At is cross entropy loss value between output of the
                    accumulation classifier and GT value.
                    '''
                    L_It = cross_entropy(inj_logit, y)
                    L_At = cross_entropy(acc_logit, y)
                    
                    # Train memory if task>0
                    '''
                    The memory is used after first task.
                    At the first task, there are nothing in memory.
                    '''
                    if task > 0:
                        # print(f'prev_K_grad : {prev_avg_K_grad.shape}, K : {self.model.get_K().shape}')
                        # print(f'prev_B_grad : {prev_avg_bias_grad.shape}, B : {self.model.get_bias().shape}')
                        '''
                        L_a value can be calculated 
                        when the previous gradient value exists.
                        This loss can be regarded as the interation with previous task.
                        '''
                        L_a = (torch.abs(torch.tensordot(prev_avg_K_grad, (self.model.get_K() - K_w_prev)))).sum() + \
                                (torch.abs(torch.tensordot(prev_avg_bias_grad, (self.model.get_bias() - K_bias_prev), dims=([2, 1], [2, 1])))).sum()
                        
                        '''
                        Calculate the logit value from accumulation classifier on the data in memory buffer.
                        '''                        
                        t = np.random.randint(0, task)
                        chunk_size  = (self.memory_size // (self.increment * task)) * self.increment
                        memory_idx = np.random.permutation(np.arange(chunk_size*t, chunk_size*(t+1)))[:self.batch_size]
                        mx,my,mt = self.memory[memory_idx]

                        # if examplars are smaller than batch, repeat to match the size
                        if chunk_size < self.batch_size:
                            mx = torch.concat([mx, mx])[:self.batch_size]
                            my = torch.concat([my, my])[:self.batch_size]
                            mt = torch.concat([mt, mt])[:self.batch_size]

                        mx = mx.to(self.device)
                        my = my.type(torch.LongTensor).to(self.device)
                        assert(mt.sum()==t*mt.size(0))
                        mt = t

                        # Shift label to first task
                        shift_my = torch.full_like(my, mt*self.increment)
                        my = my - shift_my

                        z = self.prev_model.forward_acc(self.prev_model.forward_backbone(mx))
                        if self.ILtype=='task':
                            acc_logit = self.model.forward_acc(self.model.forward_backbone(mx), mt)
                        else:
                            acc_logit = self.model.forward_acc(self.model.forward_backbone(mx))
                        
                        # print(f'For dim: acclogit size {acc_logit.size()}, z size {z.size()}')
                    else:
                        L_a = torch.zeros_like(L_It).to(self.device)
                        z = acc_logit

                    '''If first task, then only the losses obtained by new data are backpropagated.
                    Or, accumulate the losses from memory such as L_r, L_d into L_l'''
                    if task == 0:
                        total_loss = L_It + L_At
                    else:
                        L_r = cross_entropy(acc_logit, my)
                        L_d = kl_divergence(nn.functional.log_softmax((z/self.T), dim=1), self.act(acc_logit/self.T))
                        L_l = self.alpha*L_r + self.beta*L_d + self.rt*L_At
                        total_loss = L_l + L_It + self.gamma*L_a
                        
                    # To log the accuracy, calculate that
                    _, predicted = torch.max(inj_logit, 1)
                    correct += (predicted == y).sum().item()
                    total += y.size(0)
                    
                    # print(f'batch {batch_idx} | L_l : {L_l}| L_r : {L_r}| L_d : {L_d}| L_At :{L_At}| L_It : {L_It}| L_a : {L_a}| train_loss :{total_loss}|  accuracy : {100*correct/total}')
                    '''
                    Backward and optimize
                    '''                    
                    self.optimizer.zero_grad()
                    total_loss.backward()
                    nn.utils.clip_grad_norm_(self.model.parameters(), 5.)
                    self.optimizer.step()
                    self.optimizer.zero_grad()
                    # print(f'batch : {batch_idx} | L : {total_loss} | L_It : {L_It} | L_d :{L_d} | acc : {acc_logit.max()}')
                '''
                Logging
                '''
                if task == 0:
                    print(f'epoch {epoch} | L_At :{L_At:.3f}| L_It : {L_It:.3f}| train_loss :{total_loss:.3f} |  accuracy : {100*correct/total:.3f}')
                else:
                    print(f'epoch {epoch} | L_At (acc):{L_At:.3f}| L_It (inj): {L_It:.3f}| L_a (att): {L_a:.3f}| L_l (accum): {L_l:.3f}| L_r (replay): {L_r:.3f}| L_d (dark) : {L_d:.3f}|  train_loss :{total_loss:.3f} |  accuracy : {100*correct/total:.3f}')
            

            '''Update memory'''
            conf_score_list = []
            x_list = []
            labels_list = []
            
            '''Calculate confidence score'''
            for x, y, t in data_loader:
                x_list.append(x)
                labels_list.append(y)
                x = x.to(device=self.device)
                y = y.to(device=self.device)
                shift_y = torch.full_like(y, task*self.increment)
                y = y - shift_y
                feature = self.model.forward_backbone(x)
                inj_logit = self.model.forward_inj(feature)

                conf_score_list.append(confidence_score(inj_logit.detach(), y.detach()).numpy())
                # store logit z=inj_logit for each x
            
            conf_score = np.array(conf_score_list).flatten()
            labels = torch.cat(labels_list).flatten()
            xs = torch.cat(x_list).view(-1, *x.shape[1:])

            '''To add new examplars, reduce examplars to K'''
            if task > 0:
                self.memory.remove_examplars(K)

            '''Add new examplars'''
            conf_score_sorted = conf_score.argsort()[::-1]
            for label in range(self.increment*task, self.increment*(task+1)):
                new_x = xs[conf_score_sorted[labels==label][:K]]
                new_y = labels[conf_score_sorted[labels==label][:K]]
                new_t = torch.full((K,), task).type(torch.LongTensor)
                self.memory.update_memory(label, new_x, new_y, new_t)
                
            '''updatae r(t)'''
            self.rt *= 0.9

            '''
            After task, the number of output of classifier should be extended.
            In Task IL, LVT generates new classifier
            and store the currently used classifier.
            In Class IL, LVT extendes the classifiers.
            '''
            if self.ILtype == 'task':
                self.model.add_classes(self.increment)
            if self.ILtype == 'class':
                self.model.add_classes(self.increment)
                self.cur_classes += self.increment
            
            '''Save previous model'''
            self.prev_model = copy.deepcopy(self.model)
            self.prev_model.eval()
            
            '''Reset optimizer'''
            self.optimizer = optim.SGD(self.model.parameters(), lr = self.lr)
            if self.scheduler:
                self.lr_scheduler = torch.optim.lr_scheduler.StepLR(self.optimizer, self.train_epoch/10, 0.1)
            
            '''Save model and memory'''
            self.save(self.model, self.memory, task)
            
            '''test'''
            self.eval(task)
    
    '''
    In this function, just evaluate the model on whole previous tasks.
    '''
    def eval(self, task):
        self.model.eval()
        acc = []
        with torch.no_grad():
            for task_id in range(task+1):
                correct, total = 0, 0
                data_loader = IncrementalDataLoader(self.dataset, self.data_path, False, self.split, task_id, self.batch_size, transform_test)
                for x, y, t in data_loader:
                    x = x.to(device=self.device)
                    y = y.to(device=self.device)
                    shift_y = torch.full_like(y, task_id*self.increment)
                    y = y - shift_y

                    acc_logit = self.model.forward_acc(self.model.forward_backbone(x), task_id)
                    _, predicted = torch.max(acc_logit, 1)
                    correct += (predicted == y).sum().item()
                    total += y.size(0)
                acc.append(100*correct/total)
                print(toGreen(f'Test accuracy on task {task_id} : {100*correct/total}'))
        print(toGreen(f'Total test accuracy on task {task} : {100*sum(acc)/len(acc)}'))
        self.model.train()