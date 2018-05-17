import torch
import pprint
import Helpers
from scripts import cle
from torch.autograd import Variable
import torch.nn.functional as F
from Modules import CharEmbedding, ShorterBiaffine, LongerBiaffine, WeightedCombUnb


class Tagger(torch.nn.Module):
    def __init__(self, sizes, args, embeddings=None, embed_dim=100, lstm_dim=100, lstm_layers=3,
                 mlp_dim=100, learning_rate=1e-5):
        super().__init__()

        self.embeds = torch.nn.Embedding(sizes['vocab'], embed_dim)
        self.embeds.weight.data.copy_(embeddings.vectors)
        self.lstm = torch.nn.LSTM(embed_dim, lstm_dim, lstm_layers, batch_first=True, bidirectional=True, dropout=0.5)
        self.relu = torch.nn.ReLU()
        self.mlp = torch.nn.Linear(2 * lstm_dim, mlp_dim)
        self.out = torch.nn.Linear(mlp_dim, sizes['postags'])
        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate, betas=(0.9, 0.9))
        self.dropout = torch.nn.Dropout(p=0.5)

    def forward(self, forms, pack):
        # embeds + dropout
        form_embeds = self.dropout(self.embeds(forms))

        # pack/unpack for LSTM
        packed = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        lstm_out, _ = self.lstm(packed)
        lstm_out, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out, batch_first=True)

        # LSTM => dense ReLU
        mlp_out = self.dropout(self.relu(self.mlp(lstm_out)))

        # reduce to dim no_of_tags
        return self.out(mlp_out)

    def train_(self, epoch, train_loader):
        self.train()
        train_loader.init_epoch()

        for i, batch in enumerate(train_loader):
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            y_pred = self(x_forms, pack)

            # reshape for cross-entropy
            batch_size, longest_sentence_in_batch = x_forms.size()

            # predictions: (B x S x T) => (B * S, T)
            # heads: (B x S) => (B * S)
            y_pred = y_pred.view(batch_size * longest_sentence_in_batch, -1)
            x_tags = x_tags.contiguous().view(batch_size * longest_sentence_in_batch)

            train_loss = self.criterion(y_pred, x_tags)

            self.zero_grad()
            train_loss.backward()
            self.optimizer.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(
                epoch, (i + 1) * len(x_forms), len(train_loader.dataset), train_loss.data[0]))

    def evaluate_(self, test_loader):
        correct, total = 0, 0
        self.eval()
        for i, batch in enumerate(test_loader):
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            # get tags
            y_pred = self(x_forms, pack).max(2)[1]

            mask = Variable(mask.type(torch.ByteTensor))

            correct += ((x_tags == y_pred) * mask).nonzero().size(0)

            total += mask.nonzero().size(0)

        print("Accuracy = {}/{} = {}".format(correct, total, (correct / total)))


class Parser(torch.nn.Module):
    def __init__(self, sizes, args, vocab, embeddings=None, embed_dim=100, lstm_dim=400, lstm_layers=3,
                 reduce_dim_arc=100, reduce_dim_label=100, learning_rate=1e-3):
        super().__init__()

        self.use_cuda = args.use_cuda
        self.use_chars = args.use_chars
        self.save = args.save
        self.vocab = vocab
        # for writer
        self.test_file = args.test[0]

        if self.use_chars:
            self.embeddings_chars = CharEmbedding(sizes['chars'], embed_dim, lstm_dim, lstm_layers)

        self.embeddings_forms = torch.nn.Embedding(sizes['vocab'], embed_dim)
        self.embeddings_tags = torch.nn.Embedding(sizes['postags'], embed_dim)
        self.lstm = torch.nn.LSTM(2 * embed_dim, lstm_dim, lstm_layers,
                                  batch_first=True, bidirectional=True, dropout=0.33)
        self.mlp_head = torch.nn.Linear(2 * lstm_dim, reduce_dim_arc)
        self.mlp_dep = torch.nn.Linear(2 * lstm_dim, reduce_dim_arc)
        self.mlp_deprel_head = torch.nn.Linear(2 * lstm_dim, reduce_dim_label)
        self.mlp_deprel_dep = torch.nn.Linear(2 * lstm_dim, reduce_dim_label)
        self.relu = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(p=0.33)
        # self.biaffine = Biaffine(reduce_dim_arc + 1, reduce_dim_arc, BATCH_SIZE)
        self.biaffine = ShorterBiaffine(reduce_dim_arc)
        self.label_biaffine = LongerBiaffine(reduce_dim_label, reduce_dim_label, sizes['deprels'])
        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimiser = torch.optim.Adam(self.parameters(), lr=learning_rate, betas=(0.9, 0.9))

        if self.use_cuda:
            self.biaffine.cuda()
            self.label_biaffine.cuda()

    def forward(self, forms, tags, pack, chars, char_pack):
        form_embeds = self.dropout(self.embeddings_forms(forms))
        tag_embeds = self.dropout(self.embeddings_tags(tags))
        composed_embeds = form_embeds

        if self.use_chars:
            composed_embeds += self.dropout(self.embeddings_chars(chars, char_pack))

        embeds = torch.cat([composed_embeds, tag_embeds], dim=2)

        # pack/unpack for LSTM
        embeds = torch.nn.utils.rnn.pack_padded_sequence(embeds, pack.tolist(), batch_first=True)
        output, _ = self.lstm(embeds)
        output, _ = torch.nn.utils.rnn.pad_packed_sequence(output, batch_first=True)

        # predict heads
        reduced_head_head = self.dropout(self.relu(self.mlp_head(output)))
        reduced_head_dep = self.dropout(self.relu(self.mlp_dep(output)))
        y_pred_head = self.biaffine(reduced_head_head, reduced_head_dep)

        # predict deprels using heads
        reduced_deprel_head = self.dropout(self.relu(self.mlp_deprel_head(output)))
        reduced_deprel_dep = self.dropout(self.relu(self.mlp_deprel_dep(output)))
        predicted_labels = y_pred_head.max(2)[1]
        selected_heads = torch.stack([torch.index_select(reduced_deprel_head[n], 0, predicted_labels[n])
                                        for n, _ in enumerate(predicted_labels)])
        y_pred_label = self.label_biaffine(selected_heads, reduced_deprel_dep)
        y_pred_label = Helpers.extract_best_label_logits(predicted_labels, y_pred_label, pack)
        if self.use_cuda:
            y_pred_label = y_pred_label.cuda()

        return y_pred_head, y_pred_label

    '''
    1. the bare minimum that needs to be loaded is forms, upos, head, deprel (could change later); load those
    2. initialise everything else to none; load it if necessary based on command line args
    3. pass everything, whether it's been loaded or not, to the forward function; if it's unnecessary it won't use it
    '''
    def train_(self, epoch, train_loader):
        self.train()
        train_loader.init_epoch()

        for i, batch in enumerate(train_loader):
            chars, length_per_word_per_sent = None, None
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            # TODO: add something similar for semtags
            if self.use_chars:
                (chars, _, length_per_word_per_sent) = batch.char

            y_pred_head, y_pred_deprel = self(x_forms, x_tags, pack, chars, length_per_word_per_sent)

            # reshape for cross-entropy
            batch_size, longest_sentence_in_batch = y_heads.size()

            # predictions: (B x S x S) => (B * S x S)
            # heads: (B x S) => (B * S)
            y_pred_head = y_pred_head.view(batch_size * longest_sentence_in_batch, -1)
            y_heads = y_heads.contiguous().view(batch_size * longest_sentence_in_batch)

            # predictions: (B x S x D) => (B * S x D)
            # heads: (B x S) => (B * S)
            y_pred_deprel = y_pred_deprel.view(batch_size * longest_sentence_in_batch, -1)
            y_deprels = y_deprels.contiguous().view(batch_size * longest_sentence_in_batch)

            # sum losses
            train_loss = self.criterion(y_pred_head, y_heads) + self.criterion(y_pred_deprel, y_deprels)

            self.zero_grad()
            train_loss.backward()
            self.optimiser.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(epoch, (i + 1) * len(x_forms), len(train_loader.dataset), train_loss.data[0]))

        if self.save:
            with open(self.save[0], "wb") as f:
                torch.save(self.state_dict(), f)

    def evaluate_(self, test_loader):
        las_correct, uas_correct, total = 0, 0, 0
        self.eval()
        for i, batch in enumerate(test_loader):
            chars, length_per_word_per_sent = None, None
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            # TODO: add something similar for semtags
            if self.use_chars:
                (chars, _, length_per_word_per_sent) = batch.char

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            # get labels
            # TODO: ensure well-formed tree
            y_pred_head, y_pred_deprel = [i.max(2)[1] for i in
                                          self(x_forms, x_tags, pack, chars, length_per_word_per_sent)]

            mask = mask.type(torch.ByteTensor)
            if self.use_cuda:
                mask = mask.cuda()

            mask = Variable(mask)
            mask[0, 0] = 0
            heads_correct = ((y_heads == y_pred_head) * mask)
            deprels_correct = ((y_deprels == y_pred_deprel) * mask)

            # excepts should never trigger; leave them in just in case
            try:
                uas_correct += heads_correct.nonzero().size(0)
            except RuntimeError:
                pass

            try:
                las_correct += (heads_correct * deprels_correct) .nonzero().size(0)
            except RuntimeError:
                pass

            total += mask.nonzero().size(0)


            deprel_vocab = self.vocab[1]
            deprels = [deprel_vocab.itos[i.data[0]] for i in y_pred_deprel.view(-1, 1)]

            heads_softmaxes = self(x_forms, x_tags, pack, chars, length_per_word_per_sent)[0][0]
            heads_softmaxes = F.softmax(heads_softmaxes, dim=1)
            json = cle.mst(heads_softmaxes.data.numpy())


#            json = cle.mst(i, pad) for i, pad in zip(self(x_forms, x_tags, pack, chars,
#                                                           length_per_word_per_sent)[0], pack)

            Helpers.write_to_conllu(self.test_file, json, deprels, i)

        print("UAS = {}/{} = {}\nLAS = {}/{} = {}".format(uas_correct, total, uas_correct / total,
                                                          las_correct, total, las_correct / total))


class CLTagger(torch.nn.Module):
    def __init__(self, args, main_sizes, aux_sizes, main_embeds, aux_embeds, embed_dim=100, lstm_dim=100, lstm_layers=2,
                 mlp_dim=100, learning_rate=1e-5):

        super().__init__()

        #Load pretrained embeds
        self.embeds_main = torch.nn.Embedding(main_sizes['vocab'], embed_dim)
        self.embeds_aux = torch.nn.Embedding(aux_sizes['vocab'], embed_dim)

        if args.embed:
            self.embeds_main.weight.data.copy_(main_embeds.vectors)
            self.embeds_aux.weight.data.copy_(aux_embeds.vectors)

        #Pass through shared then individual LSTMs
        self.lstm_shared = torch.nn.LSTM(embed_dim, lstm_dim, lstm_layers, batch_first=True, bidirectional=True, dropout=0.5)
        self.lstm_main = torch.nn.LSTM(lstm_dim * 2, lstm_dim, lstm_layers, batch_first=True, bidirectional=True, dropout=0.5)
        self.lstm_aux = torch.nn.LSTM(lstm_dim * 2, lstm_dim, lstm_layers, batch_first=True, bidirectional=True, dropout=0.5)

        #Pass through individual MLPs
        self.relu = torch.nn.ReLU()
        self.mlp_main = torch.nn.Linear(lstm_dim * 2, mlp_dim)
        self.mlp_aux = torch.nn.Linear(lstm_dim * 2, mlp_dim)
        #Outs
        self.out_main = torch.nn.Linear(mlp_dim, main_sizes['postags'])
        self.out_aux = torch.nn.Linear(mlp_dim, aux_sizes['postags'])
        #Losses
        self.criterion_main = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.criterion_aux = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimizer = torch.optim.Adam(self.parameters(), lr=learning_rate, betas=(0.9, 0.9))
        self.dropout = torch.nn.Dropout(p=0.5)

    def forward(self, forms, pack, type_task):
        if type_task == "main":
            return self.forward_main(forms, pack)
        elif type_task == "aux":
            return self.forward_aux(forms, pack)
        else:
            raise TypeError

    def forward_main(self, forms, pack):
        # embeds + dropout
        form_embeds = self.dropout(self.embeds_main(forms))

        # pack/unpack for LSTM
        packed = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        lstm_out, _ = self.lstm_shared(packed)
        lstm_out_main, _ = self.lstm_main(lstm_out)
        lstm_out_main, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out_main, batch_first=True)

        # LSTM => dense ReLU
        mlp_out = self.dropout(self.relu(self.mlp_main(lstm_out_main)))

        # reduce to dim no_of_tags
        return self.out_main(mlp_out)

    def forward_aux(self, forms, pack):
        # embeds + dropout
        form_embeds = self.dropout(self.embeds_aux(forms))

        # pack/unpack for LSTM
        packed = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        lstm_out, _ = self.lstm_shared(packed)
        lstm_out_aux, _ = self.lstm_aux(lstm_out)
        lstm_out_aux, _ = torch.nn.utils.rnn.pad_packed_sequence(lstm_out_aux, batch_first=True)

        # LSTM => dense ReLU
        mlp_out = self.dropout(self.relu(self.mlp_aux(lstm_out_aux)))

        # reduce to dim no_of_tags
        return self.out_aux(mlp_out)

    def train_(self, epoch, train_loader, type_task="main"):


        self.train()
        train_loader.init_epoch()

        for i, batch in enumerate(train_loader):
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            y_pred = self(x_forms, pack, type_task)
            # reshape for cross-entropy
            batch_size, longest_sentence_in_batch = x_forms.size()

            # predictions: (B x S x T) => (B * S, T)
            # heads: (B x S) => (B * S)
            y_pred = y_pred.view(batch_size * longest_sentence_in_batch, -1)
            x_tags = x_tags.contiguous().view(batch_size * longest_sentence_in_batch)

            if type_task == "aux":
                train_loss = self.criterion_aux(y_pred, x_tags)
            else:
                train_loss = self.criterion_main(y_pred, x_tags)

            self.zero_grad()
            train_loss.backward()
            self.optimizer.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(
                epoch, (i + 1) * len(x_forms), len(train_loader.dataset), train_loss.data))

    def evaluate_(self, test_loader, type_task="main"):
        correct, total = 0, 0
        self.eval()
        for i, batch in enumerate(test_loader):
            (x_forms, pack), x_tags, y_heads, y_deprels = batch.form, batch.upos, batch.head, batch.deprel

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

                # get tags
            y_pred = self(x_forms, pack, type_task).max(2)[1]
            mask = Variable(mask.type(torch.ByteTensor))

            correct += ((x_tags == y_pred) * mask).nonzero().size(0)

            total += mask.nonzero().size(0)

        print("Accuracy = {}/{} = {}".format(correct, total, (correct / total)))


class TagTwiceParserLearn(torch.nn.Module):
    def __init__(self, sizes, args, vocab, embeddings=None, embed_dim=100, lstm_dim=500, lstm_layers=2,
                 reduce_dim_arc=400, reduce_dim_label=100, learning_rate=1e-4):
        super().__init__()

        self.use_cuda = args.use_cuda
        self.use_chars = args.use_chars
        self.save = args.save
        self.vocab = vocab
        # for writer
        self.test_file = args.test[0]

        if self.use_chars:
            self.embeddings_chars = CharEmbedding(sizes['chars'], embed_dim, lstm_dim, lstm_layers)

        self.embeddings_forms = torch.nn.Embedding(sizes['vocab'], embed_dim)
        self.embeddings_forms.weight.data.copy_(vocab[0].vectors)

        self.embeddings_forms_rand = torch.nn.Embedding(sizes['vocab'], embed_dim)
     #   self.embeddings_tags = torch.nn.Embedding(sizes['postags'], embed_dim)
        self.lstm = torch.nn.LSTM(500 + sizes['postags'], lstm_dim, lstm_layers,
                                  batch_first=True, bidirectional=True, dropout=0.33)
        self.mlp_head = torch.nn.Linear(2 * lstm_dim, reduce_dim_arc)
        self.mlp_dep = torch.nn.Linear(2 * lstm_dim, reduce_dim_arc)
        self.mlp_deprel_head = torch.nn.Linear(2 * lstm_dim, reduce_dim_label)
        self.mlp_deprel_dep = torch.nn.Linear(2 * lstm_dim, reduce_dim_label)

        #pos
        self.mlp_tag = torch.nn.Linear(300, 150)
        self.out_tag = torch.nn.Linear(150, sizes['postags'])
        #sem
        self.mlp_semtag = torch.nn.Linear(1000, 200)
        self.out_semtag = torch.nn.Linear(200, sizes['semtags'])
        self.lstm_tag = torch.nn.LSTM(embed_dim * 2, 150, 1,
                                  batch_first=True, bidirectional=True, dropout=0.33)

        self.lstm_semtag = torch.nn.LSTM(embed_dim * 5 + sizes['postags'], lstm_dim, 1,
                                  batch_first=True, bidirectional=True, dropout=0.33)

        self.relu = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(p=0.33)
        # self.biaffine = Biaffine(reduce_dim_arc + 1, reduce_dim_arc, BATCH_SIZE)
        self.biaffine = ShorterBiaffine(reduce_dim_arc)
        self.label_biaffine = LongerBiaffine(reduce_dim_label, reduce_dim_label, sizes['deprels'])
        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimiser = torch.optim.Adam(self.parameters(), lr=learning_rate, betas=(0.9, 0.9))

        self.weightedcomb_unb = WeightedCombUnb(embed_dim * 10, embed_dim * 10)

        if self.use_cuda:
            self.biaffine.cuda()
            self.label_biaffine.cuda()

    def forward(self, forms, tags, sem, pack, chars, char_pack):
        form_embeds = self.dropout(self.embeddings_forms(forms))
        form_embeds_rand = self.dropout(self.embeddings_forms_rand(forms))

        if self.use_chars:
            form_embeds += self.dropout(self.embeddings_chars(chars, char_pack))

        form_embeds = torch.cat([form_embeds, form_embeds_rand], dim=2)

        # pack/unpack for LSTM_tag
        tagging_embeds = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        output_tag, _ = self.lstm_tag(tagging_embeds)
        output_tag, _ = torch.nn.utils.rnn.pad_packed_sequence(output_tag, batch_first=True)
        # pos
        mlp_tag = self.dropout(self.relu(self.mlp_tag(output_tag)))
        y_pred_tag = self.out_tag(mlp_tag)

        # concat original embeddings with POS lstm and softmaxc outs
        output_tag = torch.cat([output_tag, form_embeds, y_pred_tag], dim=2)

        # pack/unpack for LSTM_semtag
        semtagging_embeds = torch.nn.utils.rnn.pack_padded_sequence(output_tag, pack.tolist(), batch_first=True)
        output_semtag, _ = self.lstm_semtag(semtagging_embeds)
        output_semtag, _ = torch.nn.utils.rnn.pad_packed_sequence(output_semtag, batch_first=True)

        # sem
        mlp_semtag = self.dropout(self.relu(self.mlp_semtag(output_semtag)))
        y_pred_semtag = self.out_semtag(mlp_semtag)

        # concat original embeddings with sem lstm and softmax outs
        # embeds = torch.cat([form_embeds, y_pred_semtag, y_pred_tag], dim = 2)
        embeds = output_tag

        # pack/unpack for LSTM_parse
        embeds = torch.nn.utils.rnn.pack_padded_sequence(embeds, pack.tolist(), batch_first=True)
        output, _ = self.lstm(embeds)
        output, _ = torch.nn.utils.rnn.pad_packed_sequence(output, batch_first=True)

        # learn
        output = self.weightedcomb_unb(output, output_semtag)

        # predict heads
        reduced_head_head = self.dropout(self.relu(self.mlp_head(output)))
        reduced_head_dep = self.dropout(self.relu(self.mlp_dep(output)))
        y_pred_head = self.biaffine(reduced_head_head, reduced_head_dep)


        # predict deprels using heads
        reduced_deprel_head = self.dropout(self.relu(self.mlp_deprel_head(output)))
        reduced_deprel_dep = self.dropout(self.relu(self.mlp_deprel_dep(output)))
        predicted_labels = y_pred_head.max(2)[1]
        selected_heads = torch.stack([torch.index_select(reduced_deprel_head[n], 0, predicted_labels[n])
                                     for n, _ in enumerate(predicted_labels)])
        y_pred_label = self.label_biaffine(selected_heads, reduced_deprel_dep)
        y_pred_label = Helpers.extract_best_label_logits(predicted_labels, y_pred_label, pack)
        if self.use_cuda:
            y_pred_label = y_pred_label.cuda()

        return y_pred_head, y_pred_label, y_pred_semtag, y_pred_tag

    '''
    1. the bare minimum that needs to be loaded is forms, upos, head, deprel (could change later); load those
    2. initialise everything else to none; load it if necessary based on command line args
    3. pass everything, whether it's been loaded or not, to the forward function; if it's unnecessary it won't use it
    '''
    def train_(self, epoch, train_loader):
        self.train()
        train_loader.init_epoch()

        for i, batch in enumerate(train_loader):
            (x_forms, pack), (chars, _, length_per_word_per_sent), x_tags, y_heads, y_deprels, x_sem = \
                batch.form, batch.char, batch.upos, batch.head, batch.deprel, batch.sem

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            y_pred_head, y_pred_deprel, y_pred_semtag, y_pred_tag = \
                self(x_forms, x_tags, x_sem, pack, chars, length_per_word_per_sent)

            # reshape for cross-entropy
            batch_size, longest_sentence_in_batch = y_heads.size()

            # predictions: (B x S x S) => (B * S x S)
            # heads: (B x S) => (B * S)
            y_pred_head = y_pred_head.view(batch_size * longest_sentence_in_batch, -1)
            y_heads = y_heads.contiguous().view(batch_size * longest_sentence_in_batch)

            # predictions: (B x S x D) => (B * S x D)
            # heads: (B x S) => (B * S)
            y_pred_deprel = y_pred_deprel.view(batch_size * longest_sentence_in_batch, -1)
            y_deprels = y_deprels.contiguous().view(batch_size * longest_sentence_in_batch)

            #sem
            y_pred_semtag = y_pred_semtag.view(batch_size * longest_sentence_in_batch, -1)
            x_sem = x_sem.contiguous().view(batch_size * longest_sentence_in_batch)

            #pos
            y_pred_tag = y_pred_tag.view(batch_size * longest_sentence_in_batch, -1)
            x_tags = x_tags.contiguous().view(batch_size * longest_sentence_in_batch)

            # sum losses
            train_loss = self.criterion(y_pred_head, y_heads) + self.criterion(y_pred_deprel, y_deprels)
            train_loss += 0.5 * self.criterion(y_pred_semtag, x_sem)
            train_loss += 0.5 * self.criterion(y_pred_tag, x_tags)

            self.zero_grad()
            train_loss.backward()
            self.optimiser.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(epoch, (i + 1) * len(x_forms), len(train_loader.dataset), train_loss.data[0]))

        if self.save:
            with open(self.save[0], "wb") as f:
                torch.save(self.state_dict(), f)

    def evaluate_(self, test_loader, print_conllu=False):
        las_correct, uas_correct, semtags_correct, tags_correct, total = 0, 0, 0, 0, 0
        self.eval()
        for i, batch in enumerate(test_loader):
            (x_forms, pack), (chars, _, length_per_word_per_sent), x_tags, y_heads, y_deprels, x_sem = \
                batch.form, batch.char, batch.upos, batch.head, batch.deprel, batch.sem

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            # get labels
            # TODO: ensure well-formed tree
            y_pred_head, y_pred_deprel, y_pred_semtag, y_pred_tag = [i.max(2)[1] for i in
                                          self(x_forms, x_tags, x_sem, pack, chars, length_per_word_per_sent)]

            mask = mask.type(torch.ByteTensor)
            if self.use_cuda:
                mask = mask.cuda()

            mask = Variable(mask)
            mask[0, 0] = 0
            heads_correct = ((y_heads == y_pred_head) * mask)
            deprels_correct = ((y_deprels == y_pred_deprel) * mask)
           # semtags_correct = ((x_sem == y_pred_semtag) * mask)
            #tags_correct = ((x_tags == y_pred_tag) * mask)
            # excepts should never trigger; leave them in just in case
            try:
                uas_correct += heads_correct.nonzero().size(0)
            except RuntimeError:
                pass

            try:
                las_correct += (heads_correct * deprels_correct).nonzero().size(0)
            except RuntimeError:
                pass

            try:
                semtags_correct += ((x_sem == y_pred_semtag) * mask).nonzero().size(0)
            except RuntimeError:
                pass

            try:
                tags_correct += ((x_tags == y_pred_tag) * mask).nonzero().size(0)
            except RuntimeError:
                pass

            total += mask.nonzero().size(0)

            if print_conllu:
                deprel_vocab = self.vocab[1]
                deprels = [deprel_vocab.itos[i.data[0]] for i in y_pred_deprel.view(-1, 1)]
                heads_softmaxes = self(x_forms, x_tags, x_sem, pack, chars, length_per_word_per_sent)[0][0]
                heads_softmaxes = F.softmax(heads_softmaxes, dim=1)
                json = cle.mst(heads_softmaxes.data.cpu().numpy())

                Helpers.write_to_conllu(self.test_file, json, deprels, i)

        print("UAS = {}/{} = {}\nLAS = {}/{} = {}\nTAG = {}/{} = {}\n\nSEMTAG = {}/{} = {}\n".format(uas_correct, total, uas_correct / total,
                                                          las_correct, total, las_correct / total, 
                                                          tags_correct, total,  tags_correct / total,
                                                          semtags_correct, total,  semtags_correct / total))



class TagTwiceParser(torch.nn.Module):
    def __init__(self, sizes, args, vocab, embeddings=None, embed_dim=100, lstm_dim=500, lstm_layers=2,
                 reduce_dim_arc=400, reduce_dim_label=100, learning_rate=2e-3):
        super().__init__()

        self.use_cuda = args.use_cuda
        self.use_chars = args.use_chars
        self.save = args.save
        self.vocab = vocab
        # for writer
        self.test_file = args.test[0]

        if self.use_chars:
            self.embeddings_chars = CharEmbedding(sizes['chars'], embed_dim, lstm_dim, lstm_layers)

        self.embeddings_forms = torch.nn.Embedding(sizes['vocab'], embed_dim)
        self.embeddings_forms.weight.data.copy_(vocab[0].vectors)

        self.embeddings_forms_rand = torch.nn.Embedding(sizes['vocab'], embed_dim)
     #   self.embeddings_tags = torch.nn.Embedding(sizes['postags'], embed_dim)
        self.lstm = torch.nn.LSTM(700  + sizes['semtags'] + sizes['postags'], lstm_dim, lstm_layers + 1,
                                  batch_first=True, bidirectional=True, dropout=0.33)
        self.mlp_head = torch.nn.Linear(2 * lstm_dim, reduce_dim_arc)
        self.mlp_dep = torch.nn.Linear(2 * lstm_dim, reduce_dim_arc)
        self.mlp_deprel_head = torch.nn.Linear(2 * lstm_dim, reduce_dim_label)
        self.mlp_deprel_dep = torch.nn.Linear(2 * lstm_dim, reduce_dim_label)

        #pos
        self.mlp_tag = torch.nn.Linear(300, 150)
        self.out_tag = torch.nn.Linear(150, sizes['postags'])
        #sem
        self.mlp_semtag = torch.nn.Linear(500, 200)
        self.out_semtag = torch.nn.Linear(200, sizes['semtags'])
        self.lstm_tag = torch.nn.LSTM(embed_dim * 2, 150, 1,
                                  batch_first=True, bidirectional=True, dropout=0.33)

        self.lstm_semtag = torch.nn.LSTM(embed_dim * 5 + sizes['postags'], 250, 1,
                                  batch_first=True, bidirectional=True, dropout=0.33)

        self.relu = torch.nn.ReLU()
        self.dropout = torch.nn.Dropout(p=0.33)
        # self.biaffine = Biaffine(reduce_dim_arc + 1, reduce_dim_arc, BATCH_SIZE)
        self.biaffine = ShorterBiaffine(reduce_dim_arc)
        self.label_biaffine = LongerBiaffine(reduce_dim_label, reduce_dim_label, sizes['deprels'])
        self.criterion = torch.nn.CrossEntropyLoss(ignore_index=-1)
        self.optimiser = torch.optim.Adam(self.parameters(), lr=learning_rate, betas=(0.9, 0.9))

        if self.use_cuda:
            self.biaffine.cuda()
            self.label_biaffine.cuda()

    def forward(self, forms, tags, sem, pack, chars, char_pack):
        form_embeds = self.dropout(self.embeddings_forms(forms))
        form_embeds_rand = self.dropout(self.embeddings_forms_rand(forms))

        if self.use_chars:
            form_embeds += self.dropout(self.embeddings_chars(chars, char_pack))

        form_embeds = torch.cat([form_embeds_rand, form_embeds], dim=2)

        # pack/unpack for LSTM_tag
        tagging_embeds = torch.nn.utils.rnn.pack_padded_sequence(form_embeds, pack.tolist(), batch_first=True)
        output_tag, _ = self.lstm_tag(tagging_embeds)
        output_tag, _ = torch.nn.utils.rnn.pad_packed_sequence(output_tag, batch_first=True)
        # pos
        mlp_tag = self.dropout(self.relu(self.mlp_tag(output_tag)))
        y_pred_tag = self.out_tag(mlp_tag)

        # concat original embeddings with POS lstm and softmaxc outs
        output_tag = torch.cat([output_tag, form_embeds, y_pred_tag], dim=2)

        # pack/unpack for LSTM_semtag
        semtagging_embeds = torch.nn.utils.rnn.pack_padded_sequence(output_tag, pack.tolist(), batch_first=True)
        output_semtag, _ = self.lstm_semtag(semtagging_embeds)
        output_semtag, _ = torch.nn.utils.rnn.pad_packed_sequence(output_semtag, batch_first=True)

        # sem
        mlp_semtag = self.dropout(self.relu(self.mlp_semtag(output_semtag)))
        y_pred_semtag = self.out_semtag(mlp_semtag)

        # concat original embeddings with sem lstm and softmax outs
        embeds = torch.cat([form_embeds, output_semtag, y_pred_semtag, y_pred_tag], dim = 2)

        #embeds = output_tag

        # pack/unpack for LSTM_parse
        embeds = torch.nn.utils.rnn.pack_padded_sequence(embeds, pack.tolist(), batch_first=True)
        output, _ = self.lstm(embeds)
        output, _ = torch.nn.utils.rnn.pad_packed_sequence(output, batch_first=True)

        # predict heads
        reduced_head_head = self.dropout(self.relu(self.mlp_head(output)))
        reduced_head_dep = self.dropout(self.relu(self.mlp_dep(output)))
        y_pred_head = self.biaffine(reduced_head_head, reduced_head_dep)


        # predict deprels using heads
        reduced_deprel_head = self.dropout(self.relu(self.mlp_deprel_head(output)))
        reduced_deprel_dep = self.dropout(self.relu(self.mlp_deprel_dep(output)))
        predicted_labels = y_pred_head.max(2)[1]
        selected_heads = torch.stack([torch.index_select(reduced_deprel_head[n], 0, predicted_labels[n])
                                     for n, _ in enumerate(predicted_labels)])
        y_pred_label = self.label_biaffine(selected_heads, reduced_deprel_dep)
        y_pred_label = Helpers.extract_best_label_logits(predicted_labels, y_pred_label, pack)
        if self.use_cuda:
            y_pred_label = y_pred_label.cuda()

        return y_pred_head, y_pred_label, y_pred_semtag, y_pred_tag

    '''
    1. the bare minimum that needs to be loaded is forms, upos, head, deprel (could change later); load those
    2. initialise everything else to none; load it if necessary based on command line args
    3. pass everything, whether it's been loaded or not, to the forward function; if it's unnecessary it won't use it
    '''
    def train_(self, epoch, train_loader):
        self.train()
        train_loader.init_epoch()

        for i, batch in enumerate(train_loader):
            (x_forms, pack), (chars, _, length_per_word_per_sent), x_tags, y_heads, y_deprels, x_sem = \
                batch.form, batch.char, batch.upos, batch.head, batch.deprel, batch.sem

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            y_pred_head, y_pred_deprel, y_pred_semtag, y_pred_tag = \
                self(x_forms, x_tags, x_sem, pack, chars, length_per_word_per_sent)

            # reshape for cross-entropy
            batch_size, longest_sentence_in_batch = y_heads.size()

            # predictions: (B x S x S) => (B * S x S)
            # heads: (B x S) => (B * S)
            y_pred_head = y_pred_head.view(batch_size * longest_sentence_in_batch, -1)
            y_heads = y_heads.contiguous().view(batch_size * longest_sentence_in_batch)

            # predictions: (B x S x D) => (B * S x D)
            # heads: (B x S) => (B * S)
            y_pred_deprel = y_pred_deprel.view(batch_size * longest_sentence_in_batch, -1)
            y_deprels = y_deprels.contiguous().view(batch_size * longest_sentence_in_batch)

            #sem
            y_pred_semtag = y_pred_semtag.view(batch_size * longest_sentence_in_batch, -1)
            x_sem = x_sem.contiguous().view(batch_size * longest_sentence_in_batch)

            #pos
            y_pred_tag = y_pred_tag.view(batch_size * longest_sentence_in_batch, -1)
            x_tags = x_tags.contiguous().view(batch_size * longest_sentence_in_batch)

            # sum losses
            train_loss = self.criterion(y_pred_head, y_heads) + self.criterion(y_pred_deprel, y_deprels)
            train_loss += 0.5 * self.criterion(y_pred_semtag, x_sem)
            train_loss += 0.5 * self.criterion(y_pred_tag, x_tags)

            self.zero_grad()
            train_loss.backward()
            self.optimiser.step()

            print("Epoch: {}\t{}/{}\tloss: {}".format(epoch, (i + 1) * len(x_forms), len(train_loader.dataset), train_loss.data[0]))

        if self.save:
            with open(self.save[0], "wb") as f:
                torch.save(self.state_dict(), f)

    def evaluate_(self, test_loader, print_conllu=False):
        las_correct, uas_correct, semtags_correct, tags_correct, total = 0, 0, 0, 0, 0
        self.eval()
        for i, batch in enumerate(test_loader):
            (x_forms, pack), (chars, _, length_per_word_per_sent), x_tags, y_heads, y_deprels, x_sem = \
                batch.form, batch.char, batch.upos, batch.head, batch.deprel, batch.sem

            mask = torch.zeros(pack.size()[0], max(pack)).type(torch.LongTensor)
            for n, size in enumerate(pack):
                mask[n, 0:size] = 1

            # get labels
            # TODO: ensure well-formed tree
            y_pred_head, y_pred_deprel, y_pred_semtag, y_pred_tag = [i.max(2)[1] for i in
                                          self(x_forms, x_tags, x_sem, pack, chars, length_per_word_per_sent)]

            mask = mask.type(torch.ByteTensor)
            if self.use_cuda:
                mask = mask.cuda()

            mask = Variable(mask)
            mask[0, 0] = 0
            heads_correct = ((y_heads == y_pred_head) * mask)
            deprels_correct = ((y_deprels == y_pred_deprel) * mask)
           # semtags_correct = ((x_sem == y_pred_semtag) * mask)
            #tags_correct = ((x_tags == y_pred_tag) * mask)
            # excepts should never trigger; leave them in just in case
            try:
                uas_correct += heads_correct.nonzero().size(0)
            except RuntimeError:
                pass

            try:
                las_correct += (heads_correct * deprels_correct).nonzero().size(0)
            except RuntimeError:
                pass

            try:
                semtags_correct += ((x_sem == y_pred_semtag) * mask).nonzero().size(0)
            except RuntimeError:
                pass

            try:
                tags_correct += ((x_tags == y_pred_tag) * mask).nonzero().size(0)
            except RuntimeError:
                pass

            total += mask.nonzero().size(0)

            if print_conllu:
                deprel_vocab = self.vocab[1]
                deprels = [deprel_vocab.itos[i.data[0]] for i in y_pred_deprel.view(-1, 1)]
                heads_softmaxes = self(x_forms, x_tags, x_sem, pack, chars, length_per_word_per_sent)[0][0]
                heads_softmaxes = F.softmax(heads_softmaxes, dim=1)
                json = cle.mst(heads_softmaxes.data.cpu().numpy())

                Helpers.write_to_conllu(self.test_file, json, deprels, i)

        print("UAS = {}/{} = {}\nLAS = {}/{} = {}\nTAG = {}/{} = {}\n\nSEMTAG = {}/{} = {}\n".format(uas_correct, total, uas_correct / total,
                                                          las_correct, total, las_correct / total,
                                                          tags_correct, total,  tags_correct / total,
                                                          semtags_correct, total,  semtags_correct / total))
