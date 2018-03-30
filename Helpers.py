import os
import torch
import torch.utils.data
from torch.autograd import Variable
from Conllu import ConllParser

DEBUG_SIZE = 1000


def build_data(fname, batch_size, train_conll=None):
    # build data
    with open(os.path.join('data', fname), 'r') as f:
        conll = ConllParser(f) if not train_conll else ConllParser(f, train_conll)

    # sentences
    print("Preparing %s.." % fname)
    forms, rels, tags, deprels = conll.get_tensors()
    assert forms.shape == torch.Size([len(conll), conll.longest_sent])
    assert tags.shape == torch.Size([len(conll), conll.longest_sent])
    assert deprels.shape == torch.Size([len(conll), conll.longest_sent])

    # heads
    heads = -torch.ones(forms.shape[0], conll.longest_sent)
    heads.scatter_(1, rels[:, :, 1], rels[:, :, 0].type(torch.FloatTensor))
    heads[:, 0] = 0
    heads = heads.type(torch.LongTensor)

    assert heads.shape == torch.Size([len(conll), conll.longest_sent])

    # sizes
    sizes_int = torch.zeros(len(conll)).view(-1, 1).type(torch.LongTensor)
    sizes = torch.zeros(len(conll), conll.longest_sent)
    for n, form in enumerate(forms):
        sizes_int[n] = form[form != 0].shape[0]

    for n, size in enumerate(sizes_int):
        sizes[n, 1:size[0]] = 1

    assert sizes.shape == torch.Size([len(conll), conll.longest_sent])

    # build loader & model
    data = list(zip(forms, tags, heads, deprels, sizes))[:DEBUG_SIZE]
    loader = torch.utils.data.DataLoader(data, batch_size=batch_size, shuffle=True, drop_last=True)

    return conll, loader


def process_batch(batch):
    forms, tags, heads, deprels, sizes = [torch.stack(list(i)) for i in zip(*sorted(zip(*batch),
                                                                            key=lambda x: x[4].nonzero().size(0),
                                                                            reverse=True))]
    trunc = max([i.nonzero().size(0) + 1 for i in sizes])
    x_forms = Variable(forms[:, :trunc])
    x_tags = Variable(tags[:, :trunc])
    mask = Variable(sizes[:, :trunc])
    pack = [i.nonzero().size(0) + 1 for i in sizes]
    y_heads = Variable(heads[:, :trunc], requires_grad=False)
    y_deprels = Variable(deprels[:, :trunc], requires_grad=False)

    return x_forms, x_tags, mask, pack, y_heads, y_deprels


def extract_best_label_logits(pred_arcs, label_logits, lengths):
    # pred_arcs = torch.squeeze(torch.max(arc_logits, dim=1)[1], dim=1).data.cpu().numpy()
    pred_arcs = pred_arcs.data.cpu().numpy()
    size = label_logits.size()
    output_logits = Variable(torch.zeros(size[0], size[1], size[3]))
    for batch_index, (_logits, _arcs, _length) in enumerate(zip(label_logits, pred_arcs, lengths)):
        for i in range(_length):
            output_logits[batch_index] = _logits[_arcs[i]]
    return output_logits

