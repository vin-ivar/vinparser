import os
import math
import argparse
import torch
import torch.utils.data
import torch.nn.functional as F
import numpy as np
from torch.autograd import Variable
from Helpers import build_data, process_batch
import Helpers

LSTM_DIM = 400
LSTM_DEPTH = 3
EMBEDDING_DIM = 100
REDUCE_DIM = 500
BATCH_SIZE = 50
EPOCHS = 2
LEARNING_RATE = 2e-3


class Biaffine(torch.nn.Module):

    def __init__(self, in1_features, in2_features):
        super(Biaffine, self).__init__()
        self.in1_features = in1_features
        self.in2_features = in2_features

        self.weight = torch.nn.Parameter(torch.rand(BATCH_SIZE, in1_features, in2_features))
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.weight.size(0))
        self.weight.data.uniform_(-stdv, stdv)

    def forward(self, input1, input2):
        batch_size, len1, dim1 = input1.size()
        ones = torch.ones(batch_size, len1, 1)
        input1 = torch.cat((input1, Variable(ones)), dim=2)

        biaffine = input1 @ self.weight @ input2.transpose(1, 2)
        return biaffine

    def __repr__(self):
        return self.__class__.__name__ + ' (' \
            + 'in1_features=' + str(self.in1_features) \
            + ', in2_features=' + str(self.in2_features) \
            + ', out_features=' + str(self.out_features) + ')'


class RowBiaffine(torch.nn.Module):
    def __init__(self, in1_features, in2_features, dep_labels):
        super().__init__()
        self.in1_features = in1_features
        self.in2_features = in2_features
        self.dep_labels = dep_labels
        self.weight = torch.nn.Parameter(torch.rand(dep_labels, in1_features, in2_features))
        self.bias = torch.nn.Parameter(torch.rand(dep_labels))
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.weight.size(0))
        self.weight.data.uniform_(-stdv, stdv)
        self.bias.data.uniform_(-stdv, stdv)

    def forward(self, input1, input2):
        batch_size, sent_len, dim = input1.size()
        S = []
        for batch in range(batch_size):
            s_i = []
            for word in range(sent_len):
                h_head = input1[batch, word].view(1, -1)
                h_dep = input2[batch, word]
                s_i.append(h_head @ self.weight @ h_dep)
            s_i = torch.stack(s_i)
            S.append(s_i)
        S = torch.stack(S)
        return S.squeeze(3)

    def forward_(self, input1, input2):
        batch_size, sent_len, dim = input1.size()
        S = []
        for batch in range(batch_size):
            s_i = []
            for word in range(sent_len):
                h_head = input1[batch, word].view(1, -1)
                h_dep = input2[batch, word]
                s_i.append(h_head @ self.weight @ h_dep)
            s_i = torch.stack(s_i)
            S.append(s_i)
        S = torch.stack(S)
        return S.squeeze(3)


class LongerBiaffine(torch.nn.Module):
    def __init__(self, in1_features, in2_features, dep_labels):
        super().__init__()
        self.in1_features = in1_features
        self.in2_features = in2_features
        self.dep_labels = dep_labels
        self.weight = torch.nn.Parameter(torch.rand(in1_features + 1, in2_features + 1, dep_labels))
        self.bias = torch.nn.Parameter(torch.rand(dep_labels))
        self.reset_parameters()

    def reset_parameters(self):
        stdv = 1. / math.sqrt(self.weight.size(0))
        self.weight.data.uniform_(-stdv, stdv)
        self.bias.data.uniform_(-stdv, stdv)

    def forward(self, input1, input2):
        batch_size, len1, dim1 = input1.size()
        batch_size, len2, dim2 = input2.size()
        ones = torch.ones(batch_size, len1, 1)
        input1 = torch.cat((input1, Variable(ones)), dim=2)
        input2 = torch.cat((input2, Variable(ones)), dim=2)
        dim1 += 1
        dim2 += 1
        input1 = input1.view(batch_size * len1, dim1)
        weight = self.weight.transpose(1, 2).contiguous().view(dim1, self.dep_labels * dim2)
        affine = (input1 @ weight).view(batch_size, len1 * self.dep_labels, dim2)
        biaffine = (affine @ input2.transpose(1, 2)).view(batch_size, len1, self.dep_labels, len2).transpose(2, 3)
        biaffine += self.bias.expand_as(biaffine)
        return biaffine


class Network(torch.nn.Module):
    def __init__(self, vocab_size, tag_vocab, deprel_vocab):
        super().__init__()
        self.embeddings_forms = torch.nn.Embedding(vocab_size, EMBEDDING_DIM)
        self.embeddings_tags = torch.nn.Embedding(tag_vocab, EMBEDDING_DIM)
        self.lstm = torch.nn.LSTM(2 * EMBEDDING_DIM, LSTM_DIM, LSTM_DEPTH,
                                  batch_first=True, bidirectional=True, dropout=0.33)
        self.mlp_head = torch.nn.Linear(2 * LSTM_DIM, REDUCE_DIM)
        self.mlp_dep = torch.nn.Linear(2 * LSTM_DIM, REDUCE_DIM)
        self.mlp_deprel_head = torch.nn.Linear(2 * LSTM_DIM, REDUCE_DIM)
        self.mlp_deprel_dep = torch.nn.Linear(2 * LSTM_DIM, REDUCE_DIM)
        self.relu = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(p=0.33)
        self.biaffine = Biaffine(REDUCE_DIM + 1, REDUCE_DIM)
        self.label_biaffine = LongerBiaffine(REDUCE_DIM, REDUCE_DIM, deprel_vocab)
        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimiser = torch.optim.Adam(self.parameters(), lr=LEARNING_RATE, betas=(0.9, 0.9))

    def forward(self, forms, tags, pack):
        form_embeds = self.dropout(self.embeddings_forms(forms))

        tag_embeds = self.dropout(self.embeddings_tags(tags))

        embeds = torch.cat([form_embeds, tag_embeds], dim=2)

        embeds = torch.nn.utils.rnn.pack_padded_sequence(embeds, pack, batch_first=True)
        output, _ = self.lstm(embeds)
        output, _ = torch.nn.utils.rnn.pad_packed_sequence(output, batch_first=True)

        reduced_head = self.dropout(self.relu(self.mlp_head(output)))
        reduced_dep = self.dropout(self.relu(self.mlp_dep(output)))

        y_pred_head = self.biaffine(reduced_head, reduced_dep)
        reduced_deprel_head = self.dropout(self.relu(self.mlp_deprel_head(output)))
        reduced_deprel_dep = self.dropout(self.relu(self.mlp_deprel_dep(output)))

        # DEBUGGING
        predicted_labels = y_pred_head.max(2)[1]
        selected_heads = torch.stack([torch.index_select(reduced_deprel_head[n], 0, predicted_labels[n])
                                        for n, _ in enumerate(predicted_labels)])
        y_pred_label = self.label_biaffine(selected_heads, reduced_deprel_dep)
        y_pred_label = Helpers.extract_best_label_logits(predicted_labels, y_pred_label, pack)
        return y_pred_head, y_pred_label

    def train_(self, epoch, train_loader):
        self.train()
        for i, batch in enumerate(train_loader):
            x_forms, x_tags, mask, pack, y_heads, y_deprels = process_batch(batch)

            y_pred_head, y_pred_deprel = self(x_forms, x_tags, pack)
            longest_sentence_in_batch = y_heads.size()[1]
            y_pred_head = y_pred_head.view(BATCH_SIZE * longest_sentence_in_batch, -1)
            y_heads = y_heads.contiguous().view(BATCH_SIZE * longest_sentence_in_batch)
            y_pred_deprel = y_pred_deprel.view(BATCH_SIZE * longest_sentence_in_batch, -1)
            y_deprels = y_deprels.contiguous().view(BATCH_SIZE * longest_sentence_in_batch)

            train_loss = self.criterion(y_pred_head, y_heads) + self.criterion(y_pred_deprel, y_deprels)

            self.zero_grad()
            train_loss.backward()
            self.optimiser.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(epoch, (i + 1) * len(x_forms), len(train_loader.dataset), train_loss.data[0]))

    def evaluate_(self, test_loader):
        LAS_correct, UAS_correct, total = 0, 0, 0
        self.eval()
        for i, batch in enumerate(test_loader):
            x_forms, x_tags, mask, pack, y_heads, y_deprels = process_batch(batch)

            # get labels
            y_pred_head, y_pred_deprel = [i.max(2)[1] for i in self(x_forms, x_tags, pack)]

            heads_correct = ((y_heads == y_pred_head) * mask.type(torch.ByteTensor))
            deprels_correct = ((y_deprels == y_pred_deprel) * mask.type(torch.ByteTensor))
            try:
                UAS_correct += heads_correct.nonzero().size(0)
            except RuntimeError:
                UAS_correct += 0

            try:
                LAS_correct += (heads_correct * deprels_correct) .nonzero().size(0)
            except RuntimeError:
                LAS_correct += 0

            total += mask.nonzero().size(0)

        print("UAS = {}/{} = {}\nLAS = {}/{} = {}".format(UAS_correct, total, UAS_correct / total,
                                                          LAS_correct, total, LAS_correct / total))


if __name__ == '__main__':
    # args
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    conll, train_loader = build_data('sv-ud-train.conllu', BATCH_SIZE)
    _, test_loader = build_data('sv-ud-test.conllu', BATCH_SIZE, conll)

    parser = Network(conll.vocab_size, conll.pos_size, conll.deprel_size)

    # training
    print("Training")
    for epoch in range(EPOCHS):
        parser.train_(epoch, train_loader)

    # test
    print("Eval")
    parser.evaluate_(test_loader)
