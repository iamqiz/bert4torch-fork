#! -*- coding:utf-8 -*-
# global_pointer用来做实体识别
# 数据集：http://s3.bmio.net/kashgari/china-people-daily-ner-corpus.tar.gz
# 博客：https://kexue.fm/archives/8373

import numpy as np
import json
from bert4pytorch.models import build_transformer_model, BaseModel
import torch
from torch.utils.data import DataLoader
import torch.nn as nn
import torch.optim as optim
from bert4pytorch.snippets import sequence_padding, Callback, get_sinusoid_encoding_table, ListDataset
from bert4pytorch.tokenizers import Tokenizer
from bert4pytorch.losses import MultilabelCategoricalCrossentropy
from bert4pytorch.layers import RoPEPositionEncoding


maxlen = 512
batch_size = 6
categories_label2id = {"LOC": 0, "ORG": 1, "PER": 2}
categories_id2label = dict((value, key) for key,value in categories_label2id.items())
ner_vocab_size = len(categories_label2id)
ner_head_size = 64

# BERT base
config_path = 'F:/Projects/pretrain_ckpt/bert/[google_tf_base]--chinese_L-12_H-768_A-12/bert_config.json'
checkpoint_path = 'F:/Projects/pretrain_ckpt/bert/[google_tf_base]--chinese_L-12_H-768_A-12/pytorch_model.bin'
dict_path = 'F:/Projects/pretrain_ckpt/bert/[google_tf_base]--chinese_L-12_H-768_A-12/vocab.txt'

device = 'cuda' if torch.cuda.is_available() else 'cpu'

# 加载数据集
class MyDataset(ListDataset):
    @staticmethod
    def load_data(filename):
        data = []
        with open(filename, encoding='utf-8') as f:
            f = f.read()
            for l in f.split('\n\n'):
                if not l:
                    continue
                text, label = '', []
                for i, c in enumerate(l.split('\n')):
                    char, flag = c.split(' ')
                    text += char
                    if flag[0] == 'B':
                        label.append([i, i, flag[2:]])
                    elif flag[0] == 'I':
                        label[-1][1] = i
                text_list = tokenizer.tokenize(text)[1:-1]  #不保留首位[CLS]和末位[SEP]
                tokens = [j for i in text_list for j in i][:maxlen]
                data.append((tokens, label))
        return data


# 建立分词器
tokenizer = Tokenizer(dict_path, do_lower_case=True)

def collate_fn(batch):
    batch_token_ids = []
    max_seq_len = min(max([len(tokens) for tokens, _ in batch]), maxlen)
    batch_labels = torch.zeros((len(batch), len(categories_label2id), max_seq_len, max_seq_len), device=device)
    for i, (tokens, labels) in enumerate(batch):
        batch_token_ids.append(tokenizer.tokens_to_ids(tokens))
        for s_i in labels:
            if s_i[1] >= max_seq_len:  # 实体的结尾超过文本长度，则不标记
                continue
            batch_labels[i, categories_label2id[s_i[-1]], s_i[0], s_i[1]] = 1
    batch_token_ids = torch.tensor(sequence_padding(batch_token_ids), dtype=torch.long, device=device)
    return batch_token_ids, batch_labels

# 转换数据集
train_dataloader = DataLoader(MyDataset('F:/Projects/data/corpus/ner/china-people-daily-ner-corpus/example.train'), batch_size=batch_size, shuffle=True, collate_fn=collate_fn) 
valid_dataloader = DataLoader(MyDataset('F:/Projects/data/corpus/ner/china-people-daily-ner-corpus/example.dev'), batch_size=batch_size, collate_fn=collate_fn) 

# 定义bert上的模型结构
class Model(BaseModel):
    def __init__(self):
        super().__init__()
        self.bert = build_transformer_model(config_path=config_path, checkpoint_path=checkpoint_path, segment_vocab_size=0)
        self.p_dense = nn.Linear(768, ner_head_size * 2)
        self.q_dense = nn.Linear(ner_head_size * 2, ner_vocab_size * 2)
        self.RoPE = True
        if self.RoPE:
            self.position_embedding = RoPEPositionEncoding(maxlen, ner_head_size)

    def forward(self, token_ids):
        sequence_output = self.bert([token_ids])  # [btz, seq_len, hdsz]
        sequence_output = self.p_dense(sequence_output)  # [bts, seq_len, ner_head_size * 2]
        qw, kw = sequence_output[:, :, :ner_head_size], sequence_output[:, :, ner_head_size:]  # [bts, seq_len, ner_head_size]

        # ROPE编码
        if self.RoPE:
            qw = self.position_embedding(qw)
            kw = self.position_embedding(kw)

        logits = torch.einsum('bmd,bnd->bmn', qw, kw) / ner_head_size**0.5  # [btz, seq_len, seq_len]
        btz, seq_len = token_ids.shape[:2]
        bias = self.q_dense(sequence_output).reshape(btz, seq_len, ner_vocab_size, 2).transpose(1, 2) / 2  # [btz, ner_vocab_size, seq_len, 2]
        logits = logits.unsqueeze(1) + bias[:, :, :, :1] + bias[:, :, :, 1:].transpose(2, 3)

        # 排除padding
        attention_mask = token_ids.gt(0).long()
        attention_mask1 = 1 - attention_mask.unsqueeze(1).unsqueeze(3)  # [btz, 1, seq_len, 1]
        attention_mask2 = 1 - attention_mask.unsqueeze(1).unsqueeze(2)  # [btz, 1, 1, seq_len]
        logits = logits.masked_fill(attention_mask1.gt(0), value=-float('inf'))
        logits = logits.masked_fill(attention_mask2.gt(0), value=-float('inf'))

        # 排除下三角
        logits = logits - torch.tril(torch.ones_like(logits), -1) * 1e12

        # scale返回
        return logits
        

model = Model().to(device)
model.compile(loss=MultilabelCategoricalCrossentropy(), optimizer=optim.Adam(model.parameters(), lr=2e-5))


def evaluate(data, threshold=0.5):
    X, Y, Z, threshold = 1e-10, 1e-10, 1e-10, 0
    for x_true, label in data:
        scores = model.predict(x_true)
        for i, score in enumerate(scores):
            R = set()
            for l, start, end in zip(*np.where(score.cpu() > threshold)):
                R.add((start, end, categories_id2label[l]))  

            T = set()
            for l, start, end in zip(*np.where(label[i].cpu() > threshold)):
                T.add((start, end, categories_id2label[l]))
            X += len(R & T)
            Y += len(R)
            Z += len(T)
    f1, precision, recall = 2 * X / (Y + Z), X / Y, X / Z
    return f1, precision, recall


class Evaluator(Callback):
    """评估与保存
    """
    def __init__(self):
        self.best_val_f1 = 0.

    def on_epoch_end(self, steps, epoch, logs=None):
        f1, precision, recall = evaluate(valid_dataloader)
        if f1 > self.best_val_f1:
            self.best_val_f1 = f1
            # model.save_weights('best_model.pt')
        print(f'[val] f1: {f1:.5f}, p: {precision:.5f} r: {recall:.5f} best_f1: {self.best_val_f1:.5f}')


if __name__ == '__main__':

    evaluator = Evaluator()

    model.fit(train_dataloader, epochs=50, steps_per_epoch=100, callbacks=[evaluator])

else: 

    model.load_weights('best_model.pt')
