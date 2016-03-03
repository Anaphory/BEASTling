import codecs
import datetime
import os
import pdb
import sys
import xml.etree.ElementTree as ET
from itertools import chain

from beastling import __version__
import beastling.beast_maps as beast_maps

def indent(elem, level=0):
    i = "\n" + level*"  "
    if len(elem):
        if not elem.text or not elem.text.strip():
            elem.text = i + "  "
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
        for elem in elem:
            indent(elem, level+1)
        if not elem.tail or not elem.tail.strip():
            elem.tail = i
    else:
        if level and (not elem.tail or not elem.tail.strip()):
            elem.tail = i

class BeastXml(object):

    def __init__(self, config):
        self.config = config
        if not self.config.processed:
            self.config.process()
        self.build_xml()

    def build_xml(self):

        # Root "beast" node
        attribs = {}
        attribs["beautitemplate"] = "Standard"
        attribs["beautistatus"] = ""
        attribs["namespace"] = "beast.core:beast.evolution.alignment:beast.evolution.tree.coalescent:beast.core.util:beast.evolution.nuc:beast.evolution.operators:beast.evolution.sitemodel:beast.evolution.substitutionmodel:beast.evolution.likelihood"
        attribs["version"] ="2.0"
        self.beast = ET.Element("beast", attrib=attribs)

        # "Generated by..." comment
        comment_lines = []
        comment_lines.append("Generated by BEASTling %s on %s." % (__version__,datetime.datetime.now().strftime("%A, %d %b %Y %l:%M %p")))
        if self.config.configfile:
            comment_lines.append("Original config file:")
            comment_lines.append(self.config.configfile.write_string())
        else:
            comment_lines.append("Configuration built programmatically.")
            comment_lines.append("No config file to include.")
        self.beast.append(ET.Comment("\n".join(comment_lines)))

        # Embed data
        if self.config.embed_data:
            for model in self.config.models:
                self.beast.append(self.format_data_file(model.data_filename))

        # Maps
        for a, b in beast_maps.maps:
            mapp = ET.SubElement(self.beast, "map", attrib={"name":a})
            mapp.text = b

        # State
        state = ET.SubElement(self.beast, "state", {"id":"state","storeEvery":"5000"})

        ## Taxon set
        taxonset = ET.SubElement(state, "taxonset", {"id":"taxa"})
        for lang in self.config.languages:
            ET.SubElement(taxonset, "taxon", {"id":lang,})

        ## Tree
        tree = ET.SubElement(state, "tree", {"id":"Tree.t:beastlingTree"})
        ET.SubElement(tree, "taxonset", {"idref":"taxa"})

        ## Birth model
        param = ET.SubElement(state, "parameter", {"id":"birthRate.t:beastlingTree","name":"stateNode"})
        param.text="1.0"

        for model in self.config.models:
            model.add_state(state)

        # Misc
        for model in self.config.models:
            model.add_misc(self.beast)

        # Run
        attribs = {}
        attribs["id"] = "mcmc"
        attribs["spec"] = "MCMC"
        attribs["chainLength"] = str(self.config.chainlength)
        if self.config.sample_from_prior:
            attribs["sampleFromPrior"] = "true"
        self.run = ET.SubElement(self.beast, "run", attrib=attribs)

        ## Init
        ### If a starting tree is specified, use it...
        if self.config.starting_tree:
            init = ET.SubElement(self.run, "init", {"estimate":"false", "id":"startingTree", "initial":"@Tree.t:beastlingTree", "spec":"beast.util.TreeParser","IsLabelledNewick":"true", "newick":self.config.starting_tree})
        ### ...if not, use a random tree
        else:
            ### But the random tree must respect any constraints!
            if self.config.monophyly:
                init = ET.SubElement(self.run, "init", {"estimate":"false", "id":"startingTree", "initial":"@Tree.t:beastlingTree", "taxonset":"@taxa", "spec":"beast.evolution.tree.ConstrainedRandomTree", "constraints":"@constraints"})
            else:
                init = ET.SubElement(self.run, "init", {"estimate":"false", "id":"startingTree", "initial":"@Tree.t:beastlingTree", "taxonset":"@taxa", "spec":"beast.evolution.tree.RandomTree"})
            popmod = ET.SubElement(init, "populationModel", {"spec":"ConstantPopulation"})
            ET.SubElement(popmod, "popSize", {"spec":"parameter.RealParameter","value":"1"})

        ## Distributions
        self.master_distribution = ET.SubElement(self.run,"distribution",{"id":"posterior","spec":"util.CompoundDistribution"})

        ### Prior
        self.add_prior()
        for model in self.config.models:
            model.add_prior(self.prior)

        ### Likelihood
        self.likelihood = ET.SubElement(self.master_distribution,"distribution",{"id":"likelihood","spec":"util.CompoundDistribution"})
        for model in self.config.models:
            model.add_likelihood(self.likelihood)

        ## Operators
        self.add_operators()

        ## Logging
        self.add_loggers()

    def format_data_file(self, filename):

        header = "BEASTling embedded data file: %s" % filename
        fp = open(filename, "r")
        data_block = "\n".join([header, fp.read()])
        fp.close()
        return ET.Comment(data_block)

    def add_prior(self):

        """Add "master prior" features, independent of any data
        or models.  E.g. monophyly constraints, clade calibration
        dates, tree priors, etc."""

        self.prior = ET.SubElement(self.master_distribution,"distribution",{"id":"prior","spec":"util.CompoundDistribution"})

        # Monophyly
        glotto_iso_langs = [l for l in self.config.languages if l.lower() in self.config.classifications]
        if self.config.monophyly and glotto_iso_langs:
            attribs = {}
            attribs["id"] = "constraints"
            attribs["spec"] = "beast.math.distributions.MultiMonophyleticConstraint"
            attribs["tree"] = "@Tree.t:beastlingTree"
            attribs["newick"] = self.make_monophyly_newick(glotto_iso_langs)
            ET.SubElement(self.prior, "distribution", attribs)

        # Calibration
        if self.config.calibrations:
            for n, clade in enumerate(self.config.calibrations):
                if clade == "root":
                    langs = self.config.languages
                else:
                    langs = []
                    for l in self.config.languages:
                        for name, glottocode in self.config.classifications[l.lower()]:
                            if clade == name.lower() or clade == glottocode:
                                langs.append(l)
                                break
                if not langs:
                    continue
                lower, upper = self.config.calibrations[clade]
                mean = (upper + lower) / 2.0
                stddev = (upper - mean) / 2.0
                attribs = {}
                attribs["id"] = clade + "-calibration.prior"
                attribs["monophyletic"] = "true"
                attribs["spec"] = "beast.math.distributions.MRCAPrior"
                attribs["tree"] = "@Tree.t:beastlingTree"
                cal_prior = ET.SubElement(self.prior, "distribution", attribs)

                taxonset = ET.SubElement(cal_prior, "taxonset", {"id" : clade, "spec":"TaxonSet"})
                for lang in langs:
                    ET.SubElement(taxonset, "taxon", {"idref":lang})
                normal = ET.SubElement(cal_prior, "Normal", {"id":"CalibrationNormal.%d" % n, "name":"distr", "offset":str(mean)})
                ET.SubElement(normal, "parameter", {"id":"parameter.hyperNormal-mean-%s.prior" % clade, "name":"mean", "estimate":"false"}).text = "0.0"
                ET.SubElement(normal, "parameter", {"id":"parameter.hyperNormal-sigma-%s.prior" % clade, "name":"sigma", "estimate":"false"}).text = str(stddev)


        # Tree prior
        attribs = {}
        attribs["birthDiffRate"] = "@birthRate.t:beastlingTree"
        attribs["id"] = "YuleModel.t:beastlingTree"
        attribs["spec"] = "beast.evolution.speciation.YuleModel"
        attribs["tree"] = "@Tree.t:beastlingTree"
        ET.SubElement(self.prior, "distribution", attribs)

        # Birth rate
        attribs = {}
        attribs["id"] = "YuleBirthRatePrior.t:beastlingTree"
        attribs["name"] = "distribution"
        attribs["x"] = "@birthRate.t:beastlingTree"
        sub_prior = ET.SubElement(self.prior, "prior", attribs)
        uniform = ET.SubElement(sub_prior, "Uniform", {"id":"Uniform.0","name":"distr","upper":"Infinity"})


    def add_operators(self):

        # Tree operators
        # Operators which affect the tree must respect the sample_topology and
        # sample_branch_length options.
        if self.config.sample_topology:
            ## Tree topology operators
            ET.SubElement(self.run, "operator", {"id":"SubtreeSlide.t:beastlingTree","spec":"SubtreeSlide","tree":"@Tree.t:beastlingTree","markclades":"true", "weight":"15.0"})
            ET.SubElement(self.run, "operator", {"id":"narrow.t:beastlingTree","spec":"Exchange","tree":"@Tree.t:beastlingTree","markclades":"true", "weight":"15.0"})
            ET.SubElement(self.run, "operator", {"id":"wide.t:beastlingTree","isNarrow":"false","spec":"Exchange","tree":"@Tree.t:beastlingTree","markclades":"true", "weight":"3.0"})
            ET.SubElement(self.run, "operator", {"id":"WilsonBalding.t:beastlingTree","spec":"WilsonBalding","tree":"@Tree.t:beastlingTree","markclades":"true","weight":"3.0"})
        if self.config.sample_branch_lengths:
            ## Branch length operators
            ET.SubElement(self.run, "operator", {"id":"UniformOperator.t:beastlingTree","spec":"Uniform","tree":"@Tree.t:beastlingTree","weight":"30.0"})
            ET.SubElement(self.run, "operator", {"id":"treeScaler.t:beastlingTree","scaleFactor":"0.5","spec":"ScaleOperator","tree":"@Tree.t:beastlingTree","weight":"3.0"})
            ET.SubElement(self.run, "operator", {"id":"treeRootScaler.t:beastlingTree","scaleFactor":"0.5","spec":"ScaleOperator","tree":"@Tree.t:beastlingTree","rootOnly":"true","weight":"3.0"})
            ## Up/down operator which scales tree height
            updown = ET.SubElement(self.run, "operator", {"id":"UpDown","spec":"UpDownOperator","scaleFactor":"0.5", "weight":"3.0"})
            ET.SubElement(updown, "tree", {"idref":"Tree.t:beastlingTree", "name":"up"})
            ET.SubElement(updown, "parameter", {"idref":"birthRate.t:beastlingTree", "name":"down"})
            ### Include clock rates in up/down only if calibrations are given
            if self.config.calibrations:
                for model in self.config.models:
                    ET.SubElement(updown, "parameter", {"idref":"clockRate.c:%s" % model.name, "name":"down"})

        # Birth rate scaler
        # Birth rate is *always* scaled.
        ET.SubElement(self.run, "operator", {"id":"YuleBirthRateScaler.t:beastlingTree","spec":"ScaleOperator","parameter":"@birthRate.t:beastlingTree", "scaleFactor":"0.5", "weight":"3.0"})

        # Model specific operators
        for model in self.config.models:
            model.add_operators(self.run)

    def add_loggers(self):

        # Screen logger
        if self.config.screenlog:
            screen_logger = ET.SubElement(self.run, "logger", attrib={"id":"screenlog", "logEvery":str(self.config.log_every)})
            log = ET.SubElement(screen_logger, "log", attrib={"arg":"@posterior", "id":"ESS.0", "spec":"util.ESS"})
            log = ET.SubElement(screen_logger, "log", attrib={"idref":"prior"})
            log = ET.SubElement(screen_logger, "log", attrib={"idref":"likelihood"})
            log = ET.SubElement(screen_logger, "log", attrib={"idref":"posterior"})

        # Tracer log
        if self.config.log_probabilities or self.config.log_params or self.config.log_all:
            tracer_logger = ET.SubElement(self.run,"logger",{"id":"tracelog","fileName":self.config.basename+".log","logEvery":str(self.config.log_every),"model":"@posterior","sanitiseHeaders":"true","sort":"smart"})
            if self.config.log_probabilities or self.config.log_all:
                ET.SubElement(tracer_logger,"log",{"idref":"prior"})
                ET.SubElement(tracer_logger,"log",{"idref":"likelihood"})
                ET.SubElement(tracer_logger,"log",{"idref":"posterior"})
            if self.config.log_params or self.config.log_all:
                ET.SubElement(tracer_logger,"log",{"idref":"birthRate.t:beastlingTree"})
                for model in self.config.models:
                    model.add_param_logs(tracer_logger)
                
        # Tree log
        if ((self.config.log_trees or self.config.log_all) and not
            self.config.tree_logging_pointless):
            tree_logger = ET.SubElement(self.run, "logger", {"mode":"tree", "fileName":self.config.basename+".nex","logEvery":str(self.config.log_every),"id":"treeWithMetaDataLogger"})
            log = ET.SubElement(tree_logger, "log", attrib={"id":"TreeLogger","spec":"beast.evolution.tree.TreeWithMetaDataLogger","tree":"@Tree.t:beastlingTree"})

    def tostring(self):
        indent(self.beast)
        return ET.tostring(self.beast, encoding="UTF-8")

    def write_file(self, filename=None):
        xml_string = self.tostring()
        if not filename:
            filename = self.config.basename+".xml"
        if filename in ("stdout", "-"):
            sys.stdout.write(xml_string.decode('utf8'))
        else:
            with open(filename, "wb") as fp:
                fp.write(xml_string)

    def make_monophyly_newick(self, langs):
        # First we build a "monophyly structure".  This can be done in either
        # a "top-down" or "bottom-up" way.
        if self.config.monophyly_end_depth is not None:
            # A power user has explicitly provided start and end depths
            start = self.config.monophyly_start_depth
            end = self.config.monophyly_end_depth
        elif self.config.monophyly_direction == "top_down":
            # Compute start and end in a top-down fashion
            start = self.config.monophyly_start_depth
            end = start + self.config.monophyly_levels
        elif self.config.monophyly_direction == "bottom_up":
            # Compute start and end in a bottom-up fashion
            classifications = [self.config.classifications[name.lower()] for name in langs]
            end = max([len(c) for c in classifications]) - self.config.monophyly_start_depth
            start = max(0, end - self.config.monophyly_levels)
        struct = self.make_monophyly_structure(langs, depth=start, maxdepth=end)
        # Now we serialise the "monophyly structure" into a Newick tree.
        return self.make_monophyly_string(struct)

    def make_monophyly_structure(self, langs, depth, maxdepth):
        """
        Recursively partition a list of languages (ISO or Glottocodes) into
        lists corresponding to their Glottolog classification.  The process
        may be halted part-way down the Glottolog tree.
        """
        if depth > maxdepth:
            # We're done, so terminate recursion
            return langs

        def subgroup(name, depth):
            ancestors = self.config.classifications[name.lower()]
            return ancestors[depth][0] if depth < len(ancestors) else ''

        def sortkey(i):
            """
            Callable to pass into `sorted` to port sorting behaviour from py2 to py3.

            :param i: Either a string or a list (of lists, ...) of strings.
            :return: Pair (nesting level, first string)
            """
            d = 0
            while isinstance(i, list):
                d -= 1
                i = i[0] if i else ''
            return d, i

        # Find the ancestor of all the given languages at at particular depth 
        # (i.e. look `depth` nodes below the root of the Glottolog tree)
        levels = list(set([subgroup(l, depth) for l in langs]))
        if len(levels) == 1:
            # If all languages belong to the same classificatio at this depth,
            # there are two possibilities
            if levels[0] == "":
                # If the common classification is an empty string, then we know
                # that there is no further refinement possible, so stop
                # the recursion here.
                langs.sort()
                return langs
            else:
                # If the common classification is non-empty, we need to
                # descend further, since some languages might get
                # separated later
                return self.make_monophyly_structure(langs, depth+1, maxdepth)
        else:
            # If the languages belong to multiple classifications, split them
            # up accordingly and then break down each classification
            # individually.

            partition = [[l for l in langs if subgroup(l, depth) == level] for level in levels]
            partition = [part for part in partition if part]
            return sorted(
                [self.make_monophyly_structure(group, depth+1, maxdepth)
                 for group in partition],
                key=sortkey)

    def make_monophyly_string(self, struct, depth=0):
        if not type([]) in [type(x) for x in struct]:
            return "(%s)" % ",".join(struct)
        else:
            return "(%s)" % ",".join([self.make_monophyly_string(substruct) for substruct in struct])
