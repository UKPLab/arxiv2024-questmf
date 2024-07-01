import pandas as pd
from transformers import AutoTokenizer, AutoModel
import torch
import torch.nn as nn
from torch.utils.data import Dataset, DataLoader
import numpy as np
from transformers import HubertModel,Wav2Vec2FeatureExtractor
import random
import time
import argparse
import librosa
import os
from transformers import get_linear_schedule_with_warmup

def cmdline_args():
    # Make parser object
    p = argparse.ArgumentParser()
    p.add_argument("-d_path", "--data_path", type=str, help="Path to data files (text transcripts, audio files, video features)")
    p.add_argument("-l_path", "--label_path", type=str, help="Path to labels, i.e., PHQ-8 scores")
    p.add_argument("-t_ckpt", "--text_checkpoint_path", type=str, help="Path to checkpoint for the text model")
    p.add_argument("-a_ckpt", "--audio_checkpoint_path", type=str, help="Path to checkpoint for the audio model")
    p.add_argument("-ta_ckpt", "--ta_checkpoint_path", type=str, help="Path to checkpoint for the text+audio model")
    p.add_argument("-m_files", "--missing_video_files",nargs='+', type=int, help="List of file numbers for incomplete video files")

    return (p.parse_args())

# Mean Pooling - Take attention mask into account for correct averaging
def mean_pooling(model_output, attention_mask):
    token_embeddings = model_output[0] # First element of model_output contains all token embeddings
    input_mask_expanded = attention_mask.unsqueeze(-1).expand(token_embeddings.size()).float()
    return torch.sum(token_embeddings * input_mask_expanded, 1) / torch.clamp(input_mask_expanded.sum(1), min=1e-9)

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
            txt_list = df_txt['Text'].tolist()
            
            speech_file = data_path + p_id + '_P/' + p_id + '_AUDIO.wav'
            aud_dur = librosa.get_duration(path=speech_file)
            # Sampling-rate = 16000
            # Preprocessing start and end times
            max_times = aud_dur*16000
            start_times = (df_txt['Start_Time'].values*16000).tolist()
            end_times = (df_txt['End_Time'].values*16000).tolist()

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
            
            start_times = [round(a) for a in start_times]
            end_times = [round(b) for b in end_times]
            
            self.data.append([txt_list,speech_file,start_times,end_times,float(phq_score_list[i])])
    
    # Text Preprocess
    def preprocess_txt(self,txt_list):
        encoded_input = tokenizer_txt(txt_list, padding=True, truncation=True, return_tensors='pt').to(device)
        l = len(txt_list)
        # Maximum number of turns
        if l > 120:
            l = 120
        # Mask for attention layer
        mask_txt = torch.tensor([False]*l)
        # Compute token embeddings
        with torch.no_grad():
            embedder_output = embedder_txt(**encoded_input)
        # Perform mean pooling
        sentence_embeddings = mean_pooling(embedder_output, encoded_input['attention_mask'])[:l]
        if l < 120:
            z = torch.zeros(120-l,768).to(device)
            sentence_embeddings = torch.cat((sentence_embeddings,z),dim=0)
            mask_txt = torch.cat((mask_txt,torch.tensor([True]*(120-l))))
        return sentence_embeddings.detach().cpu(),mask_txt

    # Audio Preprocess
    def preprocess_aud(self,speech_file,start_times,end_times):
        # Sample at original sr
        speech_org, org_sr = librosa.load(speech_file,sr=None)
        # Resample with sr = 16000
        speech = librosa.resample(speech_org, orig_sr=org_sr, target_sr=16000)
        speech_0 = speech[start_times[0]:end_times[0]]
        input_values = processor_aud(speech_0, sampling_rate=16000, return_tensors="pt").to(device)
        hidden_states = embedder_aud(input_values.input_values).last_hidden_state
        # Mean pooling
        out = torch.mean(hidden_states, dim=1)
        out = out.detach().cpu()

        for i in range(1,len(start_times)):
            speech_mod = speech[start_times[i]:end_times[i]]
            input_values_i = processor_aud(speech_mod, sampling_rate=16000, return_tensors="pt").to(device)
            hidden_states_i = embedder_aud(input_values_i.input_values).last_hidden_state
            # Mean pooling
            mean_i = torch.mean(hidden_states_i, dim=1)
            mean_i = mean_i.detach().cpu()
            out = torch.cat((out,mean_i),dim=0)
        l = len(start_times)
        # Maximum number of turns
        if l > 120:
            l = 120
        # Mask for attention layer
        mask_aud = torch.tensor([False]*l)
        out = out[:l]
        if l < 120:
            z = torch.zeros(120-l,768)
            out = torch.cat((out,z),dim=0)
            mask_aud = torch.cat((mask_aud,torch.tensor([True]*(120-l))))
        return out,mask_aud
    def __getitem__(self,index):
        embedding_txt, mask_txt = self.preprocess_txt(self.data[index][0])
        embedding_aud, mask_aud = self.preprocess_aud(self.data[index][1],self.data[index][2],self.data[index][3])
        return [embedding_txt,mask_txt,embedding_aud,mask_aud,self.data[index][4]]
    def __len__(self):
        return len(self.data)

class lstm_regressor_txt(nn.Module):
    def __init__(self):
        super(lstm_regressor_txt, self).__init__()
        self.lstm_1 = nn.LSTM(768,100,batch_first=True,bidirectional=True)
        self.attention = nn.MultiheadAttention(200, 4,batch_first=True,dropout=0.5)
        self.mlp = nn.Sequential(nn.Flatten(),
                                nn.Dropout(0.5),
                                nn.Linear(24000,256),
                                nn.ReLU(),
                                nn.Dropout(0.5),
                                nn.Linear(256,1))

    def forward(self,C,key_padding_mask):
        c_lstm,_ = self.lstm_1(C)
        c_att,_ = self.attention(c_lstm,c_lstm,c_lstm,key_padding_mask=key_padding_mask)
        pred = self.mlp(c_att)
        return pred,c_att

class lstm_regressor_aud(nn.Module):
    def __init__(self):
        super(lstm_regressor_aud, self).__init__()
        self.lstm_1 = nn.LSTM(768,100,batch_first=True,bidirectional=True)
        self.attention = nn.MultiheadAttention(200, 4,batch_first=True,dropout=0.5)
        self.mlp = nn.Sequential(nn.Flatten(),
                                nn.Dropout(0.5),
                                nn.Linear(24000,256),
                                nn.ReLU(),
                                nn.Dropout(0.5),
                                nn.Linear(256,1))

    def forward(self,C,key_padding_mask):
        c_lstm,_ = self.lstm_1(C)
        c_att,_ = self.attention(c_lstm,c_lstm,c_lstm,key_padding_mask=key_padding_mask)
        pred = self.mlp(c_att)
        return pred,c_att

class lstm_regressor(nn.Module):
    def __init__(self):
        super(lstm_regressor, self).__init__()
        self.txt_model = txt_model
        self.aud_model = aud_model

        self.cross_aud_txt = nn.MultiheadAttention(200, 4,batch_first=True,dropout=0.8)
        self.cross_txt_aud = nn.MultiheadAttention(200, 4,batch_first=True,dropout=0.8)

        self.self_aud_txt = nn.MultiheadAttention(200, 4,batch_first=True,dropout=0.8)
        self.self_txt_aud = nn.MultiheadAttention(200, 4,batch_first=True,dropout=0.8)

        self.mlp = nn.Sequential(nn.Flatten(),
                                nn.Dropout(0.8),
                                nn.Linear(48000,256),
                                nn.ReLU(),
                                nn.Dropout(0.5),
                                nn.Linear(256,1))

        # Freeze Text Encoder
        for param_txt in self.txt_model.parameters():
            param_txt.requires_grad = False

    def forward(self,C_txt,key_padding_mask_txt,C_aud,key_padding_mask_aud):

        _,c_att_txt = self.txt_model(C_txt,key_padding_mask_txt)

        _,c_att_aud = self.aud_model(C_aud,key_padding_mask_aud)

        c_aud_txt,_ = self.cross_aud_txt(c_att_aud,c_att_txt,c_att_txt,key_padding_mask=key_padding_mask_txt)
        c_txt_aud,_ = self.cross_txt_aud(c_att_txt,c_att_aud,c_att_aud,key_padding_mask=key_padding_mask_aud)

        c_att_aud_txt,_ = self.self_aud_txt(c_aud_txt,c_aud_txt,c_aud_txt,key_padding_mask=key_padding_mask_txt)
        c_att_txt_aud,_ = self.self_txt_aud(c_txt_aud,c_txt_aud,c_txt_aud,key_padding_mask=key_padding_mask_aud)

        c_comb = torch.cat((c_att_aud_txt,c_att_txt_aud),dim=2)

        pred = self.mlp(c_comb)

        return pred

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

def train(model, train_dataloader, ta_ckpt_path, val_dataloader=None, epochs=10, evaluation=False):

    # Start training loop
    print("Start training...\n")
    best_val_loss = -100
    for epoch_i in range(epochs):
        # =======================================
        #               Training
        # =======================================
        # Print the header of the result table
        print(f"{'Epoch':^7} | {'Batch':^7} | {'Train Loss':^12} | {'Val Loss':^10} | {'Elapsed':^9}")
        print("-"*70)

        # Measure the elapsed time of each epoch
        t0_epoch, t0_batch = time.time(), time.time()

        # Reset tracking variables at the beginning of each epoch
        total_loss, batch_loss, batch_counts = 0, 0, 0

        # Put the model into the training mode
        model.train()

        # For each batch of training data...
        for step, batch in enumerate(train_dataloader):
            batch_counts +=1
            # Load batch to GPU
            c_txt, mask_txt, c_aud, mask_aud, phq_scores = tuple(t.to(device) for t in batch)

            # Zero out any previously calculated gradients
            model.zero_grad()

            # Perform a forward pass. This will return predictions.
            preds = model.forward(c_txt,mask_txt,c_aud,mask_aud)
            # Compute loss and accumulate the loss values
            loss = loss_fn(preds.squeeze(1), phq_scores.float())
            batch_loss += loss.item()
            total_loss += loss.item()

            # Perform a backward pass to calculate gradients
            loss.backward()
            # Clip the norm of the gradients to 1.0 to prevent "exploding gradients"
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)

            # Update parameters and the learning rate
            optimizer.step()
            scheduler.step()

            # Print the loss values and time elapsed for every 20 batches
            if (step % 20 == 0 and step != 0) or (step == len(train_dataloader) - 1):
                # Calculate time elapsed for 20 batches
                time_elapsed = time.time() - t0_batch

                # Print training results
                print(f"{epoch_i + 1:^7} | {step:^7} | {batch_loss / batch_counts:^12.6f} | {'-':^10} | {time_elapsed:^9.2f}")

                # Reset batch tracking variables
                batch_loss, batch_counts = 0, 0
                t0_batch = time.time()

        # Calculate the average loss over the entire training data
        avg_train_loss = total_loss / len(train_dataloader)

        print("-"*70)
        # =======================================
        #               Evaluation
        # =======================================
        if evaluation == True:
            # After the completion of each training epoch, measure the model's performance
            # on our validation set.
            val_loss,_,_ = evaluate(model, val_dataloader)
            if(val_loss > best_val_loss):
                best_val_loss = val_loss
                torch.save(model.state_dict(), ta_ckpt_path)

            # Print performance over the entire training data
            time_elapsed = time.time() - t0_epoch
            
            print(f"{epoch_i + 1:^7} | {'-':^7} | {avg_train_loss:^12.6f} | {val_loss:^10.6f} | {time_elapsed:^9.2f}")
            print("-"*70)
        print("\n")
    
    print("Training complete!")


def evaluate(model, val_dataloader):
    """After the completion of each training epoch, measure the model's performance
    on our validation set.
    """
    # Put the model into the evaluation mode. The dropout layers are disabled during
    # the test time.
    model.eval()

    # Tracking variables
    preds_list = []
    labels_list = []

    # For each batch in our validation set...
    for batch in val_dataloader:
        # Load batch to GPU
        c_txt, mask_txt, c_aud, mask_aud, phq_scores = tuple(t.to(device) for t in batch)

        # Compute predictions
        with torch.no_grad():
            preds = model.forward(c_txt,mask_txt,c_aud,mask_aud)
        preds_list.append(preds.squeeze(1))
        labels_list.append(phq_scores)

    # Compute the CCC, RMSE and MAE
    preds_all = torch.cat(preds_list, dim=0)
    labels_all = torch.cat(labels_list, dim=0)
    val_loss_ccc = 1 - ccc_loss_fn(preds_all, labels_all.float())
    val_loss_rmse = torch.sqrt(loss_fn(preds_all, labels_all.float()))
    val_loss_mae = mae_loss_fn(preds_all,labels_all.float())

    return val_loss_ccc,val_loss_rmse,val_loss_mae

if __name__ == '__main__':
    set_seed(42)    # Set seed for reproducibility

    args = cmdline_args()

    os.environ["TOKENIZERS_PARALLELISM"] = "false"

    if torch.cuda.is_available():
        device = torch.device("cuda")
    else:
        device = torch.device("cpu")
    print(f"# Using device: {device}")

    # Load model from HuggingFace Hub
    tokenizer_txt = AutoTokenizer.from_pretrained('sentence-transformers/all-distilroberta-v1')
    embedder_txt = AutoModel.from_pretrained('sentence-transformers/all-distilroberta-v1').to(device)
    processor_aud = Wav2Vec2FeatureExtractor.from_pretrained("facebook/hubert-base-ls960")
    embedder_aud = HubertModel.from_pretrained("facebook/hubert-base-ls960").to(device)

    # Datasets
    data_train = dds('train',args.data_path,args.label_path,args.missing_video_files)
    data_val = dds('val',args.data_path,args.label_path,args.missing_video_files)
    data_test = dds('test',args.data_path,args.label_path,args.missing_video_files)

    # Define Text Encoder
    txt_model = lstm_regressor_txt()
    # Load trained text model
    txt_model.load_state_dict(torch.load(args.text_checkpoint_path+'-parameters.pt'))
    txt_model.to(device)

    # Define Audio Encoder
    aud_model = lstm_regressor_aud()
    # Load trained audio model
    aud_model.load_state_dict(torch.load(args.audio_checkpoint_path+'-sr16k-parameters.pt'))
    aud_model.to(device)

    # Define TA Model
    model = lstm_regressor()
    model.to(device)

    # Dataloaders
    train_dataloader = DataLoader(data_train,  batch_size=10,shuffle=True)
    val_dataloader = DataLoader(data_val,  batch_size=10)
    test_dataloader = DataLoader(data_test,  batch_size=10)

    num_epochs = 20                  
    
    optimizer = torch.optim.AdamW(model.parameters(),
                      lr=5e-4,    # Default learning rate
                      eps=1e-8    # Default epsilon value
                      )
    
    # Total number of training steps
    total_steps = len(train_dataloader) * num_epochs
    
    # Set up the learning rate scheduler
    scheduler = get_linear_schedule_with_warmup(optimizer,
                                                num_warmup_steps=0, # Default value
                                                num_training_steps=total_steps)

    # Specify loss functions/Metrics
    ccc_loss_fn = ccc_loss()
    loss_fn = nn.MSELoss()
    mae_loss_fn = nn.L1Loss()

    # train(model, train_dataloader, args.ta_checkpoint_path+'-sr16k-parameters.pt', val_dataloader, epochs=num_epochs, evaluation=True)

    # Load trained TA model
    best_lstm_regressor = lstm_regressor()
    best_lstm_regressor.load_state_dict(torch.load(args.ta_checkpoint_path+'-sr16k-parameters.pt'))
    best_lstm_regressor.to(device)

    # Evaluate trained TA model
    print(evaluate(best_lstm_regressor,test_dataloader))