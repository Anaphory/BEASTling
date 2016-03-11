from __future__ import division, unicode_literals
import os
import sys
import re
import io

import importlib
import newick
from appdirs import user_data_dir
from six.moves.urllib.request import FancyURLopener
from clldutils.inifile import INI

import beastling.clocks.strict as strict
import beastling.clocks.relaxed as relaxed
import beastling.clocks.random as random

import beastling.models.bsvs as bsvs
import beastling.models.covarion as covarion
import beastling.models.mk as mk



GLOTTOLOG_NODE_LABEL = re.compile(
    "'(?P<name>[^\[]+)\[(?P<glottocode>[a-z0-9]{8})\](\[(?P<isocode>[a-z]{3})\])?'")


class URLopener(FancyURLopener):
    def http_error_default(self, url, fp, errcode, errmsg, headers):
        raise ValueError()


class UniversalSet(set):
    """Set which intersects fully with any other set."""
    # Based on https://stackoverflow.com/a/28565931
    def __and__(self, other):
        return other

    def __rand__(self, other):
        return other


def get_glottolog_newick(release):
    fname = 'glottolog-%s.newick' % release
    path = os.path.join(os.path.dirname(__file__), 'data', fname)
    if not os.path.exists(path):
        data_dir = user_data_dir('beastling')
        if not os.path.exists(data_dir):
            os.makedirs(data_dir)
        path = os.path.join(data_dir, fname)
        if not os.path.exists(path):
            try:
                URLopener().retrieve(
                    'http://glottolog.org/static/download/%s/tree-glottolog-newick.txt'
                    % release,
                    path)
            except (IOError, ValueError):
                raise ValueError(
                    'Could not retrieve classification for Glottolog %s' % release)
    return newick.read(path)


class Configuration(object):

    def __init__(self, basename="beastling", configfile=None, stdin_data=False):
        self.processed = False
        self.messages = []
        self.message_flags = []

        # Set up default options
        self.basename = basename
        self.clock_configs = []
        self.configfile = None
        self.configfile_text = None
        self.chainlength = 10000000
        self.embed_data = False
        self.sample_from_prior = False
        self.families = "*"
        self.languages = "*"
        self.overlap = "union"
        self.starting_tree = ""
        self.sample_branch_lengths = True
        self.sample_topology = True
        self.model_configs = []
        self.monophyly = False
        self.monophyly_start_depth = 0
        self.monophyly_end_depth = None
        self.monophyly_levels = sys.maxsize
        self.monophyly_direction = "top_down"
        self.screenlog = True
        self.log_all = False
        self.log_every = 0
        self.log_params = False
        self.log_probabilities = True
        self.log_trees = True
        self.stdin_data = stdin_data
        self.calibrations = {}
        self.glottolog_release = '2.7'
        self.classifications = {}

        if configfile:
            self.read_from_file(configfile)

    def read_from_file(self, configfile):
        # Read config file and overwrite defaults
        self.configfile = INI(interpolation=None)
        if isinstance(configfile, dict):
            self.configfile.read_dict(configfile)
        else:
            self.configfile.read(configfile)
        p = self.configfile

        for sec, opts in {
            'admin': {
                'basename': p.get,
                'embed_data': p.getboolean,
                'screenlog': p.getboolean,
                'log_every': p.getint,
                'log_all': p.getboolean,
                'log_probabilities': p.getboolean,
                'log_params': p.getboolean,
                'log_trees': p.getboolean,
                'glottolog_release': p.get,
            },
            'MCMC': {
                'chainlength': p.getint,
                'sample_from_prior': p.getboolean,
            },
            'languages': {
                'languages': p.get,
                'families': p.get,
                'overlap': p.get,
                'starting_tree': p.get,
                'sample_branch_lengths': p.getboolean,
                'sample_topology': p.getboolean,
                'monophyly_start_depth': p.getint,
                'monophyly_end_depth': p.getint,
                'monophyly_levels': p.getint,
                'monophyly_direction': lambda s, o: p.get(s, o).lower(),
            },
        }.items():
            for opt, getter in opts.items():
                if p.has_option(sec, opt):
                    setattr(self, opt, getter(sec, opt))

        ## Languages
        sec = "languages"
        if self.overlap.lower() not in ("union", "intersection"):  # pragma: no cover
            raise ValueError(
                "Value for overlap needs to be either 'union', or 'intersection'."
            )
        if (self.starting_tree and not
                (self.sample_topology or self.sample_branch_lengths)):
            self.tree_logging_pointless = True
            self.messages.append(
                "[INFO] Tree logging disabled because starting tree is known and fixed.")
        else:
            self.tree_logging_pointless = False

        if p.has_option(sec, "monophyletic"):
            self.monophyly = p.getboolean(sec, "monophyletic")
        elif p.has_option(sec, "monophyly"):
            self.monophyly = p.getboolean(sec, "monophyly")

        ## Calibration
        if p.has_section("calibration"):
            for clades, dates in p.items("calibration"):
                for clade in clades.split(','):
                    clade = clade.strip()
                    if clade:
                        self.calibrations[clade.lower()] = [
                            float(x.strip()) for x in dates.split("-", 1)]

        ## Clocks
        clock_sections = [s for s in p.sections() if s.lower().startswith("clock")]
        for section in clock_sections:
            self.clock_configs.append(self.get_clock_config(p, section))

        ## Models
        model_sections = [s for s in p.sections() if s.lower().startswith("model")]
        if not model_sections:
            raise ValueError("Config file contains no model sections.")
        for section in model_sections:
            self.model_configs.append(self.get_model_config(p, section))

    def get_clock_config(self, p, section):
        cfg = {
            'name': section[5:].strip(),
        }
        for key, value in p[section].items():
            cfg[key] = value
        return cfg

    def get_model_config(self, p, section):
        cfg = {
            'name': section[5:].strip(),
            'binarised': None,
            'rate_variation': False,
            'remove_constant_features': True,
        }
        for key, value in p[section].items():
            # "binarised" is the canonical name for this option and used everywhere
            # internally, but "binarized" is accepted in the config file.
            if key == 'binarized':
                value = p.getboolean(section, key)
                key = 'binarised'

            if key in ['rate_variation', 'remove_constant_features']:
                value = p.getboolean(section, key)

            if key in ['minimum_data']:
                value = p.getfloat(section, key)

            cfg[key] = value
        return cfg

    def process(self):
        # Add dependency notice if required
        if self.monophyly and not self.starting_tree:
            self.messages.append("[DEPENDENCY] ConstrainedRandomTree is implemented in the BEAST package BEASTLabs.")

        # If log_every was not explicitly set to some non-zero
        # value, then set it such that we expect 10,000 log
        # entries
        if not self.log_every:
            # If chainlength < 10000, this results in log_every = zero.
            # This causes BEAST to die.
            # So in this case, just log everything.
            self.log_every = self.chainlength // 10000 or 1

        self.load_glotto_class()
        self.build_language_filter()
        self.instantiate_clocks()
        self.instantiate_models()
        self.link_clocks_to_models()
        self.build_language_list()
        self.handle_starting_tree()
        self.processed = True

    def load_glotto_class(self):
        label2name = {}

        def parse_label(label):
            match = GLOTTOLOG_NODE_LABEL.match(label)
            label2name[label] = (match.group('name').strip().replace("\\'","'"), match.group('glottocode'))
            return (
                match.group('name').strip(),
                match.group('glottocode'),
                match.group('isocode'))

        def get_classification(node):
            res = []
            ancestor = node.ancestor
            while ancestor:
                res.append(label2name[ancestor.name])
                ancestor = ancestor.ancestor
            return list(reversed(res))

        glottolog_trees = get_glottolog_newick(self.glottolog_release)
        for tree in glottolog_trees:
            for node in tree.walk():
                name, glottocode, isocode = parse_label(node.name)
                classification = get_classification(node)
                self.classifications[glottocode] = classification
                if isocode:
                    self.classifications[isocode] = classification

    def build_language_filter(self):
        # Handle languages - could be a list or a file
        if os.path.exists(self.languages):
            with io.open(self.languages, encoding="UTF-8") as fp:
                self.languages = [x.strip() for x in fp.readlines()]
        else:
            self.languages = [x.strip() for x in self.languages.split(",")]

        # Handle families - could be a list or a file
        if os.path.exists(self.families):
            with io.open(self.families, encoding="UTF-8") as fp:
                self.families = [x.strip() for x in fp.readlines()]
        else:
            self.families = [x.strip() for x in self.families.split(",")]

        # Build language filter
        if self.languages != ["*"] and self.families != ["*"]:
            # Can't filter by languages and families at same time!
            raise ValueError("languages and families both defined in [languages]!")
        elif self.languages != ["*"]:
            # Filter by language
            self.lang_filter = set(self.languages)
        elif self.families != ["*"]:
            self.lang_filter = {
                l for l in self.classifications
                if any([family in [n for t in self.classifications[l] for n in t]
                        for family in self.families])}
        else:
            self.lang_filter = UniversalSet()

    def instantiate_clocks(self):
        # Instantiate clocks
        self.clocks = []
        self.clocks_by_name = {}
        for config in self.clock_configs:
            if config["type"].lower() == "strict":
                clock = strict.StrictClock(config, self) 
            elif config["type"].lower() == "relaxed":
                clock = relaxed.relaxed_clock_factory(config, self)
            elif config["type"].lower() == "random":
                clock = random.RandomLocalClock(config, self) 
            self.clocks.append(clock)
            self.clocks_by_name[clock.name] = clock
        # Create default clock if necessary
        if "default" not in self.clocks_by_name:
            config = {}
            config["name"] = "default"
            config["type"] = "strict"
            clock = strict.StrictClock(config, self)
            self.clocks.append(clock)
            self.clocks_by_name[clock.name] = clock

    def instantiate_models(self):
        # Instantiate models
        if not self.model_configs:
            raise ValueError("No models specified!")

        # Handle request to read data from stdin
        if self.stdin_data:
            for config in self.model_configs:
                config["data"] = "stdin"

        self.models = []
        for config in self.model_configs:
            # Validate config
            if "model" not in config:
                raise ValueError("Model not specified for model section %s." % config["name"])
            if "data" not in config:
                raise ValueError("Data source not specified in model section %s." % config["name"])

            # Instantiate model
            if config["model"].lower() == "bsvs":
                model = bsvs.BSVSModel(config, self)
                if "bsvs_used" not in self.message_flags:
                    self.message_flags.append("bsvs_used")
                    self.messages.append(bsvs.BSVSModel.package_notice)
            elif config["model"].lower() == "covarion":
                model = covarion.CovarionModel(config, self)
            elif config["model"].lower() == "mk":
                model = mk.MKModel(config, self)
                if "mk_used" not in self.message_flags:
                    self.message_flags.append("mk_used")
                    self.messages.append(mk.MKModel.package_notice)
            else:
                model_name = config["model"].lower()
                try:
                    model = importlib.import_module(".models."+config["model"], "beastling").Model(config, self)
                except ImportError:
                    raise ValueError("Unknown model type '%s' for model section '%s'." % (config["model"], config["name"]))
                try:
                    if model.package_notice not in self.messages:
                        self.messages.append(model.package_notice)
                except AttributeError:
                    pass
            if config["model"].lower() != "covarion":
                self.messages.append("""[DEPENDENCY] Model %s: AlignmentFromTrait is implemented in the BEAST package "BEAST_CLASSIC".""" % config["name"])
            self.messages.extend(model.messages)
            self.models.append(model)

    def link_clocks_to_models(self):
        # Add a clock object to each model object
        for model in self.models:
            if model.clock:
                # User has explicitly specified a clock
                if model.clock not in self.clocks_by_name:
                    raise ValueError("Unknown clock '%s' for model section '%s'." % (model.clock, model.name))
                model.clock = self.clocks_by_name[model.clock]
            elif model.name in self.clocks_by_name:
                # Clock is associated by a common name
                model.clock = self.clocks_by_name[model.name]
            else:
                # No clock specification - use default
                model.clock = self.clocks_by_name["default"]
            model.clock.is_used = True

        # Warn user about unused clock(s)
        for clock in self.clocks:
            if not clock.is_used:
                self.messages.append("""[INFO] Clock %s is not being used.  Change its name to "default", or explicitly associate it with a model.""" % clock.name)

    def build_language_list(self):
        # Finalise language list.
        self.languages = set(self.models[0].data.keys())
        self.overlap_warning = False
        for model in self.models:
            addition = set(model.data.keys())
            # If we're about to do a non-trivial union/intersect, alert the
            # user.
            if addition != self.languages and not self.overlap_warning:
                self.messages.append("""[INFO] Not all data files have equal language sets.  BEASTling will use the %s of all language sets.  Set the "overlap" option in [languages] to change this.""" % self.overlap.lower())
                self.overlap_warning = True
            if self.overlap.lower() == "union":
                self.languages = set.union(self.languages, addition)
            elif self.overlap.lower() == "intersection":
                self.languages = set.intersection(self.languages, addition)

        ## Make sure there's *something* left
        if not self.languages:
            raise ValueError("No languages specified!")

        ## Convert back into a sorted list
        self.languages = sorted(self.languages)
        self.messages.append("[INFO] %d languages included in analysis." % len(self.languages))

    def handle_starting_tree(self):
        # Read starting tree from file
        if os.path.exists(self.starting_tree):
            with io.open(self.starting_tree, encoding="UTF-8") as fp:
                self.starting_tree = fp.read().strip()
        if self.starting_tree:
            # Make sure starting tree can be parsed
            try:
                tree = newick.loads(self.starting_tree)[0]
            except:
                raise ValueError("Could not parse starting tree.  Is it valid Newick?")
            # Make sure starting tree contains no duplicate taxa
            tree_langs = [n.name for n in tree.walk() if n.is_leaf]
            if not len(set(tree_langs)) == len(tree_langs):
                dupes = [l for l in tree_langs if tree_langs.count(l) > 1]
                dupestring = ",".join(["%s (%d)" % (d, tree_langs.count(d)) for d in dupes])
                raise ValueError("Starting tree contains duplicate taxa: %s" % dupestring)
            tree_langs = set(tree_langs)
            # Make sure languges in tree is a superset of languages in the analysis
            if not tree_langs.issuperset(self.languages):
                missing_langs = self.languages.difference(tree_langs)
                miss_string = ",".join(missing_langs)
                raise ValueError("Some languages in the data are not in the starting tree: %s" % miss_string)
            # If the trees' language set is a proper superset, prune the tree to fit the analysis
            if not tree_langs == self.languages:
                tree.prune_by_names(self.languages, inverse=True)
                self.messages.append("[INFO] Starting tree includes languages not present in any data set and will be pruned.")
            # Get the tree looking nice
            tree.remove_redundant_nodes()
            tree.resolve_polytomies()
            # Replace the starting_tree from the config with the new one
            self.starting_tree = newick.dumps(tree)

