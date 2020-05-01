import json
import random
import torch
import numpy as np
import os
from copy import deepcopy
from collections import defaultdict

from utils import constant, helper, vocab


class DataProcessor(object):
    def __init__(self, config, vocab, data_dir, partition_names=['train', 'dev', 'test']):
        self.config = config
        self.data_dir = data_dir
        self.partition_names = partition_names
        self.name2id = {
            'ent2id': vocab.word2id,
            'pos2id': constant.POS_TO_ID,
            'ner2id': constant.NER_TO_ID,
            'deprel2id': constant.DEPREL_TO_ID,
            'subj2id': {},
            'obj2id': {},
            # Curriculum learning reverse mappings
            'rel2id': {},
            'binary_rel2rels': defaultdict(lambda: set()),
            'subj2rels': defaultdict(lambda: set()),
            'subj_obj2triples': defaultdict(lambda: set())
        }

        self.graph = {}
        self.preprocess_data()

    def preprocess_data(self):
        partitions = defaultdict(list)
        for partition_name in self.partition_names:
            partition_file = os.path.join(self.data_dir, partition_name + '.json')
            with open(partition_file, 'rb') as handle:
                partition_data = json.load(handle)
                partition_parsed = self.parse_data(partition_data)
                partitions[partition_name] = partition_parsed
        self.partitions = partitions

    def parse_data(self, data):
        parsed_data = []
        num_rel = 0
        for idx, d in enumerate(data):
            tokens = d['token']
            if self.config['lower']:
                tokens = [t.lower() for t in tokens]
            # anonymize tokens
            ss, se = d['subj_start'], d['subj_end']
            os, oe = d['obj_start'], d['obj_end']
            subject_type = 'SUBJ-' + d['subj_type']
            object_type = 'OBJ-' + d['obj_type']
            subject_id = self.name2id['ent2id'][subject_type]
            object_id = self.name2id['ent2id'][object_type]
            self.name2id['subj2id'][subject_type] = subject_id
            self.name2id['obj2id'][object_type] = object_id
            tokens[ss:se + 1] = [subject_type] * (se - ss + 1)
            tokens[os:oe + 1] = [object_type] * (oe - os + 1)
            tokens = self.map_to_ids(tokens, self.name2id['ent2id'])
            pos = self.map_to_ids(d['stanford_pos'], self.name2id['pos2id'])
            ner = self.map_to_ids(d['stanford_ner'], self.name2id['ner2id'])
            deprel = self.map_to_ids(d['stanford_deprel'], self.name2id['deprel2id'])
            l = len(tokens)
            subj_positions = self.get_positions(d['subj_start'], d['subj_end'], l)
            obj_positions = self.get_positions(d['obj_start'], d['obj_end'], l)

            relation_name = d['relation']
            if self.config['typed_relations']:
                relation_name = '{}:{}:{}'.format(d['subj_type'], d['relation'], d['obj_type'])
            # Relation ids
            if relation_name not in self.name2id['rel2id']:
                self.name2id['rel2id'][relation_name] = len(self.name2id['rel2id'])
            relation_id = self.name2id['rel2id'][relation_name]
            triple = (subject_id, relation_id, object_id)
            # binary relations
            if relation_name == 'no_relation':
                self.name2id['binary_rel2rels']['no_relation'].add(relation_id)
            else:
                self.name2id['binary_rel2rels']['has_relation'].add(relation_id)
            # Type relations
            if 'per' in relation_name:
                self.name2id['subj2rels']['per:relation'].add(relation_id)
            elif 'org' in relation_name:
                self.name2id['subj2rels']['org:relation'].add(relation_id)
            else:
                self.name2id['subj2rels']['no_relation'].add(relation_id)
            # subject & object relations
            if relation_name != 'no_relation':
                subj_objs = '{}:{}'.format(subject_type, object_type)
                self.name2id['subj_obj2triples'][subj_objs].add(triple)
            else:
                self.name2id['subj_obj2triples']['no_relation'].add(triple)

            base_sample = {'tokens': tokens,
                           'pos': pos,
                           'ner': ner,
                           'deprel': deprel,
                           'subj_positions': subj_positions,
                           'obj_positions': obj_positions,
                           'relation': relation_id,
                           'subj_id': subject_id,
                           'obj_id': object_id}
            base_sample = base_sample

            supplemental_sample = {}
            supplemental_sample['triple'] = triple

            if self.config['relation_masking']:
                pair = (subject_id, object_id)
                if pair not in self.graph:
                    self.graph[pair] = set()
                self.graph[pair].add(relation_id)

                supplemental_sample['relation_masking'] = (subject_id, relation_id, object_id)

            parsed_sample = {'base': base_sample, 'supplemental': supplemental_sample}

            parsed_data.append(parsed_sample)

        # Calculate Curriculum mappings
        self.name2id['id2rel'] = dict([(v, k) for (k, v) in self.name2id['rel2id'].items()])
        self.name2id['rel2binary_rel'] = self.reverse_set_maps(self.name2id['binary_rel2rels'])
        self.name2id['rel2subj'] = self.reverse_set_maps(self.name2id['subj2rels'])
        self.name2id['triple2subj_obj'] = self.reverse_set_maps(self.name2id['subj_obj2triples'])
        # Add unused triples to map to no_relation. This is performed so that if the model predicts
        # an unseen triple, it does not fail. And instead is treated correctly.
        all_possible_triples = self.create_all_possible_triples()
        unused_triples = all_possible_triples - set(list(self.name2id['triple2subj_obj']))
        self.add_unused_triples(unused_triples, self.name2id['triple2subj_obj'])

        self.name2id['rel2ids'] = dict([(k, set([v])) for (k, v) in self.name2id['rel2id'].items()])
        self.num_rel = len(self.name2id['rel2id'])
        return parsed_data

    def triple2rel(self, triple):
        return triple[1]

    def triple2triple(self, triple):
        return triple

    def create_all_possible_triples(self):
        subjects = self.name2id['subj2id'].values()
        relations = self.name2id['rel2id'].values()
        objects = self.name2id['obj2id'].values()

        all_triples = set()
        for subject in subjects:
            for relation in relations:
                for object in objects:
                    triple = (subject, relation, object)
                    all_triples.add(triple)
        return all_triples

    def add_unused_triples(self, unused_triples, mappings):
        for unused_triple in unused_triples:
            assert unused_triple not in mappings
            mappings[unused_triple] = 'no_relation'

    def reverse_set_maps(self, dict2set):
        id2id = {}
        for key, value_set in dict2set.items():
            for value in value_set:
                id2id[value] = key
        return id2id

    def get_positions(self, start_idx, end_idx, length):
        """ Get subj/obj position sequence. """
        return list(range(-start_idx, 0)) + [0] * (end_idx - start_idx + 1) + \
               list(range(1, length - end_idx))

    def map_to_ids(self, names, mapper):
        return [mapper[t] if t in mapper else constant.UNK_ID for t in names]

    def perform_stratified_sampling(self, data):
        sample_size = self.config['sample_size']
        class2size = self.distribute_sample_size(sample_size)
        class2indices = self.group_by_class(data)
        class2sample = self.sample_by_class(class2indices, class2size)
        sample_indices = self.aggregate_sample_indices(class2sample)
        sample_data = np.array(data)[sample_indices]
        return sample_data

    def aggregate_sample_indices(self, class2sample):
        sample_indices = []
        for sample in class2sample.values():
            sample_indices.append(sample)
        sample_indices = np.concatenate(sample_indices)
        return sample_indices

    def sample_by_class(self, class2indices, class2size):
        class2sample = {}
        for relation, indices in class2indices.items():
            sample_size = min(class2size[relation], len(indices))
            sample_indices = np.random.choice(indices, sample_size, replace=False)
            class2sample[relation] = sample_indices
        return class2sample

    def group_by_class(self, data):
        class2indices = {}
        for idx, sample in enumerate(data):
            relation = sample['base']['relation']
            if relation not in class2indices:
                class2indices[relation] = []
            class2indices[relation].append(idx)
        return class2indices

    def distribute_sample_size(self, sample_size):
        class2size = {}
        class_sample_size = int(sample_size / len(self.name2id['rel2id']))
        remainder = sample_size % len(self.name2id['rel2id'])
        class_sample_bonus = np.random.choice(len(self.name2id['rel2id']), remainder, replace=False)
        for rel_id in self.name2id['rel2id'].values():
            class2size[rel_id] = class_sample_size
            if rel_id in class_sample_bonus:
                class2size[rel_id] += 1
        return class2size

    def create_iterator(self, config, partition_name='train', curriculum_stage='full'):
        partition_data = self.partitions[partition_name]
        cleaned_data = []
        is_eval = True if partition_name != 'train' else False
        if self.config['sample_size'] is not None:
            partition_data = self.perform_stratified_sampling(partition_data)
        # Specify curriculum
        if curriculum_stage == 'binary':
            id2label = self.name2id['rel2binary_rel']
            label2id = self.name2id['binary_rel2rels']
            triple2id_fn = self.triple2rel
        elif curriculum_stage == 'subj_type':
            id2label = self.name2id['rel2subj']
            label2id = self.name2id['subj2rels']
            triple2id_fn = self.triple2rel
        elif curriculum_stage == 'subj_obj_type':
            id2label = self.name2id['triple2subj_obj']
            label2id = self.name2id['subj_obj2triples']
            triple2id_fn = self.triple2triple
        elif curriculum_stage == 'full':
            id2label = self.name2id['id2rel']
            label2id = self.name2id['rel2ids']
            triple2id_fn = self.triple2rel
        else:
            raise ValueError('Curriculum stage unsupported.')

        for raw_sample in partition_data:
            sample = deepcopy(raw_sample)
            # Extract matched relations
            relation_id = sample['base']['relation']

            subject_id = sample['base']['subj_id']
            object_id = sample['base']['obj_id']
            match_triple = (subject_id, relation_id, object_id)
            id = triple2id_fn(match_triple)
            relation_label = id2label[id]
            if curriculum_stage == 'subj_obj_type':
                matched_ids = list(map(lambda triple: triple[1], label2id[relation_label]))
            else:
                matched_ids = list(label2id[relation_label])
            matched_ids = np.unique(matched_ids)

            # Create binary relation vector for all correct relations at curriculum stage
            activated_relations = np.zeros(self.num_rel, dtype=np.float32)
            activated_relations[matched_ids] = 1.
            sample['base']['activated_relations'] = activated_relations

            if config['relation_masking']:
                subject, _, object = sample['supplemental']['relation_masking']
                known_relations = self.graph[(subject, object)]
                sample['supplemental']['relation_masking'] = (known_relations,)
            cleaned_data.append(sample)

        return Batcher(dataset=cleaned_data,
                       config=self.config,
                       id2label=id2label,
                       triple2id_fn=triple2id_fn,
                       is_eval=is_eval,
                       batch_size=self.config['batch_size'])

class Batcher(object):
    def __init__(self, dataset, config, id2label, triple2id_fn, batch_size=50, is_eval=False):
        self.id2label = id2label
        self.triple2id_fn = triple2id_fn
        self.batch_size = batch_size
        self.is_eval = is_eval
        self.config = config

        if not self.is_eval:
            np.random.shuffle(dataset)

        self.labels = [id2label[
                           self.triple2id_fn(d['supplemental']['triple'])
                       ] for d in dataset]
        self.num_examples = len(dataset)
        self.batches = self.create_batches(dataset, batch_size=batch_size)

    def create_batches(self, data, batch_size=50):
        batched_data = []
        for batch_start in range(0, len(data), batch_size):
            batch_end = batch_start + batch_size
            batch = data[batch_start: batch_end]
            # merge base batch components
            base_batch = list(map(lambda sample: self.base_mapper(sample['base']), batch))
            # merge supplemental components
            supplemental_names = data[0]['supplemental'].keys()
            supplemental = dict()
            for name in supplemental_names:
                supplemental[name] = list(map(lambda sample: sample['supplemental'][name], batch))
            
            data_batch = {'base': base_batch, 'supplemental': supplemental}
            batched_data.append(data_batch)
        return batched_data

    def base_mapper(self, sample):
        return (
            sample['tokens'],
            sample['pos'],
            sample['ner'],
            sample['deprel'],
            sample['subj_positions'],
            sample['obj_positions'],
            sample['activated_relations']
        )
    
    def ready_base_batch(self, base_batch, batch_size):
        batch = list(zip(*base_batch))
        assert len(batch) == 7

        # sort all fields by lens for easy RNN operations
        lens = [len(x) for x in batch[0]]
        batch, orig_idx = self.sort_all(batch, lens)

        # word dropout
        if not self.is_eval:
            words = [self.word_dropout(sent, self.config['word_dropout']) for sent in batch[0]]
        else:
            words = batch[0]

        # convert to tensors
        words = self.get_long_tensor(words, batch_size)
        masks = torch.eq(words, 0)
        pos = self.get_long_tensor(batch[1], batch_size)
        ner = self.get_long_tensor(batch[2], batch_size)
        deprel = self.get_long_tensor(batch[3], batch_size)
        subj_positions = self.get_long_tensor(batch[4], batch_size)
        obj_positions = self.get_long_tensor(batch[5], batch_size)

        rels = self.get_long_tensor(batch[-1], batch_size).type(torch.float32)

        merged_components = (words, masks, pos, ner, deprel, subj_positions, obj_positions, rels, orig_idx)
        return {'base': merged_components, 'sentence_lengths': lens}

    def ready_masks_batch(self, masks_batch, batch_size, sentence_lengths):
        batch = list(zip(*masks_batch))
        # sort all fields by lens for easy RNN operations
        batch, _ = self.sort_all(batch, sentence_lengths)

        subj_masks = self.get_long_tensor(batch[0], batch_size)
        obj_masks = self.get_long_tensor(batch[1], batch_size)
        merged_components = (subj_masks, obj_masks)
        return merged_components

    def ready_relation_masks_batch(self, mask_batch, sentence_lengths):
        num_rel = len(self.rel2id)
        batch = list(zip(*mask_batch))
        known_relations, _ = self.sort_all(batch, sentence_lengths)
        labels = []
        for sample_labels in known_relations[0]:
            binary_labels = np.zeros(num_rel, dtype=np.float32)
            binary_labels[list(sample_labels)] = 1.
            labels.append(binary_labels)
        labels = np.stack(labels, axis=0)
        labels = torch.FloatTensor(labels)
        return (labels,)

    def ready_binary_labels_batch(self, label_batch, sentence_lengths):
        # Remove the no_relation from the mappings. Note, this is only possible because we've
        # guaranteed that no_relation is the last index - so "positive" relations naturally
        # map to the resultant label vector.
        num_rel = len(self.rel2id) - 1
        batch = list(zip(*label_batch))
        batch_labels, _ = self.sort_all(batch, sentence_lengths)
        labels = []
        for label in batch_labels[0]:
            binary_labels = np.zeros(num_rel, dtype=np.float32)
            # don't add no_relation index because that would be out of bounds.
            if label < num_rel:
                binary_labels[label] = 1.
            labels.append(binary_labels)
        labels = np.stack(labels, axis=0)
        labels = torch.FloatTensor(labels)
        return (labels,)

    def ready_binary_classification_batch(self, label_batch, sentence_lengths):
        batch = list(zip(*label_batch))
        batch_labels, _ = self.sort_all(batch, sentence_lengths)
        labels = torch.LongTensor(batch_labels[0])
        return (labels,)

    def ready_triple_batch(self, triple_batch, sentence_lengths):
        batch = list(zip(*triple_batch))
        sorted_batch, _ = self.sort_all(batch, sentence_lengths)
        subjects, relations, objects = sorted_batch
        subjects = torch.LongTensor(subjects)
        relations = torch.LongTensor(relations)
        objects = torch.LongTensor(objects)
        return (subjects, relations, objects)

    def ready_data_batch(self, batch):
        batch_size = len(batch['base'])
        readied_batch = self.ready_base_batch(batch['base'], batch_size)
        readied_batch['supplemental'] = dict()
        readied_supplemental = readied_batch['supplemental']
        for name, supplemental_batch in batch['supplemental'].items():
            if name == 'relation_masks':
                readied_supplemental[name] = self.ready_relation_masks_batch(
                    mask_batch=supplemental_batch,
                    sentence_lengths=readied_batch['sentence_lengths'])
            elif name == 'binary_labels':
                readied_supplemental[name] = self.ready_binary_labels_batch(
                    label_batch=supplemental_batch,
                    sentence_lengths=readied_batch['sentence_lengths'])
            elif name == 'triple':
                readied_supplemental[name] = self.ready_triple_batch(
                    triple_batch=supplemental_batch,
                    sentence_lengths=readied_batch['sentence_lengths']
                )
        return readied_batch

    def get_long_tensor(self, tokens_list, batch_size):
        """ Convert list of list of tokens to a padded LongTensor. """
        token_len = max(len(x) for x in tokens_list)
        tokens = torch.LongTensor(batch_size, token_len).fill_(constant.PAD_ID)
        for i, s in enumerate(tokens_list):
            tokens[i, :len(s)] = torch.LongTensor(s)
        return tokens

    def word_dropout(self, tokens, dropout):
        """ Randomly dropout tokens (IDs) and replace them with <UNK> tokens. """
        return [constant.UNK_ID if x != constant.UNK_ID and np.random.random() < dropout \
                    else x for x in tokens]

    def sort_all(self, batch, lens):
        """ Sort all fields by descending order of lens, and return the original indices. """
        unsorted_all = [lens] + [range(len(lens))] + list(batch)
        sorted_all = [list(t) for t in zip(*sorted(zip(*unsorted_all), reverse=True))]
        return sorted_all[2:], sorted_all[1]
    
    def __len__(self):
        return len(self.batches)
    
    def __getitem__(self, item):
        if not isinstance(item, int):
            raise TypeError
        if item < 0 or item >= self.__len__():
            raise IndexError
        batch = self.batches[item]
        batch = self.ready_data_batch(batch)
        return batch
        
    def __iter__(self):
        for i in range(self.__len__()):
            yield self.__getitem__(i)