#!/usr/bin/env python3
from pathlib import Path
from argparse import ArgumentParser
from collections import defaultdict
from itertools import count, product
from pickle import load, dump
from docria.storage import DocumentIO
from utils import pickled, langforia
import requests
import numpy as np


def get_args():
    parser = ArgumentParser()
    parser.add_argument('file', type=Path)
    parser.add_argument('glove', type=Path)
    return parser.parse_args()


def load_glove(path):
    with path.open('r') as f:
        rows = map(lambda x: x.split(), f)
        embed = {}
        for row in rows:
            embed[row[0]] = np.asarray(row[1:], dtype='float32')
        return embed


def model():
    model = Sequential()
    # input vectors of dim 108 -> output vectors of dim 25
    model.add(Embedding(108, 25, input_length=10))
    # each sample is 10 vectors of 25 dimensions
    model.add(Bidirectional(LSTM(25, return_sequences=True), input_shape=(10, 25)))
    # arbitrarily (?) pick 25 hidden units
    model.add(Bidirectional(LSTM(25)))
    model.add(Dense(16))
    model.add(Activation('softmax'))
    model.compile(loss='categorical_crossentropy', optimizer='nadam', metrics=['accuracy'])
    return model


def core_nlp_features(doc, lang):
    train = []
    lbl_sets = defaultdict(set)

    def add(features, name):
        lbl_sets[name].add(features[name])

    for i, a in enumerate(doc):
        if i % 10 == 0:
            print(i)
        text = str(a.texts['main'])
        corenlp = iter(langforia(text, lang).split('\n'))
        head = next(corenlp).split('\t')[1:]
        sentences = [[]]
        for row in corenlp:
            if row:
                cols = row.split('\t')
                features = dict(zip(head, cols[1:]))
                add(features, 'pos')
                add(features, 'ne')
                sentences[-1].append(features)
            else:
                sentences.append([])
        if not sentences[-1]:
            sentences.pop(-1)
        train.extend(sentences)
    return train, lbl_sets


def extract_features(embed, core_nlp):
    train, lbl_sets = core_nlp
    print(len(lbl_sets['pos']), lbl_sets['pos'])
    print(len(lbl_sets['ne']), lbl_sets['ne'])

    labels = {}
    mappings = {key: dict(zip(lbls, count(0))) for key, lbls in lbl_sets.items()}
    mappings['form'] = embed
    for key, mapping in mappings.items():
        labels[key] = []
        for sentence in train:
            labels[key].append([])
            for features in sentence:
                try:
                    label = mapping[features[key]]
                except KeyError:
                    label = np.zeros(len(next(iter(mapping.values()))))
                labels[key][-1].append(label)

    for key, lbls in lbl_sets.items():
        labels[key] = [to_categorical(vals, num_classes=len(lbls)) for vals in labels[key]]

    return [[np.concatenate(word) for word in zip(*sentence)] for sentence in zip(*labels.values())]


if __name__ == '__main__':
    args = get_args()
    for arg in vars(args).values():
        try:
            assert(arg.exists())
        except AssertionError:
            raise FileNotFoundError(arg)
        except AttributeError:
            pass

    embed = pickled(args.glove, load_glove)

    def read_and_extract(path):
        with list(DocumentIO.read(path)) as doc:
            print('Documents:', len(doc))
            return core_nlp_features(doc, 'en')
    
    core_nlp = pickled(args.file, read_and_extract)
    
    from keras.utils import to_categorical
    from keras.layers import Bidirectional, LSTM, Dense, Activation, Embedding
    from keras.models import Sequential

    features = extract_features(embed, core_nlp)

    # placeholders
    x_train = []
    y_train = []
    x_test = []
    y_test = []

    model = model()
    model.fit(x_train, y_train, batch_size=8, epochs=1,
            validation_data=[x_test, y_test])
