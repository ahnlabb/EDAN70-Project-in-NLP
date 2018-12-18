#!/usr/bin/env python3
from pathlib import Path
from argparse import ArgumentParser
from collections import defaultdict
from itertools import count
from pickle import load
from collections import Counter
from random import shuffle
import base64
from docria.storage import DocumentIO
from docria.algorithm import span_translate
from docria import T
from utils import pickled, langforia, inverted, build_sequence, emb_mat_init, mapget, zip_from_end, print_dims
from gold_std import entity_to_dict, from_one_hot, one_hot, gold_std_idx, to_neleval, interpret_prediction
from model import make_model, make_model_batches
from structs import ModelJar
from print_neleval import docria_to_neleval
import numpy as np
import time
import sys

from keras.preprocessing.sequence import pad_sequences
from keras.utils import to_categorical


def get_args():
    parser = ArgumentParser()
    parser.add_argument('file', type=Path, nargs='+', help='one or more docria files to use for training')
    parser.add_argument('glove', type=Path, help='file with embedding (tab or space separated, no header)')
    parser.add_argument('model', type=Path,
            help='path to where model will be saved, '
                 'if this file already exists training will be skipped and this model will be loaded')
    parser.add_argument('lang', type=str, choices=['en','zh','es'], help='language')
    parser.add_argument('elmapping', type=Path, help='')
    parser.add_argument('--predict', type=Path, help='')
    parser.add_argument('--gold', action='store_true', help='')
    return parser.parse_args()


@pickled
def read_and_extract(path, fun):
    with DocumentIO.read(path) as doc:
        return fun(list(doc))


@pickled
def load_glove(path):
    with path.open('r') as f:
        rows = map(lambda x: x.split(), f)
        embed = {}
        for row in rows:
            embed[row[0]] = np.asarray(row[1:], dtype='float32')
        return embed


def get_core_nlp(docs, lang):
    def call_api(doc):
        text = str(doc.texts['main'])
        return langforia(text, lang).split('\n')
    api_data = []
    start = time.perf_counter()
    for i, doc in enumerate(docs, 1):
        print("Document %d/%d " % (i, len(docs)), end='', flush=True, file=sys.stderr)
        api_data.append(call_api(doc))
        time_left = int((time.perf_counter() - start) / i * (len(docs) - i))
        print(f' ETA: {time_left // 60} min {time_left % 60} s    ', end='\r', flush=True, file=sys.stderr)
    time_tot = int(time.perf_counter() - start)
    print(f'Done: {len(docs)} documents in {time_tot // 60} min {time_tot % 60} s', file=sys.stderr)
    return api_data, docs


def txt2xml(doc):
    index = {}
    for node in doc.layers['tac/segments']:
        text = node.fld.text
        if text:
            i, j = text.start, text.stop
            x, y = node.fld.xml.start, node.fld.xml.stop
            indices = enumerate([(x,y)]*(j-i), i)
            for txt, xml in indices:
                index[txt] = xml
    #for s, e in index.items():
    #    print(s, e)
    return index


def docria_extract(core_nlp, docs, saved_cats=None, per_doc=False):
    train, gold, spandex = [], [], []
    lbl_sets = defaultdict(set)

    gold_std, cats = gold_std_idx(docs)
    if saved_cats:
        cats = saved_cats

    def get_entity(docid, span):
        none = ('O', 'NOE', 'OUT')
        return gold_std[docid].get(span, none)

    for cnlp, doc in zip(core_nlp, docs):
        docid = doc.props['docid']
        sentences, spans = core_nlp_features(cnlp, lbl_sets)
        entities = [[get_entity(docid, s) for s in sentence] for sentence in spans]
        if per_doc:
            gold.append(entities)
            train.append(sentences)
            spandex.append(spans)
        else:
            gold.extend(entities)
            train.extend(sentences)
            spandex.extend(spans)

    return train, lbl_sets, gold, cats, spandex, list(docs)


def build_indices(train, embed):
    wordset = set([features['form'] for sentence in train for features in sentence])
    wordset.update(embed.keys())
    word_inv = dict(zip(wordset, count(2)))
    return word_inv


def core_nlp_features(corenlp, lbl_sets):
    corenlp = iter(corenlp)
    spans = []

    def add(features, name):
        feat = features[name]
        lbl_sets[name].add(feat)

    head = next(corenlp).split('\t')[1:]
    sentences = [[]]
    spans = [[]]
    inside = ''
    for row in corenlp:
        if row:
            cols = row.split('\t')
            features = dict(zip(head, cols[1:]))
            if 'ne' in features:
                inside, features['ne'] = normalize_ne(inside, features['ne'])
            else:
                features['ne'] = '_'
            features['capital'] = features['form'][0].isupper()
            features['form'] = features['form'].lower()
            add(features, 'pos')
            add(features, 'ne')
            add(features, 'capital')
            sentences[-1].append(features)
            spans[-1].append((int(features['start']), int(features['end'])))
        else:
            sentences.append([])
            spans.append([])
    if not sentences[-1]:
        sentences.pop(-1)
        spans.pop(-1)
    return sentences, spans


def create_mappings(train, embed, lbl_sets):
    mappings = {key: dict(zip(lbls, count(1))) for key, lbls in lbl_sets.items()}
    for key in mappings:
        mappings[key]['OOV'] = 0
    mappings['form'] = build_indices(train, embed)
    return mappings


def extract_features(mappings, sentences, padding=False):
    labels = {}
    for feat_name, mapping in mappings.items():
        labels[feat_name] = []
        for sentence in sentences:
            label_list = []
            for features in sentence:
                feature = features[feat_name]
                if feature in mapping:
                    label = mapping[feature]
                else:
                    # Out of vocabulary
                    if feat_name == 'form':
                        mapping_len = len(next(iter(mapping.values())))
                        label = np.zeros(mapping_len)
                    else:
                        label = 0
                label_list.append(label)
            labels[feat_name].append(label_list)

    for feat, lbls in mappings.items():
        if feat == 'form':
            continue
        labels[feat] = [to_categorical(vals, num_classes=len(lbls)) for vals in labels[feat]]

    def concat(word):
        if padding:
            word = word + (np.zeros(1),)
        return np.concatenate(word)

    for sentence in zip(*labels.values()):
        yield [concat(word) for word in zip(*sentence)]


def normalize_ne(inside, ne):
    if inside:
        if ne == ')':
            ne = 'E-' + inside
            inside = ''
        else:
            ne = 'I-' + inside
    elif ne[-1] == ')':
        ne = 'S-' + ne[1:-1]
        inside = ''
    elif len(ne) > 1:
        inside = ne[1:]
        ne = 'B-' + inside

    return inside, ne


def corenlp_parse(text, lang='en'):
    result = langforia(text, lang).split('\n')
    itr = iter(result)
    head = next(itr).split('\t')[1:]
    sentences = [[]]
    spans = [[]]
    inside = ""
    for row in itr:
        if row:
            cols = row.split('\t')
            features = dict(zip(head, cols[1:]))
            if 'ne' in features:
                inside, features['ne'] = normalize_ne(inside, features['ne'])

            sentences[-1].append(features)
            spans[-1].append((features['start'], features['end']))
        else:
            sentences.append([])
            spans.append([])
    if not sentences[-1]:
        sentences.pop(-1)
        spans.pop(-1)
    return sentences, spans


def debug_log(x, fun=lambda x: x):
    print(fun(x))
    return x


def batch_generator(train, gold, mappings, batch_len=128, **keys):
    xy = sorted(zip(train, gold), key=lambda x: len(x[0]))
    n_batches = len(train) // batch_len + (len(train) % batch_len > 0)
    batches = list(range(n_batches))
    while True:
        shuffle(batches)
        for k in batches:
            s = slice(k * batch_len, (k + 1) * batch_len)
            outputs = []
            maxlen = len(xy[s][-1][0])
            shuffle(xy[s])
            x, y = zip(*xy[s])
            for key, args in keys.items():
                outputs.append(field_as_category(x, key, mappings[key], maxlen=maxlen, **args))
            yield outputs, pad_sequences(y, maxlen=maxlen)


def predict_batch_generator(test, mappings, **keys):
    for sentences in test:
        maxlen = len(max(sentences, key=len))
        yield list(field_as_category(sentences, key, mappings[key], maxlen=maxlen, **args) for key, args in keys.items())


def predict_to_layer(model, docs, test, gold, spandex, mappings, inv_cats, elmap={}, **keys):
    batches = predict_batch_generator(test, mappings, **keys)
    i = 0
    for doc, doc_spans, batch in zip(docs, spandex, batches):
        layer = doc.add_layer('tac/entity', text=T.span('main'), xml=T.span('xml')) 
        main = doc.text['main']
        predictions = model.predict_on_batch(batch)
        
        for pred, spans in zip(predictions, doc_spans):
            for p, s in zip_from_end(pred, spans):
                _, tp, lbl = inv_cats[np.argmax(p)]
                text = main[s]
                if str(text) in elmap:
                    tgt = elmap[str(text)]
                else:
                    tgt, i = 'NIL%s' % format(i, '05d'), i + 1
                layer.add(text=text, type=tp, label=lbl, target=tgt)
        
        span_translate(doc, 'tac/segments', ('xml', 'text'), 'tac/entity', ('text', 'xml')) 


def predict(model, mappings, cats, text, padding=False):
    lbl_sets = defaultdict(set)
    sentences, spans = core_nlp_features(langforia(text, 'en').split('\n'), lbl_sets)
    features = list(extract_features(mappings, sentences, padding=padding))
    x = list(batch_generator(features, batch_len=len(features)))
    Y = model.predict(x)
    pred = [[interpret_prediction(p, cats) for p in y] for y in Y]
    return format_predictions(text, pred, sentences)


def format_predictions(input_text, predictions, sentences):
    pred_dict = {'text': input_text, 'entities': []}
    for sent, pred in zip(sentences, predictions):
        for word, class_tuple in zip(sent, pred):
            entity_dict = entity_to_dict(word['start'], word['end'], class_tuple)
            pred_dict['entities'].append(entity_dict)
    return pred_dict


def field_as_category(data, key, inv, default=None, categorical=True, maxlen=None):
    fields = (mapget(key, sentence) for sentence in data)
    return to_categories(fields, inv, default=default, categorical=categorical, maxlen=maxlen)


def to_categories(data, inv, default=None, categorical=True, maxlen=None):
    cat_seq = [build_sequence(d, inv, default=default) for d in data]
    padded = pad_sequences(cat_seq, maxlen=maxlen)
    if categorical:
        return to_categorical(padded, num_classes=len(inv))
    return padded


def simple_eval(pred, gold, out_index):
    actual = Counter()
    correct = Counter()
    for p, g in zip(pred, gold):
        for a, b in zip_from_end(p, g):
            actual_tag = out_index[np.argmax(b)]
            actual[actual_tag] += 1
            if np.argmax(a) == np.argmax(b):
                correct[actual_tag] += 1

    for k in actual:
        print('-'.join(k), correct[k]/actual[k], actual[k])

    corr_sum = sum(correct[k] for k in correct if k != ('O', 'NOE', 'OUT'))
    act_sum = sum(actual[k] for k in actual if k != ('O', 'NOE', 'OUT'))
    print(corr_sum/act_sum)


if __name__ == '__main__':
    args = get_args()
    for arg in vars(args).values():
        if arg == args.model:
            continue
        try:
            assert(arg.exists())
        except AssertionError:
            raise FileNotFoundError(arg)
        except AttributeError:
            pass

    embed = load_glove(args.glove)
    embed_len = len(next(iter(embed.values())))

    jar = None
    if args.model.exists():
        jar = ModelJar.load(args.model, lambda jar: emb_mat_init(embed, jar.mappings['form']))
        saved_cats = jar.cats
        mappings = jar.mappings
    else:
        saved_cats = None
        
    train, lbl_sets, gold, cats = [], defaultdict(set), [], {}
    for f in args.file:
        corenlp, docs = read_and_extract(f, lambda docs: get_core_nlp(docs, lang=args.lang))
        t, ls, g, cs, _, _ = docria_extract(corenlp, docs, saved_cats=saved_cats)
        train.extend(t)
        for k in ls:
            lbl_sets[k] |= ls[k]
        gold.extend(g)
        cats.update(cs)
    gold = to_categories(gold, cats)
        
    if not args.model.exists():
        mappings = create_mappings(train, embed, lbl_sets)

    #x_word = field_as_category(train, 'form', mappings['form'], default=1, categorical=False)
    #x_pos = field_as_category(train, 'pos', mappings['pos'], categorical=False)
    #x_ne = field_as_category(train, 'ne', mappings['ne'], categorical=False)
    #y = pad_sequences(gold)

    keys = {'form': {'default': 1, 'categorical': False},
            'pos': {'categorical': False},
            'ne': {'categorical': False},}

    if not args.model.exists():
        batch_len = 128
        batches = batch_generator(train, gold, mappings, batch_len=batch_len, **keys)
        #model = make_model([x_word, x_pos, x_ne], y, embed, mappings['form'], len(mappings['pos']), len(mappings['ne']), len(cats), embed_len, len(train), epochs=10, batch_size=batch_len)
        model = make_model_batches(batches, embed, mappings['form'], len(mappings['pos']), len(mappings['ne']), len(cats), embed_len, len(train), epochs=10, batch_size=batch_len)
        jar = ModelJar(model, mappings, cats, path=args.model)
        jar.save()
    else:
        model = jar.model

    with args.elmapping.open('r+b') as f:
        elmap = load(f)

    if args.predict:
        core_nlp_test, docs_test = read_and_extract(args.predict, lambda docs: get_core_nlp(docs, lang=args.lang))
        test, _, gold_test, _, spandex, docs = docria_extract(core_nlp_test, docs_test, per_doc=True)
        gold_test = [to_categories(g, cats) for g in gold_test]
        predict_to_layer(model, docs, test, gold_test, spandex, mappings, inverted(cats), elmap=elmap, **keys)
        with open('predict.en.tsv', 'w') as f:
            f.write(docria_to_neleval(docs, 'tac/entity'))
        #x_word_test = field_as_category(test, 'form', mappings['form'], default=1, categorical=False)
        #x_pos_test = field_as_category(test, 'pos', mappings['pos'], categorical=False)
        #x_ne_test = field_as_category(test, 'ne', mappings['ne'], categorical=False)
        #y_test = pad_sequences(gold_test)
        #pred = model.predict([x_word_test, x_pos_test, x_ne_test])
        #simple_eval(pred, gold_test, inverted(cats))
