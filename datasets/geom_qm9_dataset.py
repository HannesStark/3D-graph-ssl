import json
import os
import pickle

import goli
import msgpack
import torch
import dgl
from goli.features.positional_encoding import graph_positional_encoder
from rdkit import Chem
from rdkit.Chem.rdmolops import GetAdjacencyMatrix
from scipy.sparse import csr_matrix
from torch.utils.data import Dataset
import numpy as np
import pandas as pd
from tqdm import tqdm
import torch.nn.functional as F
from scipy.constants import physical_constants

from commons.spherical_encoding import dist_emb

hartree2eV = physical_constants['hartree-electron volt relationship'][0]


class GEOMqm9(Dataset):
    """The GEOM Drugs Dataset using drugs_crude.msgpack as input from https://github.com/learningmatter-mit/geom
    Attributes
    ----------
    return_types: list
        A list with which types of data should be loaded and returened by getitems. Possible options are
        ['mol_graph', 'raw_features', 'coordinates', 'mol_id', 'targets', 'one_hot_bond_types', 'edge_indices', 'smiles', 'atomic_number_long']
        and the default is ['mol_graph', 'targets']
    target_tasks: list
        A list specifying which targets should be included in the returend targets, if targets are returned
        options are ['A', 'B', 'C', 'mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv', 'u0_atom', 'u298_atom', 'h298_atom', 'g298_atom']
        and default is ['mu', 'alpha', 'homo', 'lumo', 'gap', 'r2', 'zpve', 'u0', 'u298', 'h298', 'g298', 'cv']
        which is the stuff that is commonly predicted by papers like DimeNet, Equivariant GNNs, Spherical message passing
        The returned targets will be in the order specified by this list
    features:
        possible features are ['standard_normal_noise', 'implicit-valence','degree','hybridization','chirality','mass','electronegativity','aromatic-bond','formal-charge','radical-electron','in-ring','atomic-number', 'pos-enc', 'vec1', 'vec2', 'vec3', 'vec-1', 'vec-2', 'vec-3', 'inv_vec1', 'inv_vec2', 'inv_vec3', 'inv_vec-1', 'inv_vec-2', 'inv_vec-3']

    features3d:
        possible features are ['standard_normal_noise', 'implicit-valence','degree','hybridization','chirality','mass','electronegativity','aromatic-bond','formal-charge','radical-electron','in-ring','atomic-number', 'pos-enc', 'vec1', 'vec2', 'vec3', 'vec-1', 'vec-2', 'vec-3', 'inv_vec1', 'inv_vec2', 'inv_vec3', 'inv_vec-1', 'inv_vec-2', 'inv_vec-3']
    e_features:
        possible are ['bond-type-onehot','stereo','conjugated','in-ring-edges']

    """

    def __init__(self, return_types: list = None, features: list = [], features3d: list = [],
                 e_features: list = [], e_features3d: list = [], pos_dir: bool = False,
                 target_tasks: list = None,
                 normalize: bool = True, device='cuda:0', dist_embedding: bool = False, num_radial: int = 6,
                 prefetch_graphs=True, transform=None, **kwargs):
        self.return_type_options = ['mol_graph', 'complete_graph', 'mol_graph3d', 'complete_graph3d', 'san_graph',
                                    'mol_complete_graph',
                                    'se3Transformer_graph', 'se3Transformer_graph3d',
                                    'pairwise_distances', 'pairwise_distances_squared',
                                    'pairwise_indices',
                                    'raw_features', 'coordinates',
                                    'dist_embedding',
                                    'mol_id', 'targets',
                                    'one_hot_bond_types', 'edge_indices', 'smiles', 'atomic_number_long',
                                    'conformations', 'uniqueconfs']
        self.target_types = ['ensembleenergy', 'ensembleentropy', 'ensemblefreeenergy', 'lowestenergy', 'poplowestpct',
                             'temperature', 'uniqueconfs']
        self.directory = 'dataset/GEOM'
        self.processed_file = 'geom_qm9_processed.pt'
        self.atom_types = {'H': 0, 'C': 1, 'N': 2, 'O': 3, 'F': 4}
        self.symbols = {'H': 1, 'C': 6, 'N': 7, 'O': 8, 'F': 9}
        self.normalize = normalize
        self.device = device
        self.transform = transform
        self.pos_dir = pos_dir
        self.num_radial = num_radial

        if return_types == None:  # set default
            self.return_types: list = ['mol_graph', 'targets']
        else:
            self.return_types: list = return_types
        for return_type in self.return_types:
            if not return_type in self.return_type_options: raise Exception(f'return_type not supported: {return_type}')

        # load the data and get normalization values
        if not os.path.exists(os.path.join(self.directory, 'processed', self.processed_file)):
            self.process()
        print('load pickle')
        data_dict = torch.load(os.path.join(self.directory, 'processed', self.processed_file))
        print('finish loading')

        if features and 'constant_ones' in features or features3d and 'constant_ones' in features3d:
            data_dict['constant_ones'] = torch.ones_like(data_dict['atomic_numbers_long'], dtype=torch.float)[:, None]
        if features and 'standard_normal_noise' in features or features3d and 'standard_normal_noise' in features3d:
            data_dict['standard_normal_noise'] = torch.normal(
                mean=torch.zeros_like(data_dict['atomic_number_long'], dtype=torch.float),
                std=torch.ones_like(data_dict['atomic_number_long'], dtype=torch.float))

        self.features_tensor = None if features == [] else torch.cat([data_dict[k] for k in features], dim=-1)
        self.features3d_tensor = None if features3d == [] else torch.cat([data_dict[k] for k in features3d], dim=-1)

        self.e_features_tensor = None if e_features == [] else torch.cat([data_dict[k] for k in e_features],
                                                                         dim=-1).float()
        self.e_features3d_tensor = None if e_features3d == [] else torch.cat([data_dict[k] for k in e_features3d],
                                                                             dim=-1).float()

        self.coordinates = data_dict['coordinates'][:, :3]
        self.conformations = data_dict['coordinates'] if 'conformations' in self.return_types else None
        self.edge_indices = data_dict['edge_indices']

        self.meta_dict = {k: data_dict[k] for k in ('smiles', 'edge_slices', 'atom_slices', 'n_atoms', 'uniqueconfs')}

        if 'san_graph' in self.return_types:
            self.eig_vals = data_dict['eig_vals']
            self.eig_vecs = data_dict['eig_vecs']

        self.prefetch_graphs = prefetch_graphs
        if self.prefetch_graphs and any(return_type in self.return_types for return_type in
                                        ['mol_graph', 'mol_graph3d', 'se3Transformer_graph', 'se3Transformer_graph3d']):
            print(
                'Load molecular graphs into memory (set prefetch_graphs to False to load them on the fly => slower training)')
            self.mol_graphs = []
            for idx in tqdm(range(len(self.meta_dict['edge_slices']) - 1)):
                e_start = self.meta_dict['edge_slices'][idx]
                e_end = self.meta_dict['edge_slices'][idx + 1]
                edge_indices = self.edge_indices[:, e_start: e_end]
                n_atoms = self.meta_dict['n_atoms'][idx]
                self.mol_graphs.append(dgl.graph((edge_indices[0], edge_indices[1]), num_nodes=n_atoms))
        self.pairwise = {}  # for memoization
        if self.prefetch_graphs and (
                'complete_graph' in self.return_types or 'complete_graph3d' in self.return_types or 'san_graph' in self.return_types):
            print(
                'Load complete graphs into memory (set prefetch_graphs to False to load them on the fly => slower training)')
            self.complete_graphs = []
            for idx in tqdm(range(len(self.meta_dict['edge_slices']) - 1)):
                src, dst = self.get_pairwise(self.meta_dict['n_atoms'][idx])
                self.complete_graphs.append(dgl.graph((src, dst)))
        if self.prefetch_graphs and (
                'mol_complete_graph' in self.return_types or 'mol_complete_graph3d' in self.return_types):
            print(
                'Load mol_complete_graph graphs into memory (set prefetch_graphs to False to load them on the fly => slower training)')
            self.mol_complete_graphs = []
            for idx in tqdm(range(len(self.meta_dict['edge_slices']) - 1)):
                src, dst = self.get_pairwise(self.meta_dict['n_atoms'][idx])
                self.mol_complete_graphs.append(
                    dgl.heterograph({('atom', 'bond', 'atom'): (src, dst), ('atom', 'complete', 'atom'): (src, dst)}))
        print('Finish loading data into memory')

        self.avg_degree = data_dict['avg_degree']
        # indices of the tasks that should be retrieved
        # select targets in the order specified by the target_tasks argument
        if 'targets' in self.return_types:
            self.targets = data_dict[target_tasks[0]]
            self.targets_mean = self.targets.mean(dim=0)
            self.targets_std = self.targets.std(dim=0)
            if self.normalize:
                self.targets = ((self.targets - self.targets_mean) / self.targets_std)
            self.targets_mean = self.targets_mean.to(device)
            self.targets_std = self.targets_std.to(device)

    def __len__(self):
        return len(self.meta_dict['smiles'])

    def get_pairwise(self, n_atoms):
        if n_atoms in self.pairwise:
            return self.pairwise[n_atoms]
        else:
            src = torch.repeat_interleave(torch.arange(n_atoms), n_atoms - 1)
            dst = torch.cat([torch.cat([torch.arange(n_atoms)[:idx], torch.arange(n_atoms)[idx + 1:]]) for idx in
                             range(n_atoms)])  # without self loops
            self.pairwise[n_atoms] = (src, dst)
            return src, dst

    def __getitem__(self, idx):
        """

        Parameters
        ----------
        idx: integer between 0 and len(self) - 1

        Returns
        -------
        tuple of all data specified via the return_types parameter of the constructor
        """
        data = []
        e_start = self.meta_dict['edge_slices'][idx]
        e_end = self.meta_dict['edge_slices'][idx + 1]
        start = self.meta_dict['atom_slices'][idx]
        n_atoms = self.meta_dict['n_atoms'][idx]

        for return_type in self.return_types:
            data.append(self.data_by_type(idx, return_type, e_start, e_end, start, n_atoms))
        return tuple(data)

    def get_graph(self, idx, e_start, e_end, n_atoms):
        if self.prefetch_graphs:
            g = self.mol_graphs[idx]
        else:
            edge_indices = self.edge_indices[:, e_start: e_end]
            g = dgl.graph((edge_indices[0], edge_indices[1]), num_nodes=n_atoms)
        return g

    def get_complete_graph(self, idx, n_atoms):
        if self.prefetch_graphs:
            g = self.complete_graphs[idx]
        else:
            src, dst = self.get_pairwise(n_atoms)
            g = dgl.graph((src, dst))
        return g

    def get_mol_complete_graph(self, idx, e_start, e_end, n_atoms):
        if self.prefetch_graphs:
            g = self.mol_complete_graphs[idx]
        else:
            edge_indices = self.edge_indices[:, e_start: e_end]
            src, dst = self.get_pairwise(n_atoms)
            g = dgl.heterograph({('atom', 'bond', 'atom'): (edge_indices[0], edge_indices[1]),
                                 ('atom', 'complete', 'atom'): (src, dst)})
        return g

    def data_by_type(self, idx, return_type, e_start, e_end, start, n_atoms):
        if return_type == 'conformations':
            return self.conformations[start: start + n_atoms].to(self.device)
        if return_type == 'mol_graph':
            g = self.get_graph(idx, e_start, e_end, n_atoms).to(self.device)
            g.ndata['f'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            if self.e_features_tensor != None:
                g.edata['w'] = self.e_features_tensor[e_start: e_end].to(self.device)
            return g
        elif return_type == 'mol_graph3d':
            g = self.get_graph(idx, e_start, e_end, n_atoms).to(self.device)
            g.ndata['f'] = self.features3d_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            if self.e_features3d_tensor != None:
                g.edata['w'] = self.e_features3d_tensor[e_start: e_end].to(self.device)
            return g
        elif return_type == 'complete_graph':  # complete graph without self loops
            g = self.get_complete_graph(idx, n_atoms).to(self.device)
            g.ndata['f'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            g.edata['d'] = torch.norm(g.ndata['x'][g.edges()[0]] - g.ndata['x'][g.edges()[1]], p=2, dim=-1).unsqueeze(
                -1)
            if self.e_features_tensor != None:
                bond_features = self.e_features_tensor[e_start: e_end].to(self.device)
                e_features = torch.zeros((n_atoms * n_atoms, bond_features.shape[1]), device=self.device)
                edge_indices = self.edge_indices[:, e_start: e_end]
                bond_indices = edge_indices[0] * n_atoms + edge_indices[1]
                e_features[bond_indices] = bond_features
                src, dst = self.get_pairwise(n_atoms)
                g.edata['w'] = e_features[src * n_atoms + dst]
            return g
        elif return_type == 'complete_graph3d':
            g = self.get_complete_graph(idx, n_atoms).to(self.device)
            g.ndata['f'] = self.features3d_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            g.edata['d'] = torch.norm(g.ndata['x'][g.edges()[0]] - g.ndata['x'][g.edges()[1]], p=2, dim=-1).unsqueeze(
                -1)
            if self.e_features3d_tensor != None:
                bond_features = self.e_features3d_tensor[e_start: e_end].to(self.device)
                e_features = torch.zeros((n_atoms * n_atoms, bond_features.shape[1]), device=self.device)
                edge_indices = self.edge_indices[:, e_start: e_end]
                bond_indices = edge_indices[0] * n_atoms + edge_indices[1]
                e_features[bond_indices] = bond_features
                src, dst = self.get_pairwise(n_atoms)
                g.edata['w'] = e_features[src * n_atoms + dst]
            return g
        if return_type == 'mol_complete_graph':
            g = self.get_mol_complete_graph(idx, e_start, e_end, n_atoms).to(self.device)
            g.ndata['f'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            if self.e_features_tensor != None:
                g.edges['bond'].data['w'] = self.e_features_tensor[e_start: e_end].to(self.device)
            return g
        if return_type == 'san_graph':
            g = self.get_complete_graph(idx, n_atoms).to(self.device)
            g.ndata['f'] = self.features_tensor[start: start + n_atoms].to(self.device)
            g.ndata['x'] = self.coordinates[start: start + n_atoms].to(self.device)
            eig_vals = self.eig_vals[idx].to(self.device)
            sign_flip = torch.rand(eig_vals.shape[0], device=self.device)
            sign_flip[sign_flip >= 0.5] = 1.0
            sign_flip[sign_flip < 0.5] = -1.0
            eig_vecs = self.eig_vecs[start: start + n_atoms].to(self.device) * sign_flip.unsqueeze(0)
            eig_vals = eig_vals.unsqueeze(0).repeat(n_atoms, 1)
            g.ndata['pos_enc'] = torch.stack([eig_vals, eig_vecs], dim=-1)
            if self.e_features_tensor != None:
                e_features = self.e_features_tensor[e_start: e_end].to(self.device)
                g.edata['w'] = torch.zeros(g.number_of_edges(), e_features.shape[1], dtype=torch.float32,
                                           device=self.device)
                g.edata['real'] = torch.zeros(g.number_of_edges(), dtype=torch.long, device=self.device)
                edge_indices = self.edge_indices[:, e_start: e_end].to(self.device)
                g.edges[edge_indices[0], edge_indices[1]].data['w'] = e_features
                g.edges[edge_indices[0], edge_indices[1]].data['real'] = torch.ones(e_features.shape[0],
                                                                                    dtype=torch.long,
                                                                                    device=self.device)  # This indicates real edges
            return g
        elif return_type == 'se3Transformer_graph' or return_type == 'se3Transformer_graph3d':
            g = self.get_graph(idx, e_start, e_end, n_atoms).to(self.device)
            x = self.coordinates[start: start + n_atoms].to(self.device)
            if self.transform:
                x = self.transform(x)
            g.ndata['x'] = x
            g.ndata['f'] = self.features3d_tensor[start: start + n_atoms].to(self.device)[
                ..., None] if return_type == 'se3Transformer_graph3d' else \
                self.features_tensor[start: start + n_atoms].to(self.device)[..., None]
            g.edata['d'] = torch.norm(g.ndata['x'][g.edges()[0]] - g.ndata['x'][g.edges()[1]], p=2, dim=-1).unsqueeze(
                -1)
            if self.e_features_tensor != None and return_type == 'se3Transformer_graph':
                g.edata['w'] = self.e_features_tensor[e_start: e_end].to(self.device)
            elif self.e_features3d_tensor != None and return_type == 'se3Transformer_graph3d':
                g.edata['w'] = self.e_features3d_tensor[e_start: e_end].to(self.device)
            return g
        elif return_type == 'raw_features':
            return self.features_tensor[start: start + n_atoms]
        elif return_type == 'coordinates':
            return self.coordinates[start: start + n_atoms]
        elif return_type == 'conformations':
            return self.conformations[start: start + n_atoms]
        elif return_type == 'uniqueconfs':
            return self.meta_dict['uniqueconfs'][idx]
        elif return_type == 'targets':
            return self.targets[idx]
        elif return_type == 'edge_indices':
            return self.meta_dict['edge_indices'][:, e_start: e_end]
        elif return_type == 'smiles':
            return self.meta_dict['smiles'][idx]
        else:
            raise Exception(f'return type not supported: ', return_type)

    def process(self):
        print('processing data from ({}) and saving it to ({})'.format(self.directory,
                                                                       os.path.join(self.directory, 'processed')))

        with open(os.path.join(self.directory, "summary_qm9.json"), "r") as f:
            summary = json.load(f)

        atom_slices = [0]
        edge_slices = [0]
        atom_one_hot = []
        atomic_numbers_long = []
        n_atoms_list = []
        e_features = {'bond-type-onehot': [], 'stereo': [], 'conjugated': [], 'in-ring': []}
        atom_float = {'implicit-valence': [], 'degree': [], 'hybridization': [], 'chirality': [], 'mass': [],
                      'electronegativity': [], 'aromatic-bond': [], 'formal-charge': [], 'radical-electron': [],
                      'in-ring': []}
        targets = {'ensembleenergy': [], 'ensembleentropy': [], 'ensemblefreeenergy': [], 'lowestenergy': [],
                   'poplowestpct': [], 'temperature': [], 'uniqueconfs': []}
        edge_indices = []  # edges of each molecule in coo format
        coordinates = []
        smiles_list = []
        total_atoms = 0
        total_edges = 0
        avg_degree = 0  # average degree in the dataset
        for smiles, sub_dic in tqdm(summary.items()):
            pickle_path = os.path.join(self.directory, sub_dic.get("pickle_path", ""))
            if os.path.isfile(pickle_path):
                pickle_file = open(pickle_path, 'rb')
                mol_dict = pickle.load(pickle_file)
                if 'ensembleenergy' in mol_dict:
                    conformers = mol_dict['conformers']
                    mol = conformers[0]['rd_mol']
                    n_atoms = len(mol.GetAtoms())
                    for key, item in goli.features.get_mol_atomic_features_float(mol, list(atom_float.keys())).items():
                        atom_float[key].append(torch.tensor(item)[:, None])
                    type_idx = []
                    symbols = []
                    for atom in mol.GetAtoms():
                        type_idx.append(self.atom_types[atom.GetSymbol()])
                        atomic_numbers_long.append(self.symbols[atom.GetSymbol()])
                        symbols.append(atom.GetSymbol())
                    row, col = [], []
                    for ii in range(mol.GetNumBonds()):
                        bond = mol.GetBondWithIdx(ii)
                        start, end = bond.GetBeginAtomIdx(), bond.GetEndAtomIdx()
                        row += [start, end]
                        col += [end, start]
                    avg_degree += (len(row) / 2) / n_atoms
                    edge_index = torch.tensor([row, col], dtype=torch.long)
                    perm = (edge_index[0] * n_atoms + edge_index[1]).argsort()
                    edge_index = edge_index[:, perm]
                    for key, item in goli.features.get_mol_edge_features(mol, list(e_features.keys())).items():
                        # repeat interleave for src dst and dst src edges (see above where we add the edges) and then reorder using perm
                        e_features[key].append(torch.tensor(item).repeat_interleave(2, dim=0)[perm])

                    targets['ensembleenergy'].append(mol_dict['ensembleenergy'])
                    targets['ensembleentropy'].append(mol_dict['ensembleentropy'])
                    targets['ensemblefreeenergy'].append(mol_dict['ensemblefreeenergy'])
                    targets['lowestenergy'].append(mol_dict['lowestenergy'])
                    targets['poplowestpct'].append(mol_dict['poplowestpct'])
                    targets['temperature'].append(mol_dict['temperature'])
                    targets['uniqueconfs'].append(mol_dict['uniqueconfs'])
                    conformers = [torch.tensor(conformer['rd_mol'].GetConformer().GetPositions(), dtype=torch.float) for
                                  conformer in conformers[:10]]
                    if len(conformers) < 10:  # if there are less than 10 conformers we add the first one a few times
                        conformers.extend([conformers[0]] * (10 - len(conformers)))
                    coordinates.append(torch.cat(conformers, dim=1))
                    edge_indices.append(edge_index)
                    total_edges += len(row)
                    total_atoms += n_atoms
                    smiles_list.append(smiles)
                    edge_slices.append(total_edges)
                    atom_slices.append(total_atoms)
                    n_atoms_list.append(n_atoms)
                    atom_one_hot.append(F.one_hot(torch.tensor(type_idx), num_classes=len(self.atom_types)))
        data_dict = {}
        data_dict.update(e_features)
        data_dict.update(atom_float)
        for key, value in data_dict.items():
            data_dict[key] = torch.cat(data_dict[key])
        for key, value in targets.items():
            targets[key] = torch.tensor(value)[:, None]
        data_dict.update(targets)
        data_dict.update({'smiles': smiles_list,
                          'n_atoms': torch.tensor(n_atoms_list, dtype=torch.long),
                          'atom_slices': torch.tensor(atom_slices, dtype=torch.long),
                          'edge_slices': torch.tensor(edge_slices, dtype=torch.long),
                          'in-ring-edges': torch.cat(e_features['in-ring']),
                          'atomic-number': torch.cat(atom_one_hot).float(),
                          'atomic_numbers_long': torch.tensor(atomic_numbers_long, dtype=torch.long),
                          'edge_indices': torch.cat(edge_indices, dim=1),
                          'coordinates': torch.cat(coordinates, dim=0).float(),
                          'targets': targets,
                          'avg_degree': avg_degree / len(n_atoms_list)
                          })
        if not os.path.exists(os.path.join(self.directory, 'processed')):
            os.mkdir(os.path.join(self.directory, 'processed'))
        torch.save(data_dict, os.path.join(self.directory, 'processed', self.processed_file))
