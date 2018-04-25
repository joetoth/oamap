#!/usr/bin/env python

# Copyright (c) 2017, DIANA-HEP
# All rights reserved.
# 
# Redistribution and use in source and binary forms, with or without
# modification, are permitted provided that the following conditions are met:
# 
# * Redistributions of source code must retain the above copyright notice, this
#   list of conditions and the following disclaimer.
# 
# * Redistributions in binary form must reproduce the above copyright notice,
#   this list of conditions and the following disclaimer in the documentation
#   and/or other materials provided with the distribution.
# 
# * Neither the name of the copyright holder nor the names of its
#   contributors may be used to endorse or promote products derived from
#   this software without specific prior written permission.
# 
# THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
# AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
# IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
# DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
# FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
# DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
# SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
# CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
# OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
# OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.

import numpy

import oamap.schema
import oamap.dataset
import oamap.database
import oamap.backend.packing
from oamap.util import OrderedDict

def dataset(path, treepath, namespace=None, **kwargs):
    import uproot

    if namespace is None:
        namespace = "ROOT({0}, {1})".format(repr(path), repr(treepath))

    if "localsource" not in kwargs:
        kwargs["localsource"] = lambda path: uproot.source.file.FileSource(path, chunkbytes=8*1024, limitbytes=None)
    kwargs["total"] = False
    kwargs["blocking"] = True

    paths2entries = uproot.tree.numentries(path, treepath, **kwargs)
    if len(paths2entries) == 0:
        raise ValueError("path {0} matched no TTrees".format(repr(path)))

    offsets = [0]
    paths = []
    for path, numentries in paths2entries.items():
        offsets.append(offsets[-1] + numentries)
        paths.append(path)

    sch = schema(paths[0], treepath, namespace=namespace)
    doc = sch.doc
    sch.doc = None

    return oamap.dataset.Dataset(treepath.split("/")[-1].split(";")[0],
                                 sch,
                                 {namespace: ROOTBackend(paths, treepath)},
                                 oamap.dataset.SingleThreadExecutor(),
                                 offsets,
                                 extension=None,
                                 packing=None,
                                 doc=doc,
                                 metadata={"schemafrom": paths[0]})

def schema(path, treepath, namespace=""):
    import uproot

    def localsource(path):
        return uproot.source.file.FileSource(path, chunkbytes=8*1024, limitbytes=None)
    tree = uproot.open(path, localsource=localsource)[treepath]

    def accumulate(node):
        out = oamap.schema.Record(OrderedDict(), namespace=namespace)
        for branchname, branch in node.iteritems(aliases=False) if isinstance(node, uproot.tree.TTreeMethods) else node.iteritems():
            fieldname = branchname.split(".")[-1]

            if len(branch.fBranches) > 0:
                subrecord = accumulate(branch)
                if len(subrecord.fields) > 0:
                    out[fieldname] = subrecord

            elif isinstance(branch.interpretation, (uproot.interp.asdtype, uproot.interp.numerical.asdouble32)):
                subnode = oamap.schema.Primitive(branch.interpretation.todtype, data=branchname, namespace=namespace)
                for i in range(len(branch.interpretation.todims)):
                    subnode = oamap.schema.List(subnode, starts="{0}:/{1}".format(branchname, i), stops="{0}:/{1}".format(branchname, i), namespace=namespace)
                out[fieldname] = subnode

            elif isinstance(branch.interpretation, uproot.interp.asjagged) and isinstance(branch.interpretation.asdtype, uproot.interp.asdtype):
                subnode = oamap.schema.Primitive(branch.interpretation.asdtype.todtype, data=branchname, namespace=namespace)
                for i in range(len(branch.interpretation.asdtype.todims)):
                    subnode = oamap.schema.List(subnode, starts="{0}:/{1}".format(branchname, i), stops="{0}:/{1}".format(branchname, i), namespace=namespace)
                out[fieldname] = oamap.schema.List(subnode, starts=branchname, stops=branchname, namespace=namespace)

            elif isinstance(branch.interpretation, uproot.interp.asstrings):
                out[fieldname] = oamap.schema.List(oamap.schema.Primitive(oamap.interp.strings.CHARTYPE, data=branchname, namespace=namespace), starts=branchname, stops=branchname, namespace=namespace, name="ByteString")
        
        return out

    def combinelists(schema):
        if isinstance(schema, oamap.schema.Record) and all(isinstance(x, oamap.schema.List) for x in schema.fields.values()):
            out = oamap.schema.List(oamap.schema.Record(OrderedDict(), namespace=namespace), namespace=namespace)

            countbranch = None
            for fieldname, field in schema.items():
                try:
                    branch = tree[field.starts]
                except KeyError:
                    return schema

                if branch.countbranch is None:
                    return schema

                if countbranch is None:
                    countbranch = branch.countbranch
                elif countbranch is not branch.countbranch:
                    return schema

                out.content[fieldname] = field.content

            if countbranch is not None:
                out.starts = countbranch.name
                out.stops = countbranch.name
                return out

        return schema

    return oamap.schema.List(accumulate(tree).replace(combinelists), namespace=namespace, doc=tree.title)

class ROOTBackend(oamap.database.Backend):
    def __init__(self, paths, treepath):
        self._paths = paths
        self._treepath = treepath

    @property
    def args(self):
        return (self._path, self._treepath)

    def instantiate(self, partitionid):
        return ROOTArrays(self._paths[partitionid], self._treepath)

class ROOTArrays(object):
    def __init__(self, path, treepath):
        import uproot
        self._file = uproot.open(path, keep_source=True)
        self._tree = self._file[treepath]
        self._cache = {}

    def getall(self, roles):
        import uproot

        def chop(role):
            try:
                colon = str(role).rindex(":")
            except ValueError:
                return str(role), None
            else:
                return str(role)[:colon], str(role)[colon + 1:]
            
        arrays = self._tree.arrays(set(chop(x)[0] for x in roles), cache=self._cache, keycache=self._cache)

        out = {}
        for role in roles:
            branchname, leafname = chop(role)
            array = arrays[branchname]

            if leafname is not None and leafname.startswith("/"):
                if isinstance(array, (uproot.interp.jagged.JaggedArray, uproot.interp.strings.Strings)):
                    array = array.content

                length = array.shape[0]
                stride = 1
                for depth in range(int(leafname[1:])):
                    length *= array.shape[depth + 1]
                    stride *= array.shape[depth + 1]

                if isinstance(role, oamap.generator.StartsRole) and role not in out:
                    offsets = numpy.arange(0, (length + 1)*stride, stride)
                    out[role] = offsets[:-1]
                    out[role.stops] = offsets[1:]

                elif isinstance(role, oamap.generator.StopsRole) and role not in out:
                    offsets = numpy.arange(0, (length + 1)*stride, stride)
                    out[role.starts] = offsets[:-1]
                    out[role] = offsets[1:]

            elif isinstance(array, numpy.ndarray):
                if isinstance(role, oamap.generator.StartsRole) and role not in out:
                    starts, stops = oamap.backend.packing.ListCounts.fromcounts(array)
                    out[role] = starts
                    out[role.stops] = stops

                elif isinstance(role, oamap.generator.StopsRole) and role not in out:
                    starts, stops = oamap.backend.packing.ListCounts.fromcounts(array)
                    out[role.starts] = starts
                    out[role] = stops

                elif isinstance(role, oamap.generator.DataRole):
                    if leafname is None:
                        out[role] = array.reshape(-1)
                    else:
                        out[role] = array[leafname].reshape(-1)

            elif isinstance(array, (uproot.interp.jagged.JaggedArray, uproot.interp.strings.Strings)):
                if isinstance(role, oamap.generator.StartsRole):
                    out[role] = array.starts

                elif isinstance(role, oamap.generator.StopsRole):
                    out[role] = array.stops

                elif isinstance(role, oamap.generator.DataRole):
                    if leafname is None:
                        out[role] = array.content.reshape(-1)
                    else:
                        out[role] = array.content[leafname].reshape(-1)

            if role not in out:
                raise AssertionError(role)

        return out

    def close(self):
        self._file._context.source.close()
        self._file = None
        self._tree = None