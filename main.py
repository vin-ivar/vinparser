import os
import sys
import math
import argparse
import torch
import torch.utils.data
import torch.nn.functional as F
from torch.autograd import Variable
from conllu import ConllParser

LSTM_DIM = 40
LSTM_DEPTH = 3
EMBEDDING_DIM = 100
REDUCE_DIM = 500
BATCH_SIZE = 10
EPOCHS = 2
LEARNING_RATE = 2e-3
DEBUG_SIZE = 200


class BiasedBilinear(torch.nn.Module):
    def __init__(self, batch_size, dim_features):
        super().__init__()
        self.batch_size = batch_size
        self.dim_features = dim_features
        self.weight = torch.nn.Parameter(torch.Tensor(batch_size, dim_features + 1, dim_features))
        self.reset_params()

    def reset_params(self):
        stdv = 1. / math.sqrt(self.weight.size(1))
        self.weight.data.uniform_(-stdv, stdv)

    def forward(self, reduced_dep, reduced_head):
        return reduced_dep @ self.weight @ reduced_head.transpose(1, 2)


class Network(torch.nn.Module):
    def __init__(self, vocab_size):
        super().__init__()
        self.embeddings_forms = torch.nn.Embedding(vocab_size, EMBEDDING_DIM)
        self.embeddings_tags = torch.nn.Embedding(vocab_size, EMBEDDING_DIM)
        self.lstm = torch.nn.LSTM(2 * EMBEDDING_DIM, LSTM_DIM, LSTM_DEPTH,
                                  batch_first=True, bidirectional=True, dropout=0.33)
        self.mlp_head = torch.nn.Linear(2 * LSTM_DIM, REDUCE_DIM)
        self.mlp_dep = torch.nn.Linear(2 * LSTM_DIM, REDUCE_DIM)
        self.biaffine_weight = torch.nn.Parameter(torch.rand(BATCH_SIZE, REDUCE_DIM + 1, REDUCE_DIM), requires_grad=True)
        self.softmax = torch.nn.LogSoftmax(dim=2)
        self.criterion = torch.nn.NLLLoss(reduce=False)
        self.optimiser = torch.optim.Adam(self.parameters(), lr=LEARNING_RATE, betas=(0.9, 0.9))
        self.weird_thing = BiasedBilinear(BATCH_SIZE, REDUCE_DIM)
        self.dropout = torch.nn.Dropout(p=0.33)

    def forward(self, forms, tags):
        # for debug:
        MAX_SENT = forms.size(1)
        form_embeds = self.dropout(self.embeddings_forms(forms))
        assert form_embeds.shape == torch.Size([BATCH_SIZE, MAX_SENT, EMBEDDING_DIM])

        tag_embeds = self.dropout(self.embeddings_forms(tags))
        assert tag_embeds.shape == torch.Size([BATCH_SIZE, MAX_SENT, EMBEDDING_DIM])

        embeds = torch.cat([form_embeds, tag_embeds], dim=2)
        output, (h_n, c_n) = self.lstm(embeds)
        assert output.shape == torch.Size([BATCH_SIZE, MAX_SENT, 2 * LSTM_DIM])

        reduced_head = F.relu(self.mlp_head(output))
        assert reduced_head.shape == torch.Size([BATCH_SIZE, MAX_SENT, REDUCE_DIM])

        reduced_dep = F.relu(self.mlp_dep(output))
        bias = Variable(torch.ones(BATCH_SIZE, MAX_SENT, 1))
        reduced_dep = torch.cat([reduced_dep, bias], 2)
        assert reduced_dep.shape == torch.Size([BATCH_SIZE, MAX_SENT, REDUCE_DIM + 1])

        # ROW IS DEP, COL IS HEAD
        # y_pred = self.softmax(reduced_dep @ self.biaffine_weight @ reduced_head.transpose(1, 2))
        y_pred = self.softmax(self.weird_thing(reduced_dep, reduced_head))
        return y_pred

    def train_(self, epoch, train_loader):
        self.train()
        for i, (forms, tags, labels, sizes) in enumerate(train_loader):
            X1 = Variable(forms)
            X2 = Variable(tags)
            y = Variable(labels, requires_grad=False)
            mask = Variable(sizes)
            y_pred = self(X1, X2)
            train_loss = (self.criterion(y_pred, y) * mask).sum().sum() / mask.nonzero().size(0)
            self.optimiser.zero_grad()
            train_loss.backward()
            self.optimiser.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(epoch, (i + 1) * len(forms), len(train_loader.dataset), train_loss.data[0]))

    def evaluate_(self, test_loader):
        correct = 0
        total_deps = 0
        self.eval()
        for i, (forms, tags, labels, sizes) in enumerate(test_loader):
            X1 = Variable(forms)
            X2 = Variable(tags)
            y = Variable(labels, requires_grad=False)
            mask = Variable(sizes.type(torch.ByteTensor))
            y_pred = self(X1, X2)
            try:
                correct += ((y == y_pred.max(2)[1]) * mask).nonzero().size(0)
            except RuntimeError:
                correct += 0
            total_deps += mask.nonzero().size(0)

        print("Accuracy = {}/{} = {}".format(correct, total_deps, (correct / total_deps)))


def build_data(fname):
    # build data
    with open(os.path.join('data', fname), 'r') as f:
        conll = ConllParser(f)

    # sentences
    print("Preparing %s.." % fname)
    forms, rels, tags = conll.get_tensors()
    assert forms.shape == torch.Size([len(conll), conll.longest_sent])
    assert tags.shape == torch.Size([len(conll), conll.longest_sent])

    # labels
    labels = torch.zeros(forms.shape[0], conll.longest_sent, 1)
    for batch_no, _ in enumerate(rels):
        for rel in rels[batch_no]:
            if rel[1] == 0:
                continue
            labels[batch_no, rel[1]] = rel[0]

    labels = torch.squeeze(labels.type(torch.LongTensor))
    assert labels.shape == torch.Size([len(conll), conll.longest_sent])

    # sizes
    sizes_int = torch.zeros(len(conll)).view(-1, 1).type(torch.LongTensor)
    sizes = torch.zeros(len(conll), conll.longest_sent)
    for n, form in enumerate(forms):
        sizes_int[n] = form[form != 0].shape[0]

    for n, size in enumerate(sizes_int):
        sizes[n, 1:size[0]] = 1

    assert sizes.shape == torch.Size([len(conll), conll.longest_sent])

    # build loader & model
    data = list(zip(forms, tags, labels, sizes))[:DEBUG_SIZE]
    loader = torch.utils.data.DataLoader(data, batch_size=BATCH_SIZE, shuffle=True, drop_last=True)

    return conll, loader


def main():
    # args
    parser = argparse.ArgumentParser()
    parser.add_argument('--debug', action='store_true')
    args = parser.parse_args()

    conll, train_loader = build_data('sv-ud-train.conllu')
    _, test_loader = build_data('sv-ud-test.conllu')

    parser = Network(conll.vocab_size)
    # training
    print("Training")
    for epoch in range(EPOCHS):
        parser.train_(epoch, train_loader)

    # test
    print("Eval")
    parser.evaluate_(test_loader)


if __name__ == '__main__':
    main()
