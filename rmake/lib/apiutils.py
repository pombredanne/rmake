#
# Copyright (c) 2006 rPath, Inc.
#
# This program is distributed under the terms of the Common Public License,
# version 1.0. A copy of this license should have been distributed with this
# source file in a file called LICENSE. If it is not present, the license
# is always available at http://www.opensource.org/licenses/cpl.php.
#
# This program is distributed in the hope that it will be useful, but
# without any warranty; without even the implied warranty of merchantability
# or fitness for a particular purpose. See the Common Public License for
# full details.
#
"""
Along with apirpc, implements an API-validating and versioning scheme for
xmlrpc calls.
"""

import itertools
import traceback

from conary import versions
from conary.deps import deps
from conary.deps.deps import ThawFlavor

# ------------------ registry for api param types ------------
apitypes = { None : None }

def register(class_, name=None):
    if not name:
        if hasattr(class_, 'name'):
            name = class_.name
        else:
            name = class_.__name__
    apitypes[name] = class_

def registerMethods(name, freeze, thaw):
    apitypes[name] = (freeze, thaw)

def registerThaw(name, thawMethod):
    if name in apitypes:
        apitypes[name] = (apitypes[name][0], thawMethod)
    else:
        apitypes[name] = (None, thawMethod)

def registerFreeze(name, freezeMethod):
    if name in apitypes:
        apitypes[name] = (freezeMethod, apitypes[name][1])
    else:
        apitypes[name] = (freezeMethod, None)

def isRegistered(name):
    return name in apitypes


# ----- decorators for describing a method's API.

def api(version=0, allowed=None):
    """
        Decorator that describes the current version of the
        api as well as supported older versions.
        Allowed should be a list of allowed versions or None.

        For example:
        @api(version=5, allowed=range(2,5))
    """
    def deco(func):
        func.version = version
        if not hasattr(func, 'params'):
            func.params = {version: []}
        if not hasattr(func, 'returnType'):
            func.returnType = {version: None}

        if not allowed:
            func.allowed_versions = set([version])
        else:
            if isinstance(allowed, int):
                func.allowed_versions = set([allowed, version])
            else:
                func.allowed_versions = set(allowed + [version])
        return func
    return deco

def api_parameters(version, *paramList):
    """
        Decorator that describes the parameters accepted and their types
        for a particular version of the api.  Parameters should be classes
        with freeze and thaw methods, or None.  A None implies freezing
        and thawing of this parameter will be done manually or is not needed.

        For example:
        @api(5, api_manual, api_int)
    """
    if not isinstance(version, int):
        raise RuntimeError, 'must specify version for api parameters'

    def deco(func):
        if not hasattr(func, 'params'):
            func.params = {}
        func.params[version] = [ apitypes[x] for x in paramList ]
        return func
    return deco

def api_return(version, returnType):
    """ Decorator to be used to describe the return type of the function for
        a particular version of this method.

        Example usage:
        @api_return(1, api_troveTupleList)
    """
    if not isinstance(version, int):
        raise RuntimeError, 'must specify version for api parameters'

    def deco(func):
        if not hasattr(func, 'returnType'):
            func.returnType = {}
        if isinstance(returnType, str):
            r = apitypes[returnType]
        else:
            r = returnType

        func.returnType[version] = r
        return func
    return deco

# --- generic methods to freeze/thaw based on type

def freeze(apitype, item):
    found = False
    if isinstance(apitype, type):
        if not hasattr(apitype, '__freeze__'):
            apitype = apitype.__name__

    if not isinstance(apitype, type):
        apitype = apitypes[apitype]

    if apitype is None:
        return item
    if isinstance(apitype, tuple):
        return apitype[0](item)
    return apitype.__freeze__(item)

def thaw(apitype, item):
    if isinstance(apitype, type):
        if not hasattr(apitype, '__freeze__'):
            apitype = apitype.__name__

    if not isinstance(apitype, type):
        apitype = apitypes[apitype]
    if apitype is None:
        return item
    if isinstance(apitype, tuple):
        return apitype[1](item)
    return apitype.__thaw__(item)

# ---- individual api parameter types below this point. ---

class api_troveTupleList:
    name = 'troveTupleList'

    @staticmethod
    def __freeze__(tupList):
        return [(x[0], x[1].freeze(), (x[2] is not None) and x[2].freeze() or '')
                for x in tupList]

    @staticmethod
    def __thaw__(tupList):
        return [(x[0], versions.ThawVersion(x[1]),
                 ThawFlavor(x[2])) for x in tupList ]
register(api_troveTupleList)

class api_specList:
    name = 'troveSpecList'

    @staticmethod
    def __freeze__(tupList):
        return [(x[0], x[1] or '', (x[2] is not None) and x[2].freeze() or '')
                for x in tupList]

    @staticmethod
    def __thaw__(tupList):
        return [(x[0], x[1], ThawFlavor(x[2])) for x in tupList ]
register(api_specList)


class api_troveTuple:
    name = 'troveTuple'

    @staticmethod
    def __freeze__((n,v,f)):
        return (n, v.freeze(), (f is not None) and f.freeze() or '')

    @staticmethod
    def __thaw__((n,v,f)):
        return (n, versions.ThawVersion(v), ThawFlavor(f))
register(api_troveTuple)



class api_jobList:
    name = 'jobList'

    @staticmethod
    def __freeze__(jobList):
        return [(x[0], freeze('troveTupleList', x[1])) for x in jobList]

    @staticmethod
    def __thaw__(jobList):
        return [(x[0], thaw('troveTupleList', x[1])) for x in jobList]
register(api_jobList)

class api_version:
    name = 'version'

    @staticmethod
    def __freeze__(version):
        return version.freeze()

    @staticmethod
    def __thaw__(versionStr):
        return versions.ThawVersion(versionStr)
register(api_version)

class api_label:
    name = 'label'

    @staticmethod
    def __freeze__(label):
        return str(label)

    @staticmethod
    def __thaw__(label):
        return versions.Label(label)
register(api_label)


class api_flavor:
    name = 'flavor'

    @staticmethod
    def __freeze__(flavor):
        return flavor.freeze()

    @staticmethod
    def __thaw__(flavorStr):
        return ThawFlavor(flavorStr)
register(api_flavor)

class api_manual:
    name = 'manual'

    @staticmethod
    def __freeze__(item):
        return item

    @staticmethod
    def __thaw__(item):
        return item
register(api_manual)

def api_freezable(itemType):
    """ Wraps around another object that provides the freeze/thaw
        mechanism as methods.
    """
    class _api_freezable:

        name = itemType.__name__

        @staticmethod
        def __freeze__(item):
            return item.__freeze__()

        @staticmethod
        def __thaw__(item):
            return itemType.__thaw__(item)

    return _api_freezable

def _thawException(val):
    return RuntimeError('Exception from server:\n%s: %s\n%s' % tuple(val[0]))

def _freezeException(err):
    return str(err.__class__), str(err), traceback.format_exc()
registerMethods('Exception', _freezeException, _thawException)

register(None, 'bool')
register(None, 'int')
register(None, 'str')
register(None, 'float')


