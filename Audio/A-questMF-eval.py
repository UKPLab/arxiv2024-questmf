import pandas as pd
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import torch.nn.functional as F
import numpy as np
import random
import librosa
import argparse
import os

def cmdline_args():
    # Make parser object
    p = argparse.ArgumentParser()
    p.add_argument("-s", "--seed", type=int, help="Set a random seed")
    p.add_argument("-d_path", "--data_path", type=str, help="Path to data files (text transcripts, audio files, video features)")
    p.add_argument("-l_path", "--label_path", type=str, help="Path to labels, i.e., PHQ-8 scores")
    p.add_argument("-a_ckpt", "--audio_checkpoint_path", type=str, help="Path to checkpoint for the audio model")
    p.add_argument("-m_files", "--missing_video_files",nargs='+', type=int, help="List of file numbers for incomplete video files")
    
    return (p.parse_args())

def set_seed(seed_value=42):
    """Set seed for reproducibility.
    """
    random.seed(seed_value)
    np.random.seed(seed_value)
    torch.manual_seed(seed_value)
    torch.cuda.manual_seed_all(seed_value)

class dds(Dataset):
    def __init__(self,split,data_path,label_path,missing_files_list):
        super(dds,self).__init__()
        if split == 'train':
            df_data = pd.read_csv(label_path + 'train_split.csv')
        elif split == 'val':
            df_data = pd.read_csv(label_path + 'dev_split.csv')
        elif split == 'test':
            df_data = pd.read_csv(label_path + 'test_split.csv')
        else:
            raise Exception(f"wrong split: {split}")
        self.data = []
        p_id_list = df_data['Participant_ID'].tolist()
        phq_score_list = df_data['PHQ_Score'].tolist()
        for i in range(len(p_id_list)):
            p_id = str(p_id_list[i])
            # Files not complete for video
            if p_id_list[i] in missing_files_list:
                continue
            df_txt = pd.read_csv(data_path + p_id + '_P/' + p_id + '_Transcript.csv')
            speech_file = data_path + p_id + '_P/' + p_id + '_AUDIO.wav'
            aud_dur = librosa.get_duration(path=speech_file)
            # Sampling-rate = 100
            # Preprocessing start and end times
            max_times = aud_dur*100
            df_speech = pd.read_csv(data_path + p_id + '_P/features/' + p_id + '_OpenSMILE2.3.0_egemaps.csv',sep=";")
            start_times = (df_txt['Start_Time'].values*100).tolist()
            end_times = (df_txt['End_Time'].values*100).tolist()

            x = 1
            y = len(start_times)
            
            while x < y:
                if start_times[x-1]>start_times[x] or start_times[x]>max_times:
                    del start_times[x]
                    del end_times[x]
                    x = x-1
                    y = y-1
                if end_times[x-1]>end_times[x] or end_times[x]>max_times:
                    del start_times[x]
                    del end_times[x]
                    x = x-1
                    y = y-1
                x = x+1
            
            start_times = [round(z) for z in start_times]
            end_times = [round(z) for z in end_times]      
                
            self.data.append([df_speech,start_times,end_times,float(phq_score_list[i])])
    
    def preprocess(self,df_speech,start_times,end_times):
        df_mod = df_speech.iloc[:,2:]
        speech= df_mod.values.tolist()
        speech_0 = torch.tensor(speech[start_times[0]:end_times[0]]).unsqueeze(0)
        out = torch.mean(speech_0, dim=1)
        out = out.detach().cpu()

        for i in range(1,len(start_times)):
            speech_mod = torch.tensor(speech[start_times[i]:end_times[i]]).unsqueeze(0)
            mean_i = torch.mean(speech_mod, dim=1)
            mean_i = mean_i.detach().cpu()
            out = torch.cat((out,mean_i),dim=0)
        
        l = len(start_times)
        if l > 120:
            l = 120
        mask = torch.tensor([False]*l)
        out = out[:l]
        if l < 120:
            z = torch.zeros(120-l,23)
            out = torch.cat((out,z),dim=0)
            mask = torch.cat((mask,torch.tensor([True]*(120-l))))
        # out = F.normalize(out, p=2, dim=1)
        return out,mask
    def __getitem__(self,index):
        embedding, mask = self.preprocess(self.data[index][0],self.data[index][1],self.data[index][2])
        return [embedding,mask,self.data[index][3]]
    def __len__(self):
        return len(self.data)

class lstm_regressor(nn.Module):
    def __init__(self):
        super(lstm_regressor, self).__init__()
        self.lstm_1 = nn.LSTM(23,50,batch_first=True,bidirectional=True)
        self.attention1 = nn.MultiheadAttention(100, 4,batch_first=True,dropout=0.2)
        self.attention2 = nn.MultiheadAttention(100, 4,batch_first=True,dropout=0.2)
        self.mlp = nn.Sequential(nn.Flatten(),
                                nn.Dropout(0.2),
                                nn.Linear(12000,256),
                                nn.ReLU(),
                                nn.Dropout(0.2),
                                nn.Linear(256,4))

    def forward(self,C,key_padding_mask):
        c_lstm,_ = self.lstm_1(C)
        c_att,_ = self.attention1(c_lstm,c_lstm,c_lstm,key_padding_mask=key_padding_mask)
        c_att2,_ = self.attention2(c_att,c_att,c_att,key_padding_mask=key_padding_mask)
        logits = self.mlp(c_att2)
        return logits

# CCC loss
class ccc_loss(nn.Module):
    def __init__(self):
        super(ccc_loss, self).__init__()
        self.mean = torch.mean
        self.var = torch.var
        self.sum = torch.sum
        self.sqrt = torch.sqrt
        self.std = torch.std
    def forward(self, prediction, ground_truth):
        mean_gt = self.mean (ground_truth, 0)
        mean_pred = self.mean (prediction, 0)
        var_gt = self.var (ground_truth, 0)
        var_pred = self.var (prediction, 0)
        v_pred = prediction - mean_pred
        v_gt = ground_truth - mean_gt
        cor = self.sum (v_pred * v_gt) / (self.sqrt(self.sum(v_pred ** 2)) * self.sqrt(self.sum(v_gt ** 2)))
        sd_gt = self.std(ground_truth)
        sd_pred = self.std(prediction)
        numerator=2*cor*sd_gt*sd_pred
        denominator=var_gt+var_pred+(mean_gt-mean_pred)**2
        ccc = numerator/denominator
        return 1-ccc

def evaluate(model1,model2,model3,model4,model5,model6,model7,model8, val_dataloader):
    """After the completion of each training epoch, measure the model's performance
    on our validation set.
    """
    # Put the models into the evaluation mode. The dropout layers are disabled during
    # the test time.
    model1.eval()
    model2.eval()
    model3.eval()
    model4.eval()
    model5.eval()
    model6.eval()
    model7.eval()
    model8.eval()

    # Tracking variables
    preds_list = []
    labels_list = []

    # For each batch in our validation set...
    for batch in val_dataloader:
        # Load batch to GPU
        c, mask, phq_scores = tuple(t.to(device) for t in batch)

        # Compute logits
        with torch.no_grad():
            logits1 = model1.forward(c,mask)
            logits2 = model2.forward(c,mask)
            logits3 = model3.forward(c,mask)
            logits4 = model4.forward(c,mask)
            logits5 = model5.forward(c,mask)
            logits6 = model6.forward(c,mask)
            logits7 = model7.forward(c,mask)
            logits8 = model8.forward(c,mask)

        # Compute preds
        preds1 = torch.argmax(logits1, dim=1).flatten()
        preds2 = torch.argmax(logits2, dim=1).flatten()
        preds3 = torch.argmax(logits3, dim=1).flatten()
        preds4 = torch.argmax(logits4, dim=1).flatten()
        preds5 = torch.argmax(logits5, dim=1).flatten()
        preds6 = torch.argmax(logits6, dim=1).flatten()
        preds7 = torch.argmax(logits7, dim=1).flatten()
        preds8 = torch.argmax(logits8, dim=1).flatten()
        
        preds = preds1+preds2+preds3+preds4+preds5+preds6+preds7+preds8
        preds_list.append(preds)
        labels_list.append(phq_scores)

        preds = preds.detach().cpu()
        phq_scores = phq_scores.detach().cpu()

    # Compute the CCC, RMSE and MAE
    preds_all = torch.cat(preds_list, dim=0)
    labels_all = torch.cat(labels_list, dim=0)
    val_loss_ccc = 1 - ccc_loss_fn(preds_all.float(), labels_all.float())
    val_loss_rmse = torch.sqrt(loss_fn_mse(preds_all.float(), labels_all.float()))
    val_loss_mae = mae_loss_fn(preds_all.float(),labels_all.float())

    return val_loss_ccc,val_loss_rmse,val_loss_mae

if __name__ == '__main__':

    args = cmdline_args()

    set_seed(args.seed)    # Set seed for reproducibility

    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
    print(f"# Using device: {device}")
    
    # Datasets
    data_train = dds('train',args.data_path,args.label_path,args.missing_video_files)
    data_val = dds('val',args.data_path,args.label_path,args.missing_video_files)
    data_test = dds('test',args.data_path,args.label_path,args.missing_video_files)
    
    # Define Audio Encoder for each Question
    r1 = lstm_regressor()
    r2 = lstm_regressor()
    r3 = lstm_regressor()
    r4 = lstm_regressor()
    r5 = lstm_regressor()
    r6 = lstm_regressor()
    r7 = lstm_regressor()
    r8 = lstm_regressor()
    
    ## Load pretrained weights
    r1.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq1-seed-' + str(args.seed) + '-ccc.pt'))
    r2.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq2-seed-' + str(args.seed) + '-ccc.pt'))
    r3.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq3-seed-' + str(args.seed) + '-ccc.pt'))
    r4.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq4-seed-' + str(args.seed) + '-ccc.pt'))
    r5.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq5-seed-' + str(args.seed) + '-ccc.pt'))
    r6.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq6-seed-' + str(args.seed) + '-ccc.pt'))
    r7.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq7-seed-' + str(args.seed) + '-ccc.pt'))
    r8.load_state_dict(torch.load(args.audio_checkpoint_path + '-phq8-seed-' + str(args.seed) + '-ccc.pt'))
    
    r1.to(device)
    r2.to(device)
    r3.to(device)
    r4.to(device)
    r5.to(device)
    r6.to(device)
    r7.to(device)
    r8.to(device)
    
    # Dataloaders
    train_dataloader = DataLoader(data_train,  batch_size=10)
    val_dataloader = DataLoader(data_val,  batch_size=10)
    test_dataloader = DataLoader(data_test,  batch_size=10)

    # Specify loss functions/Metrics
    loss_fn_mse = nn.MSELoss()
    ccc_loss_fn = ccc_loss()
    mae_loss_fn = nn.L1Loss()
    
    # Evaluate trained model
    print(evaluate(r1,r2,r3,r4,r5,r6,r7,r8,test_dataloader))