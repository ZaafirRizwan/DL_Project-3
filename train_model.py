#Dependencies
import numpy as np
import torch
import torch.nn as nn
import torch.optim as optim
import torchvision
import torchvision.models as models
import torchvision.transforms as transforms
import torchvision.datasets as datasets
import boto3
from torch.utils.data import Dataset
import tempfile
from PIL import Image
from PIL import ImageFile
ImageFile.LOAD_TRUNCATED_IMAGES = True
import os
import logging
import sys
import time
import json
import os
import io

import argparse

# For Profiling
from smdebug import modes
from smdebug.profiler.utils import str2bool
from smdebug.pytorch import get_hook



#  For Debugging
import smdebug.pytorch as smd
import subprocess



logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
logger.addHandler(logging.StreamHandler(sys.stdout))




s3 = boto3.client('s3')

device = torch.device('cuda:0' if torch.cuda.is_available() else 'cpu')

# Define the S3 bucket and local directory
bucket_name = 'myimageclassificationbucket'
local_dir = './'

# Construct the sync command as a list of arguments
cmd = ['aws', 's3', 'sync', 's3://' + bucket_name, local_dir]
subprocess.run(cmd,stdout=subprocess. DEVNULL)



def input_fn(request_body, request_content_type):
    """
    Deserialize and prepare the prediction input
    """

    if request_content_type == "application/json":
        # Create an instance of the JSONDeserializer
        deserializer = JSONDeserializer()

        # Deserialize the data using the JSONDeserializer
        deserialized_data = deserializer.deserialize(BytesIO(serialized_data.encode('utf-8')), 'application/json')
        test_transform = transforms.Compose([
                transforms.ToPILImage(),
                transforms.Resize((224, 224)),
                transforms.ToTensor(),
                transforms.Normalize(mean = [0.485, 0.456, 0.406],
                                    std = [0.229, 0.224, 0.225])
            ])

        deserialized_data = np.array(deserialized_data)
        train_inputs = test_transform(deserialized_data)
        return train_inputs

def save_model(model, model_dir):
    logger.info("Saving the model.")
    path = os.path.join(model_dir, "model.pth")
    torch.save(model.cpu().state_dict(), path)

    
def predict_fn(input_data, model):
    """
    Apply model to the incoming request
    """

    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model.to(device)
    model.eval()
    with torch.no_grad():
        return model(input_data)
    

def output_fn(prediction, content_type, context):
    if content_type == "application/json":
        data = {'body': prediction}
        # Create an instance of the JSONSerializer
        serializer = JSONSerializer()

        # Serialize the data using the JSONSerializer
        serialized_data = serializer.serialize(data)
        
        return serialized_data
    
    

def test(model, test_loader,criterion,hook):
    '''
    This function takes two arguments and returns None
    
    Parameters:
        -model: Trained Image Classification Network
        -test_loader: DataLoader for test dataset
        -hook: hook for saving model tensors during testing for ananlyzing model behavior
        
    Returns:
        None
    '''
    
    # Setting SMDEBUG hook for testing Phase
    model.eval()
    hook.set_mode(smd.modes.EVAL)
    test_loss = 0
    correct = 0
    loss = 0
    with torch.no_grad():
        for data, target in test_loader:
            data = data.to(device)
            target = target.to(device)
            output = model(data)
            loss = criterion(output,target)
            test_loss += loss.item() # sum up batch loss
            pred = output.argmax(dim=1,keepdim=True) # get the index of the max log-probability
            correct += pred.eq(target.view_as(pred)).sum().item()
            
    test_loss /= len(test_loader.dataset)
    
    logger.info(
        "Test set: Average loss: {:.4f}, Accuracy: {}/{} ({:.0f}%)\n".format(
            test_loss, correct, len(test_loader.dataset), 100.0 * correct / len(test_loader.dataset)
        )
    )

def train(model, train_loader, criterion, optimizer, epoch,hook,args):
    '''
    This function takes five arguments and returns None
    
    Parameters:
        -model: Untrained Image Classification Network
        -train_loader: DataLoader for train dataset
        -criterion: Loss Function
        -optimizer: The optimization algorithm to use
        -epoch: Epoch Number
        -hook: hook for saving model tensors during training for ananlyzing model behavior
        
    Returns:
        None
    '''
    
    model.fc.require_grad = True
    model. train()
    # Setting SMDEBUG hook for model training loop
    hook.set_mode(smd.modes.TRAIN)
    for batch_idx, (data, target) in enumerate(train_loader):
        data = data.to(device)
        target = target.to(device)
        optimizer.zero_grad()
        output = model(data)
        loss = criterion(output, target)
        loss.backward()
        optimizer.step()
        if batch_idx % 100 == 0:
            logger.info(
                "Train Epoch: {} [{}/{} ({:.0f}%)] Loss: {:.6f}".format(
                    epoch,
                    batch_idx * len(data),
                    len(train_loader.dataset),
                    100.0 * batch_idx / len(train_loader),
                    loss.item(),
                )
            )

    save_model(model, args.model_dir)
    
    
def net():
    '''
    This function takes zero parameters and returns a Network
    
    Parameters:
        None
        
    Returns:
        Untrained Image Classification Model
        
    '''
    pretrained_model = models.resnet18(pretrained=True)
    
    # Freezing Pretrained Weights
    for param in pretrained_model.parameters():
        param.requires_grad = False
    
    # Append Fully_Connected layer
    num_ftrs = pretrained_model.fc.in_features
    pretrained_model.fc = nn.Linear(num_ftrs, 133)

    model_ft = pretrained_model.to(device)
    
    return pretrained_model
    

    
def model_fn(model_dir, context):
    model = net()
    with open(os.path.join(model_dir, 'model.pth'), 'rb') as f:
        model.load_state_dict(torch.load(f))
    return model    
    
    

def create_data_loaders(data, batch_size):
    '''
    This function takes two arguments and returns Dataloader

    Parameters:
        -data: dataset of train and test images
        -batch_size: No of Images feed into the network at a time

    Returns:
        Dataloader i.e Train and test
    '''
    logger.info("Get train data loader")
    train_dataset_loader = torch.utils.data.DataLoader(data["train"], batch_size=batch_size, shuffle=True, num_workers=1)
    logger.info("Get test data loader")
    test_dataset_loader  = torch.utils.data.DataLoader(data["test"] , batch_size=batch_size, shuffle=False,num_workers=1)
    dataloaders = {'train': train_dataset_loader, 'test': test_dataset_loader}
    
    return dataloaders
    
def main(args):

    # Initializing Model
    model = net()
    
    hook = smd.Hook.create_from_json_file()
    hook.register_hook(model)
    
    
    # Creating Loss Function and optimizer
    loss_criterion = nn.CrossEntropyLoss(reduction='sum')
    optimizer = optim.Adadelta(model.parameters(), lr=args.lr)
    
    train_transform = transforms.Compose([
                transforms.Resize((224, 224)),
                transforms.RandomHorizontalFlip(),
                transforms.RandomRotation(10),
                transforms.RandomResizedCrop(size=224,scale=(0.8, 1.0)),
                transforms.ToTensor(),
                transforms.Normalize(mean = [0.485, 0.456, 0.406],
                                    std = [0.229, 0.224, 0.225])
            ])

    test_transform = transforms.Compose([
                    transforms.Resize((224, 224)),
                    transforms.ToTensor(),
                    transforms.Normalize(mean = [0.485, 0.456, 0.406],
                                        std = [0.229, 0.224, 0.225])
                ])
    
    

            
    
    train_dataset = torchvision.datasets.ImageFolder(root="./train", transform = train_transform)
    test_dataset = torchvision.datasets.ImageFolder(root="./test", transform = test_transform)
    
    
    data = {"train":train_dataset, "test":test_dataset}
    
    dataloaders = create_data_loaders(data,args.batch_size)
    
    '''
    TODO: Call the train function to start training your model
    Remember that you will need to set up a way to get training data from S3
    '''
    
    
    epoch_times = []
    for epoch in range(args.epochs):
        start = time.time()
        train(model, dataloaders['train'], loss_criterion, optimizer, epoch, hook,args)
        test(model, dataloaders['test'], loss_criterion, hook)
        epoch_time = time.time() - start
        epoch_times.append(epoch_time)
    
    
    p50 = np.percentile(epoch_times, 50)
    logger.info("Median training time per Epoch=%.1f sec" % p50)


if __name__=='__main__':
    parser = argparse.ArgumentParser(description="Deep Learning on Amazon Sagemaker")
    parser.add_argument("--batch-size",
                        type=int,
                        default=64,
                        metavar="N",
                        help="input batch size for training (default: 64)",
                       )
    
    parser.add_argument("--epochs",
                       type=int,
                       default=3,
                       metavar="N",
                       help="input batch size for training (default: 64)"
                       )
    parser.add_argument("--lr",
                   type=float,
                   default=1.0,
                   metavar="LR",
                   help="learning rate (default: 1.0)",
                   )
    
    parser.add_argument("--hosts", type=list, default=json.loads(os.environ["SM_HOSTS"]))
    parser.add_argument("--current-host", type=str, default=os.environ["SM_CURRENT_HOST"])
    parser.add_argument("--model-dir", type=str, default=os.environ["SM_MODEL_DIR"])
        
    args = parser.parse_args()
    
    #  Printing Arguments
    for key, value in vars(args).items():
        print(f"{key}:{value}")
    
    
    main(args)