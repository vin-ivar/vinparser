import sys
import configparser
import argparse
import torch
import Loader
import torch.nn.functional as F
from torch.autograd import Variable
from Parser import build_data
from Helpers import process_batch

parser = argparse.ArgumentParser()
parser.add_argument('--debug', action='store_true')
parser.add_argument('--cuda', action='store_true')
parser.add_argument('--config', default='./config.ini')
parser.add_argument('--train', action='append')
parser.add_argument('--dev', action='append')
parser.add_argument('--test', action='append')
parser.add_argument('--embed', action='append')
args = parser.parse_args()

config = configparser.ConfigParser()
config.read(args.config)

BATCH_SIZE = int(config['tagger']['BATCH_SIZE'])
EMBED_DIM = int(config['tagger']['EMBED_DIM'])
LSTM_DIM = int(config['tagger']['LSTM_DIM'])
LSTM_LAYERS = int(config['tagger']['LSTM_LAYERS'])
MLP_DIM = int(config['tagger']['MLP_DIM'])
LEARNING_RATE = float(config['tagger']['LEARNING_RATE'])
EPOCHS = int(config['tagger']['EPOCHS'])


class CLTagger(torch.nn.Module):
    def __init__(self, main_loader, aux_loader):
        super().__init__()

        self.main_loader = main_loader
        self.aux_loader = aux_loader
        #Load pretrained embeds
        self.embeds_main = torch.nn.Embedding(main_loader['sizes']['vocab'], EMBED_DIM)
        self.embeds_main.weight.data.copy_(main_loader['vocab'].vectors)
        self.embeds_aux = torch.nn.Embedding(aux_loader['sizes']['vocab'], EMBED_DIM)
        self.embeds_aux.weight.data.copy_(aux_loader['vocab'].vectors)
        #Pass through shared then individual LSTMs
        self.lstm_shared = torch.nn.LSTM(EMBED_DIM, LSTM_DIM, LSTM_LAYERS, batch_first=True, bidirectional=True, dropout=0.5)
        self.lstm_main = torch.nn.LSTM(LSTM_DIM * 2, LSTM_DIM, LSTM_LAYERS, batch_first=True, bidirectional=True, dropout=0.5)
        self.lstm_aux = torch.nn.LSTM(LSTM_DIM * 2, LSTM_DIM, LSTM_LAYERS, batch_first=True, bidirectional=True, dropout=0.5)
        #Pass through individual MLPs
        self.relu = torch.nn.ReLU()
        self.mlp_main = torch.nn.Linear(LSTM_DIM * 2, MLP_DIM)
        self.mlp_aux = torch.nn.Linear(LSTM_DIM * 2, MLP_DIM)
        #Outs
        self.out_main = torch.nn.Linear(MLP_DIM, main_loader['sizes']['postags'])
        self.out_aux = torch.nn.Linear(MLP_DIM, aux_loader['sizes']['postags'])
        #Losses
        self.criterion_main = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.criterion_aux = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=LEARNING_RATE, betas=(0.9, 0.9))
        self.dropout = torch.nn.Dropout(p=0.5)

    def forward_main(self, forms, pack):
        # embeds + dropout
        form_embeds = self.dropout(self.embeds_main(forms))

        # pack/unpack for LSTM
        packed = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        lstm_out, _ = self.lstm_shared(packed)
        lstm_out, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True)
        # lstm_out_main, _ = self.lstm_main(lstm_out)
        # lstm_out_main, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out_main, batch_first=True)

        # LSTM => dense ReLU
        mlp_out = self.dropout(self.relu(self.mlp_main(lstm_out)))

        # reduce to dim no_of_tags
        return self.out_main(mlp_out)

    def forward_aux(self, forms, pack):
        # embeds + dropout
        form_embeds = self.dropout(self.embeds_aux(forms))

        # pack/unpack for LSTM
        packed = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        lstm_out, _ = self.lstm_shared(packed)
        lstm_out, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True)
        # lstm_out_aux, _ = self.lstm_aux(lstm_out)
        # lstm_out_aux, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out_aux, batch_first=True)

        # LSTM => dense ReLU
        mlp_out = self.dropout(self.relu(self.mlp_aux(lstm_out)))

        # reduce to dim no_of_tags
        return self.out_aux(mlp_out)

def train(model, epoch, train_loaders):
    model.train()

    def get_loss(train_loader, type_task="main"):
        train_loader["train"].init_epoch()
        for i, batch in enumerate(train_loader["train"]):
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            if type_task == "aux":
                y_pred = model.forward_aux(x_forms, pack)
            else:
                y_pred = model.forward_main(x_forms, pack)                
            # reshape for cross-entropy
            batch_size, longest_sentence_in_batch = x_forms.size()

            # predictions: (B x S x T) => (B * S, T)
            # heads: (B x S) => (B * S)
            y_pred = y_pred.view(batch_size * longest_sentence_in_batch, -1)
            x_tags = x_tags.contiguous().view(batch_size * longest_sentence_in_batch)

            if type_task == "aux":
                train_loss = model.criterion_aux(y_pred, x_tags)
            else:
                train_loss = model.criterion_main(y_pred, x_tags)

            model.zero_grad()
            train_loss.backward()
            model.optimizer.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(
                epoch, (i + 1) * len(x_forms), len(train_loader["train"].dataset), train_loss.data))

    print("Training main task...")
    print("Training aux task...")
    get_loss(train_loaders[0], type_task="main")
    get_loss(train_loaders[1], type_task="aux")

def evaluate(model, test_loader, type_task="main"):
    correct, total = 0, 0
    model.eval()
    for i, batch in enumerate(test_loader):
        (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel
        
        mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
        for n, size in enumerate(pack):
            mask[n, 0:size] = 1

            # get tags
        if type_task == "aux":
            y_pred = model.forward_aux(x_forms, pack).max(2)[1]
        else:
            y_pred = model.forward_main(x_forms, pack).max(2)[1]
            
        mask = Variable(mask.type(torch.ByteTensor))
        
        correct += ((x_tags == y_pred) * mask).nonzero().size(0)

        total += mask.nonzero().size(0)

    print("Accuracy = {}/{} = {}".format(correct, total, (correct / total)))

def main():

    loaders = Loader.get_iterators_cl(args, [100,10])

    tagger = CLTagger(loaders[0], loaders[1])
    if args.cuda:
        tagger.cuda()

    # training
    print("Training")
    for epoch in range(EPOCHS):
        train(tagger, epoch, loaders)
        print("Main task dev acc.:")
        evaluate(tagger, loaders[0]["dev"], type_task="main")
        print("Aux task dev acc.:")
        evaluate(tagger, loaders[1]["dev"], type_task="aux")

    # test
    print("Eval")
    print("Main task dev acc.:")
    evaluate(tagger, loaders[0]["test"], type_task="main")
    print("Aux task dev acc.:")
    evaluate(tagger, loaders[1]["test"], type_task="main")

if __name__ == '__main__':
    main()
